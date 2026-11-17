@echo off
setlocal
cd /d "%~dp0"
title comfy-vault

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found in PATH.
  echo         Install Python 3.9+ from https://www.python.org/downloads/
  echo         During install, tick "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

echo Starting comfy-vault ...
echo Web UI : http://127.0.0.1:8791   ^(change "port" in config.json^)
echo Press Ctrl+C to stop the server.
echo.
python app.py
echo.
echo Server stopped.
pause
