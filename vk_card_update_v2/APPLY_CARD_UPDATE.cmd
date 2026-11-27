@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0apply_card_update.ps1"
if errorlevel 1 (
  echo Card update stopped. See the message above.
  pause
  exit /b 1
)
pause
