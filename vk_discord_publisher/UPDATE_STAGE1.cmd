@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0update_stage1.ps1"
if errorlevel 1 (
  echo Update failed. See the message above. Configuration and keys were preserved.
  pause
  exit /b 1
)
pause
