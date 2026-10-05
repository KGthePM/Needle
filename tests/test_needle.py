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


@unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELLS, "PowerShell tray smoke test requires Windows")
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

    def test_check_update_failure_is_quiet_and_waits_a_day(self):
        with mock.patch.object(needle, "latest_release", side_effect=OSError("offline")), \
                mock.patch.object(needle.time, "time", return_value=5000):
            self.assertEqual(needle.check_update({}, {}), {"checked_at": 5000})

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
