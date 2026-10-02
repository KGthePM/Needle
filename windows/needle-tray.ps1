param(
    [switch]$Once,
    [switch]$Force,
    [switch]$DebugOutput,
    [string]$FetcherPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Test-Python3 {
    param([string]$File, [string[]]$Prefix)
    try {
        & $File @Prefix -c 'import sys; raise SystemExit(sys.version_info.major != 3)' *> $null
        return $LASTEXITCODE -eq 0
    }
    catch { return $false }
}

function Quote-ProcessArgument {
    param([string]$Value)
    if ($Value -notmatch '[\s"]') { return $Value }
    return '"' + ($Value -replace '"', '\"') + '"'
}

function Find-Python {
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py -and (Test-Python3 $py.Source @('-3'))) {
        return @{ File = $py.Source; Prefix = @('-3') }
    }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python -and (Test-Python3 $python.Source @())) {
        return @{ File = $python.Source; Prefix = @() }
    }
    throw 'Python 3 was not found. Install Python, then run install-windows.ps1 again.'
}

function Resolve-Fetcher {
    if ($FetcherPath) {
        return [IO.Path]::GetFullPath($FetcherPath)
    }
    $installed = Join-Path $env:LOCALAPPDATA 'Needle\bin\needle.py'
    if (Test-Path -LiteralPath $installed) {
        return $installed
    }
    return [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\fetcher\needle.py'))
}

$script:Python = Find-Python
$script:Fetcher = Resolve-Fetcher
if (-not (Test-Path -LiteralPath $script:Fetcher -PathType Leaf)) {
    throw "Needle's fetcher was not found at $($script:Fetcher)."
}

if ($Once) {
    $arguments = @($script:Python.Prefix) + @($script:Fetcher, '--text')
    if ($Force) { $arguments += '--force' }
    if ($DebugOutput) { $arguments += '--debug' }
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $script:Python.File
    $info.Arguments = (($arguments | ForEach-Object { Quote-ProcessArgument ([string]$_) }) -join ' ')
    $info.UseShellExecute = $false
    $process = [Diagnostics.Process]::Start($info)
    $process.WaitForExit()
    $exitCode = $process.ExitCode
    $process.Dispose()
    exit $exitCode
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'The Needle tray app requires Windows.'
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class NeedleNativeMethods {
    [DllImport("user32.dll", CharSet = CharSet.Auto)]
    public static extern bool DestroyIcon(IntPtr handle);

    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr handle);
}
'@
[Windows.Forms.Application]::EnableVisualStyles()

function Get-Value {
    param($Object, [string]$Name, $Default = $null)
    if ($null -eq $Object) { return $Default }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value) { return $Default }
    return $property.Value
}

function Get-BindingLeft {
    param($Provider)
    $left = @(
        foreach ($window in @(Get-Value $Provider 'windows' @())) {
            $label = [string](Get-Value $window 'label' '')
            if ($label -in @('5-hour', 'Weekly')) {
                100.0 - [double](Get-Value $window 'used' 100)
            }
        }
    )
    if ($left.Count -eq 0) { return $null }
    return [double](($left | Measure-Object -Minimum).Minimum)
}

function Get-LevelColor {
    param([double]$Left)
    if ($Left -le 10) { return [Drawing.Color]::Firebrick }
    if ($Left -le 30) { return [Drawing.Color]::DarkOrange }
    return [Drawing.Color]::ForestGreen
}

function Format-Duration {
    param([double]$Seconds)
    $seconds = [Math]::Max(0, [int]$Seconds)
    $days = [Math]::Floor($seconds / 86400)
    $hours = [Math]::Floor(($seconds % 86400) / 3600)
    $minutes = [Math]::Floor(($seconds % 3600) / 60)
    if ($days) { return '{0}d {1}h' -f $days, $hours }
    if ($hours) { return '{0}h {1}m' -f $hours, $minutes }
    return '{0}m' -f [Math]::Max($minutes, 1)
}

function Get-Pace {
    param($Window, [double]$Left)
    $length = [double](Get-Value $Window 'window_seconds' 0)
    $reset = [double](Get-Value $Window 'resets_at' 0)
    if ($length -le 0 -or $reset -le 0) { return '' }
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $timeLeft = [Math]::Max(0, [Math]::Min(1, ($reset - $now) / $length))
    if (($Left / 100) -lt ($timeLeft - 0.10)) { return 'fast' }
    if (($Left / 100) -gt ($timeLeft + 0.10)) { return 'plenty' }
    return 'on pace'
}

function New-GaugeIcon {
    param($Left)
    $bitmap = [Drawing.Bitmap]::new(32, 32)
    $graphics = [Drawing.Graphics]::FromImage($bitmap)
    $graphics.SmoothingMode = [Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $graphics.Clear([Drawing.Color]::Transparent)
    $background = [Drawing.SolidBrush]::new([Drawing.Color]::FromArgb(255, 42, 45, 50))
    $graphics.FillEllipse($background, 1, 1, 30, 30)

    $color = if ($null -eq $Left) { [Drawing.Color]::SlateGray } else { Get-LevelColor ([double]$Left) }
    $arc = [Drawing.Pen]::new($color, 4)
    $arc.StartCap = [Drawing.Drawing2D.LineCap]::Round
    $arc.EndCap = [Drawing.Drawing2D.LineCap]::Round
    $graphics.DrawArc($arc, 5, 5, 22, 22, 135, 270)

    $amount = if ($null -eq $Left) { 0.5 } else { [Math]::Max(0, [Math]::Min(1, [double]$Left / 100)) }
    $angle = (135 + 270 * $amount) * [Math]::PI / 180
    $needle = [Drawing.Pen]::new([Drawing.Color]::White, 2)
    $graphics.DrawLine($needle, 16, 16, 16 + 9 * [Math]::Cos($angle), 16 + 9 * [Math]::Sin($angle))
    $hub = [Drawing.SolidBrush]::new([Drawing.Color]::White)
    $graphics.FillEllipse($hub, 13, 13, 6, 6)

    $handle = $bitmap.GetHicon()
    try {
        return ([Drawing.Icon]::FromHandle($handle).Clone())
    }
    finally {
        [void][NeedleNativeMethods]::DestroyIcon($handle)
        $hub.Dispose()
        $needle.Dispose()
        $arc.Dispose()
        $background.Dispose()
        $graphics.Dispose()
        $bitmap.Dispose()
    }
}

function Get-Summary {
    param($Data)
    $tags = @{ claude = 'C'; codex = 'G'; zai = 'Z' }
    $parts = @()
    $enabledIds = @(Get-EnabledServiceIds (Get-Config))
    foreach ($provider in @(Get-Value $Data 'providers' @())) {
        $providerId = [string](Get-Value $provider 'id' '')
        if ($providerId -notin $enabledIds -or -not (Test-ProviderConnected $provider)) { continue }
        $left = Get-BindingLeft $provider
        if ($null -ne $left) {
            $tag = if ($tags.ContainsKey($providerId)) { $tags[$providerId] } else { ([string](Get-Value $provider 'name' '?')).Substring(0, 1) }
            $parts += '{0} {1}%' -f $tag, [Math]::Round($left)
            continue
        }
        $balance = Get-Value $provider 'balance'
        $remaining = Get-Value $balance 'remaining'
        if ($null -ne $remaining) { $parts += ('$' + [Math]::Round([double]$remaining)) }
    }
    if ($parts.Count -eq 0) { return 'Needle - no usage data yet' }
    return 'Needle | ' + ($parts -join '  ')
}

function Get-LowestLeft {
    param($Data)
    $values = @()
    $enabledIds = @(Get-EnabledServiceIds (Get-Config))
    foreach ($provider in @(Get-Value $Data 'providers' @())) {
        $providerId = [string](Get-Value $provider 'id' '')
        if ($providerId -notin $enabledIds -or -not (Test-ProviderConnected $provider)) { continue }
        $left = Get-BindingLeft $provider
        if ($null -ne $left) { $values += [double]$left }
        $balance = Get-Value $provider 'balance'
        $remaining = Get-Value $balance 'remaining'
        $total = [double](Get-Value $balance 'total' 0)
        if ($null -ne $remaining -and $total -gt 0) { $values += 100 * [double]$remaining / $total }
    }
    if ($values.Count -eq 0) { return $null }
    return [double](($values | Measure-Object -Minimum).Minimum)
}

$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$created = $false
$script:Mutex = [Threading.Mutex]::new($true, "Local\NeedleTray-$sid", [ref]$created)
if (-not $created) { exit 0 }
$script:StopEvent = [Threading.EventWaitHandle]::new(
    $false,
    [Threading.EventResetMode]::AutoReset,
    "Local\NeedleTrayStop-$sid"
)
$script:StoppedEvent = [Threading.EventWaitHandle]::new(
    $false,
    [Threading.EventResetMode]::ManualReset,
    "Local\NeedleTrayStopped-$sid"
)

$script:ConfigPath = if ($env:NEEDLE_CONFIG) { $env:NEEDLE_CONFIG } else { Join-Path $env:APPDATA 'Needle\config.json' }
$script:CachePath = if ($env:NEEDLE_CACHE) { $env:NEEDLE_CACHE } else { Join-Path $env:LOCALAPPDATA 'Needle\cache\usage.json' }
$script:StartupPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'Needle.lnk'
$script:PowerShellPath = (Get-Process -Id $PID).Path
$script:Data = $null
$script:LastError = $null
$script:RefreshProcess = $null
$script:RefreshOutput = $null
$script:RefreshError = $null
$script:CurrentIcon = $null
$script:CurrentView = 'Usage'
$script:ConfigError = $null
$script:AllowDeactivate = $false
$script:LastFlyoutClose = 0
$script:FlyoutAnchor = $null
$script:FlyoutOpensUp = $true
$script:Services = @(
    [pscustomobject]@{ Id = 'claude'; Name = 'Claude'; Description = 'Uses your Claude Code sign-in.'; Keyed = $false; KeyUrl = ''; KeyHint = '' },
    [pscustomobject]@{ Id = 'codex'; Name = 'ChatGPT / Codex'; Description = 'Uses your OpenCode or Codex CLI sign-in.'; Keyed = $false; KeyUrl = ''; KeyHint = '' },
    [pscustomobject]@{ Id = 'zai'; Name = 'z.ai'; Description = 'Coding Plan usage from an API key.'; Keyed = $true; KeyUrl = 'https://z.ai/manage-apikey/apikey-list'; KeyHint = 'Paste your z.ai Coding Plan API key.' },
    [pscustomobject]@{ Id = 'openrouter'; Name = 'OpenRouter'; Description = 'Account balance or per-key spending limit.'; Keyed = $true; KeyUrl = 'https://openrouter.ai/settings/keys'; KeyHint = 'Paste an OpenRouter key. A management key shows your whole balance; a regular key shows that key''s own limit.' }
)

if (Test-Path -LiteralPath $script:CachePath) {
    try { $script:Data = Get-Content -LiteralPath $script:CachePath -Raw | ConvertFrom-Json } catch {}
}

$script:Tray = [Windows.Forms.NotifyIcon]::new()
$script:Tray.Visible = $true
$script:UiFont = [Drawing.Font]::new('Segoe UI', 9)
$script:UiBoldFont = [Drawing.Font]::new('Segoe UI Semibold', 10)
$script:UiTitleFont = [Drawing.Font]::new('Segoe UI Semibold', 13)
$script:UiMonoFont = [Drawing.Font]::new('Consolas', 9)
$script:Flyout = [Windows.Forms.Form]::new()
$script:Flyout.FormBorderStyle = [Windows.Forms.FormBorderStyle]::None
$script:Flyout.StartPosition = [Windows.Forms.FormStartPosition]::Manual
$script:Flyout.ShowInTaskbar = $false
$script:Flyout.TopMost = $true
$script:Flyout.KeyPreview = $true
$script:Flyout.BackColor = [Drawing.Color]::FromArgb(247, 248, 250)
$script:Flyout.ClientSize = [Drawing.Size]::new(420, 480)
$script:Header = [Windows.Forms.Panel]::new()
$script:Header.Dock = [Windows.Forms.DockStyle]::Top
$script:Header.Height = 48
$script:Header.BackColor = [Drawing.Color]::White
$script:Content = [Windows.Forms.Panel]::new()
$script:Content.Dock = [Windows.Forms.DockStyle]::Fill
$script:Content.AutoScroll = $true
$script:Content.BackColor = $script:Flyout.BackColor
$script:Flyout.Controls.Add($script:Content)
$script:Flyout.Controls.Add($script:Header)

function New-FlyoutButton {
    param(
        [string]$Text,
        [int]$X,
        [int]$Y,
        [int]$Width,
        [int]$Height = 34,
        [scriptblock]$Action,
        [switch]$Primary,
        [switch]$Danger
    )
    $button = [Windows.Forms.Button]::new()
    $button.Text = $Text
    $button.Location = [Drawing.Point]::new($X, $Y)
    $button.Size = [Drawing.Size]::new($Width, $Height)
    $button.FlatStyle = [Windows.Forms.FlatStyle]::Flat
    $button.FlatAppearance.BorderColor = [Drawing.Color]::FromArgb(210, 214, 220)
    $button.Font = $script:UiFont
    $button.Cursor = [Windows.Forms.Cursors]::Hand
    $button.BackColor = [Drawing.Color]::White
    $button.ForeColor = [Drawing.Color]::FromArgb(35, 39, 47)
    if ($Primary) {
        $button.BackColor = [Drawing.Color]::FromArgb(37, 99, 235)
        $button.ForeColor = [Drawing.Color]::White
        $button.FlatAppearance.BorderColor = $button.BackColor
        $button.Font = $script:UiBoldFont
    }
    if ($Danger) { $button.ForeColor = [Drawing.Color]::Firebrick }
    if ($Action) { $button.Add_Click($Action) }
    return $button
}

function Add-FlyoutText {
    param(
        [string]$Text,
        [int]$X = 16,
        [int]$Width = 388,
        [Drawing.Font]$Font = $script:UiFont,
        [Drawing.Color]$Color = [Drawing.Color]::Empty,
        [Windows.Forms.HorizontalAlignment]$Align = [Windows.Forms.HorizontalAlignment]::Left,
        [int]$After = 8
    )
    $label = [Windows.Forms.Label]::new()
    $label.Text = $Text
    $label.Location = [Drawing.Point]::new($X, $script:RenderY)
    $label.Width = $Width
    $label.AutoSize = $true
    $label.MaximumSize = [Drawing.Size]::new($Width, 0)
    $label.Font = $Font
    $label.TextAlign = if ($Align -eq [Windows.Forms.HorizontalAlignment]::Center) { [Drawing.ContentAlignment]::TopCenter } else { [Drawing.ContentAlignment]::TopLeft }
    if ($Color -ne [Drawing.Color]::Empty) { $label.ForeColor = $Color }
    $script:Content.Controls.Add($label)
    $script:RenderY += $label.PreferredHeight + $After
    return $label
}

function Add-FlyoutDivider {
    param([int]$TopMargin = 5, [int]$BottomMargin = 10)
    $script:RenderY += $TopMargin
    $line = [Windows.Forms.Panel]::new()
    $line.Location = [Drawing.Point]::new(16, $script:RenderY)
    $line.Size = [Drawing.Size]::new(388, 1)
    $line.BackColor = [Drawing.Color]::FromArgb(220, 223, 228)
    $script:Content.Controls.Add($line)
    $script:RenderY += 1 + $BottomMargin
}

function Get-Config {
    $script:ConfigError = $null
    if (-not (Test-Path -LiteralPath $script:ConfigPath)) { return [pscustomobject]@{} }
    try {
        $config = Get-Content -LiteralPath $script:ConfigPath -Raw | ConvertFrom-Json
        if ($null -eq $config) { return [pscustomobject]@{} }
        return $config
    }
    catch {
        $script:ConfigError = 'The raw config could not be read. Open it to fix the JSON.'
        return [pscustomobject]@{}
    }
}

function Get-EnabledServiceIds {
    param($Config)
    return @(
        foreach ($service in $script:Services) {
            $section = Get-Value $Config $service.Id
            if ($null -ne $section -and [bool](Get-Value $section 'enabled' $true)) { $service.Id }
        }
    )
}

function Test-ProviderConnected {
    param($Provider)
    if ($null -eq $Provider) { return $false }
    $property = $Provider.PSObject.Properties['connected']
    if ($null -ne $property -and $null -ne $property.Value) { return [bool]$property.Value }
    return @(Get-Value $Provider 'windows' @()).Count -gt 0 -or $null -ne (Get-Value $Provider 'balance')
}

function Show-FlyoutMessage {
    param([string]$Text, [Windows.Forms.MessageBoxIcon]$Icon = [Windows.Forms.MessageBoxIcon]::Warning)
    $script:AllowDeactivate = $true
    try {
        [void][Windows.Forms.MessageBox]::Show($script:Flyout, $Text, 'Needle', [Windows.Forms.MessageBoxButtons]::OK, $Icon)
    }
    finally { $script:AllowDeactivate = $false }
}

function Invoke-ConfigCommand {
    param([string]$Flag, [string]$ServiceId, [AllowNull()][string]$InputText = $null)
    $arguments = @($script:Python.Prefix) + @($script:Fetcher, $Flag, $ServiceId)
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $script:Python.File
    $info.Arguments = (($arguments | ForEach-Object { Quote-ProcessArgument ([string]$_) }) -join ' ')
    $info.WorkingDirectory = Split-Path -Parent $script:Fetcher
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.RedirectStandardInput = $null -ne $InputText
    try {
        $process = [Diagnostics.Process]::Start($info)
        if ($null -ne $InputText) {
            $process.StandardInput.Write($InputText)
            $process.StandardInput.Close()
        }
        $stdout = $process.StandardOutput.ReadToEnd()
        $stderr = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        $exitCode = $process.ExitCode
        $process.Dispose()
        if ($exitCode -ne 0) {
            $message = if ($stderr.Trim()) { $stderr.Trim() } elseif ($stdout.Trim()) { $stdout.Trim() } else { "The settings command exited with code $exitCode." }
            Show-FlyoutMessage $message
            return $false
        }
        return $true
    }
    catch {
        Show-FlyoutMessage ("Could not update settings: " + $_.Exception.Message)
        return $false
    }
}

function Show-KeyPrompt {
    param($Service)
    $dialog = [Windows.Forms.Form]::new()
    $dialog.Text = "$($Service.Name) key"
    $dialog.FormBorderStyle = [Windows.Forms.FormBorderStyle]::FixedDialog
    $dialog.StartPosition = [Windows.Forms.FormStartPosition]::CenterParent
    $dialog.ClientSize = [Drawing.Size]::new(410, 196)
    $dialog.MaximizeBox = $false
    $dialog.MinimizeBox = $false
    $dialog.ShowInTaskbar = $false
    $dialog.Font = $script:UiFont

    $hint = [Windows.Forms.Label]::new()
    $hint.Text = $Service.KeyHint
    $hint.Location = [Drawing.Point]::new(18, 16)
    $hint.Size = [Drawing.Size]::new(374, 42)
    $dialog.Controls.Add($hint)
    $box = [Windows.Forms.TextBox]::new()
    $box.Location = [Drawing.Point]::new(18, 65)
    $box.Size = [Drawing.Size]::new(374, 25)
    $box.UseSystemPasswordChar = $true
    $dialog.Controls.Add($box)
    $link = [Windows.Forms.LinkLabel]::new()
    $link.Text = 'Get an API key'
    $link.Location = [Drawing.Point]::new(18, 101)
    $link.AutoSize = $true
    $url = $Service.KeyUrl
    $link.Add_LinkClicked(({ Start-Process $url }).GetNewClosure())
    $dialog.Controls.Add($link)
    $cancel = New-FlyoutButton 'Cancel' 217 147 82 30 ({ $dialog.DialogResult = [Windows.Forms.DialogResult]::Cancel }.GetNewClosure())
    $save = New-FlyoutButton 'Save' 310 147 82 30 ({ $dialog.DialogResult = [Windows.Forms.DialogResult]::OK }.GetNewClosure()) -Primary
    $dialog.Controls.Add($cancel)
    $dialog.Controls.Add($save)
    $dialog.AcceptButton = $save
    $dialog.CancelButton = $cancel

    $script:AllowDeactivate = $true
    try {
        $result = $dialog.ShowDialog($script:Flyout)
        if ($result -ne [Windows.Forms.DialogResult]::OK) { return $null }
        $key = $box.Text.Trim()
        if (-not $key) {
            Show-FlyoutMessage 'Enter an API key before saving.'
            return $null
        }
        return $key
    }
    finally {
        $dialog.Dispose()
        $script:AllowDeactivate = $false
    }
}

function Show-RemovePrompt {
    param($Service)
    if (-not $Service.Keyed) {
        $script:AllowDeactivate = $true
        try {
            $answer = [Windows.Forms.MessageBox]::Show($script:Flyout, "Remove $($Service.Name) from Needle?", 'Needle', [Windows.Forms.MessageBoxButtons]::OKCancel, [Windows.Forms.MessageBoxIcon]::Question)
            if ($answer -eq [Windows.Forms.DialogResult]::OK) { return 'Keep' }
            return $null
        }
        finally { $script:AllowDeactivate = $false }
    }

    $dialog = [Windows.Forms.Form]::new()
    $dialog.Text = "Remove $($Service.Name)"
    $dialog.FormBorderStyle = [Windows.Forms.FormBorderStyle]::FixedDialog
    $dialog.StartPosition = [Windows.Forms.FormStartPosition]::CenterParent
    $dialog.ClientSize = [Drawing.Size]::new(430, 158)
    $dialog.MaximizeBox = $false
    $dialog.MinimizeBox = $false
    $dialog.ShowInTaskbar = $false
    $dialog.Font = $script:UiFont
    $label = [Windows.Forms.Label]::new()
    $label.Text = "Remove $($Service.Name) from Needle? You can keep its saved key for later, or delete the key too."
    $label.Location = [Drawing.Point]::new(18, 18)
    $label.Size = [Drawing.Size]::new(394, 45)
    $dialog.Controls.Add($label)
    $cancel = New-FlyoutButton 'Cancel' 18 96 82 32 ({ $dialog.DialogResult = [Windows.Forms.DialogResult]::Cancel }.GetNewClosure())
    $keep = New-FlyoutButton 'Remove, keep key' 112 96 139 32 ({ $dialog.DialogResult = [Windows.Forms.DialogResult]::No }.GetNewClosure())
    $delete = New-FlyoutButton 'Remove and delete key' 263 96 149 32 ({ $dialog.DialogResult = [Windows.Forms.DialogResult]::Yes }.GetNewClosure()) -Danger
    $dialog.Controls.Add($cancel)
    $dialog.Controls.Add($keep)
    $dialog.Controls.Add($delete)
    $dialog.CancelButton = $cancel
    $script:AllowDeactivate = $true
    try {
        $result = $dialog.ShowDialog($script:Flyout)
        if ($result -eq [Windows.Forms.DialogResult]::No) { return 'Keep' }
        if ($result -eq [Windows.Forms.DialogResult]::Yes) { return 'Delete' }
        return $null
    }
    finally {
        $dialog.Dispose()
        $script:AllowDeactivate = $false
    }
}

function Set-ServiceKey {
    param($Service)
    $key = Show-KeyPrompt $Service
    if ($null -eq $key) { return $false }
    return Invoke-ConfigCommand '--set-service-key' $Service.Id $key
}

function Enable-Service {
    param([string]$ServiceId)
    $service = @($script:Services | Where-Object { $_.Id -eq $ServiceId })[0]
    $config = Get-Config
    $section = Get-Value $config $ServiceId
    $hasKey = -not [string]::IsNullOrWhiteSpace([string](Get-Value $section 'api_key' ''))
    $changed = if ($service.Keyed -and -not $hasKey) { Set-ServiceKey $service } else { Invoke-ConfigCommand '--enable-service' $ServiceId }
    if ($changed) {
        Start-Refresh $true
        Render-Flyout
    }
}

function Remove-Service {
    param([string]$ServiceId)
    $service = @($script:Services | Where-Object { $_.Id -eq $ServiceId })[0]
    $choice = Show-RemovePrompt $service
    if (-not $choice) { return }
    if (-not (Invoke-ConfigCommand '--disable-service' $ServiceId)) { return }
    if ($choice -eq 'Delete' -and -not (Invoke-ConfigCommand '--clear-service-key' $ServiceId)) { return }
    Start-Refresh $true
    Render-Flyout
}

function Open-Config {
    if (-not (Test-Path -LiteralPath $script:ConfigPath)) {
        [IO.Directory]::CreateDirectory((Split-Path -Parent $script:ConfigPath)) | Out-Null
        Set-Content -LiteralPath $script:ConfigPath -Value ('{}' + [Environment]::NewLine) -Encoding utf8
    }
    Start-Process -FilePath notepad.exe -ArgumentList (Quote-ProcessArgument $script:ConfigPath)
}

function Test-Startup { return Test-Path -LiteralPath $script:StartupPath }

function Set-Startup {
    param([bool]$Enabled)
    if (-not $Enabled) {
        Remove-Item -LiteralPath $script:StartupPath -Force -ErrorAction SilentlyContinue
        return
    }
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($script:StartupPath)
    $shortcut.TargetPath = $script:PowerShellPath
    $shortcut.Arguments = '-NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File ' + (Quote-ProcessArgument $PSCommandPath)
    $shortcut.WorkingDirectory = $PSScriptRoot
    $shortcut.Description = 'Needle AI usage monitor'
    $shortcut.Save()
}

function Start-DebugWindow {
    $arguments = '-NoExit -NoProfile -ExecutionPolicy Bypass -File {0} -Once -Force -DebugOutput' -f (Quote-ProcessArgument $PSCommandPath)
    Start-Process -FilePath $script:PowerShellPath -ArgumentList $arguments
}

function Render-Header {
    foreach ($control in @($script:Header.Controls)) { $control.Dispose() }
    $titleX = 16
    if ($script:CurrentView -ne 'Usage') {
        $back = New-FlyoutButton '< Back' 8 8 68 31 {
            $script:CurrentView = 'Usage'
            Render-Flyout
        }
        $back.FlatAppearance.BorderSize = 0
        $script:Header.Controls.Add($back)
        $titleX = 86
    }
    $title = [Windows.Forms.Label]::new()
    $title.Text = $script:CurrentView
    $title.Font = $script:UiTitleFont
    $title.Location = [Drawing.Point]::new($titleX, 13)
    $title.AutoSize = $true
    $script:Header.Controls.Add($title)
    $close = New-FlyoutButton 'X' 379 8 32 31 { Hide-Flyout }
    $close.FlatAppearance.BorderSize = 0
    $script:Header.Controls.Add($close)
}

function Render-UsageView {
    $config = Get-Config
    $enabledIds = @(Get-EnabledServiceIds $config)
    $providerColors = @{
        claude = [Drawing.Color]::Chocolate
        codex = [Drawing.Color]::SeaGreen
        zai = [Drawing.Color]::RoyalBlue
        openrouter = [Drawing.Color]::MediumOrchid
    }
    $providers = @()
    if ($script:Data) {
        $providers = @(
            foreach ($provider in @(Get-Value $script:Data 'providers' @())) {
                if ([string](Get-Value $provider 'id' '') -in $enabledIds -and (Test-ProviderConnected $provider)) { $provider }
            }
        )
    }

    if ($providers.Count -eq 0) {
        $script:RenderY = 52
        [void](Add-FlyoutText 'No services connected yet' 16 388 $script:UiTitleFont ([Drawing.Color]::FromArgb(45, 49, 57)) Center 8)
        [void](Add-FlyoutText 'Use Add more to connect a service.' 40 340 $script:UiFont ([Drawing.Color]::DimGray) Center 22)
        $add = New-FlyoutButton '+ Add more' 100 $script:RenderY 220 44 {
            $script:CurrentView = 'Add more'
            Render-Flyout
        } -Primary
        $script:Content.Controls.Add($add)
        $script:RenderY += 66
        if ($script:ConfigError) { [void](Add-FlyoutText $script:ConfigError 32 356 $script:UiFont ([Drawing.Color]::Firebrick) Center 8) }
        $settings = New-FlyoutButton 'Settings' 100 $script:RenderY 220 34 {
            $script:CurrentView = 'Settings'
            Render-Flyout
        }
        $settings.FlatAppearance.BorderSize = 0
        $script:Content.Controls.Add($settings)
        $script:RenderY += 44
        return
    }

    foreach ($provider in $providers) {
        $id = [string](Get-Value $provider 'id' '')
        $name = [string](Get-Value $provider 'name' $id)
        $plan = [string](Get-Value $provider 'plan' '')
        $color = if ($providerColors.ContainsKey($id)) { $providerColors[$id] } else { [Drawing.Color]::SlateGray }
        $title = if ($plan) { "$name ($plan)" } else { $name }
        $dot = [Windows.Forms.Panel]::new()
        $dot.Location = [Drawing.Point]::new(17, $script:RenderY + 4)
        $dot.Size = [Drawing.Size]::new(10, 10)
        $dot.BackColor = $color
        $script:Content.Controls.Add($dot)
        [void](Add-FlyoutText $title 34 370 $script:UiBoldFont ([Drawing.Color]::Empty) Left 8)

        foreach ($window in @(Get-Value $provider 'windows' @())) {
            $left = 100.0 - [double](Get-Value $window 'used' 100)
            $filled = [Math]::Max(0, [Math]::Min(16, [Math]::Round(16 * $left / 100)))
            $bar = ('#' * $filled) + ('-' * (16 - $filled))
            $resetAt = [double](Get-Value $window 'resets_at' 0)
            $reset = if ($resetAt -gt 0) { Format-Duration ($resetAt - [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) } else { '' }
            $pace = Get-Pace $window $left
            $detail = [string](Get-Value $window 'detail' '')
            $tail = if ($detail) { $detail } elseif ($reset -and $pace) { "$reset | $pace" } elseif ($reset) { $reset } else { $pace }
            $row = '{0,-12} [{1}] {2,3}% left' -f ([string](Get-Value $window 'label' 'Limit')), $bar, [Math]::Round($left)
            if ($tail) { $row += " | $tail" }
            [void](Add-FlyoutText $row 34 370 $script:UiMonoFont (Get-LevelColor $left) Left 5)
        }

        $balance = Get-Value $provider 'balance'
        if ($balance) {
            $remaining = Get-Value $balance 'remaining'
            $total = Get-Value $balance 'total'
            if ($null -eq $remaining) {
                $row = '{0}: ${1:N2} spent, no limit' -f (Get-Value $balance 'label' 'Credits'), [double](Get-Value $balance 'spent' 0)
                [void](Add-FlyoutText $row 34 370 $script:UiMonoFont ([Drawing.Color]::Empty) Left 5)
            }
            else {
                $row = '{0}: ${1:N2} left of ${2:N2}' -f (Get-Value $balance 'label' 'Credits'), [double]$remaining, [double]$total
                $percent = if ([double]$total -gt 0) { 100 * [double]$remaining / [double]$total } else { 0 }
                [void](Add-FlyoutText $row 34 370 $script:UiMonoFont (Get-LevelColor $percent) Left 5)
            }
        }

        $source = [string](Get-Value $provider 'source' '')
        if ($source) { [void](Add-FlyoutText "Using $source" 34 370 $script:UiFont ([Drawing.Color]::DimGray) Left 5) }
        $errorText = [string](Get-Value $provider 'error' '')
        if ($errorText) {
            [void](Add-FlyoutText ('! ' + $errorText) 34 370 $script:UiFont ([Drawing.Color]::Firebrick) Left 5)
        }
        Add-FlyoutDivider 7 10
    }

    if ($script:LastError) {
        [void](Add-FlyoutText ('! ' + $script:LastError) 16 388 $script:UiFont ([Drawing.Color]::Firebrick) Left 8)
    }
    if ($providers.Count -lt $script:Services.Count) {
        $add = New-FlyoutButton '+ Add more' 16 $script:RenderY 388 38 {
            $script:CurrentView = 'Add more'
            Render-Flyout
        }
        $script:Content.Controls.Add($add)
        $script:RenderY += 46
    }
    $settings = New-FlyoutButton 'Settings' 16 $script:RenderY 388 34 {
        $script:CurrentView = 'Settings'
        Render-Flyout
    }
    $settings.FlatAppearance.BorderSize = 0
    $script:Content.Controls.Add($settings)
    $script:RenderY += 44
}

function Render-AddServiceView {
    $config = Get-Config
    $enabledIds = @(Get-EnabledServiceIds $config)
    $providersById = @{}
    if ($script:Data) {
        foreach ($provider in @(Get-Value $script:Data 'providers' @())) {
            $providersById[[string](Get-Value $provider 'id' '')] = $provider
        }
    }
    $connectedIds = @(
        foreach ($id in $enabledIds) {
            if ($providersById.ContainsKey($id) -and (Test-ProviderConnected $providersById[$id])) { $id }
        }
    )
    $available = @($script:Services | Where-Object { $_.Id -notin $connectedIds })
    if ($available.Count -eq 0) {
        $script:RenderY = 48
        [void](Add-FlyoutText 'All services are connected.' 16 388 $script:UiBoldFont ([Drawing.Color]::DimGray) Center 10)
        return
    }
    [void](Add-FlyoutText 'Connect another service or retry an incomplete setup.' 16 388 $script:UiFont ([Drawing.Color]::DimGray) Left 12)
    foreach ($service in $available) {
        $idForClick = $service.Id
        $section = Get-Value $config $service.Id
        $enabled = $service.Id -in $enabledIds
        $hasKey = -not [string]::IsNullOrWhiteSpace([string](Get-Value $section 'api_key' ''))
        $provider = if ($providersById.ContainsKey($service.Id)) { $providersById[$service.Id] } else { $null }
        $buttonText = if ($enabled) { "Retry $($service.Name)" } else { $service.Name }
        $buttonWidth = if ($enabled -and $service.Keyed -and $hasKey) { 285 } else { 388 }
        if ($enabled -and $service.Keyed -and -not $hasKey) {
            $buttonText = "Add $($service.Name) key"
            $serviceForKey = $service
            $action = ({
                if (Set-ServiceKey $serviceForKey) {
                    Start-Refresh $true
                    Render-Flyout
                }
            }.GetNewClosure())
        }
        elseif ($enabled) {
            $action = ({ Start-Refresh $true; Render-Flyout }.GetNewClosure())
        }
        else {
            $action = ({ Enable-Service $idForClick }.GetNewClosure())
        }
        $button = New-FlyoutButton $buttonText 16 $script:RenderY $buttonWidth 38 $action
        $button.TextAlign = [Drawing.ContentAlignment]::MiddleLeft
        $button.Padding = [Windows.Forms.Padding]::new(10, 0, 0, 0)
        $button.Font = $script:UiBoldFont
        $script:Content.Controls.Add($button)
        if ($enabled -and $service.Keyed -and $hasKey) {
            $serviceForKey = $service
            $change = New-FlyoutButton 'Change key' 309 $script:RenderY 95 38 ({
                if (Set-ServiceKey $serviceForKey) {
                    Start-Refresh $true
                    Render-Flyout
                }
            }.GetNewClosure())
            $script:Content.Controls.Add($change)
        }
        $script:RenderY += 42
        [void](Add-FlyoutText $service.Description 28 364 $script:UiFont ([Drawing.Color]::DimGray) Left 13)
        $errorText = [string](Get-Value $provider 'error' '')
        if ($errorText) { [void](Add-FlyoutText $errorText 28 364 $script:UiFont ([Drawing.Color]::Firebrick) Left 10) }
    }
    if ($script:ConfigError) { [void](Add-FlyoutText $script:ConfigError 16 388 $script:UiFont ([Drawing.Color]::Firebrick) Left 8) }
}

function Render-SettingsView {
    $config = Get-Config
    $enabledIds = @(Get-EnabledServiceIds $config)
    [void](Add-FlyoutText 'Services' 16 388 $script:UiBoldFont ([Drawing.Color]::Empty) Left 8)
    if ($enabledIds.Count -eq 0) {
        [void](Add-FlyoutText 'No services added.' 16 388 $script:UiFont ([Drawing.Color]::DimGray) Left 10)
    }
    foreach ($service in @($script:Services | Where-Object { $_.Id -in $enabledIds })) {
        [void](Add-FlyoutText $service.Name 20 190 $script:UiBoldFont ([Drawing.Color]::Empty) Left 5)
        $buttonY = $script:RenderY - 29
        if ($service.Keyed) {
            $serviceForKey = $service
            $keyButton = New-FlyoutButton 'Change key' 218 $buttonY 91 30 ({
                if (Set-ServiceKey $serviceForKey) {
                    Start-Refresh $true
                    Render-Flyout
                }
            }.GetNewClosure())
            $script:Content.Controls.Add($keyButton)
        }
        $idForRemove = $service.Id
        $remove = New-FlyoutButton 'Remove' 316 $buttonY 88 30 ({ Remove-Service $idForRemove }.GetNewClosure()) -Danger
        $script:Content.Controls.Add($remove)
        $script:RenderY = [Math]::Max($script:RenderY, $buttonY + 38)
    }
    Add-FlyoutDivider 5 10
    $refreshText = if ($script:RefreshProcess) { 'Refreshing...' } else { 'Refresh' }
    $refresh = New-FlyoutButton $refreshText 16 $script:RenderY 388 34 {
        Start-Refresh $false
        Render-Flyout
    }
    $refresh.Enabled = $null -eq $script:RefreshProcess
    $script:Content.Controls.Add($refresh)
    $script:RenderY += 40
    $force = New-FlyoutButton 'Refresh skipping cooldowns' 16 $script:RenderY 388 34 {
        Start-Refresh $true
        Render-Flyout
    }
    $force.Enabled = $null -eq $script:RefreshProcess
    $script:Content.Controls.Add($force)
    $script:RenderY += 40
    $raw = New-FlyoutButton 'Open raw config' 16 $script:RenderY 388 34 { Open-Config }
    $script:Content.Controls.Add($raw)
    $script:RenderY += 40
    $debug = New-FlyoutButton 'Debug in terminal' 16 $script:RenderY 388 34 { Start-DebugWindow }
    $script:Content.Controls.Add($debug)
    $script:RenderY += 42
    $startup = [Windows.Forms.CheckBox]::new()
    $startup.Text = 'Start with Windows'
    $startup.Location = [Drawing.Point]::new(20, $script:RenderY)
    $startup.Size = [Drawing.Size]::new(384, 28)
    $startup.Checked = Test-Startup
    $startup.Font = $script:UiFont
    $startup.Add_Click({
        param($sender, $eventArgs)
        try { Set-Startup $sender.Checked }
        catch {
            $sender.Checked = -not $sender.Checked
            Show-FlyoutMessage ("Could not change startup settings: " + $_.Exception.Message)
        }
    })
    $script:Content.Controls.Add($startup)
    $script:RenderY += 37
    if ($script:ConfigError) { [void](Add-FlyoutText $script:ConfigError 16 388 $script:UiFont ([Drawing.Color]::Firebrick) Left 8) }
    Add-FlyoutDivider 0 10
    $exit = New-FlyoutButton 'Exit Needle' 16 $script:RenderY 388 34 { [Windows.Forms.Application]::Exit() } -Danger
    $script:Content.Controls.Add($exit)
    $script:RenderY += 44
}

function Position-Flyout {
    if ($null -eq $script:FlyoutAnchor) { return }
    $screen = [Windows.Forms.Screen]::FromPoint($script:FlyoutAnchor)
    $area = $screen.WorkingArea
    $x = $script:FlyoutAnchor.X - $script:Flyout.Width + 12
    $y = if ($script:FlyoutOpensUp) {
        $script:FlyoutAnchor.Y - $script:Flyout.Height - 8
    }
    else {
        $script:FlyoutAnchor.Y + 8
    }
    $x = [Math]::Max($area.Left, [Math]::Min($x, $area.Right - $script:Flyout.Width))
    $y = [Math]::Max($area.Top, [Math]::Min($y, $area.Bottom - $script:Flyout.Height))
    $script:Flyout.Location = [Drawing.Point]::new($x, $y)
}

function Render-Flyout {
    foreach ($control in @($script:Content.Controls)) { $control.Dispose() }
    $script:Content.AutoScrollPosition = [Drawing.Point]::Empty
    $script:RenderY = 16
    Render-Header
    switch ($script:CurrentView) {
        'Add more' { Render-AddServiceView }
        'Settings' { Render-SettingsView }
        default { Render-UsageView }
    }
    $contentHeight = $script:RenderY + 12
    $anchor = if ($null -ne $script:FlyoutAnchor) { $script:FlyoutAnchor } else { [Windows.Forms.Cursor]::Position }
    $screen = [Windows.Forms.Screen]::FromPoint($anchor)
    $area = $screen.WorkingArea
    $available = if ($script:FlyoutOpensUp) {
        [Math]::Min($anchor.Y - 8, $area.Bottom) - $area.Top
    }
    else {
        $area.Bottom - [Math]::Max($anchor.Y + 8, $area.Top)
    }
    $maximum = [Math]::Max(1, [Math]::Min(620, $available))
    $height = [Math]::Min($maximum, [Math]::Max(230, $contentHeight + $script:Header.Height))
    $script:Flyout.ClientSize = [Drawing.Size]::new(420, $height)
    $script:Content.AutoScrollMinSize = [Drawing.Size]::new(0, $contentHeight)
    if ($script:Flyout.Visible) { Position-Flyout }
    $script:Flyout.Invalidate()
}

function Hide-Flyout {
    if ($script:Flyout.Visible) {
        $script:LastFlyoutClose = [Environment]::TickCount
        $script:Flyout.Hide()
    }
}

function Show-Flyout {
    if ($script:Flyout.Visible) {
        Hide-Flyout
        return
    }
    $elapsed = [Environment]::TickCount - $script:LastFlyoutClose
    if ($elapsed -ge 0 -and $elapsed -lt 350) { return }
    $script:CurrentView = 'Usage'
    $cursor = [Windows.Forms.Cursor]::Position
    $screen = [Windows.Forms.Screen]::FromPoint($cursor)
    $area = $screen.WorkingArea
    $script:FlyoutAnchor = $cursor
    $spaceAbove = $cursor.Y - $area.Top
    $spaceBelow = $area.Bottom - $cursor.Y
    $script:FlyoutOpensUp = $spaceAbove -ge $spaceBelow
    Render-Flyout
    Position-Flyout
    $script:Flyout.Show()
    $script:Flyout.Activate()
    [void][NeedleNativeMethods]::SetForegroundWindow($script:Flyout.Handle)
}

function Update-Tray {
    $left = if ($script:Data) { Get-LowestLeft $script:Data } else { $null }
    $icon = New-GaugeIcon $left
    $oldIcon = $script:CurrentIcon
    $script:CurrentIcon = $icon
    $script:Tray.Icon = $icon
    if ($oldIcon) { $oldIcon.Dispose() }
    $tooltip = if ($script:Data) { Get-Summary $script:Data } elseif ($script:RefreshProcess) { 'Needle - refreshing...' } elseif ($script:LastError) { 'Needle - error; click for details' } else { 'Needle' }
    if ($tooltip.Length -gt 63) { $tooltip = $tooltip.Substring(0, 63) }
    $script:Tray.Text = $tooltip
    if ($script:Flyout.Visible) { Render-Flyout }
}

function Start-Refresh {
    param([bool]$SkipCooldowns)
    if ($script:RefreshProcess -and -not $script:RefreshProcess.HasExited) { return }
    $arguments = @($script:Python.Prefix) + @($script:Fetcher)
    if ($SkipCooldowns) { $arguments += '--force' }
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $script:Python.File
    $info.Arguments = (($arguments | ForEach-Object { Quote-ProcessArgument ([string]$_) }) -join ' ')
    $info.WorkingDirectory = Split-Path -Parent $script:Fetcher
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = [Text.Encoding]::UTF8
    $info.StandardErrorEncoding = [Text.Encoding]::UTF8
    $script:LastError = $null
    $script:RefreshProcess = [Diagnostics.Process]::Start($info)
    $script:RefreshOutput = $script:RefreshProcess.StandardOutput.ReadToEndAsync()
    $script:RefreshError = $script:RefreshProcess.StandardError.ReadToEndAsync()
    $script:Tray.Text = 'Needle - refreshing...'
    if ($script:Flyout.Visible) { Render-Flyout }
}

$script:PollTimer = [Windows.Forms.Timer]::new()
$script:PollTimer.Interval = 250
$script:PollTimer.Add_Tick({
    if ($script:StopEvent.WaitOne(0)) {
        [Windows.Forms.Application]::Exit()
        return
    }
    if (-not $script:RefreshProcess -or -not $script:RefreshProcess.HasExited) { return }
    $process = $script:RefreshProcess
    $output = $script:RefreshOutput
    $errorOutput = $script:RefreshError
    $script:RefreshProcess = $null
    $script:RefreshOutput = $null
    $script:RefreshError = $null
    try {
        $stdout = $output.GetAwaiter().GetResult()
        $stderr = $errorOutput.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) {
            $script:LastError = if ($stderr.Trim()) { $stderr.Trim() } else { "The fetcher exited with code $($process.ExitCode)." }
        }
        elseif (-not $stdout.Trim()) {
            $script:LastError = 'The fetcher returned no data.'
        }
        else {
            try { $script:Data = $stdout | ConvertFrom-Json }
            catch { $script:LastError = 'The fetcher returned unreadable data.' }
        }
    }
    finally {
        $process.Dispose()
        Update-Tray
    }
})

$script:RefreshTimer = [Windows.Forms.Timer]::new()
$script:RefreshTimer.Interval = 10 * 60 * 1000
$script:RefreshTimer.Add_Tick({ Start-Refresh $false })
$script:Flyout.Add_Deactivate({
    if (-not $script:AllowDeactivate) { Hide-Flyout }
})
$script:Flyout.Add_KeyDown({
    param($sender, $eventArgs)
    if ($eventArgs.KeyCode -eq [Windows.Forms.Keys]::Escape) {
        $eventArgs.SuppressKeyPress = $true
        Hide-Flyout
    }
})
$script:Tray.Add_MouseClick({
    param($sender, $eventArgs)
    if ($eventArgs.Button -eq [Windows.Forms.MouseButtons]::Left -or $eventArgs.Button -eq [Windows.Forms.MouseButtons]::Right) {
        if (-not $script:Flyout.Visible -and $script:Data) {
            $updated = [double](Get-Value $script:Data 'updated' 0)
            if ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - $updated -ge 300) { Start-Refresh $false }
        }
        Show-Flyout
    }
})

try {
    Update-Tray
    Start-Refresh $Force.IsPresent
    $script:PollTimer.Start()
    $script:RefreshTimer.Start()
    [Windows.Forms.Application]::Run()
}
finally {
    $script:PollTimer.Stop()
    $script:RefreshTimer.Stop()
    $refreshStopped = $true
    if ($script:RefreshProcess) {
        if (-not $script:RefreshProcess.HasExited) {
            try { & "$env:SystemRoot\System32\taskkill.exe" /PID $script:RefreshProcess.Id /T /F *> $null } catch {}
            try { $refreshStopped = $script:RefreshProcess.WaitForExit(5000) } catch { $refreshStopped = $false }
        }
        try { $script:RefreshProcess.Dispose() } catch {}
    }
    $script:Tray.Visible = $false
    $script:Tray.Dispose()
    $script:Flyout.Dispose()
    if ($script:CurrentIcon) { $script:CurrentIcon.Dispose() }
    $script:UiFont.Dispose()
    $script:UiBoldFont.Dispose()
    $script:UiTitleFont.Dispose()
    $script:UiMonoFont.Dispose()
    $script:PollTimer.Dispose()
    $script:RefreshTimer.Dispose()
    $script:Mutex.ReleaseMutex()
    $script:Mutex.Dispose()
    if ($refreshStopped) { [void]$script:StoppedEvent.Set() }
    $script:StopEvent.Dispose()
    $script:StoppedEvent.Dispose()
}
