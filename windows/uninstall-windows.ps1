param([switch]$Purge)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'This uninstaller requires Windows.'
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

$appRoot = Join-Path $env:LOCALAPPDATA 'Needle'
$configDir = Join-Path $env:APPDATA 'Needle'
$startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'Needle.lnk'
$startMenu = Join-Path ([Environment]::GetFolderPath('Programs')) 'Needle.lnk'
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value

Stop-NeedleTray $sid

if (Test-Path -LiteralPath $startup) { Remove-Item -LiteralPath $startup -Force }
if (Test-Path -LiteralPath $startMenu) { Remove-Item -LiteralPath $startMenu -Force }
if (Test-Path -LiteralPath $appRoot) { Remove-Item -LiteralPath $appRoot -Recurse -Force }
if ($Purge) {
    if (Test-Path -LiteralPath $configDir) { Remove-Item -LiteralPath $configDir -Recurse -Force }
    Write-Host 'Needle was removed, including its settings and keys.'
}
else {
    Write-Host "Needle was removed. Settings and keys were kept in $configDir (use -Purge to delete them)."
}
