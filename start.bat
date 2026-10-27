@echo off
setlocal EnableExtensions
REM ===========================================================================
REM  UniFi x flinchbot - double-click to start (Windows).
REM  Checks this computer first (Python, folder, packages) and offers fixes.
REM  First run: creates a private Python environment (.venv) and installs what
REM  the app needs. Later runs start in a few seconds.
REM  Options are passed through, e.g.:  start.bat --port 8090
REM  On a Mac use start.command instead; both share .env and data\.
REM  FB_DRY_RUN=1 makes the "open ..." offers write their files but open nothing.
REM ===========================================================================
cd /d "%~dp0"
title UniFi x flinchbot
set "FB_DIR=%~dp0"
echo.
echo  UniFi x flinchbot - checking this computer ...

REM --- running from inside the ZIP? (Explorer runs it from a Temp copy) --------
set "CHK=%FB_DIR:\AppData\Local\Temp\=%"
if /I not "%CHK%"=="%FB_DIR%" goto :inzip
set "CHK=%FB_DIR:.zip\=%"
if /I not "%CHK%"=="%FB_DIR%" goto :inzip
:afterzip

REM --- can the app write here? ------------------------------------------------
copy /y nul ".write-test.tmp" >nul 2>&1
if not exist ".write-test.tmp" goto :readonly
del /q ".write-test.tmp" >nul 2>&1

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
REM A .venv copied from a Mac has no Scripts\python.exe: rebuild it (it only holds
REM downloaded packages, nothing of yours).
if not exist ".venv\Scripts\python.exe" (
  echo  First run: creating a private Python environment in .venv ...
  if exist ".venv\" (
    %PY% -m venv --clear .venv
  ) else (
    %PY% -m venv .venv
  )
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

REM --- environment report (computer, Python, packages, passwords, internet) --
".venv\Scripts\python.exe" backend\envcheck.py
if errorlevel 1 goto :envfail

REM --- run the app (opens your browser automatically) -----------------------
".venv\Scripts\python.exe" backend\server.py %*
echo.
pause
exit /b

REM ===========================================================================
:nopython
set "OLDVER="
for /f "tokens=2" %%v in ('py --version 2^>nul') do set "OLDVER=%%v"
if not defined OLDVER for /f "tokens=2" %%v in ('python --version 2^>nul') do set "OLDVER=%%v"
if defined OLDVER (
  echo %OLDVER%| findstr /R "^[0-9]" >nul || set "OLDVER="
)
echo.
if defined OLDVER (
  echo  Python %OLDVER% is installed, but this app needs Python 3.10 or newer.
) else (
  echo  Python isn't installed on this PC ^(or Windows can't find it^).
)
echo  The app needs Python 3.10 or newer. It's free and takes a few minutes to install.
echo  If typing "python" opens the Microsoft Store, see the note in the setup guide.
where winget >nul 2>&1 && echo  Tip: you can also install it from this window with:  winget install -e --id Python.Python.3.13
echo.
choice /C YN /N /M " Open the Python download page and the setup guide now? [Y/N] "
if errorlevel 2 goto :nopython_end
if not defined FB_DRY_RUN start "" "https://www.python.org/downloads/windows/"
call :openguide python-windows
echo  Opened. After installing Python, close this window and double-click start.bat again.
:nopython_end
echo.
pause
exit /b 1

:inzip
echo.
echo  It looks like start.bat is running from inside the downloaded ZIP file
echo  ^(or from a temporary folder^). The app can't keep its settings there.
echo  Right-click the ZIP, choose "Extract All...", pick a permanent folder such
echo  as Documents, then double-click start.bat in the extracted folder.
echo.
choice /C YC /N /M " [Y] open the setup guide and stop   [C] continue anyway: "
if errorlevel 2 goto :afterzip
call :openguide get-the-app
echo.
pause
exit /b 1

:readonly
echo.
echo  This folder is read-only, so the app can't install what it needs or save
echo  its settings here. Move the whole folder somewhere you can write to, such
echo  as Documents, and start it again.
echo.
choice /C YN /N /M " Open the setup guide at that step? [Y/N] "
if errorlevel 2 goto :readonly_end
call :openguide get-the-app
:readonly_end
echo.
pause
exit /b 1

:venvfail
echo.
echo  Could not create the .venv folder. Make sure this folder is not read-only
echo  and that Python installed correctly (see SETUP.md, step 1).
echo.
choice /C YN /N /M " Open the setup guide at the Python step? [Y/N] "
if not errorlevel 2 call :openguide python-windows
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

:envfail
echo.
echo  Fix the problems marked XX above, then double-click start.bat again.
echo.
pause
exit /b 1

REM --- open SETUP.html at a section ------------------------------------------
REM Windows drops the "#section" when it opens a local file, so write a tiny page
REM in %TEMP% that redirects to SETUP.html#section and open that instead.
:openguide
set "FB_ANCHOR=%~1"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$q=[char]34; $u=([Uri](Join-Path $env:FB_DIR 'SETUP.html')).AbsoluteUri + '#' + $env:FB_ANCHOR; $f=Join-Path $env:TEMP 'flinchbot-open-guide.html'; Set-Content -Encoding UTF8 -LiteralPath $f -Value ('<!doctype html><meta charset=utf-8><title>Setup guide</title><meta http-equiv=refresh content=' + $q + '0;url=' + $u + $q + '><p><a href=' + $q + $u + $q + '>Open the setup guide</a></p>'); if (-not $env:FB_DRY_RUN) { Start-Process -FilePath $f }" >nul 2>&1
if errorlevel 1 if not defined FB_DRY_RUN start "" "%FB_DIR%SETUP.html"
exit /b 0
