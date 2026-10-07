# Needle

A PineCompute project.

Shows how much of your Claude, ChatGPT/Codex, z.ai and OpenRouter limits you have left: a Cinnamon panel applet on Linux Mint, a SwiftBar menu-bar item on macOS, and a native system-tray app on Windows. All three use the same fetcher and settings format.

The compact display reads like `C 58%   G 71%   Z 82%   $14`, where `C` is Claude, `G` is ChatGPT/Codex, and `Z` is z.ai. Cinnamon and macOS show the tighter of each service's 5-hour and weekly windows by default, so the number answers "how much can I use right now." To pick a different window, use **Settings > Menu bar shows** on the Mac or **Settings > Percentages show** in the Cinnamon popup: **5-hour**, **Weekly**, **Both** (reads like `C 92/41%`), or **Alternate**, which swaps between a `5h` line and a `Wk` line every few seconds. On the Mac the gauge icon keeps following the tightest limit whatever the text shows. A service without the chosen window shows its tightest one. The display turns amber under 30% and red under 10%. Click it for the full breakdown. When a gauge first turns red you get a desktop notification, and another when it's back above 10%. Nothing repeats while it sits in the red. This works on all three platforms; see [Notifications](#notifications).

If you run models on your own computer, Needle also counts those tokens in the popup. It counts up as a reward rather than down as a limit, so it stays out of the compact display; see [Local AI](#local-ai).

## Install on Linux Mint

```bash
git clone https://github.com/KGthePM/Needle.git && cd Needle
./install.sh
```

Then right-click the panel, choose **Applets**, find **Needle** and press **+**.

## Add services

New installations start empty. Open Needle and choose **Add more**, then select each provider you want to track. A service moves into the main usage view only after Needle retrieves usable data from it. Failed or incomplete setups stay under **Add more** with their error and retry options. z.ai and OpenRouter open a password-style box for the API key; **Get an API key** opens the provider's key page. Claude and ChatGPT/Codex use existing command-line sign-ins and do not need a pasted key.

- **Claude**: nothing to add. It uses your Claude Code sign-in, so you need to have logged in to `claude` with your Pro or Max plan. Claude Code's sign-in expires every few hours and normally refreshes only while `claude` runs. When Needle finds it expired, it runs `claude auth status` in the background so Claude Code refreshes it, then tries again. Needle never refreshes or writes the sign-in itself.
- **ChatGPT / Codex**: nothing to add. Needle first uses the OpenAI OAuth sign-in managed by OpenCode, then falls back to `~/.codex/auth.json`. Sign into OpenAI from OpenCode with the **ChatGPT Plus/Pro** option, or log into Codex CLI with your ChatGPT plan. A ChatGPT subscription does not include OpenAI API credits, and no API key is needed here. Needle reads the access token in place but never copies, refreshes, or writes it. Set `"credential_source": "opencode"` or `"codex"` if both tools use different accounts.
- **z.ai**: paste your Coding Plan API key.
- **OpenRouter**: a management key shows your whole account balance. A regular key shows that key's own spending limit.

Use **Settings** to change a key or remove a service. Removing a keyed service can retain its key for an easier reconnect or delete it. Advanced users can still edit `config.json` directly under `~/.config/needle` on Linux and macOS or `%APPDATA%\Needle` on Windows.

## Local AI

Needle counts the tokens you run on local models and shows the running total in its own card in the popup, like `133K↑`. It isn't a limit, so it stays out of the menu bar, panel and tray text. The card shows today, this week, all time, your top three models, and what those tokens would have cost on a hosted API. It needs no setup: it appears once Needle has found some local usage.

Where the numbers come from (all read-only, nothing leaves your computer):

- **Ollama's server log.** On Linux that is the systemd journal (`journalctl -u ollama`); your user needs to be able to read it, which members of the `adm` or `systemd-journal` group can. On macOS it is `~/.ollama/logs/server.log` for the Ollama app, and `/opt/homebrew/var/log/ollama.log` (or `/usr/local/var/log/ollama.log` on Intel Macs) if you run Ollama with `brew services start ollama`. On Windows it is `%LOCALAPPDATA%\Ollama\server.log`. Ollama logs the prompt and generated token counts of each chat or generate request, whatever app sent it. Older versions of Ollama, such as 0.12, don't log token counts; update Ollama if nothing shows up.
- **OpenCode.** Assistant messages sent to a local provider (`ollama`, `lmstudio`, `llamacpp`, `vllm`) in `~/.local/share/opencode`. OpenCode messages to Ollama are only counted from before Ollama's log had token counts, so nothing is counted twice.

What is not counted:

- Embeddings (`/api/embed`). Ollama doesn't log their token counts.
- Requests that Ollama serves without logging token counts, which includes some models on its own engine.
- Ollama started by hand with `ollama serve` in a terminal. It logs to that terminal, not to a file.
- Anything from before the log began recording counts, or older than your journal or log keeps. On Linux the first refresh reads the whole journal once, which can take up to a minute. After that each refresh only reads what's new.
- LM Studio, llama.cpp and vLLM on their own; they are counted only through OpenCode.

Prompt tokens Ollama reuses from its cache aren't counted again, because only newly processed tokens are logged.

**Worth about $X at API prices** is an estimate of what the same tokens would cost on a hosted model. The default is $1 per million input tokens and $5 per million output tokens, about a small hosted model. Change it in `config.json`:

```json
"local": { "price_per_million": { "input": 3.00, "output": 15.00 } }
```

You get one notification each time your all-time total passes 100K, 1M, 10M and 100M tokens. Each one fires once, using the same on/off setting as the limit notifications. Usage already found the first time Needle looks doesn't trigger one. To turn Local AI off, set `"local": { "enabled": false }`. The running total lives in `local.json` next to Needle's cache.

## Reading the gauges

The colored fill is what's left. The thin tick is how much time is left in that window. If the fill sits left of the tick, you're using it up faster than it resets.

When you are using a window faster than it resets, Needle estimates when it will run out at your average rate so far in that window, for example "At this pace it runs out around 3:40 PM, 1h 20m before it resets." It waits until 5% of the window has passed before guessing, so one early burst doesn't produce a wild number. `needle --text` shows the same estimate.

## Notifications

Needle sends one notification when a service first drops to 10% or less, and one more when it's back above 10% (usually because the window reset). It stays quiet in between. The same setting covers the [Local AI](#local-ai) milestones.

- **Linux**: a desktop notification. Turn it off with **Notify when a gauge turns red** in the applet's settings.
- **macOS**: a Notification Center banner. Turn it off with **Settings** > **Notify when a limit runs low**. Banners come from Script Editor, because SwiftBar runs `osascript` to send them, so allow notifications for Script Editor in **System Settings** > **Notifications** if none show up.
- **Windows**: a notification from the tray icon. Turn it off with **Settings** > **Notify when a limit runs low**.

On macOS and Windows the setting is `"notify": false` at the top level of `config.json`, or `needle --set-notify off`.

## Refreshing

- Opening the popup refreshes on its own if the numbers are more than 5 minutes old.
- Auto-refresh runs every 10 minutes by default. Cinnamon users can change it in Needle's **Settings** pane.
- The Windows tray app also refreshes every 10 minutes and shows the compact summary when you hover over its icon. Its **Settings** page can show the 5-hour limit, weekly limit, or both; the default is weekly. Both mode reads like `G 64%/H 71%/W`.
- Claude is never asked more than once every 5 minutes, even if you click **Refresh** repeatedly. Anthropic backs off hard on frequent polling, and that backoff can slow Claude Code itself.
- Each service shows when its numbers were last read and, while it is cooling down, when the next refresh will fetch new ones (for example `Updated 3m ago · next refresh in 2m`). On Linux and Windows the **Refresh** buttons gray out until at least one service would return new data; the Mac menu shows the time instead, because SwiftBar only redraws every 5 minutes.

## Themes

The Windows flyout and Cinnamon popup support **Light**, **System**, **Dark**, **Night**, and **Custom** themes. System follows the operating system or Cinnamon theme, while Night uses a deeper blue-black palette. Custom exposes the complete Needle palette, including surfaces, text, status colors, and provider colors.

- On Windows, open **Settings** and use the Theme picker under **Display**. **Edit custom palette** opens color controls for every palette role. Windows theme settings are stored under the `windows` section of `%APPDATA%\Needle\config.json`.
- On Cinnamon, open Needle's **Settings**, choose **Theme**, and select a mode. **Edit custom palette** opens Cinnamon's native applet settings. These appearance settings stay local to the Cinnamon applet.
- The macOS SwiftBar menu continues to follow macOS appearance because SwiftBar owns the menu surface.

## Terminal

```bash
python3 ~/.local/bin/needle --text     # readable summary
python3 ~/.local/bin/needle --debug    # also prints raw API responses
python3 ~/.local/bin/needle --update   # install the latest release
```

On Windows, choose **Settings** > **Debug in terminal** from the tray flyout.

## Updating

Once a day Needle checks GitHub for a new release. When there is one, an **Update to 1.x.x…** item shows up at the top of the menu on the Mac, and an **Update** button appears in the popup on Linux and Windows. Clicking it opens a terminal window that downloads the release and reruns the installer. Your keys and settings stay as they are, and it doesn't ask the setup questions again. From a terminal, `needle --update` does the same thing.

To check right away instead of waiting for the daily check, choose **Settings** > **Check for updates**. It shows whether you're up to date and when it last checked. From a terminal, `needle --check-updates` does the same.

To turn off the daily check, add `"check_updates": false` at the top level of `config.json`. **Check for updates** still works when you choose it.

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

On the Mac, Claude Code keeps its sign-in in the Keychain. During install, macOS asks to let `security` read it. Click **Always Allow** so the menu bar can refresh without prompting. That permission covers the `security` tool, so other scripts that use it could read that one item without asking. Click **Allow** instead if you'd rather approve it each time. If a `~/.claude/.credentials.json` file is also on the Mac, Needle reads both and uses whichever sign-in is newer, so a leftover file can't make Claude look signed out.

In the menu, each bar is green, amber or red by how much is left, and the word after the reset time says how you're pacing: **▲ fast** (using it faster than it resets), **● on pace**, or **▼ plenty**. When it's fast and Needle can estimate when it runs out, the word becomes that time, for example **▲ out 3:40 PM**. Hover over a row for the full sentence. Services without a key are grouped on one "Not set up" line. SwiftBar refreshes every 5 minutes (the `.5m.` in the file name).

The installer offers to open SwiftBar at login, so Needle is back in the menu bar after a restart. Needle uses native **Add more** and **Settings** submenus on macOS. To change login behavior later, use **System Settings** > **General** > **Login Items**. On Linux the applet loads with the panel, so there's nothing to set.

## Releasing

1. Update the root `VERSION` file and keep the fetcher, Cinnamon metadata, and SwiftBar version references synchronized with it.
2. Commit and push the release changes.
   `tests/test_needle.py` fails if they drift apart.
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
