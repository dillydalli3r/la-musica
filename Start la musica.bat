@echo off
rem Start la musica — system tray app (taskbar icon).
rem Starts the backend if needed, opens the browser, and keeps running
rem in the tray: Open / Restart / Auto-start on login / Exit.
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo Python not found on PATH. Install Python from https://www.python.org/
    pause
    exit /b 1
)
where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw tray.py
    exit /b 0
)
start "" /min python tray.py
exit /b 0
