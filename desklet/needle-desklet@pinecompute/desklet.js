// PineNeedle — Cinnamon desklet
// Same gauges as the Needle panel applet, as an on-desktop card. All network work
// happens in the `needle` Python fetcher; this file only runs it and draws the result.

const Desklet = imports.ui.desklet;
const Settings = imports.ui.settings;
const Util = imports.misc.util;
const ByteArray = imports.byteArray;
const Tooltips = imports.ui.tooltips;
const St = imports.gi.St;
const Gio = imports.gi.Gio;
const GLib = imports.gi.GLib;
const Clutter = imports.gi.Clutter;
const Pango = imports.gi.Pango;
const Cairo = imports.cairo;

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

const THEMES = ["midnight", "forest", "paper", "plum", "retro", "tv", "gb"];
const RETRO_CELLS = { portrait: 24, landscape: 20 }; // character cells in a Retro CRT text gauge
const BAR_WIDTH = { portrait: 300, landscape: 240 };  // gauge fill width at 100%; mirrors the CSS widths
// Tube TV: the cabinet and screen frame eat into the card, so the screen is narrower.
const TV_BAR_WIDTH = { portrait: 266, landscape: 208 };
const TV_ANTENNA_HEIGHT = 36; // px of rabbit ears above the cabinet; keep the card small
// Pocket handheld: gauges are rows of LCD pixel cells (8px + 2px gap) sized to the screen.
const GB_CELLS = { portrait: 26, landscape: 18 };

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
        // Rabbit-ear antenna over the cabinet. Tube TV only.
        this._antenna = new St.DrawingArea({ height: TV_ANTENNA_HEIGHT });
        this._antenna.connect("repaint", (area) => this._drawAntenna(area));
        this._card.add(this._antenna, { x_fill: true });
        // The body holds the screen, plus the TV's knob panel. In every theme but Tube TV
        // it's an unstyled box and the controls are hidden, so the card looks as before.
        this._body = new St.BoxLayout({ vertical: true, style_class: "needle-body" });
        // The "screen" only has a look of its own in the Retro CRT and Tube TV themes; elsewhere it's a plain box.
        this._content = new St.BoxLayout({ vertical: true, style_class: "needle-screen" });
        // Pocket's side controls flank the screen in landscape, like a wide-body handheld.
        this._gbLeft = new St.BoxLayout({ vertical: true, style_class: "needle-gb-side" });
        this._body.add(this._gbLeft, { y_fill: false, y_align: St.Align.MIDDLE });
        // The frame is the handheld's dark screen bezel with its power LED; a plain box elsewhere.
        this._frame = new St.BoxLayout({ vertical: true, style_class: "needle-frame" });
        this._frameTop = new St.BoxLayout({ style_class: "needle-gb-frame-top" });
        this._gbLed = new St.Label({ text: "●", style_class: "needle-gb-led" });
        this._frameTop.add(this._gbLed);
        this._frameTop.add(new St.Label({ text: "POWER", style_class: "needle-gb-frame-text" }), { expand: true });
        this._frameTop.add(new St.Label({ text: "POCKET MATRIX", style_class: "needle-gb-frame-text" }));
        this._frame.add(this._frameTop);
        this._frame.add(this._content, { expand: true });
        this._body.add(this._frame, { expand: true });
        this._controls = new St.BoxLayout({ style_class: "needle-tv-controls" });
        this._body.add(this._controls);
        this._gbRight = new St.BoxLayout({ vertical: true, style_class: "needle-gb-side" });
        this._body.add(this._gbRight, { y_fill: false, y_align: St.Align.MIDDLE });
        this._card.add(this._body);
        // Pocket's lower half in portrait: wordmark, D-pad, A/B, Start/Select and speaker.
        this._gbDeck = new St.BoxLayout({ vertical: true, style_class: "needle-gb-deck" });
        this._card.add(this._gbDeck);
        // Monitor chin under the screen: badge + power LED. Retro CRT only.
        this._bezel = new St.BoxLayout({ style_class: "needle-bezel" });
        this._bezel.add(new St.Label({ text: SIGNATURE.toUpperCase(), style_class: "needle-bezel-badge" }), { expand: true });
        this._bezel.add(new St.Label({ text: "●", style_class: "needle-bezel-led" }));
        this._card.add(this._bezel);
        // Two stubby feet under the cabinet. Tube TV only.
        this._legs = new St.BoxLayout({ style_class: "needle-tv-legs" });
        this._legs.add(new St.Widget({ style_class: "needle-tv-leg" }));
        this._legs.add(new St.Widget(), { expand: true });
        this._legs.add(new St.Widget({ style_class: "needle-tv-leg" }));
        this._card.add(this._legs);
        this._render();
        return this._card;
    }

    _render() {
        if (!this._content) return;
        this._content.destroy_all_children();
        this._applyTheme();

        const header = new St.BoxLayout({ style_class: "needle-head" });
        const title = this._retro() ? `${SIGNATURE.toUpperCase()} v${this._version} _` : SIGNATURE;
        header.add(new St.Label({ text: title, style_class: "needle-title" }));
        // Retro CRT already prints the version in its title; elsewhere it trails it, quieter.
        const version = !this._retro() && this._version ? `v${this._version}` : "";
        header.add(new St.Label({ text: version, style_class: "needle-version needle-muted", y_align: Clutter.ActorAlign.CENTER }), { expand: true });
        const refresh = new St.Button({ label: "↻", style_class: "needle-refresh-btn", can_focus: true, track_hover: true });
        refresh.connect("clicked", () => { this._lastLiveAt = 0; this._maybeLive(); });
        // Tube TV refreshes from its channel knob, Pocket from its A button.
        if (!this._tv() && !this._gb()) header.add(refresh);
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
        // Pocket's power LED dims like a low battery when any service is nearly out.
        const low = providers.some((p) => { const left = bindingLeft(p); return left !== null && level(left) === "crit"; });
        if (low) this._gbLed.add_style_class_name("needle-gb-led-low");
        else this._gbLed.remove_style_class_name("needle-gb-led-low");

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
            // A four-shade LCD can't color the nearly-out state, so Pocket spells it out.
            const low = this._gb() && level(left) === "crit" ? " LOW" : "";
            line.add(new St.Label({
                text: `${Math.round(left)}%${low}`,
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
        if (this._gb()) return this._cellBar(frac, lvl);
        const track = new St.Widget({ style_class: "needle-track", layout_manager: new Clutter.FixedLayout() });
        track.set_x_align(Clutter.ActorAlign.START);
        const px = Math.max(0, Math.min(1, frac));
        const fill = new St.Widget({ style_class: `needle-fill ${LEVEL_FILL[lvl]}` });
        const width = (this._tv() ? TV_BAR_WIDTH : BAR_WIDTH)[this._orientationName()];
        fill.set_size(Math.round(width * px), 4);
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

    // Pocket gauge: a row of square LCD pixels, darker as the level gets worse.
    _cellBar(frac, lvl) {
        const total = GB_CELLS[this._orientationName()];
        const filled = Math.round(total * Math.max(0, Math.min(1, frac)));
        const row = new St.BoxLayout({ style_class: "needle-gb-cells" });
        for (let i = 0; i < total; i++) {
            row.add(new St.Widget({ style_class: i < filled ? `needle-gb-cell needle-gb-cell-${lvl}` : "needle-gb-cell" }));
        }
        return row;
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

    _tv() {
        return this._themeName() === "tv";
    }

    _gb() {
        return this._themeName() === "gb";
    }

    _orientationName() {
        return this.orientation === "landscape" ? "landscape" : "portrait";
    }

    _landscape() {
        return this._orientationName() === "landscape";
    }

    _applyTheme() {
        if (!this._card) return;
        // Pocket's landscape tweaks get their own class: St can't match two classes on one node.
        const shape = this._landscape() ? ` needle-landscape${this._gb() ? " needle-gb-landscape" : ""}` : "";
        this._card.set_style_class_name(`needle-desklet needle-theme-${this._themeName()}${shape}`);
        this._bezel.visible = this._retro();
        const tv = this._tv();
        this._antenna.visible = tv;
        this._controls.visible = tv;
        this._legs.visible = tv;
        // Landscape puts the knobs beside the screen; portrait puts them on a strip below it.
        this._body.vertical = !(tv && this._landscape());
        // Rebuild the knobs only when their layout changes, not on every 30s redraw.
        const knobLayout = tv ? this._orientationName() : null;
        if (knobLayout && knobLayout !== this._controlsShape) this._buildControls();
        this._controlsShape = knobLayout;

        const gb = this._gb();
        this._frameTop.visible = gb;
        this._gbLeft.visible = gb && this._landscape();
        this._gbRight.visible = gb && this._landscape();
        this._gbDeck.visible = gb && !this._landscape();
        if (gb) this._body.vertical = !this._landscape();
        const padLayout = gb ? this._orientationName() : null;
        if (padLayout && padLayout !== this._gbShape) this._buildPad();
        this._gbShape = padLayout;
    }

    // Pocket controls. A refreshes the card; the rest are just for looks.
    _buildPad() {
        for (const box of [this._gbLeft, this._gbRight, this._gbDeck]) box.destroy_all_children();
        const side = this._landscape();

        const ab = new St.BoxLayout({ style_class: "needle-gb-ab" });
        ab.add(this._gbButton("B", false));
        ab.add(this._gbButton("A", true));

        const menu = new St.BoxLayout({ vertical: side, style_class: "needle-gb-menu" });
        for (const name of ["SELECT", "START"]) {
            const key = new St.BoxLayout({ vertical: true, style_class: "needle-gb-menu-key" });
            key.add(new St.Widget({ style_class: "needle-gb-pill" }), { x_fill: false, x_align: St.Align.MIDDLE });
            key.add(new St.Label({ text: name, style_class: "needle-gb-key-label" }), { x_fill: false, x_align: St.Align.MIDDLE });
            menu.add(key);
        }

        const speaker = new St.DrawingArea({ width: side ? 40 : 52, height: side ? 26 : 34 });
        speaker.connect("repaint", (area) => this._drawSpeaker(area));

        if (side) {
            this._gbLeft.add(this._dpad(), { x_fill: false, x_align: St.Align.MIDDLE });
            this._gbLeft.add(menu, { x_fill: false, x_align: St.Align.MIDDLE });
            this._gbRight.add(ab, { x_fill: false, x_align: St.Align.MIDDLE });
            this._gbRight.add(speaker, { x_fill: false, x_align: St.Align.MIDDLE });
            return;
        }

        const brand = new St.BoxLayout({ style_class: "needle-gb-brand" });
        brand.add(new St.Label({ text: SIGNATURE, style_class: "needle-gb-brand-name" }));
        brand.add(new St.Label({ text: "POCKET", style_class: "needle-gb-brand-model", y_align: Clutter.ActorAlign.END }));
        this._gbDeck.add(brand);

        const pads = new St.BoxLayout({ style_class: "needle-gb-pads" });
        pads.add(this._dpad(), { y_fill: false, y_align: St.Align.MIDDLE });
        pads.add(new St.Widget(), { expand: true });
        pads.add(ab);
        this._gbDeck.add(pads);

        const bottom = new St.BoxLayout({ style_class: "needle-gb-bottom" });
        bottom.add(new St.Widget(), { expand: true });
        bottom.add(menu, { y_fill: false, y_align: St.Align.MIDDLE });
        bottom.add(new St.Widget(), { expand: true });
        bottom.add(speaker);
        this._gbDeck.add(bottom);
    }

    // A plus-shaped D-pad from a 3x3 grid of squares.
    _dpad() {
        const pad = new St.BoxLayout({ vertical: true, style_class: "needle-gb-dpad" });
        for (const row of [[0, 1, 0], [1, 1, 1], [0, 1, 0]]) {
            const line = new St.BoxLayout();
            for (const on of row) line.add(new St.Widget({ style_class: on ? "needle-gb-dpad-arm" : "needle-gb-dpad-gap" }));
            pad.add(line);
        }
        return pad;
    }

    _gbButton(name, clickable) {
        const key = new St.BoxLayout({ vertical: true, style_class: `needle-gb-key ${clickable ? "needle-gb-key-high" : "needle-gb-key-low"}` });
        const button = new St.Button({
            style_class: `needle-gb-button${clickable ? " needle-gb-button-live" : ""}`,
            reactive: clickable, can_focus: clickable, track_hover: clickable,
        });
        if (clickable) {
            button.connect("clicked", () => { this._lastLiveAt = 0; this._maybeLive(); });
            new Tooltips.Tooltip(button, _("Press A to refresh now"));
        }
        key.add(button, { x_fill: false, x_align: St.Align.MIDDLE });
        key.add(new St.Label({ text: name, style_class: "needle-gb-key-label" }), { x_fill: false, x_align: St.Align.MIDDLE });
        return key;
    }

    // Six slanted speaker slots in the bottom corner.
    _drawSpeaker(area) {
        const cr = area.get_context();
        const [w, h] = area.get_surface_size();
        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.setLineWidth(3);
        cr.setSourceRGBA(0.42, 0.42, 0.45, 1);
        const slant = h * 0.55;
        const step = (w - slant - 4) / 5;
        for (let i = 0; i < 6; i++) {
            const x = 2 + i * step;
            cr.moveTo(x, h - 2);
            cr.lineTo(x + slant, 2);
            cr.stroke();
        }
        cr.$dispose();
    }

    // Tube TV control panel: channel and volume knobs plus speaker slots. The channel
    // knob is the refresh button; volume is just for looks.
    _buildControls() {
        this._controls.destroy_all_children();
        const side = this._landscape();
        this._controls.vertical = side;
        this._controls.set_style_class_name(`needle-tv-controls ${side ? "needle-tv-side" : "needle-tv-strip"}`);

        const channel = this._knob(true);
        channel.connect("clicked", () => { this._lastLiveAt = 0; this._maybeLive(); });
        new Tooltips.Tooltip(channel, _("Change the channel (refresh now)"));
        const knobs = new St.BoxLayout({ vertical: side, style_class: "needle-tv-knobs" });
        knobs.add(channel);
        knobs.add(this._knob(false));

        const grille = new St.BoxLayout({ vertical: side, style_class: "needle-tv-grille" });
        for (let i = 0; i < (side ? 5 : 9); i++) grille.add(new St.Widget({ style_class: "needle-tv-slot" }));

        if (side) {
            this._controls.add(knobs, { x_fill: false, x_align: St.Align.MIDDLE });
            this._controls.add(grille, { x_fill: false, x_align: St.Align.MIDDLE });
        } else {
            this._controls.add(grille, { expand: true, y_fill: false, y_align: St.Align.MIDDLE });
            this._controls.add(knobs);
        }
    }

    _knob(clickable) {
        const knob = new St.Button({
            style_class: `needle-tv-knob${clickable ? " needle-tv-knob-live" : ""}`,
            reactive: clickable, can_focus: clickable, track_hover: clickable,
            x_align: St.Align.MIDDLE, y_align: St.Align.START, x_fill: false, y_fill: false,
        });
        // the pointer notch, so it reads as a dial and not a button
        knob.set_child(new St.Widget({ style_class: "needle-tv-notch" }));
        return knob;
    }

    // Two chrome rods in a lopsided V out of a little dome that sits on the cabinet.
    _drawAntenna(area) {
        const cr = area.get_context();
        const [w, h] = area.get_surface_size();
        const cx = w / 2;
        cr.setLineCap(Cairo.LineCap.ROUND);

        cr.setSourceRGBA(0.72, 0.75, 0.79, 1);
        cr.setLineWidth(2);
        const tips = [[cx - 46, 4], [cx + 40, 2]];
        for (const [x, y] of tips) {
            cr.moveTo(cx, h - 6);
            cr.lineTo(x, y);
            cr.stroke();
        }
        for (const [x, y] of tips) {
            cr.arc(x, y + 1, 2.5, 0, 2 * Math.PI);
            cr.fill();
        }

        cr.setSourceRGBA(0.17, 0.11, 0.07, 1); // same dark brown as the cabinet edge
        cr.save();
        cr.translate(cx, h);
        cr.scale(16, 9);
        cr.arc(0, 0, 1, Math.PI, 2 * Math.PI);
        cr.restore();
        cr.fill();
        cr.$dispose();
    }
}

function main(metadata, deskletId) {
    return new PineNeedleDesklet(metadata, deskletId);
}
