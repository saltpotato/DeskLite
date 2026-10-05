# Build script: bakes the relay server from local config.json into the EXE
# (build_config.py is gitignored, so the server address never lands in git)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$relay = "ws://localhost:8765"
if (Test-Path config.json) {
    $relay = (Get-Content config.json -Raw | ConvertFrom-Json).relay_server
}
"DEFAULT_RELAY = `"$relay`"" | Out-File -Encoding utf8 build_config.py
Write-Host "Baking relay server into EXE: $relay"

pyinstaller --noconfirm --onefile --windowed --name "RustDeskLight" --hidden-import build_config main.py

New-Item -ItemType Directory -Force server/web/download | Out-Null
Copy-Item dist/RustDeskLight.exe server/web/download/RustDeskLight.exe -Force
Compress-Archive -Force -Path dist/RustDeskLight.exe, start_app.bat, README.md -DestinationPath server/web/download/RustDeskLight-Client.zip
Write-Host "Done: server/web/download/"
