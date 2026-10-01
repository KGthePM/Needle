<#
Removes the tray app and fetcher. Keys are kept unless you pass -Purge.
Usage: powershell -ExecutionPolicy Bypass -File .\windows\uninstall-windows.ps1 [-Purge]
#>
param([switch]$Purge)

$UserHome = [Environment]::GetFolderPath("UserProfile")

Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe' OR Name = 'pwsh.exe'" |
    Where-Object { $_.CommandLine -like "*needle-tray.ps1*" -and $_.ProcessId -ne $PID } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

$paths = @(
    (Join-Path ([Environment]::GetFolderPath("Startup")) "Needle.lnk"),
    (Join-Path $env:LOCALAPPDATA "Needle"),
    (Join-Path $UserHome ".local\bin\needle"),
    (Join-Path $UserHome ".cache\needle")
)
if ($Purge) { $paths += Join-Path $UserHome ".config\needle" }
foreach ($p in $paths) { Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue }

if ($Purge) { Write-Host "Removed, including keys." }
else { Write-Host "Removed. Keys kept in ~\.config\needle (use -Purge to delete them)." }
