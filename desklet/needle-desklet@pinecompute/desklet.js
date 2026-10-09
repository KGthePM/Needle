// PineNeedle — Cinnamon desklet
// Same gauges as the Needle panel applet, as an on-desktop card. All network work
// happens in the `needle` Python fetcher; this file only runs it and draws the result.

const Desklet = imports.ui.desklet;
const Settings = imports.ui.settings;
const Util = imports.misc.util;
const ByteArray = imports.byteArray;
const St = imports.gi.St;
const Gio = imports.gi.Gio;
const GLib = imports.gi.GLib;
const Clutter = imports.gi.Clutter;
const Pango = imports.gi.Pango;

const UUID = "needle-desklet@pinecompute";
const HOME = GLib.get_home_dir();
const FETCHER = GLib.build_filenamev([HOME, ".local", "bin", "needle"]);
const CONFIG = GLib.getenv("NEEDLE_CONFIG") || GLib.build_filenamev([GLib.get_user_config_dir(), "needle", "config.json"]);

// Panel-convention constants, mirrored from applet.js — do not drift.
const PANEL_TAG = { claude: "C", codex: "G", zai: "Z" };
const PACE_WINDOWS = ["5-hour", "Weekly"]; // windows that decide "how much can I use right now"
const SIGNATURE = "PineNeedle";

const CACHED_POLL_SECONDS = 30;   // --cached poll cadence
const LIVE_MIN_INTERVAL = 300;    // bare live refresh at most every 5 min
const STALE_AGE = 3600;           // snapshot older than 1h dims the card

const LEVEL_FILL = { ok: "needle-fill-ok", warn: "needle-fill-warn", crit: "needle-fill-crit" };

const THEMES = ["midnight", "forest", "paper", "plum", "retro"];
const RETRO_CELLS = { portrait: 24, landscape: 20 }; // character cells in a Retro CRT text gauge
const BAR_WIDTH = { portrait: 300, landscape: 240 };  // gauge fill width at 100%; mirrors the CSS widths

const now = () => Date.now() / 1000;

function level(left) {
    if (left <= 10) return "crit";
    if (left <= 30) return "warn";
    return "ok";
}

// 1234 -> 1.2K, 133000 -> 133K, 1200000 -> 1.2M (same format as the applet)
function compact(n) {
    n = Math.max(0, Math.floor(Number(n) || 0));
    for (const [size, suffix] of [[1e9, "B"], [1e6, "M"], [1e3, "K"]]) {
        if (n >= size) {
            const v = n / size;
            return `${v < 10 ? v.toFixed(1).replace(/\.0$/, "") : Math.round(v)}${suffix}`;
        }
    }
    return String(n);
}

function duration(secs) {
    secs = Math.max(0, Math.floor(secs));
    const d = Math.floor(secs / 86400);
    const h = Math.floor((secs % 86400) / 3600);
    const m = Math.floor((secs % 3600) / 60);
    if (d) return `${d}d ${h}h`;
    if (h) return `${h}h ${m}m`;
    return `${Math.max(m, 1)}m`;
}

function ago(ts) {
    const s = now() - ts;
    if (s < 60) return "just now";
    return `${duration(s)} ago`;
}

// Clock time in the user's 12/24-hour preference, with the weekday when it isn't today.
function clock(ts) {
    let use24h = false;
    try {
        use24h = new Gio.Settings({ schema_id: "org.cinnamon.desktop.interface" }).get_boolean("clock-use-24h");
    } catch (e) { /* schema missing: keep 12-hour */ }
    const t = GLib.DateTime.new_from_unix_local(Math.round(ts));
    const today = GLib.DateTime.new_now_local();
    const sameDay = t.get_year() === today.get_year() && t.get_day_of_year() === today.get_day_of_year();
    const time = t.format(use24h ? "%H:%M" : "%l:%M %p").trim();
    return sameDay ? time : `${t.format("%a")} ${time}`;
}

// The fetcher's one quiet sentence about where heavy work should go. Rendered, never recomputed.
function hintText(hint) {
    if (hint.type === "route") {
        return `${hint.to} has room until ${clock(hint.until)} — heavy jobs there until then`;
    }
    return `${hint.from} frees up around ${clock(hint.until)}`;
}

const money = (n) => `$${Number(n).toFixed(2)}`;

// The binding limit for a provider: lowest "left" across its pace windows.
function bindingLeft(p) {
    const ws = (p.windows || []).filter((w) => PACE_WINDOWS.indexOf(w.label) !== -1);
    if (!ws.length) return null;
    return Math.min(...ws.map((w) => 100 - w.used));
}

function tagFor(p) {
    return PANEL_TAG[p.id] || p.name[0];
}

class PineNeedleDesklet extends Desklet.Desklet {
    constructor(metadata, deskletId) {
        super(metadata, deskletId);
        this._version = metadata.version || "";
        this._data = null;
        this._fatal = null;
        this._busy = false;
        this._lastLiveAt = 0;
        this._pollId = 0;
        this._tickId = 0;

        this.setHeader(_("PineNeedle"));
        this.setContent(this._buildCard());

        this.settings = new Settings.DeskletSettings(this, UUID, deskletId);
        this.settings.bind("layout", "layout", () => this._render());
        this.settings.bind("orientation", "orientation", () => this._render());
        this.settings.bind("theme", "theme", () => this._render()); // retro swaps widgets, not just colors
        this.settings.bind("show-errored", "showErrored", () => this._render());

        this._applyTheme();
        this._run(["--cached"], () => this._scheduleLive());
        this._startPolling();
    }

    on_desklet_removed() {
        if (this._pollId) GLib.source_remove(this._pollId);
        this._pollId = 0;
        if (this._tickId) GLib.source_remove(this._tickId);
        this._tickId = 0;
        if (this.settings) this.settings.finalize();
    }

    // -------------------------------------------------------------- data

    _startPolling() {
        if (this._pollId) GLib.source_remove(this._pollId);
        this._pollId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, CACHED_POLL_SECONDS, () => {
            this._run(["--cached"], () => this._maybeLive());
            return true;
        });
    }

    // Live refresh: no sooner than 5 minutes since the last one, or sooner when a
    // provider's own refresh_at says its cooldown is up. The fetcher enforces the
    // real cooldowns; this just keeps us from calling it for nothing.
    _scheduleLive() {
        this._maybeLive();
    }

    _maybeLive() {
        if (this._busy) return;
        const wait = this._liveWait();
        if (wait > 0) {
            if (!this._tickId) {
                this._tickId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, Math.min(wait, 60), () => {
                    this._tickId = 0;
                    this._maybeLive();
                    return false;
                });
            }
            return;
        }
        this._live();
    }

    _liveWait() {
        const sinceLast = now() - this._lastLiveAt;
        const providers = (this._data && this._data.providers) || [];
        const soonest = providers.length && providers.every((p) => p.refresh_at)
            ? Math.min(...providers.map((p) => p.refresh_at)) - now()
            : 0;
        return Math.max(0, Math.max(LIVE_MIN_INTERVAL - sinceLast, soonest));
    }

    _live() {
        this._busy = true;
        this._lastLiveAt = now();
        this._run([], () => { this._busy = false; this._render(); });
        // The fetcher self-throttles; a slow live call must not stall the poll loop
        // (each _run is a Gio.Subprocess with an async callback, so it can't).
    }

    _run(args, done) {
        const finish = () => { if (done) done(); };
        if (!GLib.file_test(FETCHER, GLib.FileTest.EXISTS)) {
            this._fatal = "The fetcher isn't installed. Run install.sh from the Needle download.";
            this._render();
            return finish();
        }
        let proc;
        try {
            proc = Gio.Subprocess.new(
                ["python3", FETCHER].concat(args),
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
            );
        } catch (e) {
            this._fatal = "Couldn't start python3. Is it installed?";
            this._render();
            return finish();
        }
        proc.communicate_utf8_async(null, null, (p, res) => {
            try {
                const [, stdout, stderr] = p.communicate_utf8_finish(res);
                if (p.get_successful() && stdout && stdout.trim()) {
                    this._data = JSON.parse(stdout);
                    this._fatal = null;
                } else {
                    global.logError(`${UUID}: fetcher failed: ${stderr}`);
                    if (!this._data) this._fatal = "The fetcher stopped with an error. Run `needle --debug` in a terminal to see why.";
                }
            } catch (e) {
                global.logError(`${UUID}: ${e}`);
                if (!this._data) this._fatal = "Couldn't read the fetcher's output. Run `needle --debug` to check it.";
            }
            this._render();
            finish();
        });
    }

    // -------------------------------------------------------------- drawing

    _shownProviders() {
        const all = (this._data && this._data.providers) || [];
        return all.filter((p) => {
            if (p.id === "local") return true; // needs no setup; the fetcher decides
            const broken = !p.ok || p.error;
            if (broken && !this.showErrored) return false;
            return true;
        });
    }

    _buildCard() {
        this._card = new St.BoxLayout({ vertical: true, style_class: "needle-desklet" });
        // The "screen" only has a look of its own in the Retro CRT theme; elsewhere it's a plain box.
        this._content = new St.BoxLayout({ vertical: true, style_class: "needle-screen" });
        this._card.add(this._content);
        // Monitor chin under the screen: badge + power LED. Retro CRT only.
        this._bezel = new St.BoxLayout({ style_class: "needle-bezel" });
        this._bezel.add(new St.Label({ text: SIGNATURE.toUpperCase(), style_class: "needle-bezel-badge" }), { expand: true });
        this._bezel.add(new St.Label({ text: "●", style_class: "needle-bezel-led" }));
        this._card.add(this._bezel);
        this._render();
        return this._card;
    }

    _render() {
        if (!this._content) return;
        this._content.destroy_all_children();
        this._applyTheme();

        const header = new St.BoxLayout({ style_class: "needle-head" });
        const title = this._retro() ? `${SIGNATURE.toUpperCase()} v${this._version} _` : SIGNATURE;
        header.add(new St.Label({ text: title, style_class: "needle-title" }), { expand: true });
        const refresh = new St.Button({ label: "↻", style_class: "needle-refresh-btn", can_focus: true, track_hover: true });
        refresh.connect("clicked", () => { this._lastLiveAt = 0; this._maybeLive(); });
        header.add(refresh);
        this._content.add(header);

        if (this._fatal) {
            this._content.add(this._note(this._fatal, true));
            this._content.add(this._footer(null));
            return;
        }

        // Missing config → setup hint, never a blank desklet.
        if (!GLib.file_test(CONFIG, GLib.FileTest.EXISTS)) {
            this._content.add(this._note("Needle isn't set up yet.", true));
            this._content.add(this._note("Add the Needle panel applet and connect a service, then this card fills in.", false));
            this._content.add(this._footer(null));
            return;
        }

        const providers = this._shownProviders();
        if (!providers.length) {
            this._content.add(this._note("No services connected yet. Add one from the Needle panel applet.", false));
            this._content.add(this._footer(null));
            return;
        }

        const detailed = this.layout !== "compact";
        const rows = [];
        for (const p of providers) {
            if (p.tally) rows.push(this._tallyRow(p));
            else if (p.windows && p.windows.length) rows.push(this._gaugeRow(p, detailed));
            else if (p.balance) rows.push(this._balanceRow(p));
            else if (p.error) rows.push(this._errorRow(p));
        }
        if (this._landscape()) {
            // Two columns side by side; the left one takes the odd service out.
            const columns = new St.BoxLayout({ style_class: "needle-columns" });
            const split = Math.ceil(rows.length / 2);
            for (const part of [rows.slice(0, split), rows.slice(split)]) {
                const col = new St.BoxLayout({ vertical: true, style_class: "needle-column" });
                part.forEach((r) => col.add(r));
                columns.add(col);
            }
            this._content.add(columns);
        } else {
            rows.forEach((r) => this._content.add(r));
        }
        const hint = this._data && this._data.hint;
        if (hint) this._content.add(this._note(`${hintText(hint)}.`, false));

        this._content.add(this._footer(providers));
        if (this._data && this._data.updated && now() - this._data.updated > STALE_AGE) {
            this._card.add_style_class_name("needle-stale");
        } else {
            this._card.remove_style_class_name("needle-stale");
        }
    }

    // One provider block: letter + name once, then each window labeled underneath,
    // so the 5-hour and weekly gauges read as one account, not two.
    _gaugeRow(p, detailed) {
        const box = new St.BoxLayout({ vertical: true, style_class: "needle-row" });
        if (!p.ok || p.error) box.add(this._note(p.error || "Not working — try Refresh.", true));
        const head = new St.BoxLayout({ style_class: "needle-provider-line" });
        head.add(new St.Label({ text: tagFor(p), style_class: `needle-tag needle-tag-${p.id}` }));
        head.add(new St.Label({ text: p.name + (p.plan ? ` · ${p.plan}` : ""), style_class: "needle-name" }), { expand: true });
        box.add(head);

        let windows = p.windows || [];
        if (!detailed) {
            const pace = windows.filter((w) => PACE_WINDOWS.indexOf(w.label) !== -1);
            windows = (pace.length ? pace : windows.slice()).sort((a, b) => (100 - a.used) - (100 - b.used)).slice(0, 1);
        }
        for (const w of windows) {
            const left = 100 - w.used;
            const line = new St.BoxLayout({ style_class: "needle-provider-line" });
            line.add(new St.Label({ text: w.label, style_class: "needle-window-label needle-muted" }));
            line.add(new St.Label({
                text: `${Math.round(left)}%`,
                style_class: `needle-pct needle-level-${level(left)}`,
            }), { expand: true });
            if (w.resets_at) line.add(new St.Label({ text: duration(w.resets_at - now()), style_class: "needle-reset needle-muted" }));
            box.add(line);
            box.add(this._bar(left / 100, level(left)));
        }
        return box;
    }

    _balanceRow(p) {
        const b = p.balance;
        const box = new St.BoxLayout({ vertical: true, style_class: "needle-row" });
        const line = new St.BoxLayout({ style_class: "needle-provider-line" });
        line.add(new St.Label({ text: tagFor(p), style_class: "needle-tag" }));
        if (b.remaining !== null && b.remaining !== undefined) {
            line.add(new St.Label({ text: money(b.remaining), style_class: "needle-pct" }), { expand: true });
            line.add(new St.Label({ text: `of ${money(b.total)}`, style_class: "needle-balance needle-muted" }));
            box.add(line);
            if (b.total > 0) {
                const frac = Math.max(0, Math.min(1, b.remaining / b.total));
                box.add(this._bar(frac, level(frac * 100)));
            }
        } else {
            line.add(new St.Label({ text: `${money(b.spent || 0)} spent`, style_class: "needle-pct" }), { expand: true });
            line.add(new St.Label({ text: "no limit", style_class: "needle-balance needle-muted" }));
            box.add(line);
        }
        if (p.error) box.add(this._note(p.error, true));
        return box;
    }

    // Local AI counts up instead of down: a tally row, never a gauge.
    _tallyRow(p) {
        const t = p.tally || {};
        const box = new St.BoxLayout({ vertical: true, style_class: "needle-row" });
        const line = new St.BoxLayout({ style_class: "needle-provider-line" });
        line.add(new St.Label({ text: tagFor(p), style_class: "needle-tag" }));
        line.add(new St.Label({
            text: `${compact(t.today || 0)} today · ${compact(t.week || 0)} week`,
            style_class: "needle-tally",
        }), { expand: true });
        box.add(line);
        const sub = new St.Label({
            text: `${compact(t.all_time || 0)} all time · ~${money(t.est_cost || 0)} at API prices`,
            style_class: "needle-tally needle-muted",
        });
        box.add(sub);
        return box;
    }

    _errorRow(p) {
        const box = new St.BoxLayout({ vertical: true, style_class: "needle-row" });
        const line = new St.BoxLayout({ style_class: "needle-provider-line" });
        line.add(new St.Label({ text: tagFor(p), style_class: "needle-tag" }));
        line.add(new St.Label({ text: "not set up", style_class: "needle-pct needle-muted" }), { expand: true });
        box.add(line);
        if (p.error) box.add(this._note(p.error, false));
        return box;
    }

    _footer(providers) {
        const box = new St.BoxLayout({ style_class: "needle-footer" });
        let balanceText = null;
        if (providers) {
            const total = providers.reduce((sum, p) => {
                const b = p.balance;
                return sum + (b && b.remaining !== null && b.remaining !== undefined ? b.remaining : 0);
            }, 0);
            const anyBalance = providers.some((p) => p.balance && p.balance.remaining !== null && p.balance.remaining !== undefined);
            if (anyBalance) balanceText = `${money(total)} left`;
        }
        const age = this._data && this._data.updated ? `updated ${ago(this._data.updated)}` : "waiting for first refresh…";
        const parts = [balanceText, age].filter(Boolean);
        box.add(new St.Label({ text: parts.join("  ·  "), style_class: "needle-updated needle-muted" }), { expand: true });
        return box;
    }

    _bar(frac, lvl) {
        if (this._retro()) return this._textBar(frac, lvl);
        const track = new St.Widget({ style_class: "needle-track", layout_manager: new Clutter.FixedLayout() });
        track.set_x_align(Clutter.ActorAlign.START);
        const px = Math.max(0, Math.min(1, frac));
        const fill = new St.Widget({ style_class: `needle-fill ${LEVEL_FILL[lvl]}` });
        fill.set_size(Math.round(BAR_WIDTH[this._orientationName()] * px), 4);
        fill.set_position(0, 0);
        track.add_child(fill);
        track.set_height(4);
        return track;
    }

    // Retro CRT gauge: [██████░░░░] in character cells, colored by level like the bar fill.
    _textBar(frac, lvl) {
        const total = RETRO_CELLS[this._orientationName()];
        const filled = Math.round(total * Math.max(0, Math.min(1, frac)));
        const cells = "█".repeat(filled) + "░".repeat(total - filled);
        return new St.Label({ text: `[${cells}]`, style_class: `needle-textbar needle-level-${lvl}` });
    }

    _note(text, isError) {
        const l = new St.Label({ text: text, style_class: isError ? "needle-error-note" : "needle-setup-hint needle-muted" });
        l.clutter_text.line_wrap = true;
        l.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
        return l;
    }

    _themeName() {
        return THEMES.indexOf(this.theme) !== -1 ? this.theme : "midnight";
    }

    _retro() {
        return this._themeName() === "retro";
    }

    _orientationName() {
        return this.orientation === "landscape" ? "landscape" : "portrait";
    }

    _landscape() {
        return this._orientationName() === "landscape";
    }

    _applyTheme() {
        if (!this._card) return;
        const shape = this._landscape() ? " needle-landscape" : "";
        this._card.set_style_class_name(`needle-desklet needle-theme-${this._themeName()}${shape}`);
        this._bezel.visible = this._retro();
    }
}

function main(metadata, deskletId) {
    return new PineNeedleDesklet(metadata, deskletId);
}
