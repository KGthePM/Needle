// Needle — Cinnamon panel applet
// Shows Claude, ChatGPT/Codex, z.ai and OpenRouter limits. All network work happens in the
// `needle` Python fetcher; this file only runs it and draws the result.

const Applet = imports.ui.applet;
const PopupMenu = imports.ui.popupMenu;
const ModalDialog = imports.ui.modalDialog;
const Settings = imports.ui.settings;
const Util = imports.misc.util;
const ByteArray = imports.byteArray;
const St = imports.gi.St;
const Gio = imports.gi.Gio;
const GLib = imports.gi.GLib;
const Clutter = imports.gi.Clutter;
const Pango = imports.gi.Pango;

const UUID = "needle@pinecompute";
const HOME = GLib.get_home_dir();
const FETCHER = GLib.build_filenamev([HOME, ".local", "bin", "needle"]);
const CONFIG = GLib.getenv("NEEDLE_CONFIG") || GLib.build_filenamev([GLib.get_user_config_dir(), "needle", "config.json"]);

const BAR_W = 300;          // must match .aiu-track width in stylesheet.css
const BAR_H = 6;
const STALE_ON_OPEN = 300;  // refresh on open if the snapshot is older than this (s)
const TICK_SECONDS = 15;    // how often an open popup updates its "ago" and countdown text
const PANEL_TAG = { claude: "C", codex: "G", zai: "Z" };
const PACE_WINDOWS = ["5-hour", "Weekly"]; // windows that decide "how much can I use right now"
const ALTERNATE_SECONDS = 4; // how long each window stays up when the panel alternates
const ALTERNATE_TAG = { "5-hour": "5h", weekly: "Wk" };
const PANEL_WINDOWS = [ // ids match the panel-window choices in settings-schema.json
    { id: "tightest", name: "Tightest limit" },
    { id: "5-hour", name: "5-hour" },
    { id: "weekly", name: "Weekly" },
    { id: "both", name: "Both (5-hour/weekly)" },
    { id: "alternate", name: "Alternate 5-hour and weekly" },
];
const THEME_MODES = [
    { id: "light", name: "Light" },
    { id: "system", name: "System" },
    { id: "dark", name: "Dark" },
    { id: "night", name: "Night" },
    { id: "custom", name: "Custom" },
];
const CUSTOM_COLORS = [
    ["theme-background", "themeBackground", "background"],
    ["theme-surface", "themeSurface", "surface"],
    ["theme-text", "themeText", "text"],
    ["theme-muted", "themeMuted", "muted"],
    ["theme-border", "themeBorder", "border"],
    ["theme-accent", "themeAccent", "accent"],
    ["theme-hover", "themeHover", "hover"],
    ["theme-track", "themeTrack", "track"],
    ["theme-success", "themeSuccess", "success"],
    ["theme-warning", "themeWarning", "warning"],
    ["theme-critical", "themeCritical", "critical"],
    ["theme-claude", "themeClaude", "claude"],
    ["theme-codex", "themeCodex", "codex"],
    ["theme-zai", "themeZai", "zai"],
    ["theme-openrouter", "themeOpenrouter", "openrouter"],
];
const SERVICES = [
    { id: "claude", name: "Claude" },
    { id: "codex", name: "ChatGPT / Codex" },
    { id: "zai", name: "z.ai", keyUrl: "https://z.ai/manage-apikey/apikey-list" },
    { id: "openrouter", name: "OpenRouter", keyUrl: "https://openrouter.ai/settings/keys" },
];

// ------------------------------------------------------------------ helpers

const now = () => Date.now() / 1000;

function level(left) {
    if (left <= 10) return "crit";
    if (left <= 30) return "warn";
    return "ok";
}

const LEVEL_COLOR = { warn: "#e9a93a", crit: "#ea5f5f" };

// 1234 -> 1.2K, 133000 -> 133K, 1200000 -> 1.2M
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

function ago(ts) {
    const s = now() - ts;
    if (s < 60) return "just now";
    return `${duration(s)} ago`;
}

// "Updated 3m ago · next refresh in 2m": how old a provider's numbers are, and whether
// Refresh would fetch new ones yet. Claude and Codex wait 5 minutes between real reads.
function freshness(p) {
    if (!p.fetched_at) return "";
    const parts = [`Updated ${ago(p.fetched_at)}`];
    if (p.refresh_at && p.refresh_at > now()) parts.push(`next refresh in ${duration(p.refresh_at - now())}`);
    return parts.join(" · ");
}

// What the last release check found, for the Check for updates row.
function updateStatus(data) {
    if (!data) return "";
    if (data.update) return `${data.update.latest} available`;
    const check = data.update_check || {};
    if (!check.checked_at) return "";
    if (!check.latest) return "Couldn't reach GitHub";
    return `Up to date, checked ${ago(check.checked_at)}`;
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

// The "left" values the panel shows for one provider. A provider without the chosen
// window (a plan with only a weekly limit) shows its tightest one instead.
function shownLefts(p, mode) {
    const ws = PACE_WINDOWS.map((label) => (p.windows || []).find((w) => w.label === label)).filter(Boolean);
    if (!ws.length) return [];
    const lefts = ws.map((w) => 100 - w.used);
    if (mode === "both") return lefts;
    const chosen = ws.find((w) => w.label === { "5-hour": "5-hour", weekly: "Weekly" }[mode]);
    return [chosen ? 100 - chosen.used : Math.min(...lefts)];
}

// ------------------------------------------------------------------ applet

class AIUsageApplet extends Applet.TextIconApplet {
    constructor(metadata, orientation, panelHeight, instanceId) {
        super(orientation, panelHeight, instanceId);
        this._data = null;
        this._busy = false;
        this._timerId = 0;
        this._fatal = null;
        this._view = "usage";
        this._dialog = null;
        this._instanceId = instanceId;
        this._alerted = {}; // provider id -> true while it sits at 10% or less

        this.set_applet_icon_symbolic_path(`${metadata.path}/icons/gauge-symbolic.svg`);
        this.set_applet_tooltip("Needle");
        this.set_applet_label("");

        this.settings = new Settings.AppletSettings(this, UUID, instanceId);
        this.settings.bind("refresh-minutes", "refreshMinutes", () => this._schedule());
        this.settings.bind("panel-style", "panelStyle", () => this._render());
        this.settings.bind("panel-window", "panelWindow", () => this._render());
        this.settings.bind("show-remaining", "showRemaining", () => this._render());
        this.settings.bind("theme-mode", "themeMode", () => this._themeChanged());
        CUSTOM_COLORS.forEach((setting) => {
            this.settings.bind(setting[0], setting[1], () => this._themeChanged());
        });
        this.settings.bind("notify", "notifyAlerts", () => {});
        this.settings.bind("milestone-announced", "milestoneAnnounced", () => {});
        this.settings.bind("local-collapsed", "localCollapsed", () => this._renderMenu());

        this.menuManager = new PopupMenu.PopupMenuManager(this);
        this.menu = new Applet.AppletPopupMenu(this, orientation);
        this.menuManager.addMenu(this.menu);
        this._buildMenu();
        this._buildContextMenu();

        this._tickers = [];
        this.menu.connect("open-state-changed", (menu, open) => {
            this._setTicking(open);
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
        this._setTicking(false);
        this._setAlternating(null);
        if (this._dialog) this._dialog.close();
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

    // Keeps "ago" and countdown text current while the popup stays open.
    _setTicking(on) {
        if (this._tickId) GLib.source_remove(this._tickId);
        this._tickId = 0;
        if (!on) return;
        this._tickId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, TICK_SECONDS, () => {
            this._tickers.forEach((tick) => tick());
            return true;
        });
    }

    // Seconds until Refresh would fetch anything new: the soonest provider coming off cooldown.
    _refreshWait(force) {
        const enabledIds = this._enabledServices().map((service) => service.id);
        const providers = ((this._data && this._data.providers) || []).filter((p) => enabledIds.indexOf(p.id) !== -1);
        if (!providers.length) return 0;
        const key = force ? "force_refresh_at" : "refresh_at";
        if (providers.some((p) => !p[key])) return 0;
        return Math.max(0, Math.min(...providers.map((p) => p[key])) - now());
    }

    // Asks GitHub now instead of waiting for the daily check. The fetcher refreshes usage
    // in the same run, with the normal cooldowns.
    _checkUpdates() {
        if (this._busy) return;
        this._busy = true;
        this._checking = true;
        this._setBusy(true);
        this._run(["--check-updates"], () => {
            this._busy = false;
            this._checking = false;
            this._setBusy(false);
        });
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
        this._wrap = wrap;
        this._content = new St.BoxLayout({ vertical: true, style_class: "aiu-content" });
        wrap.add(this._content);
        this.menu.box.add(wrap);
        this._applyTheme();
    }

    _buildContextMenu() {
        const add = (text, fn) => {
            const item = new PopupMenu.PopupMenuItem(text);
            item.connect("activate", fn);
            this._applet_context_menu.addMenuItem(item);
        };
        add("Open Needle", () => { this._view = "usage"; this._renderMenu(); this.menu.open(); });
    }

    _openConfig() {
        const editor = GLib.find_program_in_path("xed") ? "xed" : "xdg-open";
        Util.spawn([editor, CONFIG]);
    }

    _openDebug() {
        this._inTerminal("--text --debug --force");
    }

    _inTerminal(args) {
        const cmd = `python3 '${FETCHER}' ${args}; echo; read -p 'Press Enter to close'`;
        if (GLib.find_program_in_path("gnome-terminal")) Util.spawn(["gnome-terminal", "--", "bash", "-c", cmd]);
        else Util.spawn(["x-terminal-emulator", "-e", `bash -c "${cmd}"`]);
    }

    _openThemeSettings() {
        this.menu.close();
        Util.spawn(["xlet-settings", "applet", UUID, "-i", String(this._instanceId)]);
    }

    _loadConfig() {
        try {
            const [, contents] = GLib.file_get_contents(CONFIG);
            const parsed = JSON.parse(ByteArray.toString(contents));
            return parsed && typeof parsed === "object" ? parsed : {};
        } catch (e) {
            return {};
        }
    }

    _enabledServices() {
        const config = this._loadConfig();
        return SERVICES.filter((service) => config[service.id] && typeof config[service.id] === "object" && config[service.id].enabled !== false);
    }

    _providerConnected(provider) {
        if (!provider) return false;
        if (provider.connected !== undefined) return provider.connected === true;
        return !!((provider.windows && provider.windows.length) || provider.balance);
    }

    _configureService(args, input, done, refresh) {
        let proc;
        try {
            let flags = Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE;
            if (input !== null && input !== undefined) flags |= Gio.SubprocessFlags.STDIN_PIPE;
            proc = Gio.Subprocess.new(["python3", FETCHER].concat(args), flags);
        } catch (e) {
            this._fatal = "Couldn't update Needle's settings.";
            this._render();
            return;
        }
        proc.communicate_utf8_async(input, null, (p, res) => {
            let success = false;
            try {
                const [, , stderr] = p.communicate_utf8_finish(res);
                success = p.get_successful();
                if (!success) this._fatal = (stderr || "Couldn't update Needle's settings.").trim();
                else this._fatal = null;
            } catch (e) {
                this._fatal = "Couldn't update Needle's settings.";
            }
            if (done) done(success);
            this._render();
            if (success && refresh !== false) this._refresh(true);
        });
    }

    _showDialog(title, message, buttons, entry) {
        if (this._dialog) this._dialog.close();
        const dialog = new ModalDialog.ModalDialog();
        this._dialog = dialog;
        dialog.contentLayout.add(label(title, "aiu-title"));
        const note = this._note(message, false);
        dialog.contentLayout.add(note);
        let input = null;
        if (entry) {
            input = new St.Entry({ style_class: "aiu-key-entry", can_focus: true, hint_text: "API key" });
            input.clutter_text.set_password_char("●");
            dialog.contentLayout.add(input);
        }
        dialog.setButtons(buttons.map((button) => ({
            label: button.label,
            action: () => {
                const value = input ? input.get_text().trim() : null;
                if (button.keepOpen && button.action) {
                    button.action(value);
                    return;
                }
                dialog.close();
                this._dialog = null;
                if (button.action) button.action(value);
            },
            key: button.key,
        })));
        dialog.open();
        if (input) global.stage.set_key_focus(input.clutter_text);
    }

    _addService(service, replaceKey) {
        const config = this._loadConfig();
        const hasKey = config[service.id] && config[service.id].api_key;
        if (!service.keyUrl || (hasKey && !replaceKey)) {
            this._configureService(["--enable-service", service.id], null);
            return;
        }
        this._showDialog(
            `Add ${service.name}`,
            `Paste your ${service.name} API key. It is saved only on this computer.`,
            [
                { label: "Get a key", keepOpen: true, action: () => Util.spawn(["xdg-open", service.keyUrl]) },
                { label: "Cancel", key: Clutter.KEY_Escape },
                { label: "Add", action: (key) => {
                    if (key) this._configureService(["--set-service-key", service.id], key);
                } },
            ],
            true
        );
    }

    _removeService(service) {
        if (!service.keyUrl) {
            this._configureService(["--disable-service", service.id], null);
            return;
        }
        this._showDialog(
            `Remove ${service.name}?`,
            "You can keep the saved API key for an easier reconnect, or delete it from Needle.",
            [
                { label: "Cancel", key: Clutter.KEY_Escape },
                { label: "Keep key", action: () => this._configureService(["--disable-service", service.id], null) },
                { label: "Delete key", action: () => {
                    this._configureService(["--clear-service-key", service.id], null, () => {
                        this._configureService(["--disable-service", service.id], null);
                    }, false);
                } },
            ],
            false
        );
    }

    _setBusy(busy) {
        [[this._refreshBtn, false], [this._forceRefreshBtn, true]].forEach(([btn, force]) => {
            if (!btn) return;
            const wait = busy ? 0 : this._refreshWait(force);
            btn._valueLabel.text = busy ? "Refreshing…" : wait > 0 ? `in ${duration(wait)}` : "";
            btn.reactive = !busy && wait <= 0;
            btn._nameLabel.opacity = btn.reactive ? 255 : 150;
        });
        if (this._checkUpdatesBtn) {
            this._checkUpdatesBtn._valueLabel.text = this._checking ? "Checking…" : updateStatus(this._data);
            this._checkUpdatesBtn.reactive = !busy;
        }
    }

    // -------------------------------------------------------------- drawing

    _render() {
        this._renderPanel();
        this._renderMenu();
    }

    // Services with data to show. Local AI needs no setup, so the fetcher alone decides.
    _shownProviders() {
        const enabled = this._enabledServices().map((service) => service.id);
        return ((this._data && this._data.providers) || []).filter(
            (provider) => (provider.id === "local" || enabled.indexOf(provider.id) !== -1) && this._providerConnected(provider)
        );
    }

    // One panel label: the text plus the worst level among the numbers it shows.
    _panelText(providers, mode) {
        const parts = [];
        let worst = "ok";
        const bump = (lvl) => {
            if (lvl === "crit" || (lvl === "warn" && worst === "ok")) worst = lvl;
        };
        for (const p of providers) {
            if (p.tally) continue; // Local AI isn't a limit, so it stays out of the panel label.
            const lefts = shownLefts(p, mode);
            if (lefts.length) {
                const shown = lefts.map((left) => Math.round(this.showRemaining ? left : 100 - left));
                parts.push(`${PANEL_TAG[p.id] || p.name[0]} ${shown.join("/")}%`);
                bump(level(Math.min(...lefts)));
            } else if (p.balance && p.balance.remaining !== null && p.balance.remaining !== undefined) {
                parts.push(`$${Math.round(p.balance.remaining)}`);
                if (p.balance.total) bump(level((100 * p.balance.remaining) / p.balance.total));
            }
        }
        return { text: parts.join("   "), worst: worst };
    }

    _showPanelLabel(label) {
        this.set_applet_label(label.text);
        if (this._applet_label) {
            this._applet_label.set_style(LEVEL_COLOR[label.worst] ? `color: ${LEVEL_COLOR[label.worst]};` : null);
        }
    }

    // Swaps between the 5-hour and weekly labels every few seconds; null stops it.
    _setAlternating(labels) {
        if (this._altId) GLib.source_remove(this._altId);
        this._altId = 0;
        if (!labels) return;
        let i = 0;
        this._showPanelLabel(labels[i]);
        this._altId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, ALTERNATE_SECONDS, () => {
            i = (i + 1) % labels.length;
            this._showPanelLabel(labels[i]);
            return true;
        });
    }

    _renderPanel() {
        const providers = this._shownProviders();
        const tips = [];

        for (const p of providers) {
            if (p.tally) {
                // Local AI isn't a limit, so it stays out of the panel label; it has its own card.
                const t = p.tally;
                tips.push(`${p.name}\n  Today ${compact(t.today)}, this week ${compact(t.week)}, all time ${compact(t.all_time)}\n  Worth about ${money(t.est_cost)} at API prices (estimate)`);
            } else if (p.windows && p.windows.length) {
                const lines = p.windows.map((w) => {
                    const v = this.showRemaining ? `${Math.round(100 - w.used)}% left` : `${Math.round(w.used)}% used`;
                    const r = w.resets_at ? `, resets in ${duration(w.resets_at - now())}` : "";
                    return `  ${w.label}: ${v}${r}`;
                });
                tips.push(`${p.name}\n${lines.join("\n")}`);
            } else if (p.balance) {
                const b = p.balance;
                if (b.remaining !== null && b.remaining !== undefined) {
                    tips.push(`${p.name}\n  ${money(b.remaining)} left of ${money(b.total)}`);
                } else {
                    tips.push(`${p.name}\n  ${money(b.spent || 0)} spent, no limit`);
                }
            } else if (p.error) {
                tips.push(`${p.name}\n  ${p.error}`);
            }
        }

        const mode = this.panelWindow || "tightest";
        if (this.panelStyle === "icon") {
            this._setAlternating(null);
            this._showPanelLabel({ text: "", worst: "ok" });
        // With nothing that has a 5-hour or weekly window, both labels would match, so show one.
        } else if (mode === "alternate" && providers.some((p) => !p.tally && bindingLeft(p) !== null)) {
            this._setAlternating(Object.keys(ALTERNATE_TAG).map((window) => {
                const label = this._panelText(providers, window);
                return { text: `${ALTERNATE_TAG[window]}  ${label.text}`, worst: label.worst };
            }));
        } else {
            this._setAlternating(null);
            this._showPanelLabel(this._panelText(providers, mode));
        }
        this.set_applet_tooltip(tips.length ? tips.join("\n\n") : this._fatal || "Needle");
        this._checkAlerts(providers);
        this._checkMilestone(providers);
    }

    // The fetcher reports each Local AI milestone on the one refresh that crossed it.
    // The last value announced is kept in settings so a restart never repeats one.
    _checkMilestone(providers) {
        const local = providers.find((p) => p.id === "local");
        const m = local && local.milestone;
        if (!m || !(m.value > (this.milestoneAnnounced || 0))) return;
        this.milestoneAnnounced = m.value;
        if (!this.notifyAlerts) return;
        try {
            Util.spawn(["notify-send", "-u", "normal", "Needle", `${compact(m.value)} tokens run locally 🎉`]);
        } catch (e) {
            global.logError(`${UUID}: ${e}`);
        }
    }

    // One desktop notification per provider the first time it drops to crit, and one
    // more when it climbs back out. No spam while it sits there.
    _checkAlerts(providers) {
        if (!this.notifyAlerts) {
            this._alerted = {};
            return;
        }
        const lefts = {};
        for (const p of providers) {
            if (p.windows) lefts[p.id] = bindingLeft(p);
        }
        const send = (urgency, text) => {
            try { Util.spawn(["notify-send", "-u", urgency, "Needle", text]); } catch (e) { global.logError(`${UUID}: ${e}`); }
        };
        for (const p of providers) {
            const left = lefts[p.id];
            if (left === null || left === undefined) continue;
            if (left <= 10 && !this._alerted[p.id]) {
                this._alerted[p.id] = true;
                send("critical", `${p.name} is nearly out: ${Math.round(left)}% left`);
            } else if (left > 10 && this._alerted[p.id]) {
                delete this._alerted[p.id];
                send("normal", `${p.name} is back: ${Math.round(left)}% left`);
            }
        }
        // Services that were removed or lost their data just drop out quietly.
        for (const id of Object.keys(this._alerted)) {
            if (lefts[id] === null || lefts[id] === undefined) delete this._alerted[id];
        }
    }

    _renderMenu() {
        if (!this._content) return;
        this._content.get_children().forEach((child) => child.destroy());
        this._refreshBtn = null;
        this._forceRefreshBtn = null;
        this._checkUpdatesBtn = null;
        this._tickers = [];
        if (this._view === "add") this._renderAddView();
        else if (this._view === "settings") this._renderSettingsView();
        else if (this._view === "theme") this._renderThemeView();
        else if (this._view === "panel-window") this._renderPanelWindowView();
        else this._renderUsageView();
        this._applyTheme();
    }

    _header(title, back) {
        const header = new St.BoxLayout({ style_class: "aiu-header" });
        if (back) {
            const backButton = new St.Button({ label: "←", style_class: "aiu-icon-btn", can_focus: true, track_hover: true });
            backButton.connect("clicked", () => {
                this._view = typeof back === "string" ? back : "usage";
                this._renderMenu();
            });
            header.add(backButton, { y_fill: false, y_align: St.Align.MIDDLE });
        }
        header.add(label(title, "aiu-title"), { expand: true, y_fill: false, y_align: St.Align.MIDDLE });
        if (!back) {
            const close = new St.Button({ label: "×", style_class: "aiu-icon-btn", can_focus: true, track_hover: true });
            close.connect("clicked", () => this.menu.close());
            header.add(close, { y_fill: false, y_align: St.Align.MIDDLE });
        }
        this._content.add(header);
    }

    _navButton(text, style, action) {
        const button = new St.Button({ label: text, style_class: style, can_focus: true, track_hover: true });
        button.connect("clicked", action);
        this._content.add(button);
        return button;
    }

    _renderUsageView() {
        this._header("Needle", false);
        const cards = new St.BoxLayout({ vertical: true });
        this._content.add(cards);

        if (this._fatal) {
            cards.add(this._note(this._fatal, true));
        }
        if (this._data && this._data.update) cards.add(this._updateRow(this._data.update));
        const providers = this._shownProviders();
        const services = providers.filter((p) => p.id !== "local");
        if (!providers.length) {
            const empty = new St.BoxLayout({ vertical: true, style_class: "aiu-empty" });
            empty.add(label("No services connected yet.", "aiu-empty-title"), { x_align: St.Align.MIDDLE });
            const add = new St.Button({ label: "+  Add more", style_class: "aiu-add-large", can_focus: true, track_hover: true });
            add.connect("clicked", () => { this._view = "add"; this._renderMenu(); });
            empty.add(add, { x_align: St.Align.MIDDLE });
            cards.add(empty);
        } else {
            providers.forEach((p) => cards.add(this._card(p)));
            if (services.length < SERVICES.length) {
                this._navButton("+  Add more", "aiu-nav-btn", () => { this._view = "add"; this._renderMenu(); });
            }
        }
        this._navButton("Settings", "aiu-nav-btn aiu-settings-btn", () => { this._view = "settings"; this._renderMenu(); });
    }

    _updateRow(update) {
        const row = new St.BoxLayout({ style_class: "aiu-update" });
        row.add(label(`Needle ${update.latest} is available`, "aiu-small"), { expand: true, y_fill: false, y_align: St.Align.MIDDLE });
        const btn = new St.Button({ label: "Update", style_class: "aiu-btn", can_focus: true, track_hover: true });
        btn.connect("clicked", () => { this.menu.close(); this._inTerminal("--update"); });
        row.add(btn);
        return row;
    }

    _renderAddView() {
        this._header("Add more", true);
        const config = this._loadConfig();
        const providerList = (this._data && this._data.providers) || [];
        const providers = {};
        providerList.forEach((provider) => { providers[provider.id] = provider; });
        const available = SERVICES.filter((service) => {
            const section = config[service.id];
            const enabled = section && typeof section === "object" && section.enabled !== false;
            return !enabled || !this._providerConnected(providers[service.id]);
        });
        if (!available.length) {
            this._content.add(this._note("All supported services are connected.", false));
            return;
        }
        available.forEach((service) => {
            const section = config[service.id] && typeof config[service.id] === "object" ? config[service.id] : {};
            const enabled = section.enabled !== false && !!config[service.id];
            const hasKey = !!section.api_key;
            const provider = providers[service.id];
            let text = `+  ${service.name}`;
            let action = () => this._addService(service);
            if (enabled && service.keyUrl && !hasKey) {
                text = `Add ${service.name} key`;
                action = () => this._addService(service, true);
            } else if (enabled) {
                text = `Retry ${service.name}`;
                action = () => this._configureService(["--enable-service", service.id], null);
            }
            this._navButton(text, "aiu-service-btn", action);
            if (provider && provider.error) this._content.add(this._note(provider.error, true));
            if (enabled && service.keyUrl && hasKey && provider && provider.error) {
                this._navButton(`Change ${service.name} key`, "aiu-service-btn", () => this._addService(service, true));
            }
        });
    }

    _settingRow(name, value, action, destructive) {
        const row = new St.Button({ style_class: destructive ? "aiu-setting-row aiu-danger" : "aiu-setting-row", can_focus: true, track_hover: true });
        const box = new St.BoxLayout({ style_class: "aiu-row" });
        row._nameLabel = label(name, "aiu-label");
        box.add(row._nameLabel, { expand: true, y_fill: false, y_align: St.Align.MIDDLE });
        row._valueLabel = label(value || "", "aiu-small", true);
        box.add(row._valueLabel, { y_fill: false, y_align: St.Align.MIDDLE });
        row.set_child(box);
        row.connect("clicked", action);
        this._content.add(row);
        return row;
    }

    _renderSettingsView() {
        this._header("Settings", true);
        this._content.add(label("SERVICES", "aiu-section-label", true));
        this._enabledServices().forEach((service) => {
            if (service.keyUrl) this._settingRow(`Change ${service.name} key`, "", () => this._addService(service, true));
            this._settingRow(`Remove ${service.name}`, "", () => this._removeService(service), true);
        });

        this._content.add(label("DISPLAY", "aiu-section-label", true));
        const intervals = [0, 5, 10, 15, 30, 60];
        this._settingRow("Auto-refresh", this.refreshMinutes ? `${this.refreshMinutes} min` : "Off", () => {
            const current = intervals.indexOf(this.refreshMinutes);
            this.refreshMinutes = intervals[(current + 1) % intervals.length];
            this._renderMenu();
        });
        this._settingRow("Panel display", this.panelStyle === "icon" ? "Icon only" : "Percentages", () => {
            this.panelStyle = this.panelStyle === "icon" ? "compact" : "icon";
            this._render();
        });
        const window = PANEL_WINDOWS.find((item) => item.id === this.panelWindow) || PANEL_WINDOWS[0];
        this._settingRow("Percentages show", window.name, () => {
            this._view = "panel-window";
            this._renderMenu();
        });
        this._settingRow("Usage values", this.showRemaining ? "Remaining" : "Used", () => {
            this.showRemaining = !this.showRemaining;
            this._render();
        });
        const mode = THEME_MODES.find((item) => item.id === this.themeMode) || THEME_MODES[1];
        this._settingRow("Theme", mode.name, () => {
            this._view = "theme";
            this._renderMenu();
        });

        this._content.add(label("UTILITIES", "aiu-section-label", true));
        this._refreshBtn = this._settingRow("Refresh", "", () => this._refresh(false));
        this._forceRefreshBtn = this._settingRow("Refresh (skip cooldowns)", "", () => this._refresh(true));
        this._checkUpdatesBtn = this._settingRow("Check for updates", "", () => {
            if (this._data && this._data.update) {
                this.menu.close();
                this._inTerminal("--update");
            } else {
                this._checkUpdates();
            }
        });
        this._settingRow("Open raw configuration", "", () => { this.menu.close(); this._openConfig(); });
        this._settingRow("Debug in terminal", "", () => { this.menu.close(); this._openDebug(); });
        this._setBusy(this._busy);
        this._tickers.push(() => this._setBusy(this._busy));
    }

    _renderThemeView() {
        this._header("Theme", "settings");
        this._content.add(label("APPEARANCE", "aiu-section-label", true));
        THEME_MODES.forEach((mode) => {
            const selected = this.themeMode === mode.id;
            const row = this._settingRow(mode.name, selected ? "Selected" : "", () => {
                this.themeMode = mode.id;
                this._render();
            });
            row.add_style_class_name("aiu-theme-choice");
            if (selected) row.add_style_class_name("aiu-theme-choice-selected");
        });
        this._content.add(label("CUSTOM", "aiu-section-label", true));
        this._settingRow("Edit custom palette", "", () => this._openThemeSettings());
    }

    _renderPanelWindowView() {
        this._header("Percentages show", "settings");
        this._content.add(label("PANEL", "aiu-section-label", true));
        const current = PANEL_WINDOWS.some((item) => item.id === this.panelWindow) ? this.panelWindow : "tightest";
        PANEL_WINDOWS.forEach((choice) => {
            const selected = current === choice.id;
            const row = this._settingRow(choice.name, selected ? "Selected" : "", () => {
                this.panelWindow = choice.id;
                this._render();
            });
            row.add_style_class_name("aiu-theme-choice");
            if (selected) row.add_style_class_name("aiu-theme-choice-selected");
        });
        this._content.add(this._note("Tightest shows whichever of the 5-hour and weekly limits has less left."));
    }

    _themeChanged() {
        if (this._content) this._render();
    }

    _customPalette() {
        const palette = {};
        CUSTOM_COLORS.forEach((setting) => { palette[setting[2]] = this[setting[1]]; });
        return palette;
    }

    _customActorStyle(actor, palette, active) {
        const classes = (actor.get_style_class_name && actor.get_style_class_name() || "").split(/\s+/);
        const has = (name) => classes.indexOf(name) !== -1;
        const rules = [];
        if (has("aiu-wrap")) rules.push(`background-color: ${palette.background}`, `color: ${palette.text}`, `border-color: ${palette.border}`);
        if (has("aiu-card")) rules.push(`background-color: ${palette.surface}`, `border-color: ${palette.border}`);
        if (has("aiu-small") || has("aiu-section-label") || has("aiu-note")) rules.push(`color: ${palette.muted}`);
        if (has("aiu-nav-btn") || has("aiu-service-btn") || has("aiu-setting-row") || has("aiu-icon-btn")) {
            rules.push(`color: ${palette.text}`, `border-color: ${palette.border}`, `background-color: ${active ? palette.hover : "transparent"}`);
        }
        if (has("aiu-theme-choice-selected") || has("aiu-add-large")) rules.push(`color: ${palette.accent}`);
        if (has("aiu-add-large")) rules.push(`background-color: ${active ? palette.hover : palette.surface}`);
        if (has("aiu-key-entry")) rules.push(`background-color: ${palette.surface}`, `color: ${palette.text}`, `border-color: ${palette.border}`);
        if (has("aiu-track")) rules.push(`background-color: ${palette.track}`);
        if (has("aiu-fill-ok") || has("aiu-dot-local")) rules.push(`background-color: ${palette.success}`);
        if (has("aiu-good")) rules.push(`color: ${palette.success}`);
        if (has("aiu-fill-warn")) rules.push(`background-color: ${palette.warning}`);
        if (has("aiu-fill-crit")) rules.push(`background-color: ${palette.critical}`);
        if (has("aiu-note-error") || has("aiu-danger")) rules.push(`color: ${palette.critical}`);
        if (has("aiu-tick")) rules.push(`background-color: ${palette.muted}`);
        if (has("aiu-dot-claude")) rules.push(`background-color: ${palette.claude}`);
        if (has("aiu-dot-codex")) rules.push(`background-color: ${palette.codex}`);
        if (has("aiu-dot-zai")) rules.push(`background-color: ${palette.zai}`);
        if (has("aiu-dot-openrouter")) rules.push(`background-color: ${palette.openrouter}`);
        return rules.length ? `${rules.join("; ")};` : null;
    }

    _applyTheme() {
        if (!this._wrap) return;
        const mode = THEME_MODES.some((item) => item.id === this.themeMode) ? this.themeMode : "system";
        this._wrap.set_style_class_name(`aiu-wrap${mode === "system" ? "" : ` aiu-theme-${mode}`}`);
        this._wrap.set_style(null);
        if (mode !== "custom") return;

        const palette = this._customPalette();
        const visit = (actor) => {
            if (actor instanceof St.Widget) {
                const update = () => {
                    const active = actor instanceof St.Button && (actor.hover || (actor.has_key_focus && actor.has_key_focus()));
                    actor.set_style(this._customActorStyle(actor, palette, active));
                };
                update();
                if (actor instanceof St.Button) {
                    actor.connect("notify::hover", update);
                    actor.connect("key-focus-in", update);
                    actor.connect("key-focus-out", update);
                }
            }
            actor.get_children().forEach(visit);
        };
        visit(this._wrap);
    }

    _card(p) {
        const card = new St.BoxLayout({ vertical: true, style_class: "aiu-card" });

        const head = new St.BoxLayout({ style_class: "aiu-card-head" });
        head.add(new St.Widget({ style_class: `aiu-dot aiu-dot-${p.id}` }), { y_fill: false, y_align: St.Align.MIDDLE });
        head.add(label(p.name, "aiu-name"), { expand: true, y_fill: false, y_align: St.Align.MIDDLE });
        if (p.plan) head.add(label(p.plan, "aiu-small", true), { y_fill: false, y_align: St.Align.MIDDLE });

        // Local AI folds down to one line: its name and the all-time total.
        if (p.tally) {
            const collapsed = !!this.localCollapsed;
            if (collapsed) head.add(label(`${compact(p.tally.all_time)} ↑`, "aiu-pct aiu-good"), { y_fill: false, y_align: St.Align.MIDDLE });
            head.add(label(collapsed ? "▸" : "▾", "aiu-small", true), { y_fill: false, y_align: St.Align.MIDDLE });
            const toggle = new St.Button({ child: head, style_class: "aiu-collapse", x_fill: true, can_focus: true, track_hover: true });
            toggle.connect("clicked", () => {
                this.localCollapsed = !collapsed;
                this._renderMenu();
            });
            card.add(toggle);
            if (collapsed) return card;
            this._tallyRows(p.tally).forEach((row) => card.add(row));
        } else {
            card.add(head);
        }

        (p.windows || []).forEach((w) => card.add(this._windowRow(w)));
        if (p.balance) card.add(this._balanceRow(p.balance));

        if (p.error) card.add(this._note(p.error, !p.stale));
        if (p.fetched_at) {
            const fresh = this._note(freshness(p), false);
            card.add(fresh);
            this._tickers.push(() => { fresh.text = freshness(p); });
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
        if (w.runs_out_at) {
            let text = `At this pace it runs out around ${clock(w.runs_out_at)}`;
            if (w.resets_at) text += `, ${duration(w.resets_at - w.runs_out_at)} before it resets`;
            box.add(this._note(`${text}.`, false));
        }
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

    // Local AI counts up: totals, top models and a price estimate, never a gauge.
    _tallyRows(t) {
        const rows = [];
        const line = (name, value) => {
            const row = new St.BoxLayout({ style_class: "aiu-row" });
            row.add(label(name, "aiu-label"), { expand: true, y_fill: false, y_align: St.Align.MIDDLE });
            row.add(label(value, "aiu-pct aiu-good"), { y_fill: false, y_align: St.Align.MIDDLE });
            return row;
        };
        const box = new St.BoxLayout({ vertical: true, style_class: "aiu-window" });
        box.add(line("Today", `${compact(t.today)} tokens`));
        box.add(line("This week", `${compact(t.week)} tokens`));
        box.add(line("All time", `${compact(t.all_time)} tokens ↑`));
        rows.push(box);
        if (t.top_models && t.top_models.length) {
            rows.push(this._note(`Top: ${t.top_models.map((m) => `${m.model} ${compact(m.tokens)}`).join(", ")}`, false));
        }
        rows.push(this._note(`Worth about ${money(t.est_cost)} at API prices (estimate)`, false));
        return rows;
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
