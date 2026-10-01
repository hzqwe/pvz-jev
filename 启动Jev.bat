@echo off
REM ==============================================================
REM  PvZ hybrid x Jev -- double-click launcher
REM
REM  Pure ASCII on purpose: every Chinese string is printed by
REM  tools\launch.py, so a mismatched console codepage cannot
REM  garble anything here.  Saved with CRLF line endings.
REM
REM  Project root follows this script, including paths with spaces.
REM ==============================================================

setlocal
title PvZ x Jev

set "PROJ=%~dp0"

if not exist "%PROJ%tools\launch.py" (
    echo [X] Project not found at: "%PROJ%"
    echo     Keep this script next to the tools folder in a complete checkout.
    echo.
    pause
    exit /b 1
)

cd /d "%PROJ%"
if errorlevel 1 (
    echo [X] Cannot enter "%PROJ%"
    pause
    exit /b 1
)

REM UTF-8 mode keeps Chinese/emoji intact when output is piped or
REM redirected.  Console output already goes through WriteConsoleW,
REM so no "chcp" is needed here.
set "PYTHONUTF8=1"

REM ---------------- locate a working Python ----------------
REM Deliberately kept as FLAT lines (no parenthesised blocks) so that
REM %PY% is always expanded at execution time, never at parse time.

set "PY="

REM 1) the official Windows py launcher, if present
for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%i"
if defined PY if not exist "%PY%" set "PY="
if defined PY goto :py_ready

REM 2) the portable interpreter bundled with this machine
if exist "%USERPROFILE%\.workbuddy-ai\binaries\python\versions\3.13.12\python.exe" set "PY=%USERPROFILE%\.workbuddy-ai\binaries\python\versions\3.13.12\python.exe"
if defined PY goto :py_ready

REM 3) whatever "python" resolves to on PATH
for /f "delims=" %%i in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%i"
if defined PY if not exist "%PY%" set "PY="

:py_ready
if not defined PY (
    echo [X] No working Python 3 found.
    echo.
    echo     Install Python 3.10+ and tick "Add python.exe to PATH",
    echo     or open this .bat and set PY to a python.exe path.
    echo.
    pause
    exit /b 1
)

REM Handed to launch.py so it can show which interpreter is in use,
REM instead of printing an ugly path line here before the banner.
set "PvZJEV_PY=%PY%"

"%PY%" "%PROJ%tools\launch.py" %*
set "RC=%ERRORLEVEL%"

echo.
echo ------------------------------------------------------------
echo Finished. Exit code = %RC%
pause
exit /b %RC%
