@echo off
title DeskLite
cd /d "%~dp0"

if exist "%~dp0DeskLite.exe" (
    start "" "%~dp0DeskLite.exe"
    exit /b
)

if exist "%~dp0dist_build\DeskLite.exe" (
    start "" "%~dp0dist_build\DeskLite.exe"
    exit /b
)

if exist "%~dp0RustDeskLight.exe" (
    start "" "%~dp0RustDeskLight.exe"
    exit /b
)

python main.py
if errorlevel 1 (
    echo.
    echo Ein Fehler ist beim Starten aufgetreten.
    pause
)
