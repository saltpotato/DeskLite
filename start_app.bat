@echo off
title RustDesk Light
cd /d "%~dp0"

if exist "RustDeskLight.exe" (
    start "" "%~dp0RustDeskLight.exe"
    exit /b
)

if exist "%~dp0dist_build\RustDeskLight.exe" (
    start "" "%~dp0dist_build\RustDeskLight.exe"
    exit /b
)

if exist "%~dp0dist\RustDeskLight.exe" (
    start "" "%~dp0dist\RustDeskLight.exe"
    exit /b
)

python main.py
if errorlevel 1 (
    echo.
    echo Ein Fehler ist beim Starten aufgetreten.
    pause
)
