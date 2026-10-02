#!/usr/bin/python3
# <xbar.title>Needle</xbar.title>
# <xbar.version>v1.2</xbar.version>
# <xbar.author>KGthePM</xbar.author>
# <xbar.desc>Claude, Codex, z.ai and OpenRouter limits at a glance.</xbar.desc>
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
CONFIG = Path.home() / ".config" / "needle" / "config.json"
PY = sys.executable or "/usr/bin/python3"
PLUGIN = Path(__file__).resolve()
# Providers whose key can be pasted from the menu: (name, where to get one, dialog hint).
KEY_SETUP = {
    "zai": ("z.ai", "https://z.ai/manage-apikey/apikey-list", "Paste your z.ai Coding Plan API key."),
    "openrouter": ("OpenRouter", "https://openrouter.ai/settings/keys",
                   "Paste an OpenRouter key. A management key shows your whole balance; "
                   "a regular key shows that key's own limit."),
}
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
PANEL_TAG = {"claude": "C", "codex": "X", "zai": "Z"}
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


def ago(ts):
    s = time.time() - ts
    return "just now" if s < 60 else f"{duration(s)} ago"


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
        when = f" Last good read {ago(p['fetched_at'])}." if p.get("stale") and p.get("fetched_at") else ""
        fix = set_key_action(p["id"]) if p["id"] in KEY_SETUP else {}
        item(p["error"] + when, sfimage="exclamationmark.triangle", **SMALL, **fix)


def set_key_action(pid):
    """Menu params that open the key dialog for one provider, then refresh."""
    return {"bash": PY, "param1": str(PLUGIN), "param2": "--set-key", "param3": pid,
            "terminal": False, "refresh": True}


def render_unset(providers):
    """One line per provider that just needs a key, instead of a section each."""
    for p in providers:
        dot = DOT.get(p["id"], "⚪")
        item(f"{dot} Add {p['name']} key…", tooltip="Opens a box to paste your key", **set_key_action(p["id"]))


def render_actions(data):
    sep()
    if data and data.get("updated"):
        item(f"Updated {ago(data['updated'])}", **SMALL)
    update = (data or {}).get("update")
    if update:
        item(f"Update to {update['latest']}…", sfimage="arrow.down.circle", tooltip=update.get("notes") or None,
             bash=PY, param1=str(FETCHER), param2="--update", terminal=True)
        if update.get("url"):
            item("--Release notes", href=update["url"])
    item("Refresh", refresh=True, sfimage="arrow.clockwise")
    item("Edit keys", sfimage="key")
    for pid, (name, _, _) in KEY_SETUP.items():
        item(f"--Set {name} key…", **set_key_action(pid))
    item("--Open keys file", bash="/usr/bin/open", param1="-t", param2=str(CONFIG), terminal=False)
    item("More", sfimage="ellipsis.circle")
    item("--Refresh now (skip cooldowns)", bash=PY, param1=str(FETCHER), param2="--force",
         terminal=False, refresh=True)
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


def set_key(pid):
    if pid not in KEY_SETUP:
        return
    key = ask_key(pid)
    if not key:
        return
    try:
        cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    except ValueError:
        alert("The keys file has a formatting error, so the key wasn't saved. It will open now so you can fix it.")
        subprocess.run(["/usr/bin/open", "-t", str(CONFIG)])
        return
    section = cfg.setdefault(pid, {})
    section["api_key"] = key
    section["enabled"] = True
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG)
    # Fetch now so the menu shows the new numbers (or the key's error) as soon as it refreshes.
    subprocess.run([PY, str(FETCHER), "--force"], capture_output=True, timeout=90)


def main():
    if sys.argv[1:2] == ["--set-key"] and len(sys.argv) > 2:
        set_key(sys.argv[2])
        return
    data, fatal = load()
    providers = (data or {}).get("providers", [])
    render_title(providers)
    sep()

    if fatal:
        item(fatal, **SMALL)
    elif not providers:
        item("Nothing to show yet. Add your keys with Edit keys, then Refresh.", **SMALL)
    else:
        shown = [p for p in providers if not p.get("needs_key")]
        unset = [p for p in providers if p.get("needs_key")]
        labels = [w["label"] for p in shown for w in p.get("windows", [])]
        labels += [p["balance"].get("label", "Credits") for p in shown if p.get("balance")]
        width = max(map(len, labels), default=8) + 2
        for i, p in enumerate(shown):
            if i:
                sep()
            render_provider(p, width)
        if unset:
            if shown:
                sep()
            render_unset(unset)

    render_actions(data)


if __name__ == "__main__":
    main()
