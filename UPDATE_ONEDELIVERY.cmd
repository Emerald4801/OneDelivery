@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "ONED_PY=%LOCALAPPDATA%\VKDiscordPublisher\.venv\Scripts\python.exe"
if not exist "%ONED_PY%" (
    echo Installed OneDelivery v9 was not found. No files were changed.
    pause
    exit /b 1
)
"%ONED_PY%" "%~dp0onedelivery_update\update_settings.py" %*
set "ONED_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %ONED_EXIT%
