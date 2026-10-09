#!/usr/bin/python3
# <xbar.title>Needle</xbar.title>
# <xbar.version>v1.14.0</xbar.version>
# <xbar.author>KGthePM</xbar.author>
# <xbar.desc>Claude, ChatGPT/Codex, z.ai and OpenRouter limits at a glance, plus a count of your local AI tokens.</xbar.desc>
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
# Which providers have had a "nearly out" notification, so each one fires once per dip.
ALERTS = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "needle" / "mac-alerts.json"
# The biggest Local AI milestone already announced, so none is announced twice.
MILESTONE = ALERTS.with_name("mac-milestone.json")
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
SIGNATURE = "PineNeedle"
SIGNATURE_URL = "https://pinecomputenj.com/needle"
# SwiftBar on macOS 26 only renders the basic 8 ANSI colors (24-bit codes come out
# white), and it ignores sfcolor on symbols, so provider dots are emoji instead.
INK = {"ok": 32, "warn": 33, "crit": 31, "dim": None}  # green, yellow, red, default text
DOT = {"claude": "🟠", "codex": "⚫", "zai": "🔵", "openrouter": "🟣", "local": "🟢"}
PANEL_TAG = {"claude": "C", "codex": "G", "zai": "Z"}
PACE_WINDOWS = ("5-hour", "Weekly")
# Settings > Menu bar shows. SwiftBar cycles through title lines on its own, so
# "alternate" just prints one line per window.
MENU_BAR_WINDOWS = (
    ("tightest", "Tightest limit"),
    ("5-hour", "5-hour"),
    ("weekly", "Weekly"),
    ("both", "Both (5-hour/weekly)"),
    ("alternate", "Alternate 5-hour and weekly"),
)
ALTERNATE_TAG = {"5-hour": "5h", "weekly": "Wk"}
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


def compact(n):
    """1234 -> 1.2K, 133000 -> 133K, 1200000 -> 1.2M."""
    n = int(n or 0)
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= size:
            value = n / size
            text = f"{value:.1f}" if value < 10 else f"{value:.0f}"
            return (text[:-2] if text.endswith(".0") else text) + suffix
    return str(n)


def level(left):
    return "crit" if left <= 10 else "warn" if left <= 30 else "ok"


def duration(secs):
    secs = max(0, int(secs))
    d, h, m = secs // 86400, secs % 86400 // 3600, secs % 3600 // 60
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{max(m, 1)}m"


def clock(ts):
    return time.strftime("%-I:%M %p", time.localtime(ts))


def day_clock(ts):
    """Clock time, with the weekday when it isn't today."""
    if time.localtime(ts)[:3] == time.localtime()[:3]:
        return clock(ts)
    return time.strftime("%a ", time.localtime(ts)) + clock(ts)


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
    """What the last check found when no update is waiting, shown in Settings."""
    if not data:
        return ""
    check = data.get("update_check") or {}
    if not check.get("checked_at"):
        return ""
    if not check.get("latest"):
        return "Couldn't reach GitHub"
    return f"Up to date as of {day_clock(check['checked_at'])}"


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


def shown_lefts(p, mode):
    """The "left" values the menu bar shows for one provider. A provider without the
    chosen window (a plan with only a weekly limit) shows its tightest one instead."""
    pace = {w["label"]: 100 - w["used"] for w in p.get("windows", []) if w["label"] in PACE_WINDOWS}
    if not pace:
        return []
    if mode == "both":
        return [pace[label] for label in PACE_WINDOWS if label in pace]
    label = {"5-hour": "5-hour", "weekly": "Weekly"}.get(mode)
    return [pace.get(label, min(pace.values()))]


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

def menu_bar_window(config):
    section = config.get("mac")
    mode = section.get("menu_bar_window") if isinstance(section, dict) else None
    return mode if mode in dict(MENU_BAR_WINDOWS) else "tightest"


def render_title(providers, mode="tightest"):
    # The icon's needle always follows the tightest limit, whichever window the text shows.
    lowest = min(filter(lambda left: left is not None, map(tightest_left, providers)), default=None)
    # With nothing that has a 5-hour or weekly window, both lines would match, so show one.
    if mode == "alternate" and any(binding_left(p) is not None for p in providers):
        for window, tag in ALTERNATE_TAG.items():
            item(f"{tag}  {title_text(providers, window)}", sfimage=gauge_symbol(lowest), ansi=True)
        return
    item(title_text(providers, mode), sfimage=gauge_symbol(lowest), ansi=True)


def tightest_left(p):
    """A provider's tightest limit, or its credit balance as a percentage of the total."""
    left = binding_left(p)
    b = p.get("balance")
    if left is None and not p.get("tally") and b and b.get("remaining") is not None and b.get("total"):
        left = 100 * b["remaining"] / b["total"]
    return left


def title_text(providers, mode):
    parts = []
    for p in providers:
        lefts = shown_lefts(p, mode)
        # Each provider is colored on its own, so one low limit doesn't turn the whole title red.
        if lefts:
            worst = min(lefts)
            text = f"{PANEL_TAG.get(p['id'], p['name'][0])} {'/'.join(str(round(left)) for left in lefts)}%"
            parts.append(paint(text, level(worst)) if level(worst) != "ok" else text)
        elif p.get("tally"):
            pass  # Local AI isn't a limit, so it stays out of the title; it has its own card.
        elif p.get("balance") and p["balance"].get("remaining") is not None:
            b = p["balance"]
            text = f"${round(b['remaining'])}"
            if b.get("total"):
                pct = 100 * b["remaining"] / b["total"]
                text = paint(text, level(pct)) if level(pct) != "ok" else text
            parts.append(text)
    return "  ".join(parts) or "AI"


def gauge_symbol(left):
    """Menu-bar icon whose needle follows the tightest limit."""
    if left is None:
        step = 67
    else:
        step = 0 if left <= 10 else 33 if left <= 30 else 50 if left <= 60 else 67 if left <= 85 else 100
    return f"gauge.with.dots.needle.{step}percent"


PACE_NOTE = {"fast": "You're using it faster than it resets.", "even": "Right on pace.",
             "slow": "Plenty of room for the time left."}


def pace_text(w):
    """Short pace word for the row and a sentence for its tooltip. The fetcher decides the pace."""
    speed = w.get("pace")
    if speed not in PACE:
        return "", ""
    text, ink = PACE[speed]
    note = PACE_NOTE[speed]
    if w.get("runs_out_at"):
        text = f"▲ out {day_clock(w['runs_out_at'])}"
        note = f"At this pace it runs out around {day_clock(w['runs_out_at'])}"
        if w.get("resets_at"):
            note += f", {duration(w['resets_at'] - w['runs_out_at'])} before it resets"
        note += "."
    return paint(text, ink), note


def hint_text(h):
    """The fetcher's one quiet sentence about where heavy work should go. Rendered, never recomputed."""
    if h.get("type") == "route":
        return f"{h['to']} has room until {day_clock(h['until'])} — heavy jobs there until then"
    return f"{h['from']} frees up around {day_clock(h['until'])}"


def render_local(p, width):
    """Local AI is one line with its total; the breakdown sits in a submenu."""
    t = p["tally"]
    item(f"{DOT['local']} {p['name']}  {paint(compact(t['all_time']) + '↑', 'ok')}", size="13", ansi=True, **LIVE)
    for name, n in (("Today", t["today"]), ("This week", t["week"]), ("All time", t["all_time"])):
        item(f"--{name:<{width}}{paint(f'{compact(n):>6}', 'ok')} tokens", **MONO)
    if t.get("top_models"):
        item("--Top: " + ", ".join(f"{m['model']} {compact(m['tokens'])}" for m in t["top_models"]), **SMALL)
    item(f"--Worth about ${t['est_cost']:.2f} at API prices (estimate)", **SMALL,
         tooltip=f"At ${t['price']['input']:g} per million input and ${t['price']['output']:g} per million "
                 "output tokens. Change price_per_million under local in config.json.")
    if p.get("error"):
        item(f"--{p['error']}", sfimage="exclamationmark.triangle", **SMALL)
    if p.get("fetched_at"):
        item(f"--{freshness(p)}", **SMALL)


def render_provider(p, width):
    if p.get("tally"):
        render_local(p, width)
        return
    head = p["name"] + (f" {p['plan']}" if p.get("plan") else "")
    item(f"{DOT.get(p['id'], '⚪')} {head}", size="13", **LIVE)

    for w in p.get("windows", []):
        left = 100 - w["used"]
        ink = level(left)
        speed, note = pace_text(w)
        reset = duration(w["resets_at"] - time.time()) if w.get("resets_at") else ""
        line = f"{w['label']:<{width}}{bar(left / 100, ink)} {paint(f'{round(left):>4}%', ink)}"
        if w.get("detail"):
            line += f"  {w['detail']}"
        else:
            line += f"  {paint('↻', 'dim')} {reset:<8}"
            if speed:
                line += f"  {speed}"
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


def render_services(enabled, stored_keys, providers):
    """One place for services: yours first, each with its key and Remove, then the ones to add."""
    if not enabled:
        item("＋ Add a service…", size="13", sfimage="plus.circle")
    else:
        item("Services", sfimage="square.stack")
    for pid, name in SERVICES:
        if pid not in enabled:
            continue
        provider = providers.get(pid, {})
        error = provider.get("error") if isinstance(provider, dict) else None
        needs_key = pid in KEY_SETUP and pid not in stored_keys
        item(f"--{DOT.get(pid, '⚪')} {name}", sfimage="exclamationmark.triangle" if error or needs_key else None)
        if error:
            item(f"----{error}", **SMALL)
        if needs_key:
            item("----Add key…", **set_key_action(pid))
        else:
            if error:
                item("----Try again", **force_refresh_action())
            if pid in KEY_SETUP:
                item("----Change key…", **set_key_action(pid))
        # Only a stored key gets a confirmation dialog (keep it or delete it).
        asks = pid in KEY_SETUP and pid in stored_keys
        item(f"----Remove{'…' if asks else ''}", **remove_action(pid, pid in stored_keys))
    available = [(pid, name) for pid, name in SERVICES if pid not in enabled]
    if not available:
        return
    if enabled:
        item("-----")
        item("--Add", **SMALL)
    for pid, name in available:
        needs_key = pid in KEY_SETUP and pid not in stored_keys
        action = add_key_action(pid) if needs_key else fetcher_action("--enable-service", pid)
        item(f"--{DOT.get(pid, '⚪')} {name}{'…' if needs_key else ''}", **action)


def render_update(data):
    update = (data or {}).get("update")
    if not update:
        return
    item(f"Update to {update['latest']}…", sfimage="arrow.down.circle", tooltip=update.get("notes") or None,
         bash=PY, param1=str(FETCHER), param2="--update", terminal=True)
    if update.get("url"):
        item("--Release notes", href=update["url"])
    sep()


def render_refresh(data, enabled):
    """Refresh, with "ignoring cooldowns" as its Option-key alternate."""
    providers = [p for p in (data or {}).get("providers", [])
                 if isinstance(p, dict) and p.get("id") in enabled]
    # Stays clickable: SwiftBar won't redraw when the cooldown ends, so a disabled row
    # could outlive it. The time tells people a click before then changes nothing.
    label = "Refresh"
    ready_at = refresh_ready_at(providers, "refresh_at")
    if ready_at > time.time():
        label += f" (new data after {clock(ready_at)})"
    item(label, refresh=True, sfimage="arrow.clockwise")
    item("Refresh ignoring cooldowns", alternate=True, sfimage="arrow.clockwise", **force_refresh_action())


def render_settings(data):
    config = load_config()
    item("Settings", sfimage="gearshape")
    item("--Menu bar shows")
    current = menu_bar_window(config)
    for mode, name in MENU_BAR_WINDOWS:
        item(f"----{name}", checked=mode == current, bash=PY, param1=str(FETCHER),
             param2="--set-mac-menu-bar-window", param3=mode, terminal=False, refresh=True)
    notify_on = notifications_on(config)
    item("--Notify when a limit runs low", checked=notify_on, bash=PY, param1=str(FETCHER),
         param2="--set-notify", param3="off" if notify_on else "on", terminal=False, refresh=True)
    item("-----")
    version = (data or {}).get("version")
    status = update_status(data)
    about = " · ".join(text for text in (f"Needle {version}" if version else "Needle", status) if text)
    item(f"--{about}", **SMALL)
    item(f"--Sharpened by {SIGNATURE} 🌲", alternate=True, href=SIGNATURE_URL, **SMALL)
    if data and data.get("update"):
        item(f"--Update to {data['update']['latest']}…", bash=PY, param1=str(FETCHER), param2="--update",
             terminal=True)
    else:
        item("--Check for Updates…", bash=PY, param1=str(FETCHER), param2="--check-updates",
             terminal=False, refresh=True)
    item("-----")
    item("--Troubleshooting")
    item("----Refresh ignoring cooldowns", **force_refresh_action())
    item("----Open config file", bash="/usr/bin/open", param1="-t", param2=str(CONFIG), terminal=False)
    item("----Debug in Terminal", bash=PY, param1=str(FETCHER), param2="--text", param3="--debug",
         param4="--force", terminal=True)
    item("-----")
    item(f"--🌲 {SIGNATURE}", href=SIGNATURE_URL, **SMALL)


# ------------------------------------------------------------------ notifications

def notifications_on(config):
    return config.get("notify", True) is not False


def notify(message):
    osascript(f"display notification {json.dumps(message, ensure_ascii=False)} with title \"Needle\"")


def check_alerts(providers, config):
    """Notify once when a provider drops to 10% or less, and once more when it's back.

    SwiftBar starts a new process every run, so who has been alerted lives in a file.
    """
    try:
        alerted = set(json.loads(ALERTS.read_text()))
    except (OSError, ValueError, TypeError):
        alerted = set()
    if not notifications_on(config):
        low = set()
    else:
        lefts = {p["id"]: binding_left(p) for p in providers}
        low = {pid for pid, left in lefts.items() if left is not None and left <= 10}
        for p in providers:
            pid, left = p["id"], lefts[p["id"]]
            if pid in low and pid not in alerted:
                notify(f"{p['name']} is nearly out: {round(left)}% left")
            elif pid in alerted and pid not in low and left is not None:
                notify(f"{p['name']} is back: {round(left)}% left")
    if low != alerted:
        try:
            ALERTS.parent.mkdir(parents=True, exist_ok=True)
            ALERTS.write_text(json.dumps(sorted(low)))
        except OSError:
            pass


def check_milestone(providers, config):
    """The fetcher reports a Local AI milestone on the one refresh that crossed it."""
    m = next((p.get("milestone") for p in providers if p["id"] == "local"), None)
    if not isinstance(m, dict):
        return
    try:
        announced = int(json.loads(MILESTONE.read_text()))
    except (OSError, ValueError, TypeError):
        announced = 0
    if not m.get("value", 0) > announced:
        return
    if notifications_on(config):
        notify(f"{compact(m['value'])} tokens run locally 🎉")
    try:
        MILESTONE.parent.mkdir(parents=True, exist_ok=True)
        MILESTONE.write_text(json.dumps(m["value"]))
    except OSError:
        pass


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
    # Local AI needs no setup: it shows once the fetcher has found some local usage.
    providers = [p for p in raw_providers
                 if isinstance(p, dict) and (p.get("id") in enabled or p.get("id") == "local")
                 and provider_connected(p)]
    render_title(providers, menu_bar_window(config))
    sep()
    if data:
        check_alerts(providers, config)
        check_milestone(providers, config)

    if providers:
        if fatal:
            item(fatal, **SMALL)
        labels = [w["label"] for p in providers for w in p.get("windows", [])]
        labels += [p["balance"].get("label", "Credits") for p in providers if p.get("balance")]
        labels += ["This week" for p in providers if p.get("tally")]
        width = max(map(len, labels), default=8) + 2
        for i, p in enumerate(providers):
            if i:
                sep()
            render_provider(p, width)
        hint = (data or {}).get("hint")
        if hint:
            sep()
            item(paint(hint_text(hint), "dim"), **SMALL)
        sep()
    elif fatal:
        item(fatal, **SMALL)

    render_update(data)
    if enabled:
        render_refresh(data, enabled)
    render_services(enabled, stored_keys, provider_map)
    render_settings(data)


if __name__ == "__main__":
    main()
