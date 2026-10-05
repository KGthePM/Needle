#!/usr/bin/python3
# <xbar.title>Needle</xbar.title>
# <xbar.version>v1.5.0</xbar.version>
# <xbar.author>KGthePM</xbar.author>
# <xbar.desc>Claude, ChatGPT/Codex, z.ai and OpenRouter limits at a glance.</xbar.desc>
# <xbar.dependencies>python3</xbar.dependencies>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
"""
SwiftBar front end for the needle fetcher (~/.local/bin/needle).
The ".5m." in the file name makes SwiftBar run this every 5 minutes. The fetcher's
own cooldowns still apply, so Claude is never asked more than once per 5 minutes.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

FETCHER = Path.home() / ".local" / "bin" / "needle"
CONFIG = Path(os.environ.get("NEEDLE_CONFIG") or
              Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "needle" / "config.json").expanduser()
PY = sys.executable or "/usr/bin/python3"
PLUGIN = Path(__file__).resolve()
# Providers whose key can be pasted from the menu: (name, where to get one, dialog hint).
KEY_SETUP = {
    "zai": ("z.ai", "https://z.ai/manage-apikey/apikey-list", "Paste your z.ai Coding Plan API key."),
    "openrouter": ("OpenRouter", "https://openrouter.ai/settings/keys",
                   "Paste an OpenRouter key. A management key shows your whole balance; "
                   "a regular key shows that key's own limit."),
}
SERVICES = (
    ("claude", "Claude"),
    ("codex", "ChatGPT / Codex"),
    ("zai", "z.ai"),
    ("openrouter", "OpenRouter"),
)
SERVICE_NAMES = dict(SERVICES)
GET_KEY = "__get_key__"

# Rows with no action are drawn disabled, and macOS greys out their colors. A no-op
# action keeps them enabled so the dots, bars and pace words stay colored.
LIVE = {"bash": "/usr/bin/true", "terminal": False}
MONO = {"font": "Menlo", "size": "12", "ansi": True, **LIVE}
SMALL = {"size": "11", "color": "#86868b"}
# SwiftBar on macOS 26 only renders the basic 8 ANSI colors (24-bit codes come out
# white), and it ignores sfcolor on symbols, so provider dots are emoji instead.
INK = {"ok": 32, "warn": 33, "crit": 31, "dim": None}  # green, yellow, red, default text
DOT = {"claude": "🟠", "codex": "⚫", "zai": "🔵", "openrouter": "🟣"}
PANEL_TAG = {"claude": "C", "codex": "G", "zai": "Z"}
PACE_WINDOWS = ("5-hour", "Weekly")
PACE = {"fast": ("▲ fast", "warn"), "even": ("● on pace", "dim"), "slow": ("▼ plenty", "ok")}
CELLS = 16


# ------------------------------------------------------------------ output helpers

def item(text, **params):
    """Print one SwiftBar line. Values with spaces get quoted."""
    parts = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        value = str(value).replace('"', "'")
        parts.append(f'{key}="{value}"' if " " in value else f"{key}={value}")
    print(f"{text} | {' '.join(parts)}" if parts else text)


def sep():
    print("---")


def level(left):
    return "crit" if left <= 10 else "warn" if left <= 30 else "ok"


def duration(secs):
    secs = max(0, int(secs))
    d, h, m = secs // 86400, secs % 86400 // 3600, secs % 3600 // 60
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{max(m, 1)}m"


def clock(ts):
    return time.strftime("%-I:%M %p", time.localtime(ts))


def freshness(p):
    """'Updated 2:14 PM · next refresh at 2:19 PM'. Clock times, not 'ago', because
    SwiftBar only redraws the menu when the plugin reruns."""
    if not p.get("fetched_at"):
        return ""
    text = f"Updated {clock(p['fetched_at'])}"
    if p.get("refresh_at", 0) > time.time():
        text += f" · next refresh at {clock(p['refresh_at'])}"
    return text


def update_status(data):
    """What the last check found when no update is waiting, shown under Check for updates."""
    if not data:
        return ""
    check = data.get("update_check") or {}
    if not check.get("checked_at"):
        return ""
    if not check.get("latest"):
        return "Couldn't reach GitHub"
    return f"Up to date, checked {clock(check['checked_at'])}"


def refresh_ready_at(providers, key):
    """When Refresh would next fetch anything new: the soonest provider off cooldown."""
    if not providers or any(not p.get(key) for p in providers):
        return 0
    return min(p[key] for p in providers)


def paint(text, ink):
    """Wrap text in a basic ANSI color (needs ansi=true on the line)."""
    code = INK[ink]
    return f"\x1b[{code}m{text}\x1b[0m" if code else text


def bar(frac, ink):
    """Text gauge. The colored fill is what's left."""
    frac = max(0.0, min(1.0, frac))
    filled = round(frac * CELLS)
    return paint("█" * filled, ink) + paint("░" * (CELLS - filled), "dim")


def binding_left(p):
    ws = [w for w in p.get("windows", []) if w["label"] in PACE_WINDOWS]
    return min(100 - w["used"] for w in ws) if ws else None


# ------------------------------------------------------------------ data

def load():
    if not FETCHER.exists():
        return None, "The fetcher isn't installed. Run install-mac.sh again."
    try:
        out = subprocess.run([PY, str(FETCHER)], capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired):
        return None, "The fetcher didn't finish. Try Refresh, or open Debug in Terminal."
    if out.returncode != 0 or not out.stdout.strip():
        return None, "The fetcher stopped with an error. Open Debug in Terminal to see why."
    try:
        return json.loads(out.stdout), None
    except ValueError:
        return None, "Couldn't read the fetcher's output. Open Debug in Terminal to check it."


def load_config():
    """Read service state without trusting stale fetcher output or malformed settings."""
    try:
        config = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    except (OSError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def service_state(config):
    enabled, keys = set(), set()
    for pid, _ in SERVICES:
        section = config.get(pid)
        if not isinstance(section, dict):
            continue
        if section.get("enabled", True) is True:
            enabled.add(pid)
        if isinstance(section.get("api_key"), str) and section["api_key"].strip():
            keys.add(pid)
    return enabled, keys


def provider_connected(provider):
    if not isinstance(provider, dict):
        return False
    if "connected" in provider:
        return provider["connected"] is True
    return bool(provider.get("windows") or provider.get("balance"))


# ------------------------------------------------------------------ rendering

def render_title(providers):
    parts, lefts = [], []
    for p in providers:
        left = binding_left(p)
        # Each provider is colored on its own, so one low limit doesn't turn the whole title red.
        if left is not None:
            text = f"{PANEL_TAG.get(p['id'], p['name'][0])} {round(left)}%"
            parts.append(paint(text, level(left)) if level(left) != "ok" else text)
            lefts.append(left)
        elif p.get("balance") and p["balance"].get("remaining") is not None:
            b = p["balance"]
            text = f"${round(b['remaining'])}"
            if b.get("total"):
                pct = 100 * b["remaining"] / b["total"]
                lefts.append(pct)
                text = paint(text, level(pct)) if level(pct) != "ok" else text
            parts.append(text)
    lowest = min(lefts, default=None)
    item("  ".join(parts) or "AI", sfimage=gauge_symbol(lowest), ansi=True)


def gauge_symbol(left):
    """Menu-bar icon whose needle follows the tightest limit."""
    if left is None:
        step = 67
    else:
        step = 0 if left <= 10 else 33 if left <= 30 else 50 if left <= 60 else 67 if left <= 85 else 100
    return f"gauge.with.dots.needle.{step}percent"


def pace(w, left):
    """Compare what's left with how much of the window is left: fast, even or slow."""
    if not (w.get("window_seconds") and w.get("resets_at")):
        return None, None
    time_left = max(0.0, min(1.0, (w["resets_at"] - time.time()) / w["window_seconds"]))
    if left / 100 < time_left - 0.10:
        return "fast", "You're using it faster than it resets."
    if left / 100 > time_left + 0.10:
        return "slow", "Plenty of room for the time left."
    return "even", "Right on pace."


def render_provider(p, width):
    head = p["name"] + (f" {p['plan']}" if p.get("plan") else "")
    item(f"{DOT.get(p['id'], '⚪')} {head}", size="13", **LIVE)

    for w in p.get("windows", []):
        left = 100 - w["used"]
        ink = level(left)
        speed, note = pace(w, left)
        reset = duration(w["resets_at"] - time.time()) if w.get("resets_at") else ""
        line = f"{w['label']:<{width}}{bar(left / 100, ink)} {paint(f'{round(left):>4}%', ink)}"
        if w.get("detail"):
            line += f"  {w['detail']}"
        else:
            line += f"  {paint('↻', 'dim')} {reset:<8}"
            if speed:
                line += f"  {paint(*PACE[speed])}"
        tip = f"{round(left)}% left" + (f", resets in {reset}" if reset else "") + (f". {note}" if note else "")
        item(line, tooltip=tip, **MONO)

    b = p.get("balance")
    if b:
        name = b.get("label", "Credits")
        if b.get("remaining") is None:
            item(f"{name:<{width}}${b.get('spent', 0):.2f} spent, no limit", **MONO)
        else:
            frac = b["remaining"] / b["total"] if b.get("total") else 0
            ink = level(frac * 100)
            money = paint(f"${b['remaining']:.2f}", ink)
            item(f"{name:<{width}}{bar(frac, ink)}  {money} of ${b['total']:.2f}", **MONO)

    if p.get("error"):
        fix = set_key_action(p["id"]) if p["id"] in KEY_SETUP else {}
        item(p["error"], sfimage="exclamationmark.triangle", **SMALL, **fix)
    if p.get("fetched_at"):
        item(freshness(p), **SMALL)


def set_key_action(pid):
    """Menu params that open the key dialog for one provider, then refresh."""
    return {"bash": PY, "param1": str(PLUGIN), "param2": "--set-key", "param3": pid,
            "terminal": False, "refresh": True}


def add_key_action(pid):
    return {"bash": PY, "param1": str(PLUGIN), "param2": "--add-service", "param3": pid,
            "terminal": False, "refresh": True}


def fetcher_action(command, pid):
    return {"bash": PY, "param1": str(FETCHER), "param2": command, "param3": pid,
            "terminal": False, "refresh": True}


def force_refresh_action():
    return {"bash": PY, "param1": str(FETCHER), "param2": "--force",
            "terminal": False, "refresh": True}


def remove_action(pid, has_key):
    if pid in KEY_SETUP and has_key:
        return {"bash": PY, "param1": str(PLUGIN), "param2": "--remove-service", "param3": pid,
                "terminal": False, "refresh": True}
    return fetcher_action("--disable-service", pid)


def render_unset(providers):
    """One line per provider that just needs a key, instead of a section each."""
    for p in providers:
        dot = DOT.get(p["id"], "⚪")
        item(f"{dot} Add {p['name']} key…", tooltip="Opens a box to paste your key", **set_key_action(p["id"]))


def render_add_services(connected, enabled, stored_keys, providers, prominent=False):
    label = "＋ Add more…" if prominent else "＋ Add more"
    item(label, size="13" if prominent else None, sfimage="plus.circle")
    available = [(pid, name) for pid, name in SERVICES if pid not in connected]
    if not available:
        item("--All services are connected", **SMALL)
        return
    for pid, name in available:
        provider = providers.get(pid, {})
        error = provider.get("error") if isinstance(provider, dict) else None
        if pid not in enabled:
            needs_key = pid in KEY_SETUP and pid not in stored_keys
            action = add_key_action(pid) if needs_key else fetcher_action("--enable-service", pid)
            item(f"--{name}", **action)
            continue

        item(f"--{name}")
        if error:
            item(f"----{error}", **SMALL)
        if pid in KEY_SETUP and pid not in stored_keys:
            item("----Add key…", **set_key_action(pid))
            continue
        item("----Retry", **force_refresh_action())
        if pid in KEY_SETUP:
            item("----Change key…", **set_key_action(pid))


def render_update(data):
    update = (data or {}).get("update")
    if not update:
        return
    item(f"Update to {update['latest']}…", sfimage="arrow.down.circle", tooltip=update.get("notes") or None,
         bash=PY, param1=str(FETCHER), param2="--update", terminal=True)
    if update.get("url"):
        item("--Release notes", href=update["url"])
    sep()


def render_refresh(label, ready_at, **action):
    # Stays clickable: SwiftBar won't redraw when the cooldown ends, so a disabled row
    # could outlive it. The time tells people a click before then changes nothing.
    if ready_at > time.time():
        label += f" (new data after {clock(ready_at)})"
    item(f"--{label}", **action)


def render_settings(data, enabled, stored_keys):
    item("Settings", sfimage="gearshape")
    if enabled:
        item("--Services")
        for pid, name in SERVICES:
            if pid not in enabled:
                continue
            item(f"----{name}")
            if pid in KEY_SETUP:
                item("------Change key…", **set_key_action(pid))
            item("------Remove…", **remove_action(pid, pid in stored_keys))
    providers = [p for p in (data or {}).get("providers", [])
                 if isinstance(p, dict) and p.get("id") in enabled]
    render_refresh("Refresh", refresh_ready_at(providers, "refresh_at"), refresh=True, sfimage="arrow.clockwise")
    render_refresh("Refresh now (skip cooldowns)", refresh_ready_at(providers, "force_refresh_at"),
                   **force_refresh_action())
    if data and data.get("update"):
        item(f"--Update to {data['update']['latest']}…", bash=PY, param1=str(FETCHER), param2="--update",
             terminal=True)
    else:
        item("--Check for updates", bash=PY, param1=str(FETCHER), param2="--check-updates",
             terminal=False, refresh=True)
        status = update_status(data)
        if status:
            item(f"--{status}", **SMALL)
    item("--Open raw config", bash="/usr/bin/open", param1="-t", param2=str(CONFIG), terminal=False)
    item("--Debug in Terminal", bash=PY, param1=str(FETCHER), param2="--text", param3="--debug",
         param4="--force", terminal=True)


# ------------------------------------------------------------------ key dialog

def osascript(script):
    return subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True)


def alert(message):
    osascript(f'display alert "Needle" message {json.dumps(message)} as warning')


def ask_key(pid):
    """Native dialog with a hidden field. Returns the pasted key, or None if cancelled."""
    name, url, hint = KEY_SETUP[pid]
    prompt = f"{hint}\n\nIt's saved only on this Mac, in {CONFIG.parent.name}/{CONFIG.name}."
    script = (
        f"set r to display dialog {json.dumps(prompt)} default answer \"\" with hidden answer "
        f"with title {json.dumps(name + ' key')} buttons {{\"Get a key…\", \"Cancel\", \"Save\"}} "
        f"default button \"Save\" cancel button \"Cancel\"\n"
        f"if button returned of r is \"Get a key…\" then return \"{GET_KEY}\"\n"
        f"return text returned of r"
    )
    while True:
        out = osascript(script)
        if out.returncode != 0:  # Cancel
            return None
        answer = out.stdout.strip()
        if answer != GET_KEY:
            return answer or None
        subprocess.run(["/usr/bin/open", url])


def run_config_command(command, pid, key=None):
    try:
        out = subprocess.run(
            [PY, str(FETCHER), command, pid], input=key, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        alert("Needle couldn't update the service settings. Try again, or open Debug in Terminal.")
        return False
    if out.returncode:
        alert(out.stderr.strip() or "Needle couldn't update the service settings.")
        return False
    return True


def set_key(pid):
    if pid not in KEY_SETUP:
        return
    key = ask_key(pid)
    if not key:
        return
    if not run_config_command("--set-service-key", pid, key):
        return
    # Fetch now so the menu shows the new numbers (or the key's error) as soon as it refreshes.
    subprocess.run([PY, str(FETCHER), "--force"], capture_output=True, timeout=90)


def remove_service(pid):
    if pid not in KEY_SETUP:
        return
    _, stored_keys = service_state(load_config())
    delete_key = False
    if pid in stored_keys:
        name = SERVICE_NAMES[pid]
        prompt = f"Remove {name} from Needle?\n\nKeep its stored API key to make it easier to add again later."
        script = (
            f"set r to display dialog {json.dumps(prompt)} with title \"Remove service\" "
            f"buttons {{\"Cancel\", \"Delete key\", \"Keep key\"}} default button \"Keep key\" "
            f"cancel button \"Cancel\"\nreturn button returned of r"
        )
        out = osascript(script)
        if out.returncode != 0:
            return
        delete_key = out.stdout.strip() == "Delete key"
    if not run_config_command("--disable-service", pid):
        return
    if delete_key:
        run_config_command("--clear-service-key", pid)


def main():
    if sys.argv[1:2] == ["--set-key"] and len(sys.argv) > 2:
        set_key(sys.argv[2])
        return
    if sys.argv[1:2] == ["--add-service"] and len(sys.argv) > 2:
        set_key(sys.argv[2])
        return
    if sys.argv[1:2] == ["--remove-service"] and len(sys.argv) > 2:
        remove_service(sys.argv[2])
        return

    config = load_config()
    enabled, stored_keys = service_state(config)
    data, fatal = load()
    raw_providers = (data or {}).get("providers", [])
    provider_map = {p.get("id"): p for p in raw_providers if isinstance(p, dict) and p.get("id")}
    providers = [p for p in raw_providers
                 if isinstance(p, dict) and p.get("id") in enabled and provider_connected(p)]
    connected = {p["id"] for p in providers}
    render_title(providers)
    sep()

    if providers:
        if fatal:
            item(fatal, **SMALL)
        labels = [w["label"] for p in providers for w in p.get("windows", [])]
        labels += [p["balance"].get("label", "Credits") for p in providers if p.get("balance")]
        width = max(map(len, labels), default=8) + 2
        for i, p in enumerate(providers):
            if i:
                sep()
            render_provider(p, width)
        sep()
    elif fatal:
        item(fatal, **SMALL)

    render_update(data)
    render_add_services(connected, enabled, stored_keys, provider_map, prominent=not connected)
    render_settings(data, enabled, stored_keys)


if __name__ == "__main__":
    main()
