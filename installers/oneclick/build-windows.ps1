# Build the SAE Books one-click community server .exe for Windows.
#
# Usage (from a checkout where saebooks and saebooks-web are siblings):
#   powershell -ExecutionPolicy Bypass -File installers\oneclick\build-windows.ps1
#
# Needs: Python 3.12 (py -3.12), curl.exe, tar.exe (both ship with Win10+).
# Output: installers\oneclick\dist\SAEBooks.exe
#
# The exe is UNSIGNED — SmartScreen will warn on first run ("More info" →
# "Run anyway"). Code-signing is a release-process decision, not a build step.
$ErrorActionPreference = "Stop"

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$EngineRepo = (Resolve-Path (Join-Path $Here "..\..")).Path
$WebRepo = if ($env:SAEBOOKS_WEB_REPO) { $env:SAEBOOKS_WEB_REPO } else { (Resolve-Path (Join-Path $EngineRepo "..\saebooks-web")).Path }
$BuildVenv = Join-Path $Here ".build-venv"
$Tools = Join-Path $Here ".build-tools"
$ConnecpyVersion = "2.3.0"
$TailwindVersion = "v3.4.17"

Write-Host "== engine: $EngineRepo"
Write-Host "== web:    $WebRepo"

if (Test-Path $BuildVenv) { Remove-Item -Recurse -Force $BuildVenv }
py -3.12 -m venv $BuildVenv
$Py = Join-Path $BuildVenv "Scripts\python.exe"
& $Py -m pip install --quiet --upgrade pip
& $Py -m pip install --quiet pyinstaller grpcio-tools

New-Item -ItemType Directory -Force -Path $Tools | Out-Null

# --- protoc-gen-connecpy plugin ----------------------------------------------
$Plugin = Join-Path $Tools "protoc-gen-connecpy.exe"
if (-not (Test-Path $Plugin)) {
    $Zip = Join-Path $Tools "connecpy.zip"
    curl.exe -fsSL -o $Zip "https://github.com/i2y/connecpy/releases/download/v$ConnecpyVersion/protoc-gen-connecpy_${ConnecpyVersion}_windows_amd64.zip"
    Expand-Archive -Path $Zip -DestinationPath $Tools -Force
    Remove-Item $Zip
}
$env:PATH = "$Tools;$env:PATH"

# --- grpc stubs into the repo BEFORE the non-editable install ----------------
Push-Location $EngineRepo
New-Item -ItemType Directory -Force -Path "saebooks\grpc_gen" | Out-Null
& $Py -m grpc_tools.protoc -I saebooks/proto `
    --python_out=saebooks/grpc_gen `
    --grpc_python_out=saebooks/grpc_gen `
    --connecpy_out=saebooks/grpc_gen `
    saebooks/proto/saebooks.proto
if ($LASTEXITCODE -ne 0) { throw "protoc failed" }
New-Item -ItemType File -Force -Path "saebooks\grpc_gen\__init__.py" | Out-Null
foreach ($f in @("saebooks\grpc_gen\saebooks_pb2_grpc.py", "saebooks\grpc_gen\saebooks_connecpy.py")) {
    (Get-Content $f) -replace '^import saebooks_pb2', 'from saebooks.grpc_gen import saebooks_pb2' | Set-Content $f
}
Pop-Location

# Non-editable installs: PyInstaller cannot collect PEP 660 editable packages.
& $Py -m pip install --quiet $EngineRepo $WebRepo

# --- tailwind.css ------------------------------------------------------------
$Tailwind = Join-Path $Tools "tailwindcss.exe"
if (-not (Test-Path $Tailwind)) {
    curl.exe -fsSL -o $Tailwind "https://github.com/tailwindlabs/tailwindcss/releases/download/$TailwindVersion/tailwindcss-windows-x64.exe"
}
Push-Location $WebRepo
& $Tailwind -c tailwind.config.js -i .\assets\tailwind.css -o .\static\tailwind.css --minify
if ($LASTEXITCODE -ne 0) { throw "tailwindcss failed" }
Pop-Location

# --- freeze ------------------------------------------------------------------
Push-Location $Here
$env:SAEBOOKS_WEB_REPO = $WebRepo
& (Join-Path $BuildVenv "Scripts\pyinstaller.exe") --distpath (Join-Path $Here "dist") --workpath (Join-Path $Here ".build") --noconfirm saebooks-oneclick.spec
if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed" }
Pop-Location

$Bin = Join-Path $Here "dist\SAEBooks.exe"
Write-Host ""
Write-Host "== built: $Bin ($([math]::Round((Get-Item $Bin).Length/1MB)) MB)"
Get-FileHash -Algorithm SHA256 $Bin | Format-List
