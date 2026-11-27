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
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --upgrade gallery-dl requests Pillow
if errorlevel 1 goto failed
echo ready>".venv\installed.txt"
:run
if not exist cookies.txt goto public
".venv\Scripts\python.exe" download_album.py --cookies cookies.txt %*
goto result
:public
".venv\Scripts\python.exe" download_album.py %*
:result
if errorlevel 1 goto failed
echo.
echo Done. Check the downloads folder and page order.
pause
exit /b 0
:missingpython
echo Install Python 3.10 or newer from https://www.python.org/downloads/windows/
echo Enable the Python launcher or Add Python to PATH. Then run start.cmd again.
pause
exit /b 1
:failed
echo.
echo Download or setup failed. See the message above and README.txt.
echo If VK requires login, see the cookies instructions in README.txt.
pause
exit /b 1
