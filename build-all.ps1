# Installs everything needed (via winget / rustup) and builds the Glassplayer installer.
# Run from this folder:  powershell -ExecutionPolicy Bypass -File .\build-all.ps1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Refresh { $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User') + ';' + "$env:USERPROFILE\.cargo\bin" }
function Have($c) { [bool](Get-Command $c -ErrorAction SilentlyContinue) }
function Step($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }

if (-not (Test-Path 'original.html')) { Write-Host 'Put your page in this folder and name it original.html first.' -ForegroundColor Red; exit 1 }
if (-not (Have 'winget')) { Write-Host 'winget not found. Update "App Installer" from the Microsoft Store, then rerun.' -ForegroundColor Red; exit 1 }
Refresh

Step '1/6 Microsoft C++ build tools'
$vsw = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$hasVC = (Test-Path $vsw) -and (& $vsw -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath)
if ($hasVC) { Write-Host 'Already installed.' }
else {
  winget install --id Microsoft.VisualStudio.2022.BuildTools -e --accept-package-agreements --accept-source-agreements --override "--passive --wait --norestart --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
}

Step '2/6 Rust'
if (Have 'cargo') { Write-Host 'Already installed.' }
else {
  Invoke-WebRequest 'https://win.rustup.rs/x86_64' -OutFile "$env:TEMP\rustup-init.exe"
  & "$env:TEMP\rustup-init.exe" -y --default-toolchain stable-x86_64-pc-windows-msvc
  Refresh
}

Step '3/6 Python'
$py = $null
foreach ($c in 'python','py') { if (Have $c) { try { & $c --version *> $null; if ($LASTEXITCODE -eq 0) { $py = $c; break } } catch {} } }
if (-not $py) {
  winget install --id Python.Python.3.12 -e --accept-package-agreements --accept-source-agreements
  Refresh
  foreach ($c in 'python','py') { if (Have $c) { try { & $c --version *> $null; if ($LASTEXITCODE -eq 0) { $py = $c; break } } catch {} } }
}
if (-not $py) { Write-Host 'Python installed but not found yet. Close this window, open a new terminal and rerun the script.' -ForegroundColor Yellow; exit 1 }

Step '4/6 WebView2 runtime (already on Windows 11, harmless if present)'
winget install --id Microsoft.EdgeWebView2Runtime -e --accept-package-agreements --accept-source-agreements --silent

Step '5/6 Tauri tool (slow the first time)'
cargo tauri --version *> $null
if ($LASTEXITCODE -ne 0) { cargo install tauri-cli --version "^2" --locked }

Step '6/6 Building Glassplayer (first build takes a while)'
& $py patch.py
if ($LASTEXITCODE -ne 0) { exit 1 }
cargo tauri build
if ($LASTEXITCODE -ne 0) { Write-Host 'Build failed - copy the red error text and send it to Claude.' -ForegroundColor Red; exit 1 }

Write-Host "`nDone! Opening the installer folder..." -ForegroundColor Green
explorer "$PSScriptRoot\src-tauri\target\release\bundle\nsis"
