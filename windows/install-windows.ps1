param(
    [switch]$NoStartup,
    [switch]$NoLaunch
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'This installer requires Windows.'
}

function Test-Python3 {
    param([string]$File, [string[]]$Prefix)
    try {
        & $File @Prefix -c 'import sys; raise SystemExit(sys.version_info.major != 3)' *> $null
        return $LASTEXITCODE -eq 0
    }
    catch { return $false }
}

function Stop-NeedleTray {
    param([string]$Sid)
    $eventName = "Local\NeedleTrayStop-$Sid"
    $mutexName = "Local\NeedleTray-$Sid"
    $stoppedName = "Local\NeedleTrayStopped-$Sid"
    try { $stopEvent = [Threading.EventWaitHandle]::OpenExisting($eventName) }
    catch [Threading.WaitHandleCannotBeOpenedException] { return }
    try { $stoppedEvent = [Threading.EventWaitHandle]::OpenExisting($stoppedName) }
    catch [Threading.WaitHandleCannotBeOpenedException] { $stoppedEvent = $null }
    [void]$stopEvent.Set()
    $stopEvent.Dispose()
    if ($stoppedEvent) {
        $stopped = $stoppedEvent.WaitOne(10000)
        $stoppedEvent.Dispose()
        if (-not $stopped) { throw 'The running Needle tray app could not finish shutting down.' }
        return
    }
    # Upgrade path for tray versions installed before the stopped acknowledgment existed.
    for ($attempt = 0; $attempt -lt 50; $attempt++) {
        Start-Sleep -Milliseconds 100
        try { $runningMutex = [Threading.Mutex]::OpenExisting($mutexName) }
        catch [Threading.WaitHandleCannotBeOpenedException] { return }
        $runningMutex.Dispose()
    }
    throw 'The running Needle tray app did not stop. Exit it from the tray menu, then try again.'
}

$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$sourceFetcher = Join-Path $root 'fetcher\needle.py'
$sourceConfig = Join-Path $root 'fetcher\config.example.json'
$sourceTray = Join-Path $PSScriptRoot 'needle-tray.ps1'
$appRoot = Join-Path $env:LOCALAPPDATA 'Needle'
$binDir = Join-Path $appRoot 'bin'
$fetcher = Join-Path $binDir 'needle.py'
$tray = Join-Path $appRoot 'needle-tray.ps1'
$configDir = Join-Path $env:APPDATA 'Needle'
$config = Join-Path $configDir 'config.json'
$startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'Needle.lnk'

foreach ($source in @($sourceFetcher, $sourceConfig, $sourceTray)) {
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Required install file is missing: $source"
    }
}

$python = $null
$candidate = Get-Command py.exe -ErrorAction SilentlyContinue
if ($candidate -and (Test-Python3 $candidate.Source @('-3'))) { $python = $candidate }
if (-not $python) {
    $candidate = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($candidate -and (Test-Python3 $candidate.Source @())) { $python = $candidate }
}
if (-not $python) {
    throw 'Python 3 is required. Install it from python.org or the Microsoft Store, then run this installer again.'
}

$powerShell = Get-Command pwsh.exe -ErrorAction SilentlyContinue
if (-not $powerShell) { $powerShell = Get-Command powershell.exe -ErrorAction Stop }

$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
Stop-NeedleTray $sid

[IO.Directory]::CreateDirectory($binDir) | Out-Null
[IO.Directory]::CreateDirectory($configDir) | Out-Null
Copy-Item -LiteralPath $sourceFetcher -Destination $fetcher -Force
Copy-Item -LiteralPath $sourceTray -Destination $tray -Force
if (-not (Test-Path -LiteralPath $config)) {
    Copy-Item -LiteralPath $sourceConfig -Destination $config
    $newConfig = $true
}
else {
    $newConfig = $false
}

if ($NoStartup) {
    if (Test-Path -LiteralPath $startup) { Remove-Item -LiteralPath $startup -Force }
}
else {
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($startup)
    $shortcut.TargetPath = $powerShell.Source
    $shortcut.Arguments = '-NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $tray + '"'
    $shortcut.WorkingDirectory = $appRoot
    $shortcut.Description = 'Needle AI usage monitor'
    $shortcut.Save()
}

if (-not $NoLaunch) {
    $arguments = '-NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $tray + '"'
    $trayProcess = Start-Process -FilePath $powerShell.Source -ArgumentList $arguments -WindowStyle Hidden -PassThru
    Start-Sleep -Milliseconds 750
    if ($trayProcess.HasExited) {
        throw 'The Needle tray app exited during startup. Run it from a terminal to see the error.'
    }
}

Write-Host 'Needle for Windows is installed.'
Write-Host "  Tray     $tray"
Write-Host "  Fetcher  $fetcher"
Write-Host "  Settings $config"
Write-Host "  Startup  $(if ($NoStartup) { 'disabled' } else { 'enabled' })"
if ($newConfig) {
    Write-Host ''
    Write-Host 'Click Needle in the notification area and choose Add more.'
}
