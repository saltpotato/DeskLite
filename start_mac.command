#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "Creating macOS Python virtual environment (.venv)..."
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -r requirements.txt
elif ! .venv/bin/python -c "import PySide6.QtWidgets, aiortc" >/dev/null 2>&1; then
    echo "Updating macOS dependencies (PySide6-Essentials / WebRTC)..."
    .venv/bin/pip install -r requirements.txt
fi

if [ ! -f "desklite.icns" ]; then
    .venv/bin/python main.py --export-icons >/dev/null 2>&1 || true
    osascript -l JavaScript -e "ObjC.import('Cocoa'); $.NSWorkspace.sharedWorkspace.setIconForFileOptions($.NSImage.alloc.initWithContentsOfFile('$(pwd)/desklite.icns'), '$(pwd)/start_mac.command', 0);" >/dev/null 2>&1 || true
fi

if [ "$1" = "--build" ]; then
    echo "Building slim native macOS DeskLite.app bundle..."
    .venv/bin/python main.py --export-icons
    .venv/bin/pip install pyinstaller
    .venv/bin/pyinstaller --noconfirm --windowed --onedir --name "DeskLite" --icon desklite.icns \
        --exclude-module tkinter \
        --exclude-module PySide6.QtQml \
        --exclude-module PySide6.QtQuick \
        --exclude-module PySide6.QtQuickWidgets \
        --exclude-module PySide6.QtNetwork \
        --exclude-module PySide6.QtSql \
        --exclude-module PySide6.QtSvg \
        --exclude-module PySide6.QtOpenGL \
        --exclude-module PySide6.QtWebEngineCore \
        --exclude-module PySide6.QtWebEngineWidgets \
        --exclude-module PySide6.QtMultimedia \
        main.py
    echo "Done! Slim native macOS bundle created at: dist/DeskLite.app"
    exit 0
fi

echo "Starting DeskLite (Qt6 Retina UI - logs in $(pwd)/desklite.log)..."
nohup .venv/bin/python main.py >/dev/null 2>&1 &
exit 0
