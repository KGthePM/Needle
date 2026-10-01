#!/usr/bin/env python3
"""
needle: one JSON snapshot of your Claude, Codex, z.ai and OpenRouter limits.

  needle            refresh (respects per-provider cooldowns), print JSON
  needle --text     same, but a readable table for the terminal
  needle --cached   print the last snapshot without touching the network
  needle --force    skip cooldowns for z.ai / OpenRouter (Claude and Codex keep a floor)
  needle --debug    also dump raw API responses to stderr

Standard library only. Works on Linux and macOS.
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.1.0"
TIMEOUT = 10

CONFIG_PATH = Path(
    os.environ.get("NEEDLE_CONFIG")
    or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "needle" / "config.json"
).expanduser()
CACHE_PATH = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")).expanduser() / "needle" / "usage.json"

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


class MissingKey(ProviderError):
    """No key configured yet. Front ends can show this as 'not set up' rather than an error."""


# ----------------------------------------------------------------- helpers

def log(*parts):
    if DEBUG:
        print(*parts, file=sys.stderr)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


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
                return "Sign-in expired. Open Codex once to refresh it."
            return "API key was rejected. Check it with Edit keys."
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
        raise ProviderError("No usage windows returned for this account.")
    return {"plan": plan.title() if isinstance(plan, str) else None, "windows": windows}


# ----------------------------------------------------------------- Codex

def jwt_claims(token):
    """Payload of a JWT, unverified. Only used to read expiry and account id."""
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (AttributeError, IndexError, ValueError):
        return {}


def codex_credentials(cfg):
    home = Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()
    path = Path(cfg.get("auth_path", home / "auth.json")).expanduser()
    data = load_json(path, None) if path.exists() else None
    if not data:
        raise ProviderError("Not signed in. Run `codex` and log in with your ChatGPT plan.")
    tokens = data.get("tokens") or {}
    token = tokens.get("access_token")
    if not token:
        raise ProviderError("No plan sign-in found. Codex may be using an API key.")
    claims = jwt_claims(token)
    if claims.get("exp") and claims["exp"] < time.time():
        raise ProviderError("Sign-in expired. Open Codex once to refresh it.")
    account = tokens.get("account_id") or jwt_claims(tokens.get("id_token")).get(
        "https://api.openai.com/auth", {}).get("chatgpt_account_id")
    return token, account


CODEX_PLANS = {"prolite": "Pro Lite", "promax": "Pro Max"}


def fetch_codex(cfg):
    token, account = codex_credentials(cfg)
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
        raise ProviderError("No usage windows returned for this account.")
    windows.sort(key=lambda w: w["label"] != "5-hour")
    plan = data.get("plan_type")
    plan = CODEX_PLANS.get(plan, plan.replace("_", " ").title()) if isinstance(plan, str) else None
    return {"plan": plan, "windows": windows}


# ----------------------------------------------------------------- z.ai

ZAI_UNITS = {3: ("5-hour", FIVE_HOURS), 6: ("Weekly", ONE_WEEK)}  # TOKENS_LIMIT.unit


def fetch_zai(cfg):
    key = os.environ.get("ZAI_API_KEY") or cfg.get("api_key")
    if not key:
        raise MissingKey("Add your z.ai API key with Edit keys.")
    base = cfg.get("base_url", "https://api.z.ai/api/monitor").rstrip("/")
    body = http_get(f"{base}/usage/quota/limit", {"Authorization": f"Bearer {key}", "Accept-Language": "en-US,en"})
    if body.get("success") is False or body.get("code") not in (None, 0, 200):
        raise ProviderError(f"z.ai said: {body.get('msg') or 'unknown error'}")
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
        raise ProviderError("No quota windows returned. Is this a Coding Plan key?")
    level = data.get("level")
    return {"plan": str(level).title() if level else None, "windows": windows}


# ----------------------------------------------------------------- OpenRouter

def fetch_openrouter(cfg):
    key = os.environ.get("OPENROUTER_API_KEY") or cfg.get("api_key")
    if not key:
        raise MissingKey("Add an OpenRouter key with Edit keys.")
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
    ("codex", "Codex", fetch_codex),
    ("zai", "z.ai", fetch_zai),
    ("openrouter", "OpenRouter", fetch_openrouter),
]


# ----------------------------------------------------------------- main

def snapshot(cfg, cache, cached_only=False, force=False):
    previous = {p["id"]: p for p in cache.get("providers", [])}
    now = time.time()
    out = []
    for pid, name, fetch in PROVIDERS:
        pcfg = cfg.get(pid, {})
        if not pcfg.get("enabled", True):
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
            if not isinstance(err, ProviderError):  # setup problems can retry immediately
                result["attempted_at"] = now
        out.append(result)
    return {"version": VERSION, "updated": now, "providers": out}


def write_cache(data):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, CACHE_PATH)


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


def main(argv):
    global DEBUG
    if "--version" in argv:
        print(VERSION)
        return 0
    DEBUG = "--debug" in argv
    cfg, config_error = {}, None
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except ValueError as e:
            config_error = f"Keys file has a formatting error near line {getattr(e, 'lineno', '?')}."
        except OSError:
            config_error = "Couldn't read the keys file."
    cache = load_json(CACHE_PATH, {})
    data = snapshot(cfg, cache, cached_only="--cached" in argv, force="--force" in argv)
    if config_error:
        for p in data["providers"]:
            if p["id"] not in ("claude", "codex") and not p.get("windows") and not p.get("balance"):
                p["error"] = config_error
    if "--cached" not in argv:
        write_cache(data)
    if "--text" in argv:
        print_text(data)
    else:
        print(json.dumps(data))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
