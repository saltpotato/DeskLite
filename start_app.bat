@echo off
title RustDesk Light
cd /d "%~dp0"
python main.py
if errorlevel 1 pause
