# Update Needle from this checkout while keeping settings and startup preference.
param([switch]$NoLaunch)

$ErrorActionPreference = 'Stop'

& (Join-Path $PSScriptRoot 'install-windows.ps1') -Update -NoLaunch:$NoLaunch
exit $LASTEXITCODE
