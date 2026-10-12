# Build script: bakes the ID / Rendezvous server from local config.json into the EXE
# (build_config.py is gitignored, so the server address never lands in git)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$relay = "ws://localhost:8765"
if (Test-Path config.json) {
    $relay = (Get-Content config.json -Raw | ConvertFrom-Json).relay_server
}
"DEFAULT_RELAY = `"$relay`"" | Out-File -Encoding utf8 build_config.py
Write-Host "Baking ID server into EXE: $relay"

$timestamp = Get-Date -Format "HHmmss"
$outDir = "dist_$timestamp"
python main.py --export-icons
pyinstaller --noconfirm --onefile --windowed --name "DeskLite" --icon desklite.ico --hidden-import build_config --uac-admin `
    --exclude-module tkinter `
    --exclude-module PySide6.QtQml `
    --exclude-module PySide6.QtQuick `
    --exclude-module PySide6.QtQuickWidgets `
    --exclude-module PySide6.QtNetwork `
    --exclude-module PySide6.QtSql `
    --exclude-module PySide6.QtSvg `
    --exclude-module PySide6.QtOpenGL `
    --exclude-module PySide6.QtWebEngineCore `
    --exclude-module PySide6.QtWebEngineWidgets `
    --exclude-module PySide6.QtMultimedia `
    --distpath $outDir main.py

New-Item -ItemType Directory -Force server/web/download | Out-Null
Copy-Item "$outDir/DeskLite.exe" server/web/download/DeskLite.exe -Force
Copy-Item "$outDir/DeskLite.exe" server/web/download/RustDeskLight.exe -Force
if (Test-Path server/web/download/RustDeskLight-Client.zip) {
    Remove-Item server/web/download/RustDeskLight-Client.zip -Force
}
Remove-Item -Recurse -Force $outDir -ErrorAction SilentlyContinue
Write-Host "Done: server/web/download/DeskLite.exe"
