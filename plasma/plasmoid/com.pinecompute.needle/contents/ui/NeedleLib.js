// Needle — shared pure helpers for the Plasma plasmoid.
// No Qt imports: everything here is plain JS so it stays testable.
// Shapes mirror the Cinnamon applet's helpers exactly.

.pragma library

var PANEL_TAG = { claude: "C", codex: "G", zai: "Z" };
var PACE_WINDOWS = ["5-hour", "Weekly"]; // windows that decide "how much can I use right now"
var SERVICES = [
    { id: "claude", name: "Claude" },
    { id: "codex", name: "ChatGPT / Codex" },
    { id: "zai", name: "z.ai", keyUrl: "https://z.ai/manage-apikey/apikey-list" },
    { id: "openrouter", name: "OpenRouter", keyUrl: "https://openrouter.ai/settings/keys" },
];

function level(left) {
    if (left <= 10) return "crit";
    if (left <= 30) return "warn";
    return "ok";
}

// 1234 -> 1.2K, 133000 -> 133K, 1200000 -> 1.2M
function compact(n) {
    n = Math.max(0, Math.floor(Number(n) || 0));
    var steps = [[1e9, "B"], [1e6, "M"], [1e3, "K"]];
    for (var i = 0; i < steps.length; i++) {
        var size = steps[i][0], suffix = steps[i][1];
        if (n >= size) {
            var v = n / size;
            return (v < 10 ? v.toFixed(1).replace(/\.0$/, "") : String(Math.round(v))) + suffix;
        }
    }
    return String(n);
}

function duration(secs) {
    secs = Math.max(0, Math.floor(secs));
    var d = Math.floor(secs / 86400);
    var h = Math.floor((secs % 86400) / 3600);
    var m = Math.floor((secs % 3600) / 60);
    if (d) return d + "d " + h + "h";
    if (h) return h + "h " + m + "m";
    return Math.max(m, 1) + "m";
}

function ago(ts, nowTs) {
    var s = nowTs - ts;
    if (s < 60) return "just now";
    return duration(s) + " ago";
}

// Clock time, with the weekday when it isn't today.
function clock(ts, nowTs) {
    var d = new Date(ts * 1000), t = new Date(nowTs * 1000);
    var h = d.getHours(), m = d.getMinutes();
    var ampm = h < 12 ? "AM" : "PM";
    var h12 = h % 12; if (h12 === 0) h12 = 12;
    var time = h12 + ":" + (m < 10 ? "0" : "") + m + " " + ampm;
    var days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    if (d.toDateString() === t.toDateString()) return time;
    return days[d.getDay()] + " " + time;
}

function money(n) {
    var v = Number(n) || 0;
    return "$" + v.toFixed(2);
}

// The fetcher's one quiet sentence about where heavy work should go. Rendered, never recomputed.
function hintText(hint, nowTs) {
    if (hint.type === "route") {
        return hint.to + " has room until " + clock(hint.until, nowTs) + " — heavy jobs there until then";
    }
    return hint.from + " frees up around " + clock(hint.until, nowTs);
}

// "Updated 3m ago · next refresh in 2m"
function freshness(p, nowTs) {
    if (!p.fetched_at) return "";
    var parts = ["Updated " + ago(p.fetched_at, nowTs)];
    if (p.refresh_at && p.refresh_at > nowTs) parts.push("next refresh in " + duration(p.refresh_at - nowTs));
    return parts.join(" · ");
}

// What the last release check found, for the Check for updates row.
function updateStatus(data, nowTs) {
    if (!data) return "";
    if (data.update) return data.update.latest + " available";
    var check = data.update_check || {};
    if (!check.checked_at) return "";
    if (!check.latest) return "Couldn't reach GitHub";
    return "Up to date · " + ago(check.checked_at, nowTs);
}

// The binding limit for a provider: lowest "left" across its 5-hour and weekly windows.
function bindingLeft(p) {
    var ws = (p.windows || []).filter(function (w) { return PACE_WINDOWS.indexOf(w.label) !== -1; });
    if (!ws.length) return null;
    return Math.min.apply(null, ws.map(function (w) { return 100 - w.used; }));
}

// The "left" values the panel shows for one provider. A provider without the chosen
// window (a plan with only a weekly limit) shows its tightest one instead.
function shownLefts(p, mode) {
    var ws = PACE_WINDOWS.map(function (label) {
        return (p.windows || []).find(function (w) { return w.label === label; });
    }).filter(Boolean);
    if (!ws.length) return [];
    var lefts = ws.map(function (w) { return 100 - w.used; });
    if (mode === "both") return lefts;
    var wanted = { "5-hour": "5-hour", weekly: "Weekly" }[mode];
    var chosen = ws.find(function (w) { return w.label === wanted; });
    return [chosen ? 100 - chosen.used : Math.min.apply(null, lefts)];
}

// One panel label: the text plus the worst level among the numbers it shows.
function panelText(providers, mode, showRemaining) {
    var parts = [];
    var worst = "ok";
    function bump(lvl) {
        if (lvl === "crit" || (lvl === "warn" && worst === "ok")) worst = lvl;
    }
    providers.forEach(function (p) {
        if (p.tally) return; // Local AI isn't a limit, so it stays out of the panel label.
        var lefts = shownLefts(p, mode);
        if (lefts.length) {
            var shown = lefts.map(function (l) { return Math.round(showRemaining ? l : 100 - l); });
            parts.push((PANEL_TAG[p.id] || (p.name || "?")[0]) + " " + shown.join("/") + "%");
            bump(level(Math.min.apply(null, lefts)));
        } else if (p.balance && p.balance.remaining !== null && p.balance.remaining !== undefined) {
            parts.push("$" + Math.round(p.balance.remaining));
            if (p.balance.total) bump(level((100 * p.balance.remaining) / p.balance.total));
        }
    });
    return { text: parts.join("   "), worst: worst };
}

function providerConnected(provider) {
    if (!provider) return false;
    if (provider.connected !== undefined) return provider.connected === true;
    return !!((provider.windows && provider.windows.length) || provider.balance);
}

function shellQuote(s) {
    return "'" + String(s).replace(/'/g, "'\\''") + "'";
}
