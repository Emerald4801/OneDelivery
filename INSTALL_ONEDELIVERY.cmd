@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "ONED_PY=%LOCALAPPDATA%\VKDiscordPublisher\.venv\Scripts\python.exe"
if not exist "%ONED_PY%" (
    echo Установленная OneDelivery не найдена. Не удаляйте её папку AppData.
    echo Полный пакет рассчитан на обновление с сохранением существующих токенов.
    pause
    exit /b 1
)
if exist "%~dp0OneDelivery_full.zip" (
    "%ONED_PY%" "%~dp0onedelivery_update\full_install.py" --archive "%~dp0OneDelivery_full.zip" %*
) else (
    "%ONED_PY%" "%~dp0onedelivery_update\full_install.py" %*
)
set "ONED_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %ONED_EXIT%
