# Needle

A PineCompute project.

Shows how much of your Claude, ChatGPT/Codex, z.ai and OpenRouter limits you have left: a Cinnamon panel applet on Linux Mint, a SwiftBar menu-bar item on macOS, and a native system-tray app on Windows. All three use the same fetcher and settings format.

The compact display reads like `C 58%   G 71%   Z 82%   $14`, where `C` is Claude, `G` is ChatGPT/Codex, and `Z` is z.ai. Cinnamon and macOS show the tighter of each service's 5-hour and weekly windows, so the number answers "how much can I use right now." The display turns amber under 30% and red under 10%. Click it for the full breakdown. When a gauge first turns red you also get a desktop notification (once — it stays quiet until the provider recovers; turn it off in the applet's settings).

## Install on Linux Mint

```bash
git clone https://github.com/KGthePM/Needle.git && cd Needle
./install.sh
```

Then right-click the panel, choose **Applets**, find **Needle** and press **+**.

## Add services

New installations start empty. Open Needle and choose **Add more**, then select each provider you want to track. A service moves into the main usage view only after Needle retrieves usable data from it. Failed or incomplete setups stay under **Add more** with their error and retry options. z.ai and OpenRouter open a password-style box for the API key; **Get an API key** opens the provider's key page. Claude and ChatGPT/Codex use existing command-line sign-ins and do not need a pasted key.

- **Claude**: nothing to add. It uses your Claude Code sign-in, so you need to have logged in to `claude` with your Pro or Max plan.
- **ChatGPT / Codex**: nothing to add. Needle first uses the OpenAI OAuth sign-in managed by OpenCode, then falls back to `~/.codex/auth.json`. Sign into OpenAI from OpenCode with the **ChatGPT Plus/Pro** option, or log into Codex CLI with your ChatGPT plan. A ChatGPT subscription does not include OpenAI API credits, and no API key is needed here. Needle reads the access token in place but never copies, refreshes, or writes it. Set `"credential_source": "opencode"` or `"codex"` if both tools use different accounts.
- **z.ai**: paste your Coding Plan API key.
- **OpenRouter**: a management key shows your whole account balance. A regular key shows that key's own spending limit.

Use **Settings** to change a key or remove a service. Removing a keyed service can retain its key for an easier reconnect or delete it. Advanced users can still edit `config.json` directly under `~/.config/needle` on Linux and macOS or `%APPDATA%\Needle` on Windows.

## Reading the gauges

The colored fill is what's left. The thin tick is how much time is left in that window. If the fill sits left of the tick, you're using it up faster than it resets.

## Refreshing

- Opening the popup refreshes on its own if the numbers are more than 5 minutes old.
- Auto-refresh runs every 10 minutes by default. Cinnamon users can change it in Needle's **Settings** pane.
- The Windows tray app also refreshes every 10 minutes and shows the compact summary when you hover over its icon. Its **Settings** page can show the 5-hour limit, weekly limit, or both; the default is weekly. Both mode reads like `G 64%/H 71%/W`.
- Claude is never asked more than once every 5 minutes, even if you click **Refresh** repeatedly. Anthropic backs off hard on frequent polling, and that backoff can slow Claude Code itself.

## Themes

The Windows flyout and Cinnamon popup support **Light**, **System**, **Dark**, **Night**, and **Custom** themes. System follows the operating system or Cinnamon theme, while Night uses a deeper blue-black palette. Custom exposes the complete Needle palette, including surfaces, text, status colors, and provider colors.

- On Windows, open **Settings** and use the Theme picker under **Display**. **Edit custom palette** opens color controls for every palette role. Windows theme settings are stored under the `windows` section of `%APPDATA%\Needle\config.json`.
- On Cinnamon, open Needle's **Settings**, choose **Theme**, and select a mode. **Edit custom palette** opens Cinnamon's native applet settings. These appearance settings stay local to the Cinnamon applet.
- The macOS SwiftBar menu continues to follow macOS appearance because SwiftBar owns the menu surface.

## Terminal

```bash
python3 ~/.local/bin/needle --text     # readable summary
python3 ~/.local/bin/needle --debug    # also prints raw API responses
```

On Windows, choose **Settings** > **Debug in terminal** from the tray flyout.

## If something looks off

The Claude, ChatGPT/Codex and z.ai usage endpoints aren't officially documented and can change. If numbers look wrong or a provider shows an error, run `needle --text --debug` (or use the desktop menu's debug action) and check the raw response. If the OpenCode sign-in has expired, open OpenCode once so it can refresh its own OAuth session.

## Install on Windows

Python 3 is required; the included Windows PowerShell is sufficient. From PowerShell in the cloned repository:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows\install-windows.ps1
```

Needle appears in the notification area and starts with Windows by default. Hover over the gauge icon for the compact summary, then click it for the usage flyout. The flyout keeps usage on its main page and places refresh, service management, startup, debug and exit actions under **Settings**. Windows only allows an icon in the notification area, so the percentages appear in its tooltip and flyout rather than directly on the taskbar.

Settings are stored in `%APPDATA%\Needle\config.json`; cached usage and the installed app live under `%LOCALAPPDATA%\Needle`. Use `-NoStartup` or `-NoLaunch` with the installer when needed.

## Install on macOS

```bash
git clone https://github.com/KGthePM/Needle.git && cd Needle/mac
./install-mac.sh
```

The script installs SwiftBar with Homebrew if it's missing, copies the plugin into your SwiftBar plugin folder, and sets up the same keys file as Linux (you can copy `config.json` over from the PC). If it can't find your plugin folder, open SwiftBar, pick one, and run `./install-mac.sh /path/to/folder`.

On the Mac, Claude Code keeps its sign-in in the Keychain. During install, macOS asks to let `security` read it. Click **Always Allow** so the menu bar can refresh without prompting. That permission covers the `security` tool, so other scripts that use it could read that one item without asking. Click **Allow** instead if you'd rather approve it each time.

In the menu, each bar is green, amber or red by how much is left, and the word after the reset time says how you're pacing: **▲ fast** (using it faster than it resets), **● on pace**, or **▼ plenty**. Hover over a row for the full sentence. Services without a key are grouped on one "Not set up" line. SwiftBar refreshes every 5 minutes (the `.5m.` in the file name).

The installer offers to open SwiftBar at login, so Needle is back in the menu bar after a restart. Needle uses native **Add more** and **Settings** submenus on macOS. To change login behavior later, use **System Settings** > **General** > **Login Items**. On Linux the applet loads with the panel, so there's nothing to set.

## Releasing

1. Update the root `VERSION` file and keep the fetcher, Cinnamon metadata, and SwiftBar version references synchronized with it.
2. Commit and push the release changes.
3. Create a stable `vMAJOR.MINOR.PATCH` GitHub release from the intended release commit (pass `--target` when it is not the default branch). The first non-heading line of its notes becomes the update description.

## Uninstall

```bash
./uninstall.sh                # Linux, keeps your keys
./mac/uninstall-mac.sh        # macOS, keeps your keys
# add --purge to either one to remove the keys too
```

On Windows:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows\uninstall-windows.ps1
# add -Purge to remove settings and keys too
```
