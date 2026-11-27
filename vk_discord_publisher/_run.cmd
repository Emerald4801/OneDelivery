@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
if exist ".venv\Scripts\python.exe" goto dependencies
py -3 -c "import sys; sys.exit(sys.version_info < (3,10))" >nul 2>&1
if not errorlevel 1 (
    py -3 -m venv .venv
) else (
    python -c "import sys; sys.exit(sys.version_info < (3,10))" >nul 2>&1
    if errorlevel 1 goto missingpython
    python -m venv .venv
)
if errorlevel 1 goto failed
:dependencies
if exist ".venv\installed.txt" goto run
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.lock
if errorlevel 1 goto failed
echo ready>".venv\installed.txt"
:run
".venv\Scripts\python.exe" publish_album_v3.py %*
if errorlevel 1 goto failed
pause
exit /b 0
:missingpython
echo Install Python 3.10+ and enable Python Launcher or Add Python to PATH.
pause
exit /b 1
:failed
echo.
echo Operation stopped. See the message above and README_V3.txt.
pause
exit /b 1
