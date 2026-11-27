@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
".venv\Scripts\python.exe" -u control_client.py pause
pause
