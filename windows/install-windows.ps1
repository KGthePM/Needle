<#
Installs the Needle tray app on Windows.
Usage (from the Needle folder):
  powershell -ExecutionPolicy Bypass -File .\windows\install-windows.ps1
  -Update replaces an existing install without asking anything (Needle's Update item uses it).
Keep this file ASCII: Windows PowerShell 5.1 reads scripts without a BOM as ANSI.
#>
param([switch]$Update)
$ErrorActionPreference = "Stop"

$Here = $PSScriptRoot
$Root = Split-Path $Here -Parent
$UserHome = [Environment]::GetFolderPath("UserProfile")
$Bin = Join-Path $UserHome ".local\bin\needle"
$ConfDir = Join-Path $UserHome ".config\needle"
$Conf = Join-Path $ConfDir "config.json"
$AppDir = Join-Path $env:LOCALAPPDATA "Needle"
$Tray = Join-Path $AppDir "needle-tray.ps1"
$Shortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "Needle.lnk"
$PowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

# 1. Python 3.7 or newer. The Microsoft Store "python" alias only opens the Store, so a
#    candidate counts only if it actually runs and reports where it lives.
function Find-Python {
    foreach ($candidate in @(@("py", "-3"), @("python"), @("python3"))) {
        $exe = $candidate[0]
        $rest = @($candidate | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $found = & $exe @rest -c "import sys; assert sys.version_info >= (3, 7); print(sys.executable)" 2>$null
        } catch { continue }
        if ($LASTEXITCODE -eq 0 -and $found -and (Test-Path -LiteralPath "$found".Trim())) { return "$found".Trim() }
    }
    return $null
}

$Python = Find-Python
if (-not $Python) {
    Write-Host "Needle needs Python 3, and it isn't installed yet."
    if (-not $Update -and (Get-Command winget -ErrorAction SilentlyContinue)) {
        $answer = Read-Host "Install it now with winget? [Y/n]"
        if ($answer -notmatch "^[Nn]") {
            winget install -e --id Python.Python.3.12 --scope user
            Write-Host
            Write-Host "Open a new PowerShell window (so it can find Python) and run this script again."
            exit 1
        }
    }
    Write-Host "Install Python 3 from https://www.python.org/downloads/ and run this script again."
    exit 1
}

# 2. Stop a tray that's already running, so the new copy replaces it.
Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe' OR Name = 'pwsh.exe'" |
    Where-Object { $_.CommandLine -like "*needle-tray.ps1*" -and $_.ProcessId -ne $PID } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# 3. Fetcher, tray app, keys file.
foreach ($dir in @((Split-Path $Bin), $ConfDir, $AppDir)) { [void](New-Item -ItemType Directory -Force -Path $dir) }
Copy-Item -LiteralPath (Join-Path $Root "fetcher\needle.py") -Destination $Bin -Force
Copy-Item -LiteralPath (Join-Path $Here "needle-tray.ps1") -Destination $Tray -Force
$NewConf = $false
if (-not (Test-Path -LiteralPath $Conf)) {
    Copy-Item -LiteralPath (Join-Path $Root "fetcher\config.example.json") -Destination $Conf
    $NewConf = $true
}

$TrayArgs = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Tray`" -Python `"$Python`""

# 4. Start at sign-in, so Needle is in the tray after a restart. An existing shortcut is
#    refreshed quietly, in case Python moved.
$wantShortcut = Test-Path -LiteralPath $Shortcut
if (-not $wantShortcut -and -not $Update) {
    Write-Host
    $answer = Read-Host "Start Needle when you sign in, so it's there after a restart? [Y/n]"
    $wantShortcut = $answer -notmatch "^[Nn]"
}
if ($wantShortcut) {
    $link = (New-Object -ComObject WScript.Shell).CreateShortcut($Shortcut)
    $link.TargetPath = $PowerShell
    $link.Arguments = $TrayArgs
    $link.WorkingDirectory = $AppDir
    $link.WindowStyle = 7  # minimized, so no window flashes up at sign-in
    $link.Description = "Needle: AI plan usage in the tray"
    $link.Save()
    Write-Host "Needle will start when you sign in."
}

# 5. Start it now.
Start-Process -FilePath $PowerShell -ArgumentList $TrayArgs -WindowStyle Hidden

Write-Host
Write-Host "Installed."
if ($Update) { exit 0 }
Write-Host "  Tray     $Tray"
Write-Host "  Fetcher  $Bin"
Write-Host "  Keys     $Conf"
Write-Host "  Python   $Python"
Write-Host
Write-Host "Windows may tuck the new icon under the ^ arrow by the clock. Drag it onto the taskbar to keep it in view."
if ($NewConf) {
    Write-Host
    Write-Host "Next: add your z.ai and OpenRouter keys (right-click the icon > Edit keys). Claude and Codex need nothing."
}
