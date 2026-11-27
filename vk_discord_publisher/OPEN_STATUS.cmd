@echo off
setlocal
set "VKP_ROOT=%LOCALAPPDATA%\VKDiscordPublisher"
if not exist "%VKP_ROOT%\status_app.py" (
  echo Install this update first: UPDATE_STAGE1.cmd
  pause
  exit /b 1
)
start "" "%VKP_ROOT%\.venv\Scripts\pythonw.exe" "%VKP_ROOT%\status_app.py"
