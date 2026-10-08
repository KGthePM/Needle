# -Update replaces an existing install and keeps the current start-at-sign-in choice
# (Needle's Update button uses it).
param(
    [switch]$NoStartup,
    [switch]$NoLaunch,
    [switch]$Update
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

function Build-NeedleLauncher {
    param([string]$Source, [string]$Icon, [string]$Destination)
    $compiler = @(
        (Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'),
        (Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe')
    ) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if (-not $compiler) { throw 'The Windows .NET Framework compiler was not found.' }
    $temporary = $Destination + '.' + $PID + '.tmp.exe'
    try {
        & $compiler /nologo /target:winexe /optimize+ "/win32icon:$Icon" "/out:$temporary" $Source
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $temporary -PathType Leaf)) {
            throw 'The Needle Windows launcher could not be built.'
        }
        Move-Item -LiteralPath $temporary -Destination $Destination -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$sourceFetcher = Join-Path $root 'fetcher\needle.py'
$sourceConfig = Join-Path $root 'fetcher\config.example.json'
$sourceTray = Join-Path $PSScriptRoot 'needle-tray.ps1'
$sourceLauncher = Join-Path $PSScriptRoot 'needle-launcher.cs'
$sourceIcon = Join-Path $PSScriptRoot 'needle.ico'
$appRoot = Join-Path $env:LOCALAPPDATA 'Needle'
$binDir = Join-Path $appRoot 'bin'
$fetcher = Join-Path $binDir 'needle.py'
$tray = Join-Path $appRoot 'needle-tray.ps1'
$launcher = Join-Path $appRoot 'Needle.exe'
$legacyLauncher = Join-Path $appRoot 'needle-launcher.exe'
$configDir = Join-Path $env:APPDATA 'Needle'
$config = Join-Path $configDir 'config.json'
$startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'Needle.lnk'
$startMenu = Join-Path ([Environment]::GetFolderPath('Programs')) 'Needle.lnk'

foreach ($source in @($sourceFetcher, $sourceConfig, $sourceTray, $sourceLauncher, $sourceIcon)) {
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

if ($Update -and -not (Test-Path -LiteralPath $startup)) { $NoStartup = [switch]$true }

$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
Stop-NeedleTray $sid

[IO.Directory]::CreateDirectory($binDir) | Out-Null
[IO.Directory]::CreateDirectory($configDir) | Out-Null
Copy-Item -LiteralPath $sourceFetcher -Destination $fetcher -Force
Copy-Item -LiteralPath $sourceTray -Destination $tray -Force
Build-NeedleLauncher $sourceLauncher $sourceIcon $launcher
if (Test-Path -LiteralPath $legacyLauncher) { Remove-Item -LiteralPath $legacyLauncher -Force }
if (-not (Test-Path -LiteralPath $config)) {
    Copy-Item -LiteralPath $sourceConfig -Destination $config
    $newConfig = $true
}
else {
    $newConfig = $false
}

$shell = New-Object -ComObject WScript.Shell
if ($NoStartup) {
    if (Test-Path -LiteralPath $startup) { Remove-Item -LiteralPath $startup -Force }
}
else {
    $shortcut = $shell.CreateShortcut($startup)
    $shortcut.TargetPath = $launcher
    $shortcut.Arguments = ''
    $shortcut.WorkingDirectory = $appRoot
    $shortcut.Description = 'Needle AI usage monitor'
    $shortcut.IconLocation = $launcher + ',0'
    $shortcut.Save()
}

$shortcut = $shell.CreateShortcut($startMenu)
$shortcut.TargetPath = $launcher
$shortcut.Arguments = ''
$shortcut.WorkingDirectory = $appRoot
$shortcut.Description = 'Needle AI usage monitor'
$shortcut.IconLocation = $launcher + ',0'
$shortcut.Save()

if (-not $NoLaunch) {
    $launcherProcess = Start-Process -FilePath $launcher -PassThru
    if (-not $launcherProcess.WaitForExit(5000)) {
        throw 'The Needle launcher did not finish starting the tray app.'
    }
    if ($launcherProcess.ExitCode -ne 0) {
        throw 'The Needle tray app could not be started.'
    }
}

Write-Host 'Needle for Windows is installed.'
if ($NoLaunch) {
    Write-Host 'Open Needle from the Start menu when you are ready.'
}
else {
    Write-Host 'Needle is running independently. You can close this PowerShell window.'
}
if ($Update) { exit 0 }
Write-Host "  Tray     $tray"
Write-Host "  Fetcher  $fetcher"
Write-Host "  Settings $config"
Write-Host "  Startup  $(if ($NoStartup) { 'disabled' } else { 'enabled' })"
if ($newConfig) {
    Write-Host ''
    Write-Host 'Click Needle in the notification area and choose Add more.'
}
