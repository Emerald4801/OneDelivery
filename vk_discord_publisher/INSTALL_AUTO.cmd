@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 goto failed
pause
exit /b 0
:failed
echo Setup stopped. Automatic publishing was not enabled.
pause
exit /b 1
