# Needle

A PineCompute project.

Shows how much of your Claude, Codex, z.ai and OpenRouter limits you have left: a Cinnamon panel applet on Linux Mint, a SwiftBar menu-bar item on macOS and a tray icon on Windows. All three use the same fetcher and the same keys file.

The panel reads like `C 58%   X 71%   Z 82%   $14`. Each percentage is the tighter of that service's 5-hour and weekly windows, so it answers "how much can I use right now." The text turns amber under 30% and red under 10%. Click it for the full breakdown.

## Install on Linux Mint

```bash
git clone https://github.com/KGthePM/Needle.git && cd Needle
./install.sh
```

Then right-click the panel, choose **Applets**, find **Needle** and press **+**.

## Keys

On the Mac, click **Add z.ai key…** or **Add OpenRouter key…** in the menu (or **Edit keys** > **Set … key…**) and paste the key into the box. **Get a key…** opens the page where you create one. On Windows, right-click the tray icon and choose **Edit keys** > **Set … key…**. On Linux, open `~/.config/needle/config.json` (or click **Edit keys** in the popup).

- **Claude**: nothing to add. It uses your Claude Code sign-in, so you need to have logged in to `claude` with your Pro or Max plan.
- **Codex**: nothing to add. It uses your Codex sign-in from `~/.codex/auth.json`, so you need to have logged in to `codex` with your ChatGPT plan (Plus, Pro, Business and so on). It shows the same 5-hour and weekly limits as `/status` in Codex. If you set Codex to keep its sign-in in the Keychain (`cli_auth_credentials_store`), switch it back to the default file storage.
- **z.ai**: paste your Coding Plan API key.
- **OpenRouter**: a management key shows your whole account balance. A regular key shows that key's own spending limit.

Set `"enabled": false` on anything you don't use and it disappears from the panel.

## Reading the gauges

The colored fill is what's left. The thin tick is how much time is left in that window. If the fill sits left of the tick, you're using it up faster than it resets.

## Refreshing

- Opening the popup refreshes on its own if the numbers are more than 5 minutes old.
- Auto-refresh runs every 10 minutes by default. Change it in the applet's settings.
- Claude is never asked more than once every 5 minutes, even if you click **Refresh** repeatedly. Anthropic backs off hard on frequent polling, and that backoff can slow Claude Code itself.

## Terminal

```bash
python3 ~/.local/bin/needle --text     # readable summary
python3 ~/.local/bin/needle --debug    # also prints raw API responses
python3 ~/.local/bin/needle --update   # install the latest release
```

On Windows, use `py "$HOME\.local\bin\needle" --text` in PowerShell.

## Updating

Once a day Needle checks GitHub for a new release. When there is one, an **Update to 1.x.x…** line shows up: in the menu on the Mac, at the top of the popup on Linux, and in the popup and right-click menu on Windows. Clicking it opens a terminal window that downloads the release and reruns the installer. Your keys and settings stay as they are, and it doesn't ask the setup questions again. From a terminal, `needle --update` does the same thing.

To turn off the check, add `"check_updates": false` at the top level of `config.json`.

## If something looks off

The Claude, Codex and z.ai usage endpoints aren't officially documented and can change. If numbers look wrong or a provider shows an error, run `needle --text --debug` (or right-click the applet and choose **Open in terminal**) and check the raw response.

## Install on macOS

```bash
git clone https://github.com/KGthePM/Needle.git && cd Needle/mac
./install-mac.sh
```

The script installs SwiftBar with Homebrew if it's missing, copies the plugin into your SwiftBar plugin folder, and sets up the same keys file as Linux (you can copy `config.json` over from the PC). If it can't find your plugin folder, open SwiftBar, pick one, and run `./install-mac.sh /path/to/folder`.

On the Mac, Claude Code keeps its sign-in in the Keychain. During install, macOS asks to let `security` read it. Click **Always Allow** so the menu bar can refresh without prompting. That permission covers the `security` tool, so other scripts that use it could read that one item without asking. Click **Allow** instead if you'd rather approve it each time.

In the menu, each bar is green, amber or red by how much is left, and the word after the reset time says how you're pacing: **▲ fast** (using it faster than it resets), **● on pace**, or **▼ plenty**. Hover over a row for the full sentence. Services without a key are grouped on one "Not set up" line. SwiftBar refreshes every 5 minutes (the `.5m.` in the file name).

The installer offers to open SwiftBar at login, so Needle is back in the menu bar after a restart. To change that later, use **System Settings** > **General** > **Login Items**. On Linux the applet loads with the panel, so there's nothing to set.

## Install on Windows

You need Python 3. The installer offers to get it with `winget` if it's missing. In PowerShell:

```powershell
git clone https://github.com/KGthePM/Needle.git; cd Needle
powershell -ExecutionPolicy Bypass -File .\windows\install-windows.ps1
```

No Git? Download the ZIP from GitHub, unzip it, and run the second line from that folder.

The script copies the fetcher to `~\.local\bin\needle` and the tray app to `%LOCALAPPDATA%\Needle`, sets up the keys file at `~\.config\needle\config.json` (the same file as on the other machines, so you can copy it over), and starts the tray icon.

Windows can't put text on the taskbar, so the icon shows the tightest percentage on a green, amber or red tile. Hover over it for the `C 58%   X 71%   Z 82%   $14` line, and click it for the full breakdown with the same bars, reset times and pace words as the Mac. Right-click it for **Refresh**, **Edit keys**, **More** > **Debug in terminal** and **Quit Needle**. It refreshes every 5 minutes. Windows may hide a new icon under the **^** arrow by the clock, so drag it onto the taskbar to keep it in view.

The installer offers to start Needle when you sign in. To change that later, turn it off in **Task Manager** > **Startup apps**, or delete `Needle` from the folder that opens when you run `shell:startup`.

## Uninstall

```bash
./uninstall.sh                # Linux, keeps your keys
./mac/uninstall-mac.sh        # macOS, keeps your keys
# add --purge to either one to remove the keys too
```

```powershell
powershell -ExecutionPolicy Bypass -File .\windows\uninstall-windows.ps1          # Windows, keeps your keys
# add -Purge to remove the keys too
```

## Releasing

1. Bump `VERSION` in `fetcher/needle.py`, `version` in `applet/needle@pinecompute/metadata.json` and `xbar.version` in `mac/needle.5m.py`.
2. Commit and push.
3. `gh release create v1.x.x --notes "One line on what's new"`. The first line of the notes that isn't a heading becomes the hover text on the update line.
