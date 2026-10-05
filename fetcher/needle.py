#!/usr/bin/env python3
"""
needle: one JSON snapshot of your Claude, ChatGPT/Codex, z.ai and OpenRouter limits.

  needle            refresh (respects per-provider cooldowns), print JSON
  needle --text     same, but a readable table for the terminal
  needle --cached   print the last snapshot without touching the network
  needle --force    skip cooldowns for z.ai / OpenRouter (Claude and Codex keep a floor)
  needle --debug    also dump raw API responses to stderr
  needle --update   download the latest release and rerun the installer

Standard library only. Works on Linux, macOS and Windows.
"""
import base64
import binascii
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

VERSION = "1.3.0"
TIMEOUT = 10


def default_paths(platform=None, environ=None, home=None):
    """Return platform-native config and cache paths."""
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    if platform == "win32":
        config_root = Path(environ.get("APPDATA") or home / "AppData" / "Roaming")
        cache_root = Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        return config_root / "Needle" / "config.json", cache_root / "Needle" / "cache" / "usage.json"
    config_root = Path(environ.get("XDG_CONFIG_HOME") or home / ".config")
    cache_root = Path(environ.get("XDG_CACHE_HOME") or home / ".cache")
    return config_root / "needle" / "config.json", cache_root / "needle" / "usage.json"


_DEFAULT_CONFIG, _DEFAULT_CACHE = default_paths()
CONFIG_PATH = Path(os.environ.get("NEEDLE_CONFIG") or _DEFAULT_CONFIG).expanduser()
CACHE_PATH = Path(os.environ.get("NEEDLE_CACHE") or _DEFAULT_CACHE).expanduser()

# Seconds between real API calls per provider. Anthropic's usage endpoint backs off
# hard if polled too often (and that backoff can hit Claude Code itself), so Claude
# has a 5-minute floor that --force does not bypass. Codex's endpoint is ChatGPT's own
# backend, so it gets a short floor too.
COOLDOWN = {"claude": 300, "codex": 300, "zai": 60, "openrouter": 60}
HARD_FLOOR = {"claude": 300, "codex": 60}

FIVE_HOURS = 5 * 3600
ONE_WEEK = 7 * 86400
DEBUG = False


class ProviderError(Exception):
    pass


class ResponseError(ProviderError):
    """The provider answered, but its response could not produce usage data."""


class MissingKey(ProviderError):
    """No key configured yet. Front ends can show this as 'not set up' rather than an error."""


# ----------------------------------------------------------------- helpers

def log(*parts):
    if DEBUG:
        print(*parts, file=sys.stderr)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    """Atomically write private JSON configuration or cache data."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def http_get(url, headers):
    req = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        body = resp.read().decode("utf-8", "replace")
    log(f"--- GET {url}\n{body}\n")
    return json.loads(body)


def parse_ts(value):
    """ISO string or epoch (s or ms) -> epoch seconds, or None."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    text = str(value).strip().replace("Z", "+00:00")
    for candidate in (text, text.split(".")[0] + text[-6:] if "." in text else text):
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return None


def window(label, used, resets_at=None, length=None, detail=None):
    used = max(0.0, min(100.0, float(used)))
    w = {"label": label, "used": round(used, 1), "resets_at": resets_at, "window_seconds": length}
    if detail:
        w["detail"] = detail
    return w


def friendly(err, provider):
    if isinstance(err, ProviderError):
        return str(err)
    if isinstance(err, urllib.error.HTTPError):
        if err.code == 401:
            if provider == "claude":
                return "Sign-in expired. Open Claude Code once to refresh it."
            if provider == "codex":
                return "Sign-in expired. Open OpenCode or Codex once to refresh it."
            return "API key was rejected. Change it in Settings."
        if err.code == 403:
            return "This key isn't allowed to read usage."
        if err.code == 429:
            return "Rate-limited. Showing the last values."
        return f"Server returned HTTP {err.code}."
    if isinstance(err, urllib.error.URLError):
        return "Couldn't reach the server. Check your connection."
    return f"Unexpected response ({type(err).__name__})."


# ----------------------------------------------------------------- Claude

def claude_credentials(cfg):
    path = Path(cfg.get("credentials_path", "~/.claude/.credentials.json")).expanduser()
    data = load_json(path, None) if path.exists() else None
    if data is None and sys.platform == "darwin":
        # On macOS Claude Code keeps its credentials in the login Keychain.
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=60,
            )
            if out.returncode == 0:
                data = json.loads(out.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            data = None
    if not data:
        raise ProviderError("Not signed in. Run `claude` and log in with your plan.")
    oauth = data.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        raise ProviderError("No plan sign-in found. Claude Code may be using an API key.")
    expires = oauth.get("expiresAt")
    if expires and parse_ts(expires) < time.time():
        raise ProviderError("Sign-in expired. Open Claude Code once to refresh it.")
    return token, oauth.get("subscriptionType")


def fetch_claude(cfg):
    token, plan = claude_credentials(cfg)
    data = http_get("https://api.anthropic.com/api/oauth/usage", {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": cfg.get("user_agent", f"needle/{VERSION}"),
    })
    spec = [("five_hour", "5-hour", FIVE_HOURS), ("seven_day", "Weekly", ONE_WEEK)]
    if cfg.get("show_model_windows"):
        spec += [("seven_day_opus", "Weekly Opus", ONE_WEEK), ("seven_day_sonnet", "Weekly Sonnet", ONE_WEEK)]
    windows = []
    for key, label, length in spec:
        w = data.get(key)
        if isinstance(w, dict) and w.get("utilization") is not None:
            windows.append(window(label, w["utilization"], parse_ts(w.get("resets_at")), length))
    if not windows:
        raise ResponseError("No usage windows returned for this account.")
    return {"plan": plan.title() if isinstance(plan, str) else None, "windows": windows}


# --------------------------------------------------------- ChatGPT / Codex

def jwt_claims(token):
    """Payload of a JWT, unverified. Only used to read expiry and account id."""
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (AttributeError, binascii.Error, IndexError, UnicodeDecodeError, ValueError):
        return {}


def account_from_claims(claims):
    if not isinstance(claims, dict):
        return None
    auth = claims.get("https://api.openai.com/auth") or {}
    if not isinstance(auth, dict):
        auth = {}
    organizations = claims.get("organizations") or []
    organization = organizations[0] if organizations and isinstance(organizations[0], dict) else {}
    return claims.get("chatgpt_account_id") or auth.get("chatgpt_account_id") or organization.get("id")


def opencode_credentials(cfg):
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    path = Path(cfg.get("opencode_auth_path", data_home / "opencode" / "auth.json")).expanduser()
    if not path.exists():
        return None
    data = load_json(path, None)
    if not isinstance(data, dict):
        raise ProviderError("Couldn't read OpenCode's sign-in file.")
    auth = data.get("openai")
    if not isinstance(auth, dict):
        return None
    if auth.get("type") != "oauth":
        raise ProviderError("OpenCode is using an API key, not a ChatGPT plan sign-in.")
    token = auth.get("access")
    if not token:
        raise ProviderError("OpenCode's ChatGPT sign-in has no access token. Reconnect OpenAI in OpenCode.")
    expires = parse_ts(auth.get("expires"))
    if expires is None:
        expires = parse_ts(jwt_claims(token).get("exp"))
    if expires is not None and expires < time.time():
        raise ProviderError("OpenCode's ChatGPT sign-in expired. Open OpenCode once to refresh it.")
    account = auth.get("accountId") or account_from_claims(jwt_claims(token))
    return token, account, "OpenCode"


def codex_cli_credentials(cfg):
    home = Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()
    path = Path(cfg.get("auth_path", home / "auth.json")).expanduser()
    if not path.exists():
        return None
    data = load_json(path, None)
    if not isinstance(data, dict):
        raise ProviderError("Couldn't read Codex's sign-in file.")
    tokens = data.get("tokens") or {}
    token = tokens.get("access_token")
    if not token:
        raise ProviderError("No plan sign-in found. Codex may be using an API key.")
    claims = jwt_claims(token)
    expires = parse_ts(claims.get("exp"))
    if expires is not None and expires < time.time():
        raise ProviderError("Sign-in expired. Open Codex once to refresh it.")
    account = tokens.get("account_id") or account_from_claims(jwt_claims(tokens.get("id_token"))) \
        or account_from_claims(claims)
    return token, account, "Codex CLI"


def codex_credentials(cfg):
    source = str(cfg.get("credential_source", "auto")).lower()
    if source not in ("auto", "opencode", "codex"):
        raise ProviderError("credential_source must be auto, opencode or codex.")
    readers = {
        "opencode": opencode_credentials,
        "codex": codex_cli_credentials,
    }
    order = ("opencode", "codex") if source == "auto" else (source,)
    problems = []
    for name in order:
        try:
            credentials = readers[name](cfg)
            if credentials:
                return credentials
        except ProviderError as err:
            problems.append(str(err))
    if problems:
        raise ProviderError(problems[0])
    if source == "opencode":
        raise ProviderError("Not signed in. Connect OpenAI to your ChatGPT plan in OpenCode.")
    if source == "codex":
        raise ProviderError("Not signed in. Run `codex` and log in with your ChatGPT plan.")
    raise ProviderError("Not signed in. Connect OpenAI in OpenCode or log in with Codex CLI.")


CODEX_PLANS = {"prolite": "Pro Lite", "promax": "Pro Max"}


def fetch_codex(cfg):
    token, account, source = codex_credentials(cfg)
    headers = {
        "Authorization": f"Bearer {token}",
        # chatgpt.com sits behind bot protection, so present the same agent Codex does.
        "User-Agent": cfg.get("user_agent", "codex-cli"),
    }
    if account:
        headers["ChatGPT-Account-Id"] = account
    data = http_get("https://chatgpt.com/backend-api/wham/usage", headers)
    limits = data.get("rate_limit") or {}
    windows = []
    for key, fallback in (("primary_window", "5-hour"), ("secondary_window", "Weekly")):
        w = limits.get(key)
        if not isinstance(w, dict) or w.get("used_percent") is None:
            continue
        # Label by length when given (a plan could have only a weekly window), else by position.
        secs = w.get("limit_window_seconds")
        label = fallback if not secs else "5-hour" if secs <= 86400 else "Weekly"
        windows.append(window(label, w["used_percent"], parse_ts(w.get("reset_at")),
                              FIVE_HOURS if label == "5-hour" else ONE_WEEK))
    if not windows:
        raise ResponseError("No usage windows returned for this account.")
    windows.sort(key=lambda w: w["label"] != "5-hour")
    plan = data.get("plan_type")
    plan = CODEX_PLANS.get(plan, plan.replace("_", " ").title()) if isinstance(plan, str) else None
    return {"plan": plan, "source": source, "windows": windows}


# ----------------------------------------------------------------- z.ai

ZAI_UNITS = {3: ("5-hour", FIVE_HOURS), 6: ("Weekly", ONE_WEEK)}  # TOKENS_LIMIT.unit


def fetch_zai(cfg):
    key = os.environ.get("ZAI_API_KEY") or cfg.get("api_key")
    if not key:
        raise MissingKey("Add your z.ai API key in Settings.")
    base = cfg.get("base_url", "https://api.z.ai/api/monitor").rstrip("/")
    body = http_get(f"{base}/usage/quota/limit", {"Authorization": f"Bearer {key}", "Accept-Language": "en-US,en"})
    if body.get("success") is False or body.get("code") not in (None, 0, 200):
        raise ResponseError(f"z.ai said: {body.get('msg') or 'unknown error'}")
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    limits = data.get("limits") or []

    token_windows, extra = [], []
    for lim in limits:
        kind = lim.get("type")
        reset = parse_ts(lim.get("nextResetTime"))
        pct = lim.get("percentage")
        cap, used_n = lim.get("usage"), lim.get("currentValue")
        if pct is None and cap:
            pct = 100.0 * (used_n or 0) / cap
        if pct is None:
            continue
        if kind == "TOKENS_LIMIT":
            label, length = ZAI_UNITS.get(lim.get("unit"), (None, None))
            token_windows.append((label, length, pct, reset))
        elif kind == "TIME_LIMIT" and cfg.get("show_tool_calls", True):
            detail = f"{used_n:,} of {cap:,}" if isinstance(cap, int) and isinstance(used_n, int) else None
            extra.append(window("Tool calls", pct, reset, None, detail))

    # Unknown units: fall back to "the one resetting soonest is the 5-hour window".
    if any(label is None for label, *_ in token_windows):
        token_windows.sort(key=lambda t: t[3] or float("inf"))
        fallback = [("5-hour", FIVE_HOURS), ("Weekly", ONE_WEEK)]
        token_windows = [
            (lbl or fallback[min(i, 1)][0], ln or fallback[min(i, 1)][1], p, r)
            for i, (lbl, ln, p, r) in enumerate(token_windows)
        ]
    order = {"5-hour": 0, "Weekly": 1}
    token_windows.sort(key=lambda t: order.get(t[0], 9))
    windows = [window(lbl, p, r, ln) for lbl, ln, p, r in token_windows] + extra
    if not windows:
        raise ResponseError("No quota windows returned. Is this a Coding Plan key?")
    level = data.get("level")
    return {"plan": str(level).title() if level else None, "windows": windows}


# ----------------------------------------------------------------- OpenRouter

def fetch_openrouter(cfg):
    key = os.environ.get("OPENROUTER_API_KEY") or cfg.get("api_key")
    if not key:
        raise MissingKey("Add an OpenRouter key in Settings.")
    auth = {"Authorization": f"Bearer {key}"}
    try:  # account balance (needs a management key)
        d = http_get("https://openrouter.ai/api/v1/credits", auth)["data"]
        total, spent = float(d["total_credits"]), float(d["total_usage"])
        return {"balance": {"label": "Credits", "remaining": round(total - spent, 2), "total": round(total, 2)}}
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403):
            raise
        log(f"/credits returned {e.code}; falling back to /key")
    d = http_get("https://openrouter.ai/api/v1/key", auth)["data"]  # per-key limit, any key
    limit, spent = d.get("limit"), float(d.get("usage") or 0)
    if limit is None:
        return {"balance": {"label": "Key spend", "remaining": None, "total": None, "spent": round(spent, 2)}}
    remaining = d.get("limit_remaining")
    remaining = float(remaining) if remaining is not None else float(limit) - spent
    return {"balance": {"label": "Key limit", "remaining": round(remaining, 2), "total": round(float(limit), 2)}}


PROVIDERS = [
    ("claude", "Claude", fetch_claude),
    ("codex", "ChatGPT / Codex", fetch_codex),
    ("zai", "z.ai", fetch_zai),
    ("openrouter", "OpenRouter", fetch_openrouter),
]
PROVIDER_IDS = {provider[0] for provider in PROVIDERS}
KEY_PROVIDERS = {"zai", "openrouter"}
TRAY_HOVER_PERCENTAGE_MODES = {"hourly", "weekly", "both"}
WINDOWS_THEME_MODES = {"light", "system", "dark", "night", "custom"}
WINDOWS_THEME_COLOR_ROLES = {
    "background",
    "surface",
    "text",
    "muted",
    "border",
    "accent",
    "hover",
    "track",
    "success",
    "warning",
    "critical",
    "claude",
    "codex",
    "zai",
    "openrouter",
}
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def load_config_for_update():
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as err:
            raise ValueError("The settings file could not be read; fix it before changing settings.") from err
    else:
        cfg = {}
    if not isinstance(cfg, dict):
        raise ValueError("The settings file does not contain a JSON object.")
    return cfg


def configure_service(provider, enabled=None, api_key=None, clear_key=False):
    """Update one service without making front ends rewrite the config file."""
    if provider not in PROVIDER_IDS:
        raise ValueError(f"Unknown service: {provider}")
    if api_key is not None and provider not in KEY_PROVIDERS:
        raise ValueError(f"{provider} does not use an API key")
    cfg = load_config_for_update()
    section = cfg.get(provider)
    if not isinstance(section, dict):
        section = {}
        cfg[provider] = section
    if enabled is not None:
        section["enabled"] = bool(enabled)
    if api_key is not None:
        section["api_key"] = api_key
        section["enabled"] = True
    if clear_key:
        section.pop("api_key", None)
    write_json(CONFIG_PATH, cfg)


def configure_tray_hover_percentage_mode(mode):
    if mode not in TRAY_HOVER_PERCENTAGE_MODES:
        raise ValueError(f"Unknown tray hover percentage mode: {mode}")
    cfg = load_config_for_update()
    section = cfg.get("windows")
    if not isinstance(section, dict):
        section = {}
        cfg["windows"] = section
    section["tray_hover_percentage_mode"] = mode
    write_json(CONFIG_PATH, cfg)


def configure_windows_theme(mode):
    if mode not in WINDOWS_THEME_MODES:
        raise ValueError(f"Unknown Windows theme: {mode}")
    cfg = load_config_for_update()
    section = cfg.get("windows")
    if not isinstance(section, dict):
        section = {}
        cfg["windows"] = section
    section["theme"] = mode
    write_json(CONFIG_PATH, cfg)


def configure_windows_custom_theme(palette):
    if not isinstance(palette, dict):
        raise ValueError("The custom theme must be a JSON object.")
    roles = set(palette)
    missing = sorted(WINDOWS_THEME_COLOR_ROLES - roles)
    unknown = sorted(roles - WINDOWS_THEME_COLOR_ROLES)
    if missing:
        raise ValueError(f"The custom theme is missing colors: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"The custom theme has unknown colors: {', '.join(unknown)}")
    invalid = sorted(
        role for role, color in palette.items()
        if not isinstance(color, str) or not HEX_COLOR.fullmatch(color)
    )
    if invalid:
        raise ValueError(f"The custom theme has invalid colors: {', '.join(invalid)}")
    cfg = load_config_for_update()
    section = cfg.get("windows")
    if not isinstance(section, dict):
        section = {}
        cfg["windows"] = section
    section["custom_theme"] = {role: palette[role].lower() for role in sorted(palette)}
    section["theme"] = "custom"
    write_json(CONFIG_PATH, cfg)


def run_config_command(argv):
    """Handle service mutations. Return None when argv is a normal fetch command."""
    commands = {
        "--enable-service": "enable",
        "--disable-service": "disable",
        "--set-service-key": "set-key",
        "--clear-service-key": "clear-key",
        "--set-tray-hover-percentage-mode": "tray-hover-percentage-mode",
        "--set-windows-theme": "windows-theme",
        "--set-windows-custom-theme": "windows-custom-theme",
    }
    selected = [(flag, action) for flag, action in commands.items() if flag in argv]
    if not selected:
        return None
    if len(selected) != 1:
        print("Choose one service configuration action at a time.", file=sys.stderr)
        return 2
    flag, action = selected[0]
    value = None
    if action != "windows-custom-theme":
        try:
            index = argv.index(flag)
            value = argv[index + 1]
        except IndexError:
            argument = "a mode" if action in {"tray-hover-percentage-mode", "windows-theme"} else "a service id"
            print(f"{flag} requires {argument}.", file=sys.stderr)
            return 2
    try:
        if action == "enable":
            configure_service(value, enabled=True)
        elif action == "disable":
            configure_service(value, enabled=False)
        elif action == "clear-key":
            configure_service(value, clear_key=True)
        elif action == "tray-hover-percentage-mode":
            configure_tray_hover_percentage_mode(value)
        elif action == "windows-theme":
            configure_windows_theme(value)
        elif action == "windows-custom-theme":
            try:
                palette = json.loads(sys.stdin.read())
            except ValueError as err:
                raise ValueError("The custom theme is not valid JSON.") from err
            configure_windows_custom_theme(palette)
        else:
            key = sys.stdin.read().strip()
            if not key:
                raise ValueError("No API key was provided.")
            configure_service(value, api_key=key)
    except (OSError, ValueError) as err:
        print(str(err), file=sys.stderr)
        return 2
    return 0


# ----------------------------------------------------------------- updates

RELEASES_URL = "https://api.github.com/repos/KGthePM/Needle/releases/latest"
UPDATE_EVERY = 86400


def version_tuple(text):
    try:
        return tuple(int(part) for part in str(text).lstrip("vV").split("."))
    except ValueError:
        return ()


def latest_release():
    data = http_get(RELEASES_URL, {"User-Agent": f"needle/{VERSION}"})
    # One-line summary: the first line of the notes that isn't a heading.
    lines = [l.strip() for l in (data.get("body") or "").splitlines()]
    notes = next((l.lstrip("*- ").strip() for l in lines if l and not l.startswith("#")), "")
    return {
        "latest": str(data.get("tag_name", "")).lstrip("vV"),
        "url": data.get("html_url"),
        "notes": notes,
        "zipball": data.get("zipball_url"),
    }


def check_update(cfg, cache, cached_only=False):
    """Last known release, refreshed at most once a day. Failures are quiet: try again tomorrow."""
    info = cache.get("update_check") or {}
    if cfg.get("check_updates", True) and not cached_only and time.time() - info.get("checked_at", 0) > UPDATE_EVERY:
        info = {"checked_at": time.time()}
        try:
            info.update(latest_release())
        except Exception as err:  # noqa: BLE001 - no release yet, offline, rate-limited
            log(f"update check: {err!r}")
    return info


def update_available(info):
    if version_tuple(info.get("latest")) > version_tuple(VERSION):
        return {k: info.get(k) for k in ("latest", "url", "notes")}
    return None


def self_update():
    import shutil
    import tempfile
    import zipfile

    try:
        release = latest_release()
    except Exception as err:  # noqa: BLE001
        print(f"Couldn't check for updates: {friendly(err, 'github')}")
        return 1
    if not update_available(release):
        print(f"Needle {VERSION} is up to date.")
        return 0
    print(f"Updating Needle {VERSION} to {release['latest']}...")
    tmp = Path(tempfile.mkdtemp(prefix="needle-update-"))
    try:
        archive = tmp / "needle.zip"
        req = urllib.request.Request(release["zipball"], headers={"User-Agent": f"needle/{VERSION}"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            archive.write_bytes(resp.read())
        with zipfile.ZipFile(archive) as z:
            z.extractall(tmp)
        root = next(p for p in tmp.iterdir() if p.is_dir())
        if sys.platform == "win32":
            cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                   "-File", str(root / "windows" / "install-windows.ps1"), "-Update"]
        elif sys.platform == "darwin":
            cmd = ["bash", str(root / "mac" / "install-mac.sh"), "--update"]
        else:
            cmd = ["bash", str(root / "install.sh"), "--update"]
        code = subprocess.run(cmd).returncode
    except Exception as err:  # noqa: BLE001
        print(f"Update failed: {friendly(err, 'github')}")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if code == 0:
        try:
            with cache_lock():
                cache = load_json(CACHE_PATH, {})
                cache.pop("update_check", None)
                write_cache(cache)
        except (OSError, TimeoutError):
            pass  # the stale check only says the installed version is current
        print(f"Needle is now {release['latest']}.")
    return code


# ----------------------------------------------------------------- main

def snapshot(cfg, cache, cached_only=False, force=False):
    previous = {p["id"]: p for p in cache.get("providers", [])}
    now = time.time()
    out = []
    for pid, name, fetch in PROVIDERS:
        pcfg = cfg.get(pid)
        if not isinstance(pcfg, dict) or not pcfg.get("enabled", True):
            continue
        prev = previous.get(pid)
        if cached_only:
            out.append(prev or {"id": pid, "name": name, "ok": False, "error": "No data yet. Press Refresh."})
            continue

        # Cooldown counts from the last attempt, successful or not, so a 429 is never
        # retried right away. Errors with no data (e.g. a missing key) only wait the hard floor.
        if prev:
            last_try = max(prev.get("fetched_at", 0), prev.get("attempted_at", 0))
            floor = HARD_FLOOR.get(pid, 0) if (force or not prev.get("ok")) else COOLDOWN[pid]
            if now - last_try < floor:
                out.append(prev)
                continue
        try:
            result = {"id": pid, "name": name, "ok": True, "fetched_at": now, **fetch(pcfg)}
        except Exception as err:  # noqa: BLE001 - every failure becomes a readable message
            log(f"{pid}: {err!r}")
            msg = friendly(err, pid)
            if prev and (prev.get("windows") or prev.get("balance")):
                result = {**prev, "ok": True, "stale": True, "error": msg}
            else:
                result = {"id": pid, "name": name, "ok": False, "error": msg}
                if isinstance(err, MissingKey):
                    result["needs_key"] = True
            if isinstance(err, ResponseError) or not isinstance(err, ProviderError):
                result["attempted_at"] = now
        out.append(result)
    for provider in out:
        # A service belongs in the usage view once it has produced usable data.
        # Cached data keeps it connected during temporary refresh failures.
        provider["connected"] = bool(provider.get("windows") or provider.get("balance"))
    return {"version": VERSION, "updated": now, "providers": out}


@contextmanager
def cache_lock(timeout=120):
    """Serialize refreshes so every process sees the latest cooldown state."""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    path = CACHE_PATH.with_suffix(CACHE_PATH.suffix + ".lock")
    handle = open(path, "a+b")  # noqa: SIM115 - held for the context lifetime
    if os.name == "nt":
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while not acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Another Needle refresh is still running.") from None
                time.sleep(0.1)
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def write_cache(data):
    write_json(CACHE_PATH, data)


def human(secs):
    if secs is None:
        return ""
    secs = max(0, int(secs))
    d, h, m = secs // 86400, secs % 86400 // 3600, secs % 3600 // 60
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{max(m, 1)}m"


def print_text(data):
    now = time.time()
    for p in data["providers"]:
        head = p["name"] + (f"  ({p['plan']})" if p.get("plan") else "")
        print(head)
        for w in p.get("windows", []):
            reset = f"resets in {human(w['resets_at'] - now)}" if w.get("resets_at") else ""
            print(f"  {w['label']:<14}{100 - w['used']:>5.0f}% left   {reset}")
        b = p.get("balance")
        if b:
            if b.get("remaining") is None:
                print(f"  {b['label']:<14} ${b.get('spent', 0):.2f} spent, no limit")
            else:
                print(f"  {b['label']:<14} ${b['remaining']:.2f} left of ${b['total']:.2f}")
        if p.get("error"):
            print(f"  ! {p['error']}")
        print()
    if data.get("update"):
        print(f"Update available: {data['update']['latest']} (run needle --update)")


def main(argv):
    global DEBUG
    if "--version" in argv:
        print(VERSION)
        return 0
    config_result = run_config_command(argv)
    if config_result is not None:
        return config_result
    DEBUG = "--debug" in argv
    if "--update" in argv:
        return self_update()
    cfg, config_error = {}, None
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except ValueError as e:
            config_error = f"Keys file has a formatting error near line {getattr(e, 'lineno', '?')}."
        except OSError:
            config_error = "Couldn't read the keys file."
    cached_only = "--cached" in argv

    def collect(cache, cache_only=cached_only):
        result = snapshot(cfg, cache, cached_only=cache_only, force="--force" in argv)
        if config_error:
            for provider in result["providers"]:
                if provider["id"] not in ("claude", "codex") and not provider.get("windows") \
                        and not provider.get("balance"):
                    provider["error"] = config_error
        result["update_check"] = check_update(cfg, cache, cached_only=cache_only)
        update = update_available(result["update_check"])
        if update:
            result["update"] = update
        return result

    if cached_only:
        data = collect(load_json(CACHE_PATH, {}))
    else:
        try:
            with cache_lock():
                data = collect(load_json(CACHE_PATH, {}))
                write_cache(data)
        except TimeoutError as err:
            data = collect(load_json(CACHE_PATH, {}), cache_only=True)
            for provider in data["providers"]:
                if not provider.get("error"):
                    provider["error"] = str(err)
    if "--text" in argv:
        print_text(data)
    else:
        print(json.dumps(data))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
