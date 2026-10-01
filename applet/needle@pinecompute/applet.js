// Needle — Cinnamon panel applet
// Shows Claude, Codex, z.ai and OpenRouter limits. All network work happens in the
// `needle` Python fetcher; this file only runs it and draws the result.

const Applet = imports.ui.applet;
const PopupMenu = imports.ui.popupMenu;
const Settings = imports.ui.settings;
const Util = imports.misc.util;
const St = imports.gi.St;
const Gio = imports.gi.Gio;
const GLib = imports.gi.GLib;
const Clutter = imports.gi.Clutter;
const Pango = imports.gi.Pango;

const UUID = "needle@pinecompute";
const HOME = GLib.get_home_dir();
const FETCHER = GLib.build_filenamev([HOME, ".local", "bin", "needle"]);
const CONFIG = GLib.build_filenamev([GLib.get_user_config_dir(), "needle", "config.json"]);

const BAR_W = 300;          // must match .aiu-track width in stylesheet.css
const BAR_H = 6;
const STALE_ON_OPEN = 300;  // refresh on open if the snapshot is older than this (s)
const PANEL_TAG = { claude: "C", codex: "X", zai: "Z" };
const PACE_WINDOWS = ["5-hour", "Weekly"]; // windows that decide "how much can I use right now"

// ------------------------------------------------------------------ helpers

const now = () => Date.now() / 1000;

function level(left) {
    if (left <= 10) return "crit";
    if (left <= 30) return "warn";
    return "ok";
}

const LEVEL_COLOR = { warn: "#e9a93a", crit: "#ea5f5f" };

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

const money = (n) => `$${Number(n).toFixed(2)}`;

function uiScale() {
    try {
        return St.ThemeContext.get_for_stage(global.stage).scale_factor || 1;
    } catch (e) {
        return 1;
    }
}

function label(text, style, dim) {
    const l = new St.Label({ text: text, style_class: style || "" });
    if (dim) l.opacity = 150;
    return l;
}

// The binding limit for a provider: lowest "left" across its 5-hour and weekly windows.
function bindingLeft(p) {
    const ws = (p.windows || []).filter((w) => PACE_WINDOWS.indexOf(w.label) !== -1);
    if (!ws.length) return null;
    return Math.min(...ws.map((w) => 100 - w.used));
}

// ------------------------------------------------------------------ applet

class AIUsageApplet extends Applet.TextIconApplet {
    constructor(metadata, orientation, panelHeight, instanceId) {
        super(orientation, panelHeight, instanceId);
        this._data = null;
        this._busy = false;
        this._timerId = 0;
        this._fatal = null;

        this.set_applet_icon_symbolic_path(`${metadata.path}/icons/gauge-symbolic.svg`);
        this.set_applet_tooltip("Needle");
        this.set_applet_label("");

        this.settings = new Settings.AppletSettings(this, UUID, instanceId);
        this.settings.bind("refresh-minutes", "refreshMinutes", () => this._schedule());
        this.settings.bind("panel-style", "panelStyle", () => this._render());
        this.settings.bind("show-remaining", "showRemaining", () => this._render());

        this.menuManager = new PopupMenu.PopupMenuManager(this);
        this.menu = new Applet.AppletPopupMenu(this, orientation);
        this.menuManager.addMenu(this.menu);
        this._buildMenu();
        this._buildContextMenu();

        this.menu.connect("open-state-changed", (menu, open) => {
            if (!open) return;
            this._render(); // fresh countdowns
            if (!this._data || now() - (this._data.updated || 0) > STALE_ON_OPEN) this._refresh(false);
        });

        // Paint the last snapshot instantly, then fetch.
        this._run(["--cached"], () => this._refresh(false));
        this._schedule();
    }

    on_applet_clicked() {
        this.menu.toggle();
    }

    on_applet_removed_from_panel() {
        if (this._timerId) GLib.source_remove(this._timerId);
        this._timerId = 0;
        this.settings.finalize();
    }

    // -------------------------------------------------------------- data

    _schedule() {
        if (this._timerId) GLib.source_remove(this._timerId);
        this._timerId = 0;
        const mins = this.refreshMinutes || 0;
        if (mins > 0) {
            this._timerId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, mins * 60, () => {
                this._refresh(false);
                return true;
            });
        }
    }

    _refresh(force) {
        if (this._busy) return;
        this._busy = true;
        this._setBusy(true);
        this._run(force ? ["--force"] : [], () => {
            this._busy = false;
            this._setBusy(false);
        });
    }

    _run(args, done) {
        const finish = () => { if (done) done(); };
        if (!GLib.file_test(FETCHER, GLib.FileTest.EXISTS)) {
            this._fatal = "The fetcher isn't installed. Run install.sh from the download folder.";
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
                    this._fatal = "The fetcher stopped with an error. Run `needle --debug` in a terminal to see why.";
                }
            } catch (e) {
                global.logError(`${UUID}: ${e}`);
                this._fatal = "Couldn't read the fetcher's output. Run `needle --debug` to check it.";
            }
            this._render();
            finish();
        });
    }

    // -------------------------------------------------------------- menus

    _buildMenu() {
        const wrap = new St.BoxLayout({ vertical: true, style_class: "aiu-wrap" });
        this._content = new St.BoxLayout({ vertical: true, style_class: "aiu-content" });
        wrap.add(this._content);

        const header = new St.BoxLayout({ style_class: "aiu-header" });
        header.add(label("Needle", "aiu-title"), { expand: true, y_fill: false, y_align: St.Align.MIDDLE });
        this._updated = label("", "aiu-small", true);
        header.add(this._updated, { y_fill: false, y_align: St.Align.MIDDLE });
        this._content.add(header);

        this._cards = new St.BoxLayout({ vertical: true });
        this._content.add(this._cards);

        const footer = new St.BoxLayout({ style_class: "aiu-footer" });
        const keys = new St.Button({ label: "Edit keys", style_class: "aiu-btn", can_focus: true, track_hover: true });
        keys.connect("clicked", () => { this.menu.close(); this._openConfig(); });
        footer.add(keys);
        footer.add(new St.Widget(), { expand: true });
        this._refreshBtn = new St.Button({ label: "Refresh", style_class: "aiu-btn", can_focus: true, track_hover: true });
        this._refreshBtn.connect("clicked", () => this._refresh(false));
        footer.add(this._refreshBtn);
        this._content.add(footer);

        this.menu.box.add(wrap);
    }

    _buildContextMenu() {
        const add = (text, fn) => {
            const item = new PopupMenu.PopupMenuItem(text);
            item.connect("activate", fn);
            this._applet_context_menu.addMenuItem(item);
        };
        add("Refresh now (skip cooldowns)", () => this._refresh(true));
        add("Edit API keys", () => this._openConfig());
        add("Open in terminal (debug)", () => {
            const cmd = `python3 '${FETCHER}' --text --debug --force; echo; read -p 'Press Enter to close'`;
            if (GLib.find_program_in_path("gnome-terminal")) Util.spawn(["gnome-terminal", "--", "bash", "-c", cmd]);
            else Util.spawn(["x-terminal-emulator", "-e", `bash -c "${cmd}"`]);
        });
        this._applet_context_menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
    }

    _openConfig() {
        const editor = GLib.find_program_in_path("xed") ? "xed" : "xdg-open";
        Util.spawn([editor, CONFIG]);
    }

    _setBusy(busy) {
        if (!this._refreshBtn) return;
        this._refreshBtn.label = busy ? "Refreshing…" : "Refresh";
        this._refreshBtn.reactive = !busy;
    }

    // -------------------------------------------------------------- drawing

    _render() {
        this._renderPanel();
        this._renderMenu();
    }

    _renderPanel() {
        const providers = (this._data && this._data.providers) || [];
        const parts = [];
        const tips = [];
        let worst = "ok";
        const bump = (lvl) => {
            if (lvl === "crit" || (lvl === "warn" && worst === "ok")) worst = lvl;
        };

        for (const p of providers) {
            if (p.windows && p.windows.length) {
                const left = bindingLeft(p);
                if (left !== null) {
                    const shown = this.showRemaining ? left : 100 - left;
                    parts.push(`${PANEL_TAG[p.id] || p.name[0]} ${Math.round(shown)}%`);
                    bump(level(left));
                }
                const lines = p.windows.map((w) => {
                    const v = this.showRemaining ? `${Math.round(100 - w.used)}% left` : `${Math.round(w.used)}% used`;
                    const r = w.resets_at ? `, resets in ${duration(w.resets_at - now())}` : "";
                    return `  ${w.label}: ${v}${r}`;
                });
                tips.push(`${p.name}\n${lines.join("\n")}`);
            } else if (p.balance) {
                const b = p.balance;
                if (b.remaining !== null && b.remaining !== undefined) {
                    parts.push(`$${Math.round(b.remaining)}`);
                    tips.push(`${p.name}\n  ${money(b.remaining)} left of ${money(b.total)}`);
                    if (b.total) bump(level((100 * b.remaining) / b.total));
                } else {
                    tips.push(`${p.name}\n  ${money(b.spent || 0)} spent, no limit`);
                }
            } else if (p.error) {
                tips.push(`${p.name}\n  ${p.error}`);
            }
        }

        this.set_applet_label(this.panelStyle === "icon" ? "" : parts.join("   "));
        if (this._applet_label) this._applet_label.set_style(LEVEL_COLOR[worst] ? `color: ${LEVEL_COLOR[worst]};` : null);
        this.set_applet_tooltip(tips.length ? tips.join("\n\n") : this._fatal || "Needle");
    }

    _renderMenu() {
        if (!this._cards) return;
        this._cards.get_children().forEach((c) => c.destroy());
        this._updated.text = this._data && this._data.updated ? `Updated ${ago(this._data.updated)}` : "";

        if (this._fatal) {
            this._cards.add(this._note(this._fatal, true));
            return;
        }
        const providers = (this._data && this._data.providers) || [];
        if (!providers.length) {
            this._cards.add(this._note("Nothing to show yet. Add your keys with Edit keys, then press Refresh.", false));
            return;
        }
        providers.forEach((p) => this._cards.add(this._card(p)));
    }

    _card(p) {
        const card = new St.BoxLayout({ vertical: true, style_class: "aiu-card" });

        const head = new St.BoxLayout({ style_class: "aiu-card-head" });
        head.add(new St.Widget({ style_class: `aiu-dot aiu-dot-${p.id}` }), { y_fill: false, y_align: St.Align.MIDDLE });
        head.add(label(p.name, "aiu-name"), { expand: true, y_fill: false, y_align: St.Align.MIDDLE });
        if (p.plan) head.add(label(p.plan, "aiu-small", true), { y_fill: false, y_align: St.Align.MIDDLE });
        card.add(head);

        (p.windows || []).forEach((w) => card.add(this._windowRow(w)));
        if (p.balance) card.add(this._balanceRow(p.balance));

        if (p.error) {
            const when = p.stale && p.fetched_at ? ` Last good read ${ago(p.fetched_at)}.` : "";
            card.add(this._note(p.error + when, !p.stale));
        }
        return card;
    }

    _windowRow(w) {
        const left = 100 - w.used;
        const shown = this.showRemaining ? left : w.used;
        const box = new St.BoxLayout({ vertical: true, style_class: "aiu-window" });

        const row = new St.BoxLayout({ style_class: "aiu-row" });
        row.add(label(w.label, "aiu-label"), { expand: true, y_fill: false, y_align: St.Align.MIDDLE });
        const sub = w.detail || (w.resets_at ? `resets in ${duration(w.resets_at - now())}` : "");
        if (sub) row.add(label(sub, "aiu-small", true), { y_fill: false, y_align: St.Align.MIDDLE });
        row.add(label(`${Math.round(shown)}%${this.showRemaining ? " left" : " used"}`, "aiu-pct"), { y_fill: false, y_align: St.Align.MIDDLE });
        box.add(row);

        // Tick = share of the window's time still to go. Only for windows with a known length.
        let tick = null;
        if (w.window_seconds && w.resets_at) {
            const timeLeft = Math.max(0, Math.min(1, (w.resets_at - now()) / w.window_seconds));
            tick = this.showRemaining ? timeLeft : 1 - timeLeft;
        }
        box.add(this._bar(shown / 100, level(left), tick), { x_fill: false, x_align: St.Align.START });
        return box;
    }

    _balanceRow(b) {
        const box = new St.BoxLayout({ vertical: true, style_class: "aiu-window" });
        const row = new St.BoxLayout({ style_class: "aiu-row" });
        row.add(label(b.label || "Credits", "aiu-label"), { expand: true, y_fill: false, y_align: St.Align.MIDDLE });

        if (b.remaining === null || b.remaining === undefined) {
            row.add(label("no limit", "aiu-small", true), { y_fill: false, y_align: St.Align.MIDDLE });
            row.add(label(`${money(b.spent || 0)} spent`, "aiu-pct"), { y_fill: false, y_align: St.Align.MIDDLE });
            box.add(row);
            return box;
        }
        row.add(label(`of ${money(b.total)}`, "aiu-small", true), { y_fill: false, y_align: St.Align.MIDDLE });
        row.add(label(`${money(b.remaining)} left`, "aiu-pct"), { y_fill: false, y_align: St.Align.MIDDLE });
        box.add(row);
        if (b.total > 0) {
            const frac = Math.max(0, Math.min(1, b.remaining / b.total));
            box.add(this._bar(frac, level(frac * 100), null), { x_fill: false, x_align: St.Align.START });
        }
        return box;
    }

    _bar(frac, lvl, tick) {
        const s = uiScale();
        const track = new St.Widget({ style_class: "aiu-track", layout_manager: new Clutter.FixedLayout() });
        const px = Math.round(BAR_W * Math.max(0, Math.min(1, frac)));
        if (px > 0) {
            const fill = new St.Widget({ style_class: `aiu-fill aiu-fill-${lvl}` });
            fill.set_size(Math.max(px, BAR_H) * s, BAR_H * s);
            fill.set_position(0, 0);
            track.add_child(fill);
        }
        if (tick !== null && tick !== undefined) {
            const t = new St.Widget({ style_class: "aiu-tick" });
            const x = Math.round(BAR_W * tick);
            t.set_size(2 * s, (BAR_H + 6) * s);
            t.set_position(Math.min(Math.max(x - 1, 0), BAR_W - 2) * s, -3 * s);
            track.add_child(t);
        }
        return track;
    }

    _note(text, isError) {
        const l = label(text, isError ? "aiu-note aiu-note-error" : "aiu-note", !isError);
        l.clutter_text.line_wrap = true;
        l.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
        l.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
        return l;
    }
}

function main(metadata, orientation, panelHeight, instanceId) {
    return new AIUsageApplet(metadata, orientation, panelHeight, instanceId);
}
