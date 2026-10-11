@echo off
title DeskLite
cd /d "%~dp0"

python main.py
if errorlevel 1 (
    if exist "%~dp0DeskLite.exe" (
        start "" "%~dp0DeskLite.exe"
    ) else (
        echo.
        echo Fehler beim Starten von DeskLite.
        pause
    )
)

