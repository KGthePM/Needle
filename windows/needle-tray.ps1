<#
Needle tray app for Windows: the tightest limit in the notification area, the full
breakdown on click. It runs the shared fetcher (~\.local\bin\needle) every 5 minutes.
The fetcher's own cooldowns still apply, so Claude is never asked more than once per 5 minutes.

install-windows.ps1 starts this hidden, and so does the Startup shortcut it creates.
Keep this file ASCII: Windows PowerShell 5.1 reads scripts without a BOM as ANSI.
#>
param([string]$Python = "python")

Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type -Namespace Needle -Name Native -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool DestroyIcon(System.IntPtr handle);
[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
'@

# One icon per user, however many times this is started.
$Mutex = New-Object System.Threading.Mutex($false, "Local\NeedleTray")
try { $owned = $Mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $owned = $true }
if (-not $owned) { exit }

[void][Needle.Native]::SetProcessDPIAware()
[System.Windows.Forms.Application]::EnableVisualStyles()
[System.Windows.Forms.Application]::SetUnhandledExceptionMode("CatchException")

$UserHome = [Environment]::GetFolderPath("UserProfile")
$Fetcher = Join-Path $UserHome ".local\bin\needle"
$Config = Join-Path $UserHome ".config\needle\config.json"
$LogFile = Join-Path $env:LOCALAPPDATA "Needle\tray.log"

$Ellipsis = [string][char]0x2026
$Cycle = [string][char]0x21BB
$Dot = [string][char]0x25CF

# Providers whose key can be pasted from the menu: name, where to get one, dialog hint.
$KeySetup = [ordered]@{
    zai        = @{ Name = "z.ai"; Url = "https://z.ai/manage-apikey/apikey-list"
                    Hint = "Paste your z.ai Coding Plan API key." }
    openrouter = @{ Name = "OpenRouter"; Url = "https://openrouter.ai/settings/keys"
                    Hint = "Paste an OpenRouter key. A management key shows your whole balance; a regular key shows that key's own limit." }
}
$PanelTag = @{ claude = "C"; codex = "X"; zai = "Z" }
$PaceWindows = @("5-hour", "Weekly")
$Pace = @{
    fast = @{ Text = "$([char]0x25B2) fast"; Ink = "warn" }
    even = @{ Text = "$Dot on pace"; Ink = "dim" }
    slow = @{ Text = "$([char]0x25BC) plenty"; Ink = "ok" }
}

# ------------------------------------------------------------------ look

function Color([string]$Hex) { [System.Drawing.ColorTranslator]::FromHtml($Hex) }

$Light = $false
try {
    $Light = 1 -eq (Get-ItemPropertyValue "HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize" `
        -Name SystemUsesLightTheme -ErrorAction Stop)
} catch { }
if ($Light) {
    $T = @{ Bg = Color "#F9F9F9"; Fg = Color "#1A1A1A"; Dim = Color "#6B6B6B"; Track = Color "#E1E1E1"; Line = Color "#E0E0E0"
            ok = Color "#1A7F37"; warn = Color "#9A6700"; crit = Color "#CF222E" }
} else {
    $T = @{ Bg = Color "#202020"; Fg = Color "#F3F3F3"; Dim = Color "#9D9D9D"; Track = Color "#3A3A3A"; Line = Color "#383838"
            ok = Color "#3FB950"; warn = Color "#D29922"; crit = Color "#F85149" }
}
$DotInk = @{ claude = Color "#D97757"; codex = $(if ($Light) { Color "#3C3C3C" } else { Color "#C8C8C8" })
             zai = Color "#3B82F6"; openrouter = Color "#8B5CF6" }
$IconInk = @{ ok = Color "#2E9E4F"; warn = Color "#E3A008"; crit = Color "#D93636"; none = Color "#6E6E6E" }

$FontUI = New-Object System.Drawing.Font("Segoe UI", 9)
$FontHead = New-Object System.Drawing.Font("Segoe UI Semibold", 10)
$FontSmall = New-Object System.Drawing.Font("Segoe UI", 8.25)

$g = [System.Drawing.Graphics]::FromHwnd([IntPtr]::Zero)
$Scale = $g.DpiX / 96
$g.Dispose()
function Px([double]$N) { [int][math]::Round($N * $Scale) }

# ------------------------------------------------------------------ helpers

function Write-Log([string]$Message) {
    try { Add-Content -LiteralPath $LogFile -Value "$(Get-Date -Format s)  $Message" -ErrorAction Stop } catch { }
}

function Get-Now { [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0 }

function Get-Level([double]$Left) {
    if ($Left -le 10) { "crit" } elseif ($Left -le 30) { "warn" } else { "ok" }
}

function Format-Duration([double]$Seconds) {
    $s = [math]::Max(0, [math]::Floor($Seconds))
    $d = [math]::Floor($s / 86400); $h = [math]::Floor(($s % 86400) / 3600); $m = [math]::Floor(($s % 3600) / 60)
    if ($d) { "${d}d ${h}h" } elseif ($h) { "${h}h ${m}m" } else { "$([math]::Max($m, 1))m" }
}

function Format-Ago([double]$Ts) {
    $s = (Get-Now) - $Ts
    if ($s -lt 60) { "just now" } else { "$(Format-Duration $s) ago" }
}

function Get-Providers {
    if ($script:Data -and $script:Data.providers) { return @($script:Data.providers) }
    return @()
}

function Get-BindingLeft($P) {
    $lefts = @(foreach ($w in $P.windows) { if ($PaceWindows -contains $w.label) { 100 - $w.used } })
    if ($lefts.Count) { return ($lefts | Measure-Object -Minimum).Minimum }
    return $null
}

function Get-Pace($W, [double]$Left) {
    # Compare what's left with how much of the window is left: fast, even or slow.
    if (-not ($W.window_seconds -and $W.resets_at)) { return $null }
    $timeLeft = [math]::Max(0.0, [math]::Min(1.0, ($W.resets_at - (Get-Now)) / $W.window_seconds))
    if ($Left / 100 -lt $timeLeft - 0.10) { return @{ Kind = "fast"; Note = "You're using it faster than it resets." } }
    if ($Left / 100 -gt $timeLeft + 0.10) { return @{ Kind = "slow"; Note = "Plenty of room for the time left." } }
    return @{ Kind = "even"; Note = "Right on pace." }
}

function Get-Summary($Providers) {
    # Same text as the panel and the menu bar: "C 58%  X 71%  Z 82%  $14".
    $parts = @(); $lefts = @()
    foreach ($p in $Providers) {
        $left = Get-BindingLeft $p
        if ($null -ne $left) {
            $tag = if ($PanelTag.ContainsKey($p.id)) { $PanelTag[$p.id] } else { $p.name.Substring(0, 1) }
            $parts += "$tag $([math]::Round($left))%"
            $lefts += $left
        } elseif ($p.balance -and $null -ne $p.balance.remaining) {
            $parts += '$' + [math]::Round($p.balance.remaining)
            if ($p.balance.total) { $lefts += 100 * $p.balance.remaining / $p.balance.total }
        }
    }
    $lowest = if ($lefts.Count) { ($lefts | Measure-Object -Minimum).Minimum } else { $null }
    return @{ Text = ($parts -join "  "); Lowest = $lowest }
}

# ------------------------------------------------------------------ data

$script:Data = $null
$script:Fatal = $null
$script:Proc = $null
$script:OutTask = $null
$script:FetchStarted = $null

function Set-Fatal([string]$Message) {
    $script:Data = $null
    $script:Fatal = $Message
    Update-Views
}

function Start-Fetch([string[]]$Extra = @()) {
    if ($script:Proc) { return }
    if (-not (Test-Path -LiteralPath $Fetcher)) { Set-Fatal "The fetcher isn't installed. Run install-windows.ps1 again."; return }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Python
    $psi.Arguments = (@($Fetcher) + $Extra | ForEach-Object { '"' + $_ + '"' }) -join " "
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    try { $proc = [System.Diagnostics.Process]::Start($psi) }
    catch { Set-Fatal "Couldn't start Python ($Python). Run install-windows.ps1 again."; return }
    $script:OutTask = $proc.StandardOutput.ReadToEndAsync()
    [void]$proc.StandardError.ReadToEndAsync()  # drain it so --debug output can't block the fetcher
    $script:Proc = $proc
    $script:FetchStarted = Get-Date
    $Poll.Start()
    Update-Views
}

function Complete-Fetch($Out, [int]$Code, [bool]$TimedOut) {
    if ($TimedOut) { Set-Fatal "The fetcher didn't finish. Try Refresh, or open Debug in terminal."; return }
    if ($Code -ne 0 -or -not $Out -or -not $Out.Trim()) {
        Set-Fatal "The fetcher stopped with an error. Open Debug in terminal to see why."; return
    }
    try { $data = $Out | ConvertFrom-Json -ErrorAction Stop }
    catch { Set-Fatal "Couldn't read the fetcher's output. Open Debug in terminal to check it."; return }
    $script:Data = $data
    $script:Fatal = $null
    Update-Views
}

# A WinForms timer checks on the fetcher so the tray never blocks while it runs.
$Poll = New-Object System.Windows.Forms.Timer
$Poll.Interval = 250
$Poll.Add_Tick({
    param($s, $e)
    $p = $script:Proc
    if (-not $p) { $s.Stop(); return }
    if ($p.HasExited -and $script:OutTask.IsCompleted) {
        $s.Stop()
        $out = $script:OutTask.Result; $code = $p.ExitCode
        $p.Dispose(); $script:Proc = $null
        Complete-Fetch $out $code $false
    } elseif (((Get-Date) - $script:FetchStarted).TotalSeconds -gt 90) {
        $s.Stop()
        try { $p.Kill() } catch { }
        $p.Dispose(); $script:Proc = $null
        Complete-Fetch $null 1 $true
    }
})

$Auto = New-Object System.Windows.Forms.Timer
$Auto.Interval = 5 * 60 * 1000
$Auto.Add_Tick({ Start-Fetch })

# ------------------------------------------------------------------ tray icon

function New-RoundedPath([single]$Size, [single]$Radius) {
    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $d = $Radius * 2
    $path.AddArc(0, 0, $d, $d, 180, 90)
    $path.AddArc($Size - $d, 0, $d, $d, 270, 90)
    $path.AddArc($Size - $d, $Size - $d, $d, $d, 0, 90)
    $path.AddArc(0, $Size - $d, $d, $d, 90, 90)
    $path.CloseFigure()
    return $path
}

function Set-TrayIcon($Left) {
    # The tightest percentage on a green, amber or red tile. 100 shows as 99 so it fits in 16 px.
    if ($null -eq $Left) { $text = "-"; $lvl = "none" }
    else { $text = [string][math]::Min(99, [math]::Round($Left)); $lvl = Get-Level $Left }
    $key = "$lvl $text"
    if ($key -eq $script:IconKey) { return }
    $script:IconKey = $key

    $size = [System.Windows.Forms.SystemInformation]::SmallIconSize.Width
    $bmp = New-Object System.Drawing.Bitmap($size, $size)
    $gr = [System.Drawing.Graphics]::FromImage($bmp)
    $gr.SmoothingMode = "AntiAlias"
    $gr.TextRenderingHint = "AntiAliasGridFit"
    $bg = New-Object System.Drawing.SolidBrush($IconInk[$lvl])
    $path = New-RoundedPath $size ([math]::Max(2, $size / 5))
    $gr.FillPath($bg, $path)
    $fg = New-Object System.Drawing.SolidBrush($(if ($lvl -eq "warn") { Color "#1A1A1A" } else { [System.Drawing.Color]::White }))
    $font = New-Object System.Drawing.Font("Segoe UI", [single]($size * 0.6), [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
    $fmt = New-Object System.Drawing.StringFormat
    $fmt.Alignment = "Center"; $fmt.LineAlignment = "Center"
    $gr.DrawString($text, $font, $fg, (New-Object System.Drawing.RectangleF(0, 0, $size, $size)), $fmt)
    $fmt.Dispose(); $font.Dispose(); $fg.Dispose(); $path.Dispose(); $bg.Dispose(); $gr.Dispose()

    $h = $bmp.GetHicon()
    $icon = ([System.Drawing.Icon]::FromHandle($h)).Clone()
    [void][Needle.Native]::DestroyIcon($h)
    $bmp.Dispose()
    $old = $Tray.Icon
    $Tray.Icon = $icon
    if ($old) { $old.Dispose() }
}

function Update-Views {
    $sum = Get-Summary (Get-Providers)
    Set-TrayIcon $sum.Lowest
    $tip = if ($script:Fatal) { "Needle: $($script:Fatal)" } elseif ($sum.Text) { $sum.Text } else { "Needle" }
    if ($tip.Length -gt 63) { $tip = $tip.Substring(0, 62) + $Ellipsis }  # NotifyIcon's limit
    $Tray.Text = $tip
    if ($script:Popup) { Fill-Popup; Set-PopupPosition $script:Popup }
}

# ------------------------------------------------------------------ popup

$script:Popup = $null
$script:PopupClosedAt = [datetime]::MinValue
$Tips = New-Object System.Windows.Forms.ToolTip

function New-TextLabel([string]$Text, $Font, $Ink, [int]$Width) {
    $l = New-Object System.Windows.Forms.Label
    $l.Text = $Text; $l.Font = $Font; $l.ForeColor = $Ink
    $l.AutoSize = $true; $l.UseMnemonic = $false
    if ($Width) { $l.MaximumSize = New-Object System.Drawing.Size($Width, 0) }
    $l.Margin = New-Object System.Windows.Forms.Padding(0, (Px 2), 0, (Px 2))
    return $l
}

function New-Link([string]$Text, $Ink, [int]$Width, $Tag, [scriptblock]$OnClick) {
    $l = New-Object System.Windows.Forms.LinkLabel
    $l.Text = $Text; $l.Font = $FontUI; $l.Tag = $Tag
    $l.AutoSize = $true; $l.UseMnemonic = $false
    if ($Width) { $l.MaximumSize = New-Object System.Drawing.Size($Width, 0) }
    $l.LinkColor = $Ink; $l.ActiveLinkColor = $Ink; $l.VisitedLinkColor = $Ink
    $l.LinkBehavior = "HoverUnderline"
    $l.Margin = New-Object System.Windows.Forms.Padding(0, (Px 2), 0, (Px 2))
    $l.Add_LinkClicked($OnClick)
    return $l
}

function New-Separator([int]$Width) {
    $s = New-Object System.Windows.Forms.Panel
    $s.Size = New-Object System.Drawing.Size($Width, [math]::Max(1, (Px 1)))
    $s.BackColor = $T.Line
    $s.Margin = New-Object System.Windows.Forms.Padding(0, (Px 8), 0, (Px 8))
    return $s
}

# Header, window and balance rows are painted, so bars, colors and columns line up.
$PaintRow = {
    param($s, $e)
    $r = $s.Tag; $gr = $e.Graphics; $h = $s.Height
    $gr.SmoothingMode = "AntiAlias"
    $left = [System.Windows.Forms.TextFormatFlags]"Left, VerticalCenter, SingleLine, NoPadding"
    $right = [System.Windows.Forms.TextFormatFlags]"Right, VerticalCenter, SingleLine, NoPadding"
    $draw = { param($text, $font, $ink, $x, $w, $flags)
        [System.Windows.Forms.TextRenderer]::DrawText($gr, $text, $font,
            (New-Object System.Drawing.Rectangle($x, 0, $w, $h)), $ink, $flags) }

    if ($r.Kind -eq "head") {
        $d = Px 9
        $b = New-Object System.Drawing.SolidBrush($r.Dot)
        $gr.FillEllipse($b, 0, [int](($h - $d) / 2), $d, $d)
        $b.Dispose()
        & $draw $r.Text $FontHead $T.Fg (Px 16) ($s.Width - (Px 16)) $left
        return
    }

    & $draw $r.Label $FontUI $T.Fg 0 $r.LabelW $left
    $x = $r.LabelW
    if ($null -eq $r.Frac) {  # balance with no limit: just the text
        & $draw $r.Detail $FontUI $T.Dim $x ($s.Width - $x) $left
        return
    }
    $bw = Px 120; $bh = Px 6; $by = [int](($h - $bh) / 2)
    $track = New-Object System.Drawing.SolidBrush($T.Track)
    $fill = New-Object System.Drawing.SolidBrush($T[$r.Ink])
    $gr.FillRectangle($track, $x, $by, $bw, $bh)
    $gr.FillRectangle($fill, $x, $by, [int]($bw * $r.Frac), $bh)
    if ($null -ne $r.Tick) {  # how much time is left in the window
        $tick = New-Object System.Drawing.SolidBrush($T.Fg)
        $tw = [math]::Max(1, (Px 1.5))
        $gr.FillRectangle($tick, $x + [int]($bw * $r.Tick) - [int]($tw / 2), $by - (Px 3), $tw, $bh + (Px 6))
        $tick.Dispose()
    }
    $track.Dispose(); $fill.Dispose()
    $x += $bw + (Px 4)
    & $draw $r.Value $FontUI $T[$r.Ink] $x (Px 44) $right
    $x += Px 52
    if ($r.Detail) { & $draw $r.Detail $FontUI $T.Dim $x ($s.Width - $x) $left; return }
    if ($r.Reset) { & $draw "$Cycle $($r.Reset)" $FontUI $T.Dim $x (Px 76) $left }
    if ($r.Pace) { & $draw $Pace[$r.Pace].Text $FontUI $T[$Pace[$r.Pace].Ink] ($x + (Px 80)) (Px 80) $left }
}

function New-Row($Row, [int]$Width, [int]$Height, [string]$Tip) {
    $p = New-Object System.Windows.Forms.Panel
    $p.Size = New-Object System.Drawing.Size($Width, $Height)
    $p.Margin = New-Object System.Windows.Forms.Padding(0)
    $p.Tag = $Row
    $p.Add_Paint($PaintRow)
    if ($Tip) { $Tips.SetToolTip($p, $Tip) }
    return $p
}

function Fill-Popup {
    $stack = $script:Stack
    $old = @($stack.Controls)
    $stack.SuspendLayout()
    $stack.Controls.Clear()
    foreach ($c in $old) { $c.Dispose() }
    $Tips.RemoveAll()

    $providers = Get-Providers
    $shown = @(foreach ($p in $providers) { if (-not $p.needs_key) { $p } })
    $unset = @(foreach ($p in $providers) { if ($p.needs_key) { $p } })
    $labels = @(foreach ($p in $shown) {
        foreach ($w in $p.windows) { $w.label }
        if ($p.balance) { if ($p.balance.label) { $p.balance.label } else { "Credits" } }
    })
    $labelW = Px 60
    foreach ($l in $labels) { $labelW = [math]::Max($labelW, [System.Windows.Forms.TextRenderer]::MeasureText($l, $FontUI).Width) }
    $labelW += Px 12
    $rowW = $labelW + (Px 332)
    $now = Get-Now

    if ($script:Fatal) {
        $stack.Controls.Add((New-TextLabel $script:Fatal $FontUI $T.Dim $rowW))
    } elseif (-not $providers.Count) {
        $msg = if ($script:Proc) { "Loading$Ellipsis" } else { "Nothing to show yet. Add your keys with Edit keys, then Refresh." }
        $stack.Controls.Add((New-TextLabel $msg $FontUI $T.Dim $rowW))
    }

    $i = 0
    foreach ($p in $shown) {
        if ($i++) { $stack.Controls.Add((New-Separator $rowW)) }
        $dot = if ($DotInk.ContainsKey($p.id)) { $DotInk[$p.id] } else { $T.Dim }
        $head = $p.name + $(if ($p.plan) { " $($p.plan)" } else { "" })
        $stack.Controls.Add((New-Row @{ Kind = "head"; Text = $head; Dot = $dot } $rowW (Px 26) $null))

        foreach ($w in $p.windows) {
            $left = 100 - $w.used
            $row = @{ Kind = "window"; Label = $w.label; LabelW = $labelW; Frac = $left / 100
                      Ink = Get-Level $left; Value = "$([math]::Round($left))%"; Detail = $w.detail; Tick = $null }
            $tip = "$([math]::Round($left))% left"
            if ($w.resets_at) {
                $row.Reset = Format-Duration ($w.resets_at - $now)
                $tip += ", resets in $($row.Reset)"
                if ($w.window_seconds) {
                    $row.Tick = [math]::Max(0.0, [math]::Min(1.0, ($w.resets_at - $now) / $w.window_seconds))
                }
            }
            $pace = Get-Pace $w $left
            if ($pace -and -not $w.detail) { $row.Pace = $pace.Kind; $tip += ". $($pace.Note)" }
            $stack.Controls.Add((New-Row $row $rowW (Px 22) $tip))
        }

        $b = $p.balance
        if ($b) {
            $name = if ($b.label) { $b.label } else { "Credits" }
            if ($null -eq $b.remaining) {
                $spent = "{0:N2}" -f [double]$(if ($b.spent) { $b.spent } else { 0 })
                $row = @{ Kind = "balance"; Label = $name; LabelW = $labelW; Frac = $null; Detail = "`$$spent spent, no limit" }
            } else {
                $frac = if ($b.total) { [math]::Max(0.0, [math]::Min(1.0, $b.remaining / $b.total)) } else { 0.0 }
                $row = @{ Kind = "balance"; Label = $name; LabelW = $labelW; Frac = $frac; Ink = Get-Level ($frac * 100)
                          Value = "`$" + ("{0:N2}" -f [double]$b.remaining); Detail = "of `$" + ("{0:N2}" -f [double]$b.total) }
            }
            $stack.Controls.Add((New-Row $row $rowW (Px 22) $null))
        }

        if ($p.error) {
            $msg = $p.error
            if ($p.stale -and $p.fetched_at) { $msg += " Last good read $(Format-Ago $p.fetched_at)." }
            if ($KeySetup.Contains($p.id)) {
                $stack.Controls.Add((New-Link $msg $T.warn $rowW $p.id { param($s, $e) Set-Key $s.Tag }))
            } else {
                $stack.Controls.Add((New-TextLabel $msg $FontSmall $T.warn $rowW))
            }
        }
    }

    # One line per provider that just needs a key, instead of a section each.
    if ($unset.Count) {
        if ($shown.Count) { $stack.Controls.Add((New-Separator $rowW)) }
        foreach ($p in $unset) {
            $stack.Controls.Add((New-Link "$Dot Add $($p.name) key$Ellipsis" $T.Fg $rowW $p.id { param($s, $e) Set-Key $s.Tag }))
        }
    }

    $stack.Controls.Add((New-Separator $rowW))
    $foot = New-Object System.Windows.Forms.FlowLayoutPanel
    $foot.FlowDirection = "LeftToRight"; $foot.WrapContents = $false
    $foot.AutoSize = $true; $foot.AutoSizeMode = "GrowAndShrink"
    $foot.Margin = New-Object System.Windows.Forms.Padding(0)
    $status = if ($script:Proc) { "Refreshing$Ellipsis" }
              elseif ($script:Data -and $script:Data.updated) { "Updated $(Format-Ago $script:Data.updated)" }
              else { "" }
    $foot.Controls.Add((New-TextLabel $status $FontSmall $T.Dim 0))
    $refresh = New-Link "Refresh" $T.Fg 0 $null { Start-Fetch }
    $refresh.Margin = New-Object System.Windows.Forms.Padding((Px 12), (Px 2), 0, (Px 2))
    $foot.Controls.Add($refresh)
    $stack.Controls.Add($foot)
    $stack.ResumeLayout()
}

function Set-PopupPosition($Form) {
    # Just above (or below) the tray, kept on screen whichever edge the taskbar is on.
    $c = $script:Anchor
    $wa = [System.Windows.Forms.Screen]::FromPoint($c).WorkingArea
    $m = Px 8
    $x = [math]::Max($wa.Left + $m, [math]::Min($c.X - [int]($Form.Width / 2), $wa.Right - $Form.Width - $m))
    if ($c.Y -ge $wa.Bottom) { $y = $wa.Bottom - $Form.Height - $m }
    elseif ($c.Y -le $wa.Top) { $y = $wa.Top + $m }
    else { $y = [math]::Max($wa.Top + $m, $c.Y - $Form.Height - $m) }
    $Form.Location = New-Object System.Drawing.Point($x, $y)
}

function Show-Popup {
    $script:Anchor = [System.Windows.Forms.Cursor]::Position
    $f = New-Object System.Windows.Forms.Form
    $f.FormBorderStyle = "None"; $f.ShowInTaskbar = $false; $f.TopMost = $true
    $f.StartPosition = "Manual"; $f.KeyPreview = $true
    $f.BackColor = $T.Bg; $f.ForeColor = $T.Fg; $f.Font = $FontUI
    $f.AutoSize = $true; $f.AutoSizeMode = "GrowAndShrink"
    $f.Padding = New-Object System.Windows.Forms.Padding((Px 14))

    $stack = New-Object System.Windows.Forms.FlowLayoutPanel
    $stack.FlowDirection = "TopDown"; $stack.WrapContents = $false
    $stack.AutoSize = $true; $stack.AutoSizeMode = "GrowAndShrink"
    $stack.Margin = New-Object System.Windows.Forms.Padding(0)
    $f.Controls.Add($stack)
    $script:Stack = $stack
    Fill-Popup

    $f.Add_Paint({ param($s, $e)
        $pen = New-Object System.Drawing.Pen($T.Line)
        $e.Graphics.DrawRectangle($pen, 0, 0, $s.Width - 1, $s.Height - 1)
        $pen.Dispose() })
    $f.Add_Load({ param($s, $e) Set-PopupPosition $s })
    $f.Add_KeyDown({ param($s, $e) if ($e.KeyCode -eq "Escape") { $s.Close() } })
    $f.Add_Deactivate({ param($s, $e) $s.Close() })
    $f.Add_FormClosed({ param($s, $e) $script:Popup = $null; $script:PopupClosedAt = Get-Date })
    $script:Popup = $f
    $f.Show()
    Set-PopupPosition $f
    $f.Activate()

    # Like the Linux applet: opening it refreshes if the numbers are more than 5 minutes old.
    if ($script:Data -and $script:Data.updated -and (Get-Now) - $script:Data.updated -gt 300) { Start-Fetch }
}

# ------------------------------------------------------------------ keys

function Show-KeyDialog([string]$Id) {
    $info = $KeySetup[$Id]
    $f = New-Object System.Windows.Forms.Form
    $f.Text = "$($info.Name) key"
    $f.FormBorderStyle = "FixedDialog"; $f.MaximizeBox = $false; $f.MinimizeBox = $false
    $f.StartPosition = "CenterScreen"; $f.TopMost = $true; $f.Font = $FontUI
    $f.AutoSize = $true; $f.AutoSizeMode = "GrowAndShrink"
    $f.Padding = New-Object System.Windows.Forms.Padding((Px 12))

    $layout = New-Object System.Windows.Forms.FlowLayoutPanel
    $layout.FlowDirection = "TopDown"; $layout.WrapContents = $false
    $layout.AutoSize = $true; $layout.AutoSizeMode = "GrowAndShrink"
    $hint = New-Object System.Windows.Forms.Label
    $hint.Text = "$($info.Hint)`n`nIt's saved only on this PC, in .config\needle\config.json."
    $hint.AutoSize = $true; $hint.UseMnemonic = $false
    $hint.MaximumSize = New-Object System.Drawing.Size((Px 360), 0)
    $box = New-Object System.Windows.Forms.TextBox
    $box.Width = Px 360; $box.UseSystemPasswordChar = $true
    $box.Margin = New-Object System.Windows.Forms.Padding(0, (Px 10), 0, (Px 10))

    $buttons = New-Object System.Windows.Forms.FlowLayoutPanel
    $buttons.FlowDirection = "RightToLeft"; $buttons.AutoSize = $true; $buttons.Anchor = "Right"
    $buttons.Margin = New-Object System.Windows.Forms.Padding(0)
    $save = New-Object System.Windows.Forms.Button
    $save.Text = "Save"; $save.DialogResult = "OK"; $save.AutoSize = $true
    $cancel = New-Object System.Windows.Forms.Button
    $cancel.Text = "Cancel"; $cancel.DialogResult = "Cancel"; $cancel.AutoSize = $true
    $get = New-Object System.Windows.Forms.Button
    $get.Text = "Get a key$Ellipsis"; $get.AutoSize = $true; $get.Tag = $info.Url
    $get.Add_Click({ param($s, $e) Start-Process $s.Tag })
    $buttons.Controls.AddRange(@($save, $cancel, $get))

    $layout.Controls.AddRange(@($hint, $box, $buttons))
    $f.Controls.Add($layout)
    $f.AcceptButton = $save; $f.CancelButton = $cancel
    $f.ActiveControl = $box
    $f.Add_Shown({ param($s, $e) $s.Activate() })

    $result = $f.ShowDialog()
    $text = $box.Text.Trim()
    $f.Dispose()
    if ($result -eq "OK" -and $text) { return $text }
    return $null
}

function Set-Key([string]$Id) {
    if ($script:Popup) { $script:Popup.Close() }
    $key = Show-KeyDialog $Id
    if (-not $key) { return }
    $cfg = $null
    if (Test-Path -LiteralPath $Config) {
        try {
            $raw = Get-Content -Raw -LiteralPath $Config -ErrorAction Stop
            if ($raw -and $raw.Trim()) { $cfg = $raw | ConvertFrom-Json -ErrorAction Stop }
        } catch {
            [void][System.Windows.Forms.MessageBox]::Show(
                "The keys file has a formatting error, so the key wasn't saved. It will open now so you can fix it.",
                "Needle", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Warning)
            Start-Process notepad.exe -ArgumentList "`"$Config`""
            return
        }
    }
    if ($null -eq $cfg) { $cfg = New-Object PSObject }
    $section = $cfg.$Id
    if ($null -eq $section) {
        $section = New-Object PSObject
        $cfg | Add-Member -NotePropertyName $Id -NotePropertyValue $section -Force
    }
    $section | Add-Member -NotePropertyName api_key -NotePropertyValue $key -Force
    $section | Add-Member -NotePropertyName enabled -NotePropertyValue $true -Force

    [void](New-Item -ItemType Directory -Force -Path (Split-Path $Config))
    $tmp = "$Config.tmp"
    [System.IO.File]::WriteAllText($tmp, ($cfg | ConvertTo-Json -Depth 10), (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tmp -Destination $Config -Force
    # Fetch now so the new numbers (or the key's error) show right away.
    Start-Fetch @("--force")
}

function Open-Debug {
    $cmd = "& '{0}' '{1}' --text --debug --force" -f ($Python -replace "'", "''"), ($Fetcher -replace "'", "''")
    Start-Process powershell.exe -ArgumentList "-NoExit -NoProfile -Command `"$cmd`""
}

# ------------------------------------------------------------------ menu and start-up

$Tray = New-Object System.Windows.Forms.NotifyIcon
$Tray.Text = "Needle"

function Add-MenuItem($Items, [string]$Text, [scriptblock]$OnClick) {
    $mi = New-Object System.Windows.Forms.ToolStripMenuItem($Text)
    if ($OnClick) { $mi.Add_Click($OnClick) }
    [void]$Items.Add($mi)
    return $mi
}

$Menu = New-Object System.Windows.Forms.ContextMenuStrip
[void](Add-MenuItem $Menu.Items "Refresh" { Start-Fetch })
$keys = Add-MenuItem $Menu.Items "Edit keys" $null
foreach ($id in $KeySetup.Keys) {
    $mi = Add-MenuItem $keys.DropDownItems "Set $($KeySetup[$id].Name) key$Ellipsis" { param($s, $e) Set-Key $s.Tag }
    $mi.Tag = $id
}
[void](Add-MenuItem $keys.DropDownItems "Open keys file" { Start-Process notepad.exe -ArgumentList "`"$Config`"" })
$more = Add-MenuItem $Menu.Items "More" $null
[void](Add-MenuItem $more.DropDownItems "Refresh now (skip cooldowns)" { Start-Fetch @("--force") })
[void](Add-MenuItem $more.DropDownItems "Debug in terminal" { Open-Debug })
[void]$Menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))
[void](Add-MenuItem $Menu.Items "Quit Needle" {
    $Auto.Stop(); $Poll.Stop()
    $Tray.Visible = $false
    $Tray.Dispose()
    $Context.ExitThread()
})
$Tray.ContextMenuStrip = $Menu

$Tray.Add_MouseClick({
    param($s, $e)
    if ($e.Button -ne "Left") { return }
    if ($script:Popup) { $script:Popup.Close(); return }
    # Clicking the icon while the popup is open closes it first (it loses focus), so don't reopen it.
    if (((Get-Date) - $script:PopupClosedAt).TotalMilliseconds -lt 300) { return }
    Show-Popup
})

[System.Windows.Forms.Application]::add_ThreadException({ param($s, $e) Write-Log $e.Exception.ToString() })

$Context = New-Object System.Windows.Forms.ApplicationContext
Set-TrayIcon $null
$Tray.Visible = $true
Start-Fetch
$Auto.Start()
[System.Windows.Forms.Application]::Run($Context)
$Mutex.ReleaseMutex()
