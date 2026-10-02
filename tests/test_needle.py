import base64
import codecs
import contextlib
import importlib.util
import io
import json
import os
import shutil
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


@unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELLS, "PowerShell tray smoke test requires Windows")
class WindowsTrayTests(unittest.TestCase):
    def test_once_mode_runs_shared_fetcher(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            config = temp / "config.json"
            cache = temp / "usage.json"
            config.write_text(
                json.dumps(
                    {
                        name: {"enabled": False}
                        for name in ("claude", "codex", "zai", "openrouter")
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


if __name__ == "__main__":
    unittest.main()
