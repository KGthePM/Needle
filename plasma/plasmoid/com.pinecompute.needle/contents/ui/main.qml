// Needle — Plasma 6 panel widget.
// Shows Claude, ChatGPT/Codex, z.ai and OpenRouter limits. All network work happens in
// the `needle` Python fetcher; this file only runs it and draws the result.
//
// The Plasma5Support "executable" dataengine is the supported way for a QML plasmoid
// to run a command in Plasma 6 (kept available by plasma-workspace for this purpose).

import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.plasma.plasmoid
import org.kde.kirigami as Kirigami
import org.kde.plasma.plasma5support as Plasma5Support

import "NeedleLib.js" as NL

PlasmoidItem {
    id: root

    Plasmoid.toolTipMainText: "Needle"
    Plasmoid.toolTipSubText: root.tooltipText()

    // ------------------------------------------------------------ state

    property var jsonData: null
    property var configCache: ({})
    property bool busy: false
    property string fatal: ""
    property string view: "usage"          // usage | services | service | settings | picker | troubleshooting
    property string serviceId: ""
    property var picker: null
    property var alerted: ({})
    property bool checking: false
    property bool keyEntryVisible: false
    property string keyEntryService: ""
    property string homeDir: ""
    property real altPhase: 0

    readonly property string fetcher: homeDir + "/.local/bin/needle"
    readonly property string configPath: homeDir + "/.config/needle/config.json"
    readonly property string signature: "PineNeedle"
    readonly property string signatureUrl: "https://pinecomputenj.com/needle"

    function nowTs() { return Date.now() / 1000 }

    // ------------------------------------------------------------ exec plumbing

    Plasma5Support.DataSource {
        id: executable
        engine: "executable"
        connectedSources: []
        onNewData: (sourceName, data) => {
            var exitCode = data["exit code"]
            var stdout = data["stdout"] || ""
            var stderr = data["stderr"] || ""
            disconnectSource(sourceName)
            var cb = callbacks[sourceName]
            delete callbacks[sourceName]
            if (cb) cb(exitCode, stdout, stderr)
        }
        property var callbacks: ({})
        function run(cmd, callback) {
            if (callbacks[cmd] !== undefined) return  // one in flight per command string
            callbacks[cmd] = callback || function () {}
            connectSource(cmd)
        }
    }

    Component.onCompleted: {
        executable.run("printf %s \"$HOME\"", function (code, out) {
            homeDir = out.trim()
            // Paint the last snapshot instantly, then fetch — same as the Cinnamon applet.
            runFetcher(["--cached"], function (c, o, e) { applyData(c, o, e); loadConfig(); refresh(false) })
        })
    }

    // ------------------------------------------------------------ fetch cycle

    function applyData(exitCode, stdout, stderr) {
        var trimmed = (stdout || "").trim()
        if (trimmed) {
            try {
                jsonData = JSON.parse(trimmed)
                fatal = ""
            } catch (e) {
                fatal = "Couldn't read the fetcher's output. Run `needle --debug` to check it."
            }
        } else if (exitCode !== 0) {
            fatal = (stderr || "").trim() || "The fetcher stopped with an error. Run `needle --debug` in a terminal to see why."
        }
        checkAlerts()
        checkMilestone()
    }

    function refresh(force) {
        if (busy) return
        busy = true
        runFetcher(force ? ["--force"] : [], function (c, o, e) {
            busy = false
            applyData(c, o, e)
        })
    }

    function checkUpdates() {
        if (busy) return
        busy = true
        checking = true
        runFetcher(["--check-updates"], function (c, o, e) {
            busy = false
            checking = false
            applyData(c, o, e)
        })
    }

    function runFetcher(args, callback) {
        var cmd = "python3 " + NL.shellQuote(fetcher) + (args.length ? " " + args.join(" ") : "")
        executable.run(cmd, callback)
    }

    function loadConfig() {
        executable.run("cat " + NL.shellQuote(configPath) + " 2>/dev/null || echo {}", function (c, out) {
            try { configCache = JSON.parse(out) } catch (e) { configCache = {} }
        })
    }

    function configureService(args, done) {
        runFetcher(args, function (code, out, err) {
            if (code !== 0) fatal = (err || "").trim() || "Couldn't update Needle's settings."
            else fatal = ""
            loadConfig()
            if (done) done(code === 0)
            refresh(true)
        })
    }

    // Key arrives on the fetcher's stdin, same as the Cinnamon applet: printf the key in.
    function setServiceKey(serviceId, key) {
        var cmd = "printf %s " + NL.shellQuote(key) + " | python3 " + NL.shellQuote(fetcher)
                  + " --set-service-key " + NL.shellQuote(serviceId)
        executable.run(cmd, function (code, out, err) {
            if (code !== 0) fatal = (err || "").trim() || "Couldn't save the key."
            keyEntryVisible = false
            loadConfig()
            refresh(true)
        })
    }

    // ------------------------------------------------------------ scheduling

    Timer {
        id: refreshTimer
        interval: Math.max(1, plasmoid.configuration.refreshMinutes || 10) * 60 * 1000
        running: true
        repeat: true
        onTriggered: root.refresh(false)
    }

    // While the popup is open: keep "ago"/countdown text and the panel alternation current.
    Timer {
        id: tickTimer
        interval: root.view === "usage" || plasmoid.configuration.panelWindow === "alternate" ? 4000 : 15000
        running: root.expanded || plasmoid.configuration.panelWindow === "alternate"
        repeat: true
        onTriggered: root.altPhase = root.altPhase + 1
    }

    // ------------------------------------------------------------ derived data

    function enabledServices() {
        return NL.SERVICES.filter(function (s) {
            var section = configCache[s.id]
            return section && typeof section === "object" && section.enabled !== false
        })
    }

    // Services with data to show. Local AI needs no setup, so the fetcher alone decides.
    function shownProviders() {
        var enabled = enabledServices().map(function (s) { return s.id })
        return ((jsonData && jsonData.providers) || []).filter(function (p) {
            return (p.id === "local" || enabled.indexOf(p.id) !== -1) && NL.providerConnected(p)
        })
    }

    function providerMap() {
        var map = {}
        ;((jsonData && jsonData.providers) || []).forEach(function (p) { map[p.id] = p })
        return map
    }

    function serviceStatus(service) {
        var section = configCache[service.id]
        if (service.keyUrl && !(section && section.api_key)) return "Needs key"
        var provider = providerMap()[service.id]
        if (provider && provider.error) return "Not working"
        return NL.providerConnected(provider) ? "Connected" : "Connecting…"
    }

    // Seconds until Refresh would fetch anything new: the soonest provider off cooldown.
    function refreshWait(force) {
        var enabledIds = enabledServices().map(function (s) { return s.id })
        var providers = ((jsonData && jsonData.providers) || []).filter(function (p) { return enabledIds.indexOf(p.id) !== -1 })
        if (!providers.length) return 0
        var key = force ? "force_refresh_at" : "refresh_at"
        if (providers.some(function (p) { return !p[key] })) return 0
        return Math.max(0, Math.min.apply(null, providers.map(function (p) { return p[key] })) - nowTs())
    }

    // ------------------------------------------------------------ panel label

    function panelLabel() {
        if (homeDir === "") return ""
        var providers = shownProviders()
        if (plasmoid.configuration.panelStyle === "icon") return ""
        var mode = plasmoid.configuration.panelWindow || "tightest"
        var showRemaining = plasmoid.configuration.showRemaining !== false
        var text
        if (mode === "alternate" && providers.some(function (p) { return !p.tally && NL.bindingLeft(p) !== null })) {
            var tags = ["5h", "Wk"], windows = ["5-hour", "weekly"]
            var i = Math.floor(root.altPhase) % 2
            var label = NL.panelText(providers, windows[i], showRemaining)
            text = tags[i] + "  " + label.text
            panelWorst = label.worst
        } else {
            var l = NL.panelText(providers, mode, showRemaining)
            text = l.text
            panelWorst = l.worst
        }
        return text === "" ? "Needle" : text
    }

    property string panelWorst: "ok"

    readonly property color levelWarn: "#e9a93a"
    readonly property color levelCrit: "#ea5f5f"

    function panelColor() {
        return panelWorst === "crit" ? levelCrit : panelWorst === "warn" ? levelWarn : Kirigami.Theme.textColor
    }

    function tooltipText() {
        if (fatal) return fatal
        var providers = shownProviders()
        if (!providers.length) return "No services connected yet."
        var tips = []
        providers.forEach(function (p) {
            if (p.tally) {
                var t = p.tally
                tips.push(p.name + "\n  Today " + NL.compact(t.today) + ", this week " + NL.compact(t.week)
                          + ", all time " + NL.compact(t.all_time)
                          + "\n  Worth about " + NL.money(t.est_cost) + " at API prices (estimate)")
            } else if (p.windows && p.windows.length) {
                var lines = p.windows.map(function (w) {
                    var v = (plasmoid.configuration.showRemaining !== false)
                        ? Math.round(100 - w.used) + "% left" : Math.round(w.used) + "% used"
                    var r = w.resets_at ? ", resets in " + NL.duration(w.resets_at - nowTs()) : ""
                    return "  " + w.label + ": " + v + r
                })
                tips.push(p.name + "\n" + lines.join("\n"))
            } else if (p.balance) {
                var b = p.balance
                if (b.remaining !== null && b.remaining !== undefined)
                    tips.push(p.name + "\n  " + NL.money(b.remaining) + " left of " + NL.money(b.total))
                else tips.push(p.name + "\n  " + NL.money(b.spent || 0) + " spent, no limit")
            } else if (p.error) {
                tips.push(p.name + "\n  " + p.error)
            }
        })
        return tips.length ? tips.join("\n\n") : "Needle"
    }

    // ------------------------------------------------------------ alerts

    function notify(urgency, text) {
        executable.run("notify-send -u " + urgency + " Needle " + NL.shellQuote(text))
    }

    // One notification per provider the first time it drops to crit, one more when it
    // climbs back out. No spam while it sits there.
    function checkAlerts() {
        if (!plasmoid.configuration.notify) { alerted = {}; return }
        var providers = shownProviders()
        var lefts = {}
        providers.forEach(function (p) { if (p.windows) lefts[p.id] = NL.bindingLeft(p) })
        providers.forEach(function (p) {
            var left = lefts[p.id]
            if (left === null || left === undefined) return
            if (left <= 10 && !alerted[p.id]) {
                alerted[p.id] = true
                notify("critical", p.name + " is nearly out: " + Math.round(left) + "% left")
            } else if (left > 10 && alerted[p.id]) {
                delete alerted[p.id]
                notify("normal", p.name + " is back: " + Math.round(left) + "% left")
            }
        })
        Object.keys(alerted).forEach(function (id) {
            if (lefts[id] === null || lefts[id] === undefined) delete alerted[id]
        })
    }

    function checkMilestone() {
        var local = shownProviders().find(function (p) { return p.id === "local" })
        var m = local && local.milestone
        if (!m || !(m.value > (plasmoid.configuration.milestoneAnnounced || 0))) return
        plasmoid.configuration.milestoneAnnounced = m.value
        if (plasmoid.configuration.notify)
            notify("normal", NL.compact(m.value) + " tokens run locally 🎉")
    }

    // ------------------------------------------------------------ actions that leave the popup

    function inTerminal(args) {
        var inner = "python3 " + NL.shellQuote(fetcher) + " " + args + "; echo; read -p 'Press Enter to close'"
        var quoted = NL.shellQuote(inner)
        var cmd = "if command -v konsole >/dev/null 2>&1; then konsole -e bash -c " + quoted
                + "; elif command -v xdg-terminal-exec >/dev/null 2>&1; then xdg-terminal-exec bash -c " + quoted
                + "; else x-terminal-emulator -e bash -c " + quoted + "; fi"
        executable.run(cmd)
    }

    function openUrl(url) {
        executable.run("xdg-open " + NL.shellQuote(url))
    }

    function openConfig() {
        executable.run("xdg-open " + NL.shellQuote(configPath))
    }

    // ------------------------------------------------------------ theme

    // Same palettes as the Cinnamon stylesheet; "system" follows the Plasma theme.
    function themeColors() {
        var mode = plasmoid.configuration.themeMode || "system"
        if (mode === "light") return { bg: "#f4f6f8", fg: "#20242b", muted: "#667085", border: "#cdd3dc", surface: "#ffffff", track: "#d8dde5" }
        if (mode === "dark")  return { bg: "#171a20", fg: "#f1f3f6", muted: "#a5adba", border: "#3b414c", surface: "#22262e", track: "#454c58" }
        if (mode === "night") return { bg: "#060a12", fg: "#e8f0ff", muted: "#8fa2bf", border: "#24324a", surface: "#0c1422", track: "#263650" }
        return { bg: Kirigami.Theme.backgroundColor, fg: Kirigami.Theme.textColor,
                 muted: Kirigami.Theme.disabledTextColor, border: Kirigami.Theme.alternateBackgroundColor,
                 surface: Kirigami.Theme.alternateBackgroundColor, track: Qt.rgba(0.5, 0.5, 0.5, 0.25) }
    }

    function providerColor(id) {
        var dark = (plasmoid.configuration.themeMode || "system") !== "light"
        if (id === "claude") return "#d97757"
        if (id === "codex") return "#10a37f"
        if (id === "zai") return dark ? "#5b83ff" : "#4169d8"
        if (id === "openrouter") return dark ? "#9b8cff" : "#725fc7"
        return "#34c58a"
    }

    function fillColor(lvl) {
        var mode = plasmoid.configuration.themeMode || "system"
        if (lvl === "warn") return mode === "light" ? "#a15c00" : mode === "night" ? "#f0b44d" : "#e9a93a"
        if (lvl === "crit") return mode === "light" ? "#c9364b" : mode === "night" ? "#ff667a" : "#f06b72"
        return "#34c58a"
    }

    // ------------------------------------------------------------ UI building blocks

    Component {
        id: noteLabel
        QQC2.Label {
            property bool isError: false
            property string noteText: ""
            text: noteText
            color: isError ? (themeColors().fg === "#20242b" ? "#c9364b" : "#f06b72") : themeColors().muted
            font.pointSize: Kirigami.Theme.smallFont.pointSize
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
        }
    }

    function barRow(frac, lvl, tickFrac, colors) {
        return { frac: frac, lvl: lvl, tick: tickFrac, colors: colors }
    }

    // ------------------------------------------------------------ representations

    compactRepresentation: Component {
        Item {
            id: compact
            MouseArea {
                anchors.fill: parent
                onClicked: {
                    root.view = "usage"
                    root.expanded = !root.expanded
                }
            }
            Text {
                anchors.centerIn: parent
                text: root.panelLabel()
                color: root.panelColor()
                font.pointSize: Math.max(Kirigami.Theme.defaultFont.pointSize - 1, 7)
            }
        }
    }

    fullRepresentation: Component {
        Item {
            id: full
            implicitWidth: 340
            implicitHeight: Math.min(720, col.implicitHeight + 16)
            clip: true

            property var tc: root.themeColors()

            Rectangle {
                anchors.fill: parent
                color: full.tc.bg
                border.color: full.tc.border
                border.width: 1
                radius: 8
            }

            ColumnLayout {
                id: col
                anchors.fill: parent
                anchors.margins: 10
                spacing: 0

                // ---- header (non-usage views get a back button)
                RowLayout {
                    Layout.fillWidth: true
                    Layout.bottomMargin: 6
                    spacing: 6
                    QQC2.ToolButton {
                        visible: root.view !== "usage"
                        text: "←"
                        onClicked: root.view = root.view === "picker" && root.picker && root.picker.back ? root.picker.back
                                 : root.view === "troubleshooting" ? "settings"
                                 : root.view === "service" ? "services" : "usage"
                    }
                    QQC2.Label {
                        text: root.view === "usage" ? "Needle"
                            : root.view === "services" ? "Services"
                            : root.view === "settings" ? "Settings"
                            : root.view === "troubleshooting" ? "Troubleshooting"
                            : root.view === "picker" ? (root.picker ? root.picker.title : "")
                            : (NL.SERVICES.find(function (s) { return s.id === root.serviceId }) || {}).name || ""
                        font.bold: true
                        Layout.fillWidth: true
                    }
                    QQC2.ToolButton {
                        visible: root.view === "usage" && root.enabledServices().length > 0
                        text: root.busy ? "…" : "↻"
                        enabled: !root.busy && root.refreshWait(false) <= 0
                        opacity: enabled ? 1 : 0.45
                        onClicked: root.refresh(false)
                        QQC2.ToolTip.text: root.busy ? "Refreshing…" : "Refresh"
                        QQC2.ToolTip.visible: hovered
                    }
                    QQC2.ToolButton {
                        text: "×"
                        onClicked: root.expanded = false
                    }
                }

                Loader { Layout.fillWidth: true; sourceComponent: {
                    if (root.view === "usage") return usageView
                    if (root.view === "services") return servicesView
                    if (root.view === "service") return serviceView
                    if (root.view === "settings") return settingsView
                    if (root.view === "picker") return pickerView
                    return troubleView
                } }

                // ================= usage =================
                Component {
                    id: usageView
                    ColumnLayout {
                        spacing: 0
                        Layout.fillWidth: true

                        QQC2.Label {
                            visible: root.fatal !== ""
                            text: root.fatal
                            color: "#f06b72"
                            wrapMode: Text.WordWrap
                            Layout.fillWidth: true
                        }

                        // update row
                        RowLayout {
                            visible: root.jsonData && root.jsonData.update
                            Layout.fillWidth: true
                            QQC2.Label {
                                text: root.jsonData && root.jsonData.update
                                       ? "Needle " + root.jsonData.update.latest + " is available" : ""
                                color: full.tc.muted
                                font.pointSize: Kirigami.Theme.smallFont.pointSize
                                Layout.fillWidth: true
                            }
                            QQC2.Button {
                                text: "Update"
                                onClicked: { root.expanded = false; root.inTerminal("--update") }
                            }
                        }

                        // empty state
                        ColumnLayout {
                            visible: root.shownProviders().length === 0
                            spacing: 12
                            Layout.topMargin: 24
                            Layout.bottomMargin: 28
                            QQC2.Label {
                                text: "No services connected yet."
                                Layout.alignment: Qt.AlignHCenter
                            }
                            QQC2.Button {
                                text: "+  Add a service"
                                Layout.alignment: Qt.AlignHCenter
                                onClicked: root.view = "services"
                            }
                        }

                        // service cards
                        Repeater {
                            model: root.shownProviders().filter(function (p) { return !p.tally })
                            delegate: windowCard
                        }
                        Repeater {
                            model: root.shownProviders().filter(function (p) { return p.tally })
                            delegate: tallyCard
                        }

                        QQC2.Label {
                            visible: root.jsonData && root.jsonData.hint
                            text: root.jsonData && root.jsonData.hint ? NL.hintText(root.jsonData.hint, root.nowTs()) + "." : ""
                            color: full.tc.muted
                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                            wrapMode: Text.WordWrap
                            Layout.fillWidth: true
                            Layout.topMargin: 8
                        }

                        settingsRow { labelText: "Services"; onOpen: root.view = "services" }
                        settingsRow { labelText: "Settings"; onOpen: root.view = "settings"; topMargin: 2 }
                    }
                }

                // ================= gauge card =================
                Component {
                    id: windowCard
                    ColumnLayout {
                        required property var modelData
                        readonly property var p: modelData
                        spacing: 5
                        Layout.fillWidth: true
                        Layout.topMargin: 12

                        RowLayout {
                            spacing: 8
                            Rectangle { width: 8; height: 8; radius: 4; color: root.providerColor(p.id) }
                            QQC2.Label { text: p.name; font.bold: true; Layout.fillWidth: true }
                            QQC2.Label { text: p.plan || ""; color: full.tc.muted; visible: !!p.plan
                                         font.pointSize: Kirigami.Theme.smallFont.pointSize }
                        }

                        Repeater {
                            model: p.windows || []
                            delegate: ColumnLayout {
                                required property var modelData
                                readonly property var w: modelData
                                spacing: 4
                                Layout.fillWidth: true
                                readonly property real leftV: 100 - w.used
                                readonly property real shownV: plasmoid.configuration.showRemaining !== false ? leftV : w.used
                                RowLayout {
                                    Layout.fillWidth: true
                                    QQC2.Label { text: w.label }
                                    QQC2.Label {
                                        text: w.detail || (w.resets_at ? "resets in " + NL.duration(w.resets_at - root.nowTs()) : "")
                                        color: full.tc.muted; visible: text !== ""
                                        font.pointSize: Kirigami.Theme.smallFont.pointSize
                                    }
                                    Item { Layout.fillWidth: true }
                                    QQC2.Label {
                                        text: Math.round(shownV) + "%" + (plasmoid.configuration.showRemaining !== false ? " left" : " used")
                                        font.bold: true
                                    }
                                }
                                // gauge: fill = what's left (or used); tick = time left in window
                                Item {
                                    Layout.fillWidth: true
                                    height: 12
                                    Rectangle { // track
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: parent.width; height: 6; radius: 3; color: full.tc.track
                                    }
                                    Rectangle { // fill
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: Math.max(0, Math.min(1, shownV / 100)) * parent.width
                                        height: 6; radius: 3
                                        color: root.fillColor(NL.level(leftV))
                                    }
                                    Rectangle { // tick
                                        visible: w.window_seconds && w.resets_at
                                        anchors.verticalCenter: parent.verticalCenter
                                        x: Math.max(0, Math.min(1, (w.resets_at - root.nowTs()) / w.window_seconds)) * (parent.width - 2)
                                        width: 2; height: 12; radius: 1; color: full.tc.muted
                                    }
                                }
                                QQC2.Label {
                                    visible: !!w.runs_out_at
                                    text: w.runs_out_at
                                          ? "At this pace it runs out around " + NL.clock(w.runs_out_at, root.nowTs())
                                            + (w.resets_at ? ", " + NL.duration(w.resets_at - w.runs_out_at) + " before it resets" : "") + "." : ""
                                    color: full.tc.muted
                                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                                    wrapMode: Text.WordWrap
                                    Layout.fillWidth: true
                                }
                            }
                        }

                        // balance (OpenRouter)
                        ColumnLayout {
                            visible: !!p.balance
                            spacing: 4
                            Layout.fillWidth: true
                            RowLayout {
                                Layout.fillWidth: true
                                QQC2.Label { text: (p.balance && p.balance.label) || "Credits" }
                                QQC2.Label {
                                    text: p.balance && (p.balance.remaining === null || p.balance.remaining === undefined)
                                          ? "no limit" : "of " + NL.money(p.balance ? p.balance.total : 0)
                                    color: full.tc.muted; font.pointSize: Kirigami.Theme.smallFont.pointSize
                                }
                                Item { Layout.fillWidth: true }
                                QQC2.Label {
                                    font.bold: true
                                    text: !p.balance ? "" : (p.balance.remaining === null || p.balance.remaining === undefined)
                                          ? NL.money(p.balance.spent || 0) + " spent"
                                          : NL.money(p.balance.remaining) + " left"
                                }
                            }
                            Rectangle {
                                visible: p.balance && p.balance.total > 0
                                Layout.fillWidth: true
                                height: 6; radius: 3; color: full.tc.track
                                Rectangle {
                                    width: p.balance ? Math.max(0, Math.min(1, p.balance.remaining / p.balance.total)) * parent.width : 0
                                    height: 6; radius: 3
                                    color: root.fillColor(NL.level(p.balance ? 100 * p.balance.remaining / p.balance.total : 100))
                                }
                            }
                        }

                        QQC2.Label {
                            visible: !!p.error
                            text: p.error || ""
                            color: p.stale ? full.tc.muted : "#f06b72"
                            wrapMode: Text.WordWrap; Layout.fillWidth: true
                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                        }
                        QQC2.Label {
                            text: NL.freshness(p, root.nowTs())
                            color: full.tc.muted; visible: text !== ""
                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                        }
                    }
                }

                // ================= local AI card =================
                Component {
                    id: tallyCard
                    ColumnLayout {
                        required property var modelData
                        readonly property var p: modelData
                        spacing: 4
                        Layout.fillWidth: true
                        Layout.topMargin: 12
                        RowLayout {
                            spacing: 8
                            Rectangle { width: 8; height: 8; radius: 4; color: "#34c58a" }
                            QQC2.Label { text: p.name; font.bold: true; Layout.fillWidth: true }
                            QQC2.Label { text: NL.compact(p.tally.all_time) + " ↑"; color: "#34c58a"; font.bold: true }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            QQC2.Label { text: "Today" }
                            Item { Layout.fillWidth: true }
                            QQC2.Label { text: NL.compact(p.tally.today) + " tokens"; color: "#34c58a" }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            QQC2.Label { text: "This week" }
                            Item { Layout.fillWidth: true }
                            QQC2.Label { text: NL.compact(p.tally.week) + " tokens"; color: "#34c58a" }
                        }
                        QQC2.Label {
                            visible: p.tally.top_models && p.tally.top_models.length
                            text: "Top: " + (p.tally.top_models || []).map(function (m) { return m.model + " " + NL.compact(m.tokens) }).join(", ")
                            color: full.tc.muted; wrapMode: Text.WordWrap; Layout.fillWidth: true
                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                        }
                        QQC2.Label {
                            text: "Worth about " + NL.money(p.tally.est_cost) + " at API prices (estimate)"
                            color: full.tc.muted
                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                        }
                        QQC2.Label {
                            text: NL.freshness(p, root.nowTs())
                            color: full.tc.muted; visible: text !== ""
                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                        }
                    }
                }

                // ================= services =================
                Component {
                    id: servicesView
                    ColumnLayout {
                        spacing: 0
                        Layout.fillWidth: true
                        sectionLabel { labelText: "YOURS" }
                        Repeater {
                            model: root.enabledServices()
                            delegate: serviceRow
                        }
                        sectionLabel { labelText: "ADD" }
                        Repeater {
                            model: NL.SERVICES.filter(function (s) { return root.enabledServices().indexOf(s) === -1 })
                            delegate: serviceRow
                        }
                    }
                }

                Component {
                    id: serviceRow
                    RowLayout {
                        required property var modelData
                        readonly property var s: modelData
                        spacing: 8
                        Layout.fillWidth: true
                        Layout.topMargin: 8
                        Rectangle { width: 8; height: 8; radius: 4; color: root.providerColor(s.id) }
                        QQC2.Label { text: s.name; Layout.fillWidth: true; MouseArea { anchors.fill: parent; onClicked: { root.serviceId = s.id; root.view = "service" } } }
                        QQC2.Label { text: root.enabledServices().indexOf(s) === -1 ? "+"
                                       : root.serviceStatus(s) + "  ›"
                                     color: full.tc.muted
                                     font.pointSize: Kirigami.Theme.smallFont.pointSize }
                        MouseArea {
                            anchors.fill: parent
                            onClicked: {
                                if (root.enabledServices().indexOf(s) === -1) root.addService(s)
                                else { root.serviceId = s.id; root.view = "service" }
                            }
                        }
                    }
                }

                // ================= single service =================
                Component {
                    id: serviceView
                    ColumnLayout {
                        spacing: 0
                        Layout.fillWidth: true
                        readonly property var s: NL.SERVICES.find(function (x) { return x.id === root.serviceId })
                        onSChanged: { if (!s) root.view = "services" }
                        settingsRow { labelText: "Status"; valueText: s ? root.serviceStatus(s) : ""; enabledRow: false }
                        QQC2.Label {
                            visible: !!(root.providerMap()[root.serviceId] || {}).error
                            text: (root.providerMap()[root.serviceId] || {}).error || ""
                            color: "#f06b72"; wrapMode: Text.WordWrap; Layout.fillWidth: true
                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                        }
                        settingsRow {
                            labelText: root.serviceStatus(s) === "Needs key" ? "Add key…" : "Change key…"
                            visibleRow: !!s && !!s.keyUrl
                            onOpen: if (s) root.keyEntryVisible = !root.keyEntryVisible
                        }
                        settingsRow {
                            labelText: "Try again"
                            visibleRow: root.serviceStatus(s) === "Not working"
                            onOpen: root.refresh(true)
                        }
                        settingsRow { labelText: "Remove…"; onOpen: root.removeService(s) }
                        ColumnLayout {
                            visible: root.keyEntryVisible && !!s && !!s.keyUrl
                            spacing: 6
                            Layout.fillWidth: true
                            Layout.topMargin: 8
                            QQC2.Label {
                                text: "Paste your " + (s ? s.name : "") + " API key. It is saved only on this computer."
                                color: full.tc.muted; wrapMode: Text.WordWrap; Layout.fillWidth: true
                                font.pointSize: Kirigami.Theme.smallFont.pointSize
                            }
                            QQC2.TextField {
                                id: keyField
                                echoMode: QQC2.TextInput.Password
                                placeholderText: "API key"
                                Layout.fillWidth: true
                            }
                            RowLayout {
                                QQC2.Button {
                                    text: "Get a key"
                                    onClicked: if (s) root.openUrl(s.keyUrl)
                                }
                                Item { Layout.fillWidth: true }
                                QQC2.Button {
                                    text: "Save"
                                    enabled: keyField.text.trim().length > 0
                                    onClicked: root.setServiceKey(root.serviceId, keyField.text.trim())
                                }
                            }
                        }
                    }
                }

                // ================= settings =================
                Component {
                    id: settingsView
                    ColumnLayout {
                        spacing: 0
                        Layout.fillWidth: true
                        sectionLabel { labelText: "PANEL" }
                        pickerRow { title: "Panel shows"; pickerId: "panel" }
                        pickerRow { title: "Numbers show"; pickerId: "numbers" }
                        pickerRow { title: "Theme"; pickerId: "theme" }
                        sectionLabel { labelText: "UPDATES & ALERTS" }
                        pickerRow { title: "Auto-refresh"; pickerId: "refresh" }
                        settingsRow {
                            labelText: "Notify when a limit runs low"
                            valueText: plasmoid.configuration.notify ? "On" : "Off"
                            onOpen: { plasmoid.configuration.notify = !plasmoid.configuration.notify; root.checkAlerts() }
                        }
                        sectionLabel { labelText: "ABOUT" }
                        settingsRow {
                            // The version shows even before the first fetch returns; the widget's
                            // own metadata is the fallback.
                            labelText: root.jsonData && root.jsonData.version ? "Needle " + root.jsonData.version
                                       : (plasmoid.metaData && plasmoid.metaData.version ? "Needle " + plasmoid.metaData.version : "Needle")
                            valueText: root.checking ? "Checking…"
                                       : root.jsonData && root.jsonData.update ? "Update to " + root.jsonData.update.latest
                                       : NL.updateStatus(root.jsonData, root.nowTs())
                            onOpen: {
                                if (root.jsonData && root.jsonData.update) { root.expanded = false; root.inTerminal("--update") }
                                else root.checkUpdates()
                            }
                        }
                        settingsRow { labelText: "Troubleshooting"; valueText: "›"; onOpen: root.view = "troubleshooting" }
                        QQC2.Label {
                            text: root.signature
                            color: full.tc.muted
                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                            Layout.alignment: Qt.AlignHCenter
                            Layout.topMargin: 12
                            MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: root.openUrl(root.signatureUrl) }
                        }
                    }
                }

                // ================= generic picker view =================
                Component {
                    id: pickerView
                    ColumnLayout {
                        spacing: 0
                        Layout.fillWidth: true
                        Repeater {
                            model: root.picker ? root.picker.choices : []
                            delegate: RowLayout {
                                required property var modelData
                                readonly property var c: modelData
                                Layout.fillWidth: true
                                Layout.topMargin: 8
                                QQC2.Label { text: c.name; Layout.fillWidth: true
                                    MouseArea { anchors.fill: parent; onClicked: { root.picker.choose(c.id); root.view = "settings" } } }
                                QQC2.Label { text: root.picker && root.picker.current() === c.id ? "✓" : ""; color: full.tc.muted }
                                MouseArea {
                                    anchors.fill: parent
                                    onClicked: { root.picker.choose(c.id); root.view = "settings" }
                                }
                            }
                        }
                    }
                }

                // ================= troubleshooting =================
                Component {
                    id: troubleView
                    ColumnLayout {
                        spacing: 0
                        Layout.fillWidth: true
                        settingsRow {
                            labelText: "Refresh ignoring cooldowns"
                            valueText: root.busy ? "Refreshing…" : root.refreshWait(true) > 0 ? "in " + NL.duration(root.refreshWait(true)) : ""
                            enabledRow: !root.busy && root.refreshWait(true) <= 0
                            onOpen: root.refresh(true)
                        }
                        settingsRow { labelText: "Open config file"; onOpen: { root.expanded = false; root.openConfig() } }
                        settingsRow { labelText: "Debug in terminal"; onOpen: { root.expanded = false; root.inTerminal("--text --debug --force") } }
                    }
                }
            }
        }
    }

    // ------------------------------------------------------------ reusable rows

    component settingsRow: RowLayout {
        property string labelText: ""
        property string valueText: ""
        property bool enabledRow: true
        property bool visibleRow: true
        signal open()
        visible: visibleRow
        enabled: enabledRow
        spacing: 8
        Layout.fillWidth: true
        Layout.topMargin: 8
        QQC2.Label { text: labelText; color: enabledRow ? full.tc.fg : full.tc.muted; Layout.fillWidth: true }
        QQC2.Label { text: valueText; color: full.tc.muted; visible: text !== ""
                     font.pointSize: Kirigami.Theme.smallFont.pointSize }
        MouseArea { anchors.fill: parent; enabled: enabledRow; onClicked: open() }
    }

    component sectionLabel: QQC2.Label {
        property string labelText: ""
        text: labelText
        color: full.tc.muted
        font.pointSize: Kirigami.Theme.smallFont.pointSize
        font.bold: true
        Layout.topMargin: 12
        Layout.bottomMargin: 2
    }

    component pickerRow: RowLayout {
        property string title: ""
        property string pickerId: ""
        Layout.fillWidth: true
        Layout.topMargin: 8
        QQC2.Label { text: title; Layout.fillWidth: true }
        QQC2.Label {
            text: pickerChoices(pickerId).current().name + "  ›"
            color: full.tc.muted
            font.pointSize: Kirigami.Theme.smallFont.pointSize
        }
        MouseArea {
            anchors.fill: parent
            onClicked: { root.picker = pickerChoices(pickerId); root.picker.back = "settings"; root.view = "picker" }
        }
    }

    function pickerChoices(id) {
        if (id === "panel") return {
            title: "Panel shows",
            choices: [
                { id: "tightest", name: "Tightest limit" },
                { id: "5-hour", name: "5-hour" },
                { id: "weekly", name: "Weekly" },
                { id: "both", name: "Both (5-hour/weekly)" },
                { id: "alternate", name: "Alternate 5-hour and weekly" },
                { id: "icon", name: "Icon only" },
            ],
            current: function () {
                if (plasmoid.configuration.panelStyle === "icon") return { id: "icon", name: "Icon only" }
                var id2 = plasmoid.configuration.panelWindow || "tightest"
                return this.choices.find(function (c) { return c.id === id2 }) || this.choices[0]
            },
            choose: function (c) {
                if (c === "icon") plasmoid.configuration.panelStyle = "icon"
                else { plasmoid.configuration.panelStyle = "compact"; plasmoid.configuration.panelWindow = c }
            },
        }
        if (id === "numbers") return {
            title: "Numbers show",
            choices: [{ id: "left", name: "What's left" }, { id: "used", name: "What's used" }],
            current: function () { return plasmoid.configuration.showRemaining !== false ? this.choices[0] : this.choices[1] },
            choose: function (c) { plasmoid.configuration.showRemaining = c === "left" },
        }
        if (id === "theme") return {
            title: "Theme",
            choices: [
                { id: "system", name: "System" },
                { id: "light", name: "Light" },
                { id: "dark", name: "Dark" },
                { id: "night", name: "Night" },
            ],
            current: function () {
                var m = plasmoid.configuration.themeMode || "system"
                return this.choices.find(function (c) { return c.id === m }) || this.choices[0]
            },
            choose: function (c) { plasmoid.configuration.themeMode = c },
        }
        // refresh
        return {
            title: "Auto-refresh",
            choices: [0, 5, 10, 15, 30, 60].map(function (n) { return { id: n, name: n ? "Every " + n + " min" : "Off" } }),
            current: function () {
                var n = plasmoid.configuration.refreshMinutes || 10
                return this.choices.find(function (c) { return c.id === n }) || this.choices[2]
            },
            choose: function (c) { plasmoid.configuration.refreshMinutes = c },
        }
    }

    // ------------------------------------------------------------ service actions

    function addService(service) {
        var section = configCache[service.id]
        var hasKey = section && section.api_key
        if (!service.keyUrl || hasKey) {
            configureService(["--enable-service", service.id])
            return
        }
        serviceId = service.id
        view = "service"
        keyEntryVisible = true
    }

    function removeService(service) {
        if (!service.keyUrl) {
            configureService(["--disable-service", service.id])
            return
        }
        // Keys are kept for an easier reconnect, same choice as Cinnamon's "Keep key".
        // (Deleting the key entirely stays available from the Services view on the
        // Cinnamon applet; here Remove keeps the key.)
        configureService(["--disable-service", service.id])
    }
}
