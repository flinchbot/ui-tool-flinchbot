@echo off
setlocal EnableExtensions
REM ===========================================================================
REM  UniFi x Claude - double-click to start.
REM  First run: creates a private Python environment (.venv) and installs what
REM  the app needs. Later runs start in a few seconds.
REM  Options are passed through, e.g.:  start.bat --port 8090
REM ===========================================================================
cd /d "%~dp0"
title UniFi x Claude

REM --- find Python 3.10+ (the "py" launcher first, then "python") -------------
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY (
  py -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1 && set "PY=py"
)
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY goto :nopython

REM --- create the private environment once ----------------------------------
if not exist ".venv\Scripts\python.exe" (
  echo  First run: creating a private Python environment in .venv ...
  %PY% -m venv .venv
  if errorlevel 1 goto :venvfail
)

REM --- install / update requirements only when requirements.txt changed -----
fc /b requirements.txt .venv\requirements.installed >nul 2>&1
if errorlevel 1 (
  echo  Installing required packages - this needs internet and takes a minute ...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
  if errorlevel 1 goto :pipfail
  copy /y requirements.txt .venv\requirements.installed >nul
)

REM --- run the app (opens your browser automatically) -----------------------
".venv\Scripts\python.exe" backend\server.py %*
echo.
pause
exit /b

:nopython
echo.
echo  Python 3.10 or newer was not found.
echo  Install it from https://www.python.org/downloads/  (see SETUP.md, step 1),
echo  then close this window and double-click start.bat again.
echo.
pause
exit /b 1

:venvfail
echo.
echo  Could not create the .venv folder. Make sure this folder is not read-only
echo  and that Python installed correctly (see SETUP.md, step 1).
echo.
pause
exit /b 1

:pipfail
echo.
echo  Installing packages failed. Check this PC's internet connection and try again.
echo  If it keeps failing, delete the .venv folder and run start.bat again.
del /q .venv\requirements.installed >nul 2>&1
echo.
pause
exit /b 1
