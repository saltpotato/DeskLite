#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "Creating macOS Python virtual environment (.venv)..."
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -r requirements.txt
fi

if [ "$1" = "--build" ]; then
    echo "Building native macOS DeskLite.app bundle..."
    .venv/bin/pip install pyinstaller
    .venv/bin/pyinstaller --noconfirm --windowed --onedir --name "DeskLite" main.py
    echo "Done! Native macOS bundle created at: dist/DeskLite.app"
    exit 0
fi

echo "Starting DeskLite (logs written to $(pwd)/desklite.log)..."
nohup .venv/bin/python main.py >/dev/null 2>&1 &
exit 0
