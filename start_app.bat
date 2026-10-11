@echo off
cd /d "%~dp0"

where pythonw >nul 2>&1
if %errorlevel% equ 0 (
    start "" pythonw "%~dp0main.py"
    exit /b 0
)

where python >nul 2>&1
if %errorlevel% equ 0 (
    start "" python "%~dp0main.py"
    exit /b 0
)

if exist "%~dp0DeskLite.exe" (
    start "" "%~dp0DeskLite.exe"
    exit /b 0
)

echo.
echo Failed to start DeskLite.
pause
