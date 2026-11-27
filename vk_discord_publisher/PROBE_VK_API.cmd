@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "VK_PROBE_PYTHON="
if exist ".venv\Scripts\python.exe" set "VK_PROBE_PYTHON=%~dp0.venv\Scripts\python.exe"
if not defined VK_PROBE_PYTHON if exist "%LOCALAPPDATA%\VKDiscordPublisher\.venv\Scripts\python.exe" set "VK_PROBE_PYTHON=%LOCALAPPDATA%\VKDiscordPublisher\.venv\Scripts\python.exe"
if not defined VK_PROBE_PYTHON if exist "%USERPROFILE%\Downloads\vk_discord_publisher_windows_v1\vk_discord_publisher\.venv\Scripts\python.exe" set "VK_PROBE_PYTHON=%USERPROFILE%\Downloads\vk_discord_publisher_windows_v1\vk_discord_publisher\.venv\Scripts\python.exe"
if not defined VK_PROBE_PYTHON (
    echo Python environment not found. Run prepare_env.ps1 first.
    pause
    exit /b 1
)
"%VK_PROBE_PYTHON%" -u "%~dp0probe_vk_api.py"
pause
