import base64
import codecs
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
WINDOWS_POWERSHELLS = tuple(
    path for path in (shutil.which("pwsh"), shutil.which("powershell")) if path
)
SPEC = importlib.util.spec_from_file_location("needle", ROOT / "fetcher" / "needle.py")
needle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(needle)

THEME_PALETTE = {
    role: "#123456"
    for role in needle.WINDOWS_THEME_COLOR_ROLES
}


def jwt(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


class PathTests(unittest.TestCase):
    def test_windows_defaults_use_appdata(self):
        config, cache = needle.default_paths(
            platform="win32",
            environ={"APPDATA": "/roaming", "LOCALAPPDATA": "/local"},
            home="/home/test",
        )
        self.assertEqual(config, Path("/roaming/Needle/config.json"))
        self.assertEqual(cache, Path("/local/Needle/cache/usage.json"))

    def test_posix_defaults_keep_xdg_layout(self):
        config, cache = needle.default_paths(
            platform="linux",
            environ={"XDG_CONFIG_HOME": "/config", "XDG_CACHE_HOME": "/cache"},
            home="/home/test",
        )
        self.assertEqual(config, Path("/config/needle/config.json"))
        self.assertEqual(cache, Path("/cache/needle/usage.json"))

    def test_json_reader_accepts_windows_powershell_bom(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_bytes(codecs.BOM_UTF8 + b'{"enabled": true}')
            self.assertEqual(needle.load_json(path, {}), {"enabled": True})


class CredentialTests(unittest.TestCase):
    def write_auth(self, directory, value):
        path = Path(directory) / "auth.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def claude_file(self, directory, token, expires_in):
        path = Path(directory) / ".credentials.json"
        path.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": token,
            "subscriptionType": "max",
            "expiresAt": int((time.time() + expires_in) * 1000),
        }}), encoding="utf-8")
        return path

    def keychain(self, token, expires_in):
        return {"claudeAiOauth": {
            "accessToken": token,
            "subscriptionType": "max",
            "expiresAt": int((time.time() + expires_in) * 1000),
        }}

    def test_mac_prefers_fresh_keychain_over_stale_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.claude_file(temp, "stale-file", -3600)
            with mock.patch.object(needle.sys, "platform", "darwin"), \
                    mock.patch.object(needle, "claude_keychain", return_value=self.keychain("live", 3600)):
                pairs = needle.claude_credentials({"credentials_path": str(path)})
        self.assertEqual(pairs, [("live", "max")])

    def test_mac_orders_sign_ins_by_expiry(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.claude_file(temp, "file", 600)
            with mock.patch.object(needle.sys, "platform", "darwin"), \
                    mock.patch.object(needle, "claude_keychain", return_value=self.keychain("keychain", 3600)):
                pairs = needle.claude_credentials({"credentials_path": str(path)})
        self.assertEqual([token for token, _ in pairs], ["keychain", "file"])

    def test_linux_ignores_keychain(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.claude_file(temp, "file", 3600)
            with mock.patch.object(needle.sys, "platform", "linux"), \
                    mock.patch.object(needle, "claude_keychain") as keychain:
                pairs = needle.claude_credentials({"credentials_path": str(path)})
        keychain.assert_not_called()
        self.assertEqual(pairs, [("file", "max")])

    def test_all_claude_sign_ins_expired(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.claude_file(temp, "file", -60)
            with mock.patch.object(needle.sys, "platform", "darwin"), \
                    mock.patch.object(needle, "claude_keychain", return_value=self.keychain("keychain", -60)):
                with self.assertRaisesRegex(needle.ProviderError, "expired"):
                    needle.claude_credentials({"credentials_path": str(path)})

    def test_claude_retries_next_sign_in_after_401(self):
        used = []

        def fake_get(url, headers):
            used.append(headers["Authorization"])
            if headers["Authorization"] == "Bearer first":
                raise needle.urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
            return {"five_hour": {"utilization": 12, "resets_at": None}}

        with mock.patch.object(needle, "claude_credentials", return_value=[("first", "max"), ("second", "pro")]), \
                mock.patch.object(needle, "http_get", side_effect=fake_get):
            result = needle.fetch_claude({})
        self.assertEqual(used, ["Bearer first", "Bearer second"])
        self.assertEqual(result["plan"], "Pro")

    def test_expired_claude_sign_in_asks_claude_code_to_refresh(self):
        expired = needle.SignInExpired("Sign-in expired.")
        usage = {"five_hour": {"utilization": 7, "resets_at": None}}
        with mock.patch.object(needle, "claude_credentials", side_effect=[expired, [("fresh", "max")]]), \
                mock.patch.object(needle, "refresh_claude_sign_in", return_value=True) as refresh, \
                mock.patch.object(needle, "http_get", return_value=usage) as get:
            result = needle.fetch_claude({})
        refresh.assert_called_once_with()
        self.assertEqual(get.call_args[0][1]["Authorization"], "Bearer fresh")
        self.assertEqual(result["windows"][0]["used"], 7.0)

    def test_expired_claude_sign_in_without_claude_cli_keeps_error(self):
        with mock.patch.object(needle, "claude_credentials", side_effect=needle.SignInExpired("Sign-in expired.")), \
                mock.patch.object(needle, "refresh_claude_sign_in", return_value=False):
            with self.assertRaises(needle.SignInExpired):
                needle.fetch_claude({})

    def test_rejected_claude_sign_in_refreshes_and_retries(self):
        calls = []

        def fake_get(url, headers):
            calls.append(headers["Authorization"])
            if headers["Authorization"] == "Bearer old":
                raise needle.urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
            return {"seven_day": {"utilization": 30, "resets_at": None}}

        with mock.patch.object(needle, "claude_credentials", side_effect=[[("old", "max")], [("new", "max")]]), \
                mock.patch.object(needle, "refresh_claude_sign_in", return_value=True), \
                mock.patch.object(needle, "http_get", side_effect=fake_get):
            needle.fetch_claude({})
        self.assertEqual(calls, ["Bearer old", "Bearer new"])

    def test_claude_server_errors_do_not_trigger_refresh(self):
        error = needle.urllib.error.HTTPError("u", 500, "Server error", {}, None)
        with mock.patch.object(needle, "claude_credentials", return_value=[("token", "max")]), \
                mock.patch.object(needle, "refresh_claude_sign_in") as refresh, \
                mock.patch.object(needle, "http_get", side_effect=error):
            with self.assertRaises(needle.urllib.error.HTTPError):
                needle.fetch_claude({})
        refresh.assert_not_called()

    def test_refresh_runs_auth_status_without_inherited_tokens(self):
        env = {"PATH": "/usr/bin", "USER": "kyle", "CLAUDE_CODE_OAUTH_TOKEN": "parent", "ANTHROPIC_API_KEY": "key"}
        with mock.patch.dict(needle.os.environ, env, clear=True), \
                mock.patch.object(needle, "claude_cli", return_value="/bin/claude"), \
                mock.patch.object(needle.subprocess, "run") as run:
            self.assertTrue(needle.refresh_claude_sign_in())
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["/bin/claude", "auth", "status"])
        self.assertEqual(kwargs["env"], {"PATH": "/usr/bin", "USER": "kyle"})

    def test_refresh_fills_missing_user(self):
        with mock.patch.dict(needle.os.environ, {"PATH": "/usr/bin"}, clear=True), \
                mock.patch.object(needle, "claude_cli", return_value="/bin/claude"), \
                mock.patch.object(needle.getpass, "getuser", return_value="kyle"), \
                mock.patch.object(needle.subprocess, "run") as run:
            needle.refresh_claude_sign_in()
        self.assertEqual(run.call_args[1]["env"]["USER"], "kyle")

    def test_refresh_skipped_without_claude_cli(self):
        with mock.patch.object(needle, "claude_cli", return_value=None), \
                mock.patch.object(needle.subprocess, "run") as run:
            self.assertFalse(needle.refresh_claude_sign_in())
        run.assert_not_called()

    def test_claude_cli_found_outside_short_path(self):
        with tempfile.TemporaryDirectory() as temp:
            cli = Path(temp) / "claude"
            cli.write_text("#!/bin/sh\n")
            cli.chmod(0o755)
            with mock.patch.object(needle.shutil, "which", return_value=None), \
                    mock.patch.object(needle, "CLAUDE_BIN_DIRS", (temp,)):
                self.assertEqual(needle.claude_cli(), str(cli))

    def test_account_id_claim_variants(self):
        self.assertEqual(needle.account_from_claims({"chatgpt_account_id": "direct"}), "direct")
        self.assertEqual(
            needle.account_from_claims(
                {"https://api.openai.com/auth": {"chatgpt_account_id": "nested"}}
            ),
            "nested",
        )
        self.assertEqual(needle.account_from_claims({"organizations": [{"id": "org"}]}), "org")

    def test_invalid_jwt_is_ignored(self):
        self.assertEqual(needle.jwt_claims("not-a-jwt"), {})
        self.assertEqual(needle.jwt_claims("a.!.c"), {})

    def test_reads_opencode_oauth_without_writing_it(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.write_auth(
                temp,
                {
                    "openai": {
                        "type": "oauth",
                        "access": jwt({"exp": time.time() + 3600, "chatgpt_account_id": "jwt-account"}),
                        "refresh": "unused-refresh-token",
                        "expires": (time.time() + 3600) * 1000,
                        "accountId": "stored-account",
                    }
                },
            )
            before = path.read_bytes()
            token, account, source = needle.opencode_credentials({"opencode_auth_path": str(path)})
            self.assertTrue(token.startswith("header."))
            self.assertEqual(account, "stored-account")
            self.assertEqual(source, "OpenCode")
            self.assertEqual(path.read_bytes(), before)

    def test_rejects_opencode_api_key(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.write_auth(temp, {"openai": {"type": "api", "key": "not-used"}})
            with self.assertRaisesRegex(needle.ProviderError, "API key"):
                needle.opencode_credentials({"opencode_auth_path": str(path)})

    def test_rejects_expired_opencode_oauth(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.write_auth(
                temp,
                {
                    "openai": {
                        "type": "oauth",
                        "access": jwt({"exp": time.time() + 3600}),
                        "refresh": "unused",
                        "expires": 0,
                    }
                },
            )
            with self.assertRaisesRegex(needle.ProviderError, "expired"):
                needle.opencode_credentials({"opencode_auth_path": str(path)})

    def test_auto_prefers_opencode(self):
        with mock.patch.object(
            needle, "opencode_credentials", return_value=("open-token", "open-account", "OpenCode")
        ), mock.patch.object(needle, "codex_cli_credentials") as codex:
            result = needle.codex_credentials({})
        self.assertEqual(result[2], "OpenCode")
        codex.assert_not_called()

    def test_auto_falls_back_to_codex(self):
        with mock.patch.object(needle, "opencode_credentials", return_value=None), mock.patch.object(
            needle,
            "codex_cli_credentials",
            return_value=("codex-token", "codex-account", "Codex CLI"),
        ):
            result = needle.codex_credentials({})
        self.assertEqual(result[2], "Codex CLI")

    def test_explicit_source_and_invalid_source(self):
        with mock.patch.object(needle, "codex_cli_credentials", return_value=None):
            with self.assertRaisesRegex(needle.ProviderError, "Run `codex`"):
                needle.codex_credentials({"credential_source": "codex"})
        with self.assertRaisesRegex(needle.ProviderError, "must be"):
            needle.codex_credentials({"credential_source": "other"})


class ProviderTests(unittest.TestCase):
    def test_chatgpt_usage_response_is_normalized(self):
        response = {
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 25,
                    "reset_at": time.time() + 3600,
                    "limit_window_seconds": 18000,
                },
                "secondary_window": {
                    "used_percent": 50,
                    "reset_at": time.time() + 86400,
                    "limit_window_seconds": 604800,
                },
            },
        }
        with mock.patch.object(
            needle, "codex_credentials", return_value=("token", "account", "OpenCode")
        ), mock.patch.object(needle, "http_get", return_value=response) as get:
            result = needle.fetch_codex({})
        self.assertEqual(result["plan"], "Plus")
        self.assertEqual(result["source"], "OpenCode")
        self.assertEqual([window["label"] for window in result["windows"]], ["5-hour", "Weekly"])
        self.assertEqual(result["windows"][0]["used"], 25.0)
        self.assertEqual(get.call_args.args[1]["ChatGPT-Account-Id"], "account")

    def test_successful_usage_marks_service_connected(self):
        fetch = mock.Mock(return_value={"windows": [{"label": "Weekly", "used": 10}]})
        with mock.patch.object(needle, "PROVIDERS", [("codex", "ChatGPT / Codex", fetch)]):
            result = needle.snapshot({"codex": {"enabled": True}}, {})
        self.assertTrue(result["providers"][0]["connected"])

    def test_failed_initial_setup_is_not_connected(self):
        fetch = mock.Mock(side_effect=needle.ProviderError("Not signed in."))
        with mock.patch.object(needle, "PROVIDERS", [("codex", "ChatGPT / Codex", fetch)]):
            result = needle.snapshot({"codex": {"enabled": True}}, {})
        self.assertFalse(result["providers"][0]["connected"])

    def test_stale_usage_remains_connected(self):
        fetch = mock.Mock(side_effect=needle.ProviderError("Temporarily unavailable."))
        cached = {
            "providers": [{
                "id": "codex",
                "name": "ChatGPT / Codex",
                "ok": True,
                "fetched_at": 1,
                "windows": [{"label": "Weekly", "used": 10}],
            }]
        }
        with mock.patch.object(needle, "PROVIDERS", [("codex", "ChatGPT / Codex", fetch)]), mock.patch.object(
            needle.time, "time", return_value=1000
        ):
            result = needle.snapshot({"codex": {"enabled": True}}, cached, force=True)
        self.assertTrue(result["providers"][0]["connected"])
        self.assertTrue(result["providers"][0]["stale"])

    def test_provider_keeps_compatible_id_with_new_name(self):
        provider = next(item for item in needle.PROVIDERS if item[0] == "codex")
        self.assertEqual(provider[1], "ChatGPT / Codex")

    def test_cache_write_uses_complete_json(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "cache" / "usage.json"
            with mock.patch.object(needle, "CACHE_PATH", target):
                needle.write_cache({"providers": [{"id": "codex"}]})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["providers"][0]["id"], "codex")
            self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_cache_lock_rejects_a_concurrent_holder(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "cache" / "usage.json"
            errors = []

            def contend():
                try:
                    with needle.cache_lock(timeout=0.1):
                        pass
                except Exception as error:  # noqa: BLE001 - asserted below
                    errors.append(error)

            with mock.patch.object(needle, "CACHE_PATH", target):
                with needle.cache_lock():
                    thread = threading.Thread(target=contend)
                    thread.start()
                    thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], TimeoutError)

    def test_response_error_records_attempt_and_honors_floor(self):
        fetch = mock.Mock(side_effect=needle.ResponseError("No usage windows returned."))
        providers = [("codex", "ChatGPT / Codex", fetch)]
        config = {"codex": {"enabled": True}}
        with mock.patch.object(needle, "PROVIDERS", providers), mock.patch.object(
            needle.time, "time", return_value=1000
        ):
            first = needle.snapshot(config, {})
        self.assertEqual(first["providers"][0]["attempted_at"], 1000)
        with mock.patch.object(needle, "PROVIDERS", providers), mock.patch.object(
            needle.time, "time", return_value=1001
        ):
            second = needle.snapshot(config, first)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(second["providers"][0]["attempted_at"], 1000)

    def test_snapshot_reports_when_refresh_would_fetch_again(self):
        fetch = mock.Mock(return_value={"windows": [{"label": "Weekly", "used": 10}]})
        providers = [("codex", "ChatGPT / Codex", fetch), ("zai", "z.ai", fetch)]
        config = {"codex": {"enabled": True}, "zai": {"enabled": True}}
        with mock.patch.object(needle, "PROVIDERS", providers), mock.patch.object(
            needle.time, "time", return_value=1000
        ):
            result = needle.snapshot(config, {})
        codex, zai = result["providers"]
        self.assertEqual(codex["refresh_at"], 1300)
        self.assertEqual(codex["force_refresh_at"], 1060)
        self.assertEqual(zai["refresh_at"], 1060)
        self.assertEqual(zai["force_refresh_at"], 1000)

    def test_cached_snapshot_keeps_original_refresh_time(self):
        fetch = mock.Mock(return_value={"windows": [{"label": "Weekly", "used": 10}]})
        providers = [("claude", "Claude", fetch)]
        config = {"claude": {"enabled": True}}
        with mock.patch.object(needle, "PROVIDERS", providers), mock.patch.object(
            needle.time, "time", return_value=1000
        ):
            first = needle.snapshot(config, {})
        with mock.patch.object(needle, "PROVIDERS", providers), mock.patch.object(
            needle.time, "time", return_value=1120
        ):
            second = needle.snapshot(config, first, force=True)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(second["updated"], 1120)
        self.assertEqual(second["providers"][0]["fetched_at"], 1000)
        self.assertEqual(second["providers"][0]["force_refresh_at"], 1300)

    def test_lock_timeout_returns_cache_without_fetching(self):
        fetch = mock.Mock()

        @contextlib.contextmanager
        def timeout_lock():
            raise TimeoutError("Another Needle refresh is still running.")
            yield

        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            config = temp / "config.json"
            cache = temp / "usage.json"
            config.write_text(json.dumps({"codex": {"enabled": True}}), encoding="utf-8")
            cache.write_text(
                json.dumps(
                    {
                        "updated": 1,
                        "providers": [
                            {"id": "codex", "name": "ChatGPT / Codex", "ok": True, "windows": []}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = io.StringIO()
            with mock.patch.object(needle, "CONFIG_PATH", config), mock.patch.object(
                needle, "CACHE_PATH", cache
            ), mock.patch.object(needle, "PROVIDERS", [("codex", "ChatGPT / Codex", fetch)]), mock.patch.object(
                needle, "cache_lock", timeout_lock
            ), contextlib.redirect_stdout(output):
                result = needle.main([])
        self.assertEqual(result, 0)
        fetch.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["providers"][0]["id"], "codex")

    def test_services_are_disabled_when_not_explicitly_enabled(self):
        fetch = mock.Mock()
        with mock.patch.object(needle, "PROVIDERS", [("codex", "ChatGPT / Codex", fetch)]):
            result = needle.snapshot({}, {})
        self.assertEqual(result["providers"], [])
        fetch.assert_not_called()

    def test_legacy_service_section_without_enabled_stays_enabled(self):
        fetch = mock.Mock(return_value={"windows": []})
        with mock.patch.object(needle, "PROVIDERS", [("codex", "ChatGPT / Codex", fetch)]):
            result = needle.snapshot({"codex": {"credential_source": "auto"}}, {})
        self.assertEqual(result["providers"][0]["id"], "codex")
        fetch.assert_called_once()

    def test_configure_service_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps({"zai": {"base_url": "https://example.test"}}), encoding="utf-8")
            with mock.patch.object(needle, "CONFIG_PATH", path):
                needle.configure_service("zai", api_key="secret")
                needle.configure_service("zai", enabled=False)
            config = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(config["zai"]["base_url"], "https://example.test")
        self.assertEqual(config["zai"]["api_key"], "secret")
        self.assertFalse(config["zai"]["enabled"])

    def test_configure_service_can_clear_key(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps({"openrouter": {"enabled": False, "api_key": "secret"}}), encoding="utf-8")
            with mock.patch.object(needle, "CONFIG_PATH", path):
                needle.configure_service("openrouter", clear_key=True)
            config = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("api_key", config["openrouter"])

    def test_configure_service_does_not_overwrite_malformed_config(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text("{broken", encoding="utf-8")
            with mock.patch.object(needle, "CONFIG_PATH", path):
                with self.assertRaisesRegex(ValueError, "could not be read"):
                    needle.configure_service("claude", enabled=True)
            contents = path.read_text(encoding="utf-8")
        self.assertEqual(contents, "{broken")

    def test_configure_tray_hover_percentage_mode_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(
                json.dumps({"codex": {"enabled": True}, "windows": {"other": "kept"}}),
                encoding="utf-8",
            )
            with mock.patch.object(needle, "CONFIG_PATH", path):
                needle.configure_tray_hover_percentage_mode("both")
            config = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(config["codex"]["enabled"])
        self.assertEqual(config["windows"]["other"], "kept")
        self.assertEqual(config["windows"]["tray_hover_percentage_mode"], "both")

    def test_configure_tray_hover_percentage_mode_rejects_unknown_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps({"windows": {"other": "kept"}}), encoding="utf-8")
            with mock.patch.object(needle, "CONFIG_PATH", path):
                with self.assertRaisesRegex(ValueError, "Unknown tray hover percentage mode"):
                    needle.configure_tray_hover_percentage_mode("tightest")
            config = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(config, {"windows": {"other": "kept"}})

    def test_tray_hover_percentage_mode_command_updates_config(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            with mock.patch.object(needle, "CONFIG_PATH", path):
                result = needle.run_config_command(
                    ["--set-tray-hover-percentage-mode", "hourly"]
                )
            config = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(result, 0)
        self.assertEqual(config["windows"]["tray_hover_percentage_mode"], "hourly")

    def test_configure_windows_theme_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(
                json.dumps({"codex": {"enabled": True}, "windows": {"other": "kept"}}),
                encoding="utf-8",
            )
            with mock.patch.object(needle, "CONFIG_PATH", path):
                needle.configure_windows_theme("night")
            config = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(config["codex"]["enabled"])
        self.assertEqual(config["windows"]["other"], "kept")
        self.assertEqual(config["windows"]["theme"], "night")

    def test_configure_windows_theme_rejects_unknown_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps({"windows": {"other": "kept"}}), encoding="utf-8")
            with mock.patch.object(needle, "CONFIG_PATH", path):
                with self.assertRaisesRegex(ValueError, "Unknown Windows theme"):
                    needle.configure_windows_theme("sepia")
            config = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(config, {"windows": {"other": "kept"}})

    def test_configure_windows_custom_theme_saves_complete_palette(self):
        palette = {**THEME_PALETTE, "accent": "#ABCDEF"}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps({"windows": {"other": "kept"}}), encoding="utf-8")
            with mock.patch.object(needle, "CONFIG_PATH", path):
                needle.configure_windows_custom_theme(palette)
            config = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(config["windows"]["other"], "kept")
        self.assertEqual(config["windows"]["theme"], "custom")
        self.assertEqual(config["windows"]["custom_theme"]["accent"], "#abcdef")
        self.assertEqual(set(config["windows"]["custom_theme"]), needle.WINDOWS_THEME_COLOR_ROLES)

    def test_configure_windows_custom_theme_rejects_partial_or_invalid_palette(self):
        invalid_palettes = [
            {key: value for key, value in THEME_PALETTE.items() if key != "accent"},
            {**THEME_PALETTE, "accent": "blue"},
            {**THEME_PALETTE, "extra": "#123456"},
        ]
        for palette in invalid_palettes:
            with self.subTest(palette=palette), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "config.json"
                original = {"windows": {"other": "kept"}}
                path.write_text(json.dumps(original), encoding="utf-8")
                with mock.patch.object(needle, "CONFIG_PATH", path):
                    with self.assertRaises(ValueError):
                        needle.configure_windows_custom_theme(palette)
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)

    def test_windows_theme_commands_update_config(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            with mock.patch.object(needle, "CONFIG_PATH", path):
                self.assertEqual(needle.run_config_command(["--set-windows-theme", "dark"]), 0)
                with mock.patch("sys.stdin", io.StringIO(json.dumps(THEME_PALETTE))):
                    self.assertEqual(needle.run_config_command(["--set-windows-custom-theme"]), 0)
            config = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(config["windows"]["theme"], "custom")
        self.assertEqual(config["windows"]["custom_theme"], {
            role: "#123456" for role in sorted(needle.WINDOWS_THEME_COLOR_ROLES)
        })


class PaceTests(unittest.TestCase):
    def window(self, used, resets_in, length=10000):
        return {"label": "5-hour", "used": used, "resets_at": 1000 + resets_in, "window_seconds": length}

    def test_fast_window_projects_run_out_before_reset(self):
        w = self.window(used=60, resets_in=6000)  # 4000s in, 60% used
        needle.annotate_pace(w, 1000)
        self.assertEqual(w["pace"], "fast")
        # 40% left at 15%/1000s is about 2667s more.
        self.assertEqual(w["runs_out_at"], 1000 + round(40 * 4000 / 60))

    def test_even_and_slow_windows_have_no_run_out(self):
        even = self.window(used=40, resets_in=6000)
        slow = self.window(used=10, resets_in=6000)
        needle.annotate_pace(even, 1000)
        needle.annotate_pace(slow, 1000)
        self.assertEqual((even["pace"], slow["pace"]), ("even", "slow"))
        self.assertNotIn("runs_out_at", even)
        self.assertNotIn("runs_out_at", slow)

    def test_too_early_or_already_out_skips_projection(self):
        early = self.window(used=30, resets_in=9800)  # only 2% of the window has passed
        out = self.window(used=100, resets_in=6000)
        needle.annotate_pace(early, 1000)
        needle.annotate_pace(out, 1000)
        self.assertEqual(early["pace"], "fast")
        self.assertNotIn("runs_out_at", early)
        self.assertEqual(out["pace"], "fast")
        self.assertNotIn("runs_out_at", out)

    def test_windows_without_length_get_nothing(self):
        w = {"label": "Tool calls", "used": 90, "resets_at": 5000, "window_seconds": None}
        needle.annotate_pace(w, 1000)
        self.assertNotIn("pace", w)

    def test_snapshot_measures_pace_when_the_numbers_were_read(self):
        fetch = mock.Mock(return_value={"windows": [
            {"label": "5-hour", "used": 60, "resets_at": 7000, "window_seconds": 10000}]})
        providers = [("zai", "z.ai", fetch)]
        config = {"zai": {"enabled": True}}
        with mock.patch.object(needle, "PROVIDERS", providers), mock.patch.object(
            needle.time, "time", return_value=1000
        ):
            first = needle.snapshot(config, {})
        with mock.patch.object(needle, "PROVIDERS", providers), mock.patch.object(
            needle.time, "time", return_value=1030
        ):
            second = needle.snapshot(config, first, cached_only=True)
        self.assertEqual(first["providers"][0]["windows"][0]["runs_out_at"], 3667)
        self.assertEqual(second["providers"][0]["windows"][0]["runs_out_at"], 3667)


class NotifySettingTests(unittest.TestCase):
    def test_set_notify_command_updates_config(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.json"
            config.write_text(json.dumps({"claude": {"enabled": True}}), encoding="utf-8")
            with mock.patch.object(needle, "CONFIG_PATH", config):
                self.assertEqual(needle.run_config_command(["--set-notify", "off"]), 0)
                self.assertIs(json.loads(config.read_text())["notify"], False)
                self.assertEqual(needle.run_config_command(["--set-notify", "on"]), 0)
                saved = json.loads(config.read_text())
        self.assertIs(saved["notify"], True)
        self.assertEqual(saved["claude"], {"enabled": True})

    def test_set_notify_rejects_other_values(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.json"
            with mock.patch.object(needle, "CONFIG_PATH", config), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(needle.run_config_command(["--set-notify", "maybe"]), 2)
            self.assertFalse(config.exists())


class MacMenuBarWindowSettingTests(unittest.TestCase):
    def test_set_mac_menu_bar_window_updates_config(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.json"
            config.write_text(json.dumps({"windows": {"theme": "dark"}}), encoding="utf-8")
            with mock.patch.object(needle, "CONFIG_PATH", config):
                self.assertEqual(needle.run_config_command(["--set-mac-menu-bar-window", "alternate"]), 0)
                saved = json.loads(config.read_text())
        self.assertEqual(saved["mac"], {"menu_bar_window": "alternate"})
        self.assertEqual(saved["windows"], {"theme": "dark"})

    def test_set_mac_menu_bar_window_rejects_unknown_modes(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.json"
            with mock.patch.object(needle, "CONFIG_PATH", config), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(needle.run_config_command(["--set-mac-menu-bar-window", "monthly"]), 2)
                self.assertEqual(needle.run_config_command(["--set-mac-menu-bar-window"]), 2)
            self.assertFalse(config.exists())


class MacAlertTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("needle_mac", ROOT / "mac" / "needle.5m.py")
        self.mac = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mac)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.mac.ALERTS = Path(self.temp.name) / "mac-alerts.json"
        self.sent = []
        self.mac.notify = self.sent.append

    def provider(self, used):
        return {"id": "claude", "name": "Claude", "windows": [{"label": "5-hour", "used": used}]}

    def test_alerts_once_when_low_and_once_when_back(self):
        self.mac.check_alerts([self.provider(92)], {})
        self.mac.check_alerts([self.provider(95)], {})
        self.mac.check_alerts([self.provider(0)], {})
        self.mac.check_alerts([self.provider(0)], {})
        self.assertEqual(self.sent, ["Claude is nearly out: 8% left", "Claude is back: 100% left"])

    def test_removed_service_and_turned_off_stay_quiet(self):
        self.mac.check_alerts([self.provider(95)], {})
        self.mac.check_alerts([], {})
        self.mac.check_alerts([self.provider(0)], {})
        self.mac.check_alerts([self.provider(95)], {"notify": False})
        self.assertEqual(self.sent, ["Claude is nearly out: 5% left"])


    def test_local_ai_stays_out_of_the_menu_bar_title(self):
        local = {"id": "local", "name": "Local AI", "tally": {"all_time": 412}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.mac.render_title([self.provider(42), local])
        title = out.getvalue().splitlines()[0]
        self.assertIn("C 58%", title)
        self.assertNotIn("412", title)

    def titles(self, providers, mode):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.mac.render_title(providers, mode)
        return out.getvalue().splitlines()

    def both_windows(self):
        return [{"id": "claude", "name": "Claude",
                 "windows": [{"label": "5-hour", "used": 8}, {"label": "Weekly", "used": 59}]},
                {"id": "openrouter", "name": "OpenRouter", "balance": {"remaining": 14, "total": 20}}]

    def test_menu_bar_shows_the_chosen_window(self):
        providers = self.both_windows()
        self.assertTrue(self.titles(providers, "tightest")[0].startswith("C 41%  $14 |"))
        self.assertTrue(self.titles(providers, "5-hour")[0].startswith("C 92%  $14 |"))
        self.assertTrue(self.titles(providers, "weekly")[0].startswith("C 41%  $14 |"))
        self.assertTrue(self.titles(providers, "both")[0].startswith("C 92/41%  $14 |"))

    def test_alternate_prints_one_title_line_per_window(self):
        lines = self.titles(self.both_windows(), "alternate")
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("5h  C 92%  $14 |"))
        self.assertTrue(lines[1].startswith("Wk  C 41%  $14 |"))
        # The icon follows the tightest limit on both lines.
        self.assertTrue(all("gauge.with.dots.needle.50percent" in line for line in lines))

    def test_alternate_with_no_windows_prints_one_line(self):
        balance_only = self.both_windows()[1:]
        self.assertEqual(len(self.titles(balance_only, "alternate")), 1)

    def test_missing_window_falls_back_to_tightest(self):
        weekly_only = [{"id": "claude", "name": "Claude", "windows": [{"label": "Weekly", "used": 30}]}]
        self.assertTrue(self.titles(weekly_only, "5-hour")[0].startswith("C 70% |"))
        self.assertTrue(self.titles(weekly_only, "both")[0].startswith("C 70% |"))

    def test_menu_bar_window_setting_defaults_to_tightest(self):
        self.assertEqual(self.mac.menu_bar_window({}), "tightest")
        self.assertEqual(self.mac.menu_bar_window({"mac": {"menu_bar_window": "bogus"}}), "tightest")
        self.assertEqual(self.mac.menu_bar_window({"mac": {"menu_bar_window": "weekly"}}), "weekly")

class UpdateTests(unittest.TestCase):
    RELEASE = {"latest": "99.0.0", "url": "https://example.test/r", "notes": "New", "zipball": "z"}

    def test_versions_agree_everywhere(self):
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        metadata = json.loads((ROOT / "applet" / "needle@pinecompute" / "metadata.json").read_text(encoding="utf-8"))
        xbar = (ROOT / "mac" / "needle.5m.py").read_text(encoding="utf-8")
        self.assertEqual(needle.VERSION, version)
        self.assertEqual(metadata["version"], version)
        self.assertIn(f"<xbar.version>v{version}</xbar.version>", xbar)

    def test_update_available_compares_numerically(self):
        with mock.patch.object(needle, "VERSION", "1.3.0"):
            self.assertEqual(needle.update_available({"latest": "1.10.0"})["latest"], "1.10.0")
            self.assertIsNone(needle.update_available({"latest": "1.3.0"}))
            self.assertIsNone(needle.update_available({"latest": "1.2.9"}))
            self.assertIsNone(needle.update_available({}))
            self.assertIsNone(needle.update_available({"latest": "not-a-version"}))

    def test_check_update_runs_at_most_daily(self):
        fresh = {"checked_at": 1000, "latest": "1.0.0"}
        with mock.patch.object(needle, "latest_release", return_value=self.RELEASE) as latest, \
                mock.patch.object(needle.time, "time", return_value=1000 + 60):
            self.assertEqual(needle.check_update({}, {"update_check": fresh}), fresh)
            latest.assert_not_called()
        with mock.patch.object(needle, "latest_release", return_value=self.RELEASE) as latest, \
                mock.patch.object(needle.time, "time", return_value=1000 + needle.UPDATE_EVERY + 1):
            self.assertEqual(needle.check_update({}, {"update_check": fresh})["latest"], "99.0.0")
            latest.assert_called_once()

    def test_check_update_can_be_turned_off_and_skips_cached_runs(self):
        with mock.patch.object(needle, "latest_release", return_value=self.RELEASE) as latest:
            self.assertEqual(needle.check_update({"check_updates": False}, {}), {})
            self.assertEqual(needle.check_update({}, {}, cached_only=True), {})
            latest.assert_not_called()

    def test_manual_check_ignores_daily_limit_and_setting(self):
        fresh = {"checked_at": 1000, "latest": "1.0.0"}
        with mock.patch.object(needle, "latest_release", return_value=self.RELEASE) as latest, \
                mock.patch.object(needle.time, "time", return_value=1000 + 60):
            result = needle.check_update({"check_updates": False}, {"update_check": fresh}, now=True)
            self.assertEqual(result["latest"], "99.0.0")
            self.assertEqual(result["checked_at"], 1060)
            self.assertEqual(needle.check_update({}, {}, cached_only=True, now=True), {})
            latest.assert_called_once()

    def test_check_update_failure_is_quiet_and_waits_a_day(self):
        with mock.patch.object(needle, "latest_release", side_effect=OSError("offline")), \
                mock.patch.object(needle.time, "time", return_value=needle.UPDATE_EVERY + 5000):
            self.assertEqual(needle.check_update({}, {}), {"checked_at": needle.UPDATE_EVERY + 5000})

    def test_refresh_reports_update_and_saves_check(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            cache = temp / "usage.json"
            output = io.StringIO()
            with mock.patch.object(needle, "CONFIG_PATH", temp / "missing.json"), mock.patch.object(
                needle, "CACHE_PATH", cache
            ), mock.patch.object(needle, "PROVIDERS", []), mock.patch.object(
                needle, "latest_release", return_value=self.RELEASE
            ), contextlib.redirect_stdout(output):
                self.assertEqual(needle.main([]), 0)
            data = json.loads(output.getvalue())
            self.assertEqual(data["update"], {"latest": "99.0.0", "url": "https://example.test/r", "notes": "New"})
            self.assertEqual(json.loads(cache.read_text(encoding="utf-8"))["update_check"]["latest"], "99.0.0")


@unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELLS, "PowerShell tray smoke test requires Windows")
class WindowsTrayTests(unittest.TestCase):
    def test_deferred_render_does_not_dispose_active_combobox(self):
        script = r'''
Set-StrictMode -Version Latest
Add-Type -AssemblyName System.Windows.Forms
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:NEEDLE_TRAY_SCRIPT,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -gt 0) { throw $errors[0] }
$definition = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Request-FlyoutRender'
}, $true)
. ([scriptblock]::Create($definition.Extent.Text))

$script:ModalDepth = 0
$script:IsRendering = $false
$script:RenderQueued = $false
$script:RenderPending = $false
$script:RenderCount = 0
$script:EventActive = $false
$script:DisposedDuringEvent = $false
$script:TimedOut = $false
$script:Flyout = [Windows.Forms.Form]::new()
$script:Flyout.ShowInTaskbar = $false
$script:Flyout.Opacity = 0
$script:Combo = [Windows.Forms.ComboBox]::new()
[void]$script:Combo.Items.Add('System')
[void]$script:Combo.Items.Add('Dark')
$script:Flyout.Controls.Add($script:Combo)

function Render-Flyout {
    if ($script:EventActive) { $script:DisposedDuringEvent = $true }
    $script:RenderCount++
    $script:Combo.Dispose()
    $script:Flyout.Close()
}

$script:Combo.Add_SelectedIndexChanged({
    $script:EventActive = $true
    Request-FlyoutRender
    if ($script:Combo.IsDisposed) { $script:DisposedDuringEvent = $true }
    $script:EventActive = $false
})
$script:Flyout.Add_Shown({ $script:Combo.SelectedIndex = 1 })
$timeout = [Windows.Forms.Timer]::new()
$timeout.Interval = 3000
$timeout.Add_Tick({
    $script:TimedOut = $true
    $script:Flyout.Close()
})
$timeout.Start()
[Windows.Forms.Application]::Run($script:Flyout)
$timeout.Stop()
$timeout.Dispose()
$script:Flyout.Dispose()
$script:RenderCount
$script:DisposedDuringEvent
$script:TimedOut
'''
        env = {**os.environ, "NEEDLE_TRAY_SCRIPT": str(ROOT / "windows" / "needle-tray.ps1")}
        for executable in WINDOWS_POWERSHELLS:
            with self.subTest(executable=executable):
                result = subprocess.run(
                    [executable, "-NoProfile", "-STA", "-Command", script],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=env,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip().splitlines(), ["1", "False", "False"])

    def test_theme_mode_and_palette_resolution(self):
        script = r'''
Set-StrictMode -Version Latest
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:NEEDLE_TRAY_SCRIPT,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -gt 0) { throw $errors[0] }
$names = @('Get-WindowsThemeMode', 'Get-ThemePalette', 'Test-CompleteThemePalette')
$definitions = $ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -in $names
}, $true) | Sort-Object { $_.Extent.StartOffset }
foreach ($definition in $definitions) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
Get-WindowsThemeMode ('{"windows":{"theme":"night"}}' | ConvertFrom-Json)
Get-WindowsThemeMode ('{"windows":{"theme":"sepia"}}' | ConvertFrom-Json)
(Get-ThemePalette 'light').background
(Get-ThemePalette 'system' $null $false).background
(Get-ThemePalette 'night').background
$custom = Get-ThemePalette 'night'
$custom.background = '#123456'
(Get-ThemePalette 'custom' $custom).background
Test-CompleteThemePalette $custom
$custom.Remove('accent')
(Get-ThemePalette 'custom' $custom).background
Test-CompleteThemePalette $custom
$empty = '{}' | ConvertFrom-Json
(Get-ThemePalette 'custom' $empty).background
Test-CompleteThemePalette $empty
$partial = '{"background":"#123456"}' | ConvertFrom-Json
(Get-ThemePalette 'custom' $partial).background
Test-CompleteThemePalette $partial
'''
        expected = [
            "night",
            "system",
            "#F7F8FA",
            "#181B20",
            "#060B14",
            "#123456",
            "True",
            "#181B20",
            "False",
            "#181B20",
            "False",
            "#181B20",
            "False",
        ]
        env = {**os.environ, "NEEDLE_TRAY_SCRIPT": str(ROOT / "windows" / "needle-tray.ps1")}
        for executable in WINDOWS_POWERSHELLS:
            with self.subTest(executable=executable):
                result = subprocess.run(
                    [executable, "-NoProfile", "-Command", script],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=env,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip().splitlines(), expected)

    def test_summary_formats_each_hover_mode(self):
        script = r'''
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:NEEDLE_TRAY_SCRIPT,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -gt 0) { throw $errors[0] }
$names = @(
    'Get-Value',
    'Get-WindowLeft',
    'Get-TrayHoverPercentageMode',
    'Get-Summary',
    'Get-EnabledServiceIds',
    'Test-ProviderConnected'
)
$definitions = $ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -in $names
}, $true) | Sort-Object { $_.Extent.StartOffset }
foreach ($definition in $definitions) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
$script:Services = @(
    [pscustomobject]@{ Id = 'claude' }
    [pscustomobject]@{ Id = 'codex' }
    [pscustomobject]@{ Id = 'zai' }
    [pscustomobject]@{ Id = 'openrouter' }
)
$script:TestConfig = $null
function Get-Config { return $script:TestConfig }
$data = '{"providers":[{"id":"codex","name":"ChatGPT / Codex","connected":true,"windows":[{"label":"5-hour","used":26},{"label":"Weekly","used":38}]}]}' | ConvertFrom-Json
foreach ($mode in @('hourly', 'weekly', 'both')) {
    $script:TestConfig = [pscustomobject]@{
        codex = [pscustomobject]@{ enabled = $true }
        windows = [pscustomobject]@{ tray_hover_percentage_mode = $mode }
    }
    Get-Summary $data
}
$script:TestConfig = [pscustomobject]@{ codex = [pscustomobject]@{ enabled = $true } }
Get-Summary $data
$hourlyOnly = '{"providers":[{"id":"codex","name":"ChatGPT / Codex","connected":true,"windows":[{"label":"5-hour","used":26}]}]}' | ConvertFrom-Json
Get-Summary $hourlyOnly
$script:TestConfig = [pscustomobject]@{
    claude = [pscustomobject]@{ enabled = $true }
    codex = [pscustomobject]@{ enabled = $true }
    zai = [pscustomobject]@{ enabled = $true }
    openrouter = [pscustomobject]@{ enabled = $true }
    windows = [pscustomobject]@{ tray_hover_percentage_mode = 'both' }
}
$longData = '{"providers":[{"id":"claude","name":"Claude","connected":true,"windows":[{"label":"5-hour","used":0},{"label":"Weekly","used":0}]},{"id":"codex","name":"ChatGPT / Codex","connected":true,"windows":[{"label":"5-hour","used":0},{"label":"Weekly","used":0}]},{"id":"zai","name":"z.ai","connected":true,"windows":[{"label":"5-hour","used":0},{"label":"Weekly","used":0}]},{"id":"openrouter","name":"OpenRouter","connected":true,"balance":{"remaining":100000}}]}' | ConvertFrom-Json
Get-Summary $longData
'''
        expected = [
            "Needle | G 74%",
            "Needle | G 62%",
            "Needle | G 74%/H 62%/W",
            "Needle | G 62%",
            "Needle | G n/a",
            "Needle | C 100%/H 100%/W G 100%/H 100%/W Z 100%/H 100%/W ...",
        ]
        env = {**os.environ, "NEEDLE_TRAY_SCRIPT": str(ROOT / "windows" / "needle-tray.ps1")}
        for executable in WINDOWS_POWERSHELLS:
            with self.subTest(executable=executable):
                result = subprocess.run(
                    [executable, "-NoProfile", "-Command", script],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=env,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip().splitlines(), expected)

    def test_once_mode_runs_shared_fetcher(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            config = temp / "config.json"
            cache = temp / "usage.json"
            config.write_text(
                json.dumps(
                    {
                        name: {"enabled": False}
                        for name in ("claude", "codex", "zai", "openrouter", "local")
                    }
                ),
                encoding="utf-8",
            )
            env = {**os.environ, "NEEDLE_CONFIG": str(config), "NEEDLE_CACHE": str(cache)}
            for executable in WINDOWS_POWERSHELLS:
                with self.subTest(executable=executable):
                    result = subprocess.run(
                        [
                            executable,
                            "-NoProfile",
                            "-ExecutionPolicy",
                            "Bypass",
                            "-File",
                            str(ROOT / "windows" / "needle-tray.ps1"),
                            "-Once",
                            "-FetcherPath",
                            str(ROOT / "fetcher" / "needle.py"),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=30,
                        env=env,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(cache.read_text(encoding="utf-8"))["providers"], [])


def journal_line(ts, text):
    return f"{ts} host ollama[1]: {text}"


def prompt_line(ts, n):
    return journal_line(ts, f"slot print_timing: id  0 | task 0 | prompt eval time =     100.00 ms / {n:>6} tokens (   1.00 ms per token,    10.00 tokens per second)")


def gen_line(ts, n):
    return journal_line(ts, f"slot print_timing: id  0 | task 0 |        eval time =     200.00 ms / {n:>6} tokens (   2.00 ms per token,     5.00 tokens per second)")


def model_line(ts, model):
    return journal_line(ts, f'time={ts} level=INFO source=images.go:395 msg="template selection" model=registry.ollama.ai/library/{model} selected=renderer_parser')


class FakeJournal:
    """Stands in for journalctl: hands out entries after the cursor it is given."""

    def __init__(self, entries):
        self.entries = list(entries)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        after = next((int(a.split("=", 1)[1][1:]) for a in cmd if a.startswith("--after-cursor=")), -1)
        new = [(i, line) for i, line in enumerate(self.entries) if i > after]
        if not new:
            return subprocess.CompletedProcess(cmd, 1, "-- No entries --\n", "")
        out = "\n".join(line for _, line in new) + f"\n-- cursor: c{new[-1][0]}\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")


def make_opencode_db(root):
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "opencode.db")
    conn.execute(
        "CREATE TABLE message (id text PRIMARY KEY, session_id text NOT NULL, "
        "time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL)"
    )
    conn.commit()
    conn.close()


def add_opencode_message(root, mid, created, provider, model, tokens_in, tokens_out, completed=True, reasoning=0):
    data = {
        "id": mid, "role": "assistant", "providerID": provider, "modelID": model,
        "time": {"created": created, **({"completed": created + 1000} if completed else {})},
        "tokens": {"input": tokens_in, "output": tokens_out, "reasoning": reasoning, "cache": {"read": 99999, "write": 0}},
    }
    conn = sqlite3.connect(root / "opencode.db")
    conn.execute("INSERT INTO message VALUES (?, 's', ?, ?, ?)", (mid, created, created, json.dumps(data)))
    conn.commit()
    conn.close()


class LocalAITests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.temp = Path(self._temp.name)
        patcher = mock.patch.object(needle, "CACHE_PATH", self.temp / "cache" / "usage.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temp.cleanup)
        self.opencode = self.temp / "opencode"
        self.log = self.temp / "server.log"
        self.cfg = {"opencode_dir": str(self.opencode), "ollama_log": str(self.log)}

    def fetch(self, journal=None, cfg=None):
        """One fetch_local, with journalctl replaced (or missing) and Linux assumed."""
        run = journal if journal is not None else mock.Mock(side_effect=FileNotFoundError("journalctl"))
        with mock.patch.object(needle.subprocess, "run", run), mock.patch.object(needle.sys, "platform", "linux"):
            return needle.fetch_local(cfg if cfg is not None else self.cfg)

    def store(self):
        return json.loads(needle.local_path().read_text())

    def test_journal_totals_add_up_across_fetches_without_double_counting(self):
        journal = FakeJournal([
            model_line("2026-10-04T10:00:00-04:00", "gemma4:e4b"),
            prompt_line("2026-10-04T10:00:01-04:00", 1000),
            gen_line("2026-10-04T10:00:02-04:00", 200),
        ])
        first = self.fetch(journal)["tally"]
        self.assertEqual((first["all_time"], first["input"], first["output"]), (1200, 1000, 200))
        self.assertEqual(first["sources"], ["ollama"])

        again = self.fetch(journal)["tally"]
        self.assertEqual(again["all_time"], 1200)
        self.assertIn("--after-cursor=c2", journal.calls[-1])

        # A request read in a later fetch still belongs to the model loaded earlier.
        journal.entries += [prompt_line("2026-10-04T11:00:00-04:00", 50), gen_line("2026-10-04T11:00:01-04:00", 7)]
        later = self.fetch(journal)["tally"]
        self.assertEqual(later["all_time"], 1257)
        self.assertEqual(later["top_models"], [{"model": "gemma4:e4b", "tokens": 1257}])
        self.assertEqual(self.store()["days"]["2026-10-04"]["gemma4:e4b"], {"in": 1050, "out": 207})

    def test_requests_are_split_by_model(self):
        journal = FakeJournal([
            model_line("2026-10-04T10:00:00-04:00", "gemma4:e4b"),
            prompt_line("2026-10-04T10:00:01-04:00", 100),
            model_line("2026-10-04T10:05:00-04:00", "qwen3.5:9b"),
            prompt_line("2026-10-04T10:05:01-04:00", 300),
            gen_line("2026-10-04T10:05:02-04:00", 30),
        ])
        tally = self.fetch(journal)["tally"]
        self.assertEqual(tally["top_models"], [{"model": "qwen3.5:9b", "tokens": 330}, {"model": "gemma4:e4b", "tokens": 100}])

    def test_journal_with_nothing_to_count_is_not_rescanned_from_the_start(self):
        journal = FakeJournal([])
        self.assertEqual(self.fetch(journal)["tally"]["all_time"], 0)
        self.fetch(journal)
        self.assertTrue(any(a.startswith("--since=@") for a in journal.calls[-1]))

    def test_journalctl_failure_keeps_the_running_total(self):
        journal = FakeJournal([prompt_line("2026-10-04T10:00:01-04:00", 500)])
        self.fetch(journal)
        failing = mock.Mock(return_value=subprocess.CompletedProcess([], 2, "", "permission denied"))
        self.assertEqual(self.fetch(failing)["tally"]["all_time"], 500)

    def test_missing_sources_count_nothing_and_do_not_fail(self):
        result = self.fetch()
        self.assertEqual(result["tally"]["all_time"], 0)
        self.assertEqual(result["tally"]["sources"], [])
        with mock.patch.object(needle, "PROVIDERS", [("local", "Local AI", lambda cfg: result)]):
            snap = needle.snapshot({}, {})
        self.assertFalse(snap["providers"][0]["connected"])

    def test_log_file_is_read_incrementally_and_after_rotation(self):
        self.log.write_text(
            'time=2026-10-04T10:00:00.000-04:00 level=INFO msg="template selection" model=registry.ollama.ai/library/llama3:8b\n'
            "slot print_timing: id  0 | task 0 | prompt eval time =  10.00 ms /    40 tokens\n"
            "slot print_timing: id  0 | task 0 |        eval time =  10.00 ms /     4 tok"  # still being written
        )
        self.assertEqual(self.fetch()["tally"]["all_time"], 40)
        with self.log.open("a") as handle:
            handle.write("ens\n[GIN] 2026/10/04 - 10:01:00 | 200 | 1s | 127.0.0.1 | POST \"/api/chat\"\n")
        tally = self.fetch()["tally"]
        self.assertEqual((tally["all_time"], tally["top_models"][0]["model"]), (44, "llama3:8b"))
        self.assertEqual(self.fetch()["tally"]["all_time"], 44)

        # Ollama started a fresh log: read it from the start, keep the old total.
        self.log.write_text("slot print_timing: id  0 | task 0 | prompt eval time =  10.00 ms /     6 tokens\n")
        self.assertEqual(self.fetch()["tally"]["all_time"], 50)

    def test_mac_reads_the_app_log_and_the_homebrew_service_log(self):
        home = self.temp / "home"
        self.assertEqual(needle.ollama_log_paths("darwin", {}, home), [
            home / ".ollama" / "logs" / "server.log",
            Path("/opt/homebrew/var/log/ollama.log"),
            Path("/usr/local/var/log/ollama.log"),
        ])
        self.assertEqual(needle.ollama_log_paths("linux", {}, home), [home / ".ollama" / "logs" / "server.log"])
        self.assertEqual(needle.ollama_log_paths("win32", {"LOCALAPPDATA": "C:/L"}, home),
                         [Path("C:/L") / "Ollama" / "server.log"])

        brew = self.temp / "ollama.log"
        self.log.write_text("slot print_timing: id  0 | task 0 | prompt eval time =  10.00 ms /    40 tokens\n")
        brew.write_text("slot print_timing: id  0 | task 0 | prompt eval time =  10.00 ms /     7 tokens\n")
        cfg = {"opencode_dir": str(self.opencode)}
        with mock.patch.object(needle, "ollama_log_paths", return_value=[self.log, brew, self.temp / "missing.log"]):
            tally = self.fetch(cfg=cfg)["tally"]
            self.assertEqual((tally["all_time"], tally["sources"]), (47, ["ollama"]))
            with brew.open("a") as handle:
                handle.write("slot print_timing: id  0 | task 1 | prompt eval time =  10.00 ms /     3 tokens\n")
            self.assertEqual(self.fetch(cfg=cfg)["tally"]["all_time"], 50)

    def test_the_1_7_0_log_bookmark_carries_over_without_counting_twice(self):
        self.log.write_text("slot print_timing: id  0 | task 0 | prompt eval time =  10.00 ms /    40 tokens\n")
        size = self.log.stat().st_size
        head = self.log.read_bytes()[:64].hex()
        needle.write_json(needle.local_path(), {
            "days": {"2026-10-04": {"llama3:8b": {"in": 40, "out": 0}}},
            "sources": {"ollama_log": {"path": str(self.log), "offset": size, "head": head}},
            "milestones": [],
        })
        self.assertEqual(self.fetch()["tally"]["all_time"], 40)
        self.assertNotIn("ollama_log", self.store()["sources"])
        self.assertEqual(self.store()["sources"]["ollama_logs"][str(self.log)]["offset"], size)

    def test_unreadable_log_is_skipped(self):
        self.log.mkdir()
        self.assertEqual(self.fetch()["tally"]["all_time"], 0)

    def test_opencode_counts_local_providers_only_and_only_once(self):
        make_opencode_db(self.opencode)
        add_opencode_message(self.opencode, "m1", 1_700_000_000_000, "ollama", "qwen2.5:7b", 1000, 100, reasoning=5)
        add_opencode_message(self.opencode, "m2", 1_700_000_000_000, "zai-coding-plan", "glm-4.7", 9999, 999)
        tally = self.fetch()["tally"]
        self.assertEqual(tally["all_time"], 1105)  # cache reads are never counted
        self.assertEqual(tally["sources"], ["opencode"])
        self.assertEqual(self.fetch()["tally"]["all_time"], 1105)

        # Same millisecond as the last one read, and a later one.
        add_opencode_message(self.opencode, "m3", 1_700_000_000_000, "lmstudio", "qwen3-coder", 10, 1)
        add_opencode_message(self.opencode, "m4", 1_700_000_100_000, "ollama", "qwen2.5:7b", 20, 2)
        self.assertEqual(self.fetch()["tally"]["all_time"], 1138)
        self.assertEqual(self.fetch()["tally"]["all_time"], 1138)

    def test_opencode_waits_for_a_message_that_is_still_streaming(self):
        make_opencode_db(self.opencode)
        now_ms = int(time.time() * 1000)
        add_opencode_message(self.opencode, "m1", now_ms - 5000, "ollama", "qwen", 0, 0, completed=False)
        self.assertEqual(self.fetch()["tally"]["all_time"], 0)
        conn = sqlite3.connect(self.opencode / "opencode.db")
        data = json.loads(conn.execute("SELECT data FROM message").fetchone()[0])
        data["time"]["completed"] = now_ms
        data["tokens"].update(input=300, output=30)
        conn.execute("UPDATE message SET data = ?", (json.dumps(data),))
        conn.commit()
        conn.close()
        self.assertEqual(self.fetch()["tally"]["all_time"], 330)

    def test_opencode_ollama_messages_are_left_to_the_ollama_log_once_it_has_counts(self):
        make_opencode_db(self.opencode)
        add_opencode_message(self.opencode, "old", 1_700_000_000_000, "ollama", "qwen", 100, 10)
        add_opencode_message(self.opencode, "new", 1_800_000_000_000, "ollama", "qwen", 5000, 500)
        add_opencode_message(self.opencode, "lms", 1_800_000_000_000, "lmstudio", "qwen", 7, 3)
        journal = FakeJournal([prompt_line("2026-06-01T00:00:00+00:00", 1)])  # 1780272000
        tally = self.fetch(journal)["tally"]
        self.assertEqual(tally["all_time"], 1 + 110 + 10)

    def test_opencode_json_files_are_read_when_there_is_no_database(self):
        folder = self.opencode / "storage" / "message" / "ses_1"
        folder.mkdir(parents=True)
        message = {"id": "m1", "role": "assistant", "providerID": "ollama", "modelID": "qwen2.5:7b",
                   "time": {"created": 1_700_000_000_000, "completed": 1_700_000_001_000},
                   "tokens": {"input": 70, "output": 7, "reasoning": 0, "cache": {"read": 0, "write": 0}}}
        (folder / "m1.json").write_text(json.dumps(message))
        before = (folder / "m1.json").read_bytes()
        self.assertEqual(self.fetch()["tally"]["all_time"], 77)
        self.assertEqual(self.fetch()["tally"]["all_time"], 77)
        self.assertEqual((folder / "m1.json").read_bytes(), before)

    def test_broken_opencode_database_is_skipped(self):
        self.opencode.mkdir()
        (self.opencode / "opencode.db").write_text("not a database")
        self.assertEqual(self.fetch()["tally"]["all_time"], 0)

    def test_new_source_appearing_later_is_added(self):
        journal = FakeJournal([prompt_line("2026-10-04T10:00:01-04:00", 100)])
        self.assertEqual(self.fetch(journal)["tally"]["all_time"], 100)
        make_opencode_db(self.opencode)
        add_opencode_message(self.opencode, "m1", 1_700_000_000_000, "ollama", "qwen", 40, 2)
        tally = self.fetch(journal)["tally"]
        self.assertEqual((tally["all_time"], tally["sources"]), (142, ["ollama", "opencode"]))

    def test_milestones_fire_once_and_not_for_usage_found_on_first_run(self):
        journal = FakeJournal([prompt_line("2026-10-04T10:00:00-04:00", 150_000)])
        self.assertNotIn("milestone", self.fetch(journal))  # 100K was already passed before Needle looked
        journal.entries.append(prompt_line("2026-10-04T10:00:01-04:00", 900_000))
        first = self.fetch(journal)
        self.assertEqual(first["milestone"]["value"], 1_000_000)
        self.assertNotIn("milestone", self.fetch(journal))
        journal.entries.append(prompt_line("2026-10-04T10:00:02-04:00", 1))
        self.assertNotIn("milestone", self.fetch(journal))
        self.assertEqual(self.store()["milestones"], [100_000, 1_000_000])

    def test_milestone_is_not_replayed_from_the_cache(self):
        tally = {"all_time": 1_000_000}
        fetch = mock.Mock(return_value={"tally": tally, "milestone": {"value": 1_000_000, "at": 1}})
        with mock.patch.object(needle, "PROVIDERS", [("local", "Local AI", fetch)]):
            first = needle.snapshot({}, {})
            self.assertEqual(first["providers"][0]["milestone"]["value"], 1_000_000)
            self.assertTrue(first["providers"][0]["connected"])
            again = needle.snapshot({}, first)
            cached = needle.snapshot({}, first, cached_only=True)
        fetch.assert_called_once()
        self.assertNotIn("milestone", again["providers"][0])
        self.assertNotIn("milestone", cached["providers"][0])

    def test_local_ai_can_be_turned_off(self):
        fetch = mock.Mock()
        with mock.patch.object(needle, "PROVIDERS", [("local", "Local AI", fetch)]):
            self.assertEqual(needle.snapshot({"local": {"enabled": False}}, {})["providers"], [])
        fetch.assert_not_called()

    def test_price_estimate(self):
        store = {"days": {"2026-10-04": {"m": {"in": 2_000_000, "out": 500_000}}}}
        self.assertEqual(needle.local_summary(store, {})["est_cost"], 2 * 1.00 + 0.5 * 5.00)
        custom = {"price_per_million": {"input": 3, "output": 15}}
        self.assertEqual(needle.local_summary(store, custom)["est_cost"], 6 + 7.5)
        partial = {"price_per_million": {"input": -1, "output": "free"}}
        self.assertEqual(needle.local_summary(store, partial)["price"], needle.LOCAL_PRICE)

    def test_today_week_and_all_time(self):
        now = time.mktime((2026, 10, 4, 12, 0, 0, 0, 0, -1))
        store = {"days": {
            "2026-10-04": {"a": {"in": 1, "out": 0}},
            "2026-09-30": {"a": {"in": 10, "out": 0}},
            "2026-09-01": {"b": {"in": 100, "out": 0}},
        }}
        s = needle.local_summary(store, {}, now)
        self.assertEqual((s["today"], s["week"], s["all_time"]), (1, 11, 111))

    def test_compact_numbers(self):
        cases = {0: "0", 999: "999", 1000: "1K", 1234: "1.2K", 133_000: "133K", 1_200_000: "1.2M", 10_000_000: "10M"}
        for n, text in cases.items():
            self.assertEqual(needle.compact(n), text)

    def test_text_output_shows_local_ai_once_found(self):
        tally = {"today": 2100, "week": 120_000, "all_time": 133_000, "est_cost": 0.2,
                 "top_models": [{"model": "gemma4:e4b", "tokens": 74_000}]}
        data = {"providers": [{"id": "local", "name": "Local AI", "connected": True, "tally": tally},
                              {"id": "local", "name": "Hidden", "connected": False, "tally": {}}]}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            needle.print_text(data)
        text = output.getvalue()
        self.assertIn("Today 2.1K   Week 120K   All time 133K", text)
        self.assertIn("gemma4:e4b 74K", text)
        self.assertIn("Worth about $0.20 at API prices (estimate)", text)
        self.assertNotIn("Hidden", text)

    def test_parallel_fetches_count_each_line_once(self):
        journal = FakeJournal([prompt_line("2026-10-04T10:00:00-04:00", 10)])
        results = []

        def worker():
            with needle.cache_lock(timeout=10):
                results.append(needle.fetch_local(self.cfg)["tally"]["all_time"])

        with mock.patch.object(needle.subprocess, "run", journal), mock.patch.object(needle.sys, "platform", "linux"):
            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(results, [10, 10, 10, 10])


if __name__ == "__main__":
    unittest.main()
