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

pyinstaller --noconfirm --onefile --windowed --name "DeskLite" --hidden-import build_config --uac-admin --distpath dist_build main.py

New-Item -ItemType Directory -Force server/web/download | Out-Null
Copy-Item dist_build/DeskLite.exe server/web/download/DeskLite.exe -Force
# Keep a backward-compatibility copy in download folder
Copy-Item dist_build/DeskLite.exe server/web/download/RustDeskLight.exe -Force
if (Test-Path server/web/download/RustDeskLight-Client.zip) {
    Remove-Item server/web/download/RustDeskLight-Client.zip -Force
}
Write-Host "Done: server/web/download/DeskLite.exe"
