@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0update_stage2.ps1"
if errorlevel 1 (
  echo Update failed. See the message above.
  pause
  exit /b 1
)
pause
