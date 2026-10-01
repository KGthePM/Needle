# Needle

A PineCompute project.

Shows how much of your Claude, Codex, z.ai and OpenRouter limits you have left: a Cinnamon panel applet on Linux Mint and a SwiftBar menu-bar item on macOS. Both use the same fetcher and the same keys file.

The panel reads like `C 58%   X 71%   Z 82%   $14`. Each percentage is the tighter of that service's 5-hour and weekly windows, so it answers "how much can I use right now." The text turns amber under 30% and red under 10%. Click it for the full breakdown.

## Install on Linux Mint

```bash
git clone https://github.com/KGthePM/Needle.git && cd Needle
./install.sh
```

Then right-click the panel, choose **Applets**, find **Needle** and press **+**.

## Keys

On the Mac, click **Add z.ai key…** or **Add OpenRouter key…** in the menu (or **Edit keys** > **Set … key…**) and paste the key into the box. **Get a key…** opens the page where you create one. On Linux, open `~/.config/needle/config.json` (or click **Edit keys** in the popup).

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
```

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

## Uninstall

```bash
./uninstall.sh                # Linux, keeps your keys
./mac/uninstall-mac.sh        # macOS, keeps your keys
# add --purge to either one to remove the keys too
```
