@echo off
rem ============================================================
rem  LongShot2PDF  double-click launcher (Windows)
rem
rem  IMPORTANT: this file must stay pure ASCII.
rem  cmd.exe parses .bat byte by byte in the active code page;
rem  any non-ASCII character inside a rem/echo line can be split
rem  into garbage tokens and break the whole script.
rem  All Chinese messages are printed by Python (run.py), not here.
rem
rem  Three things handled here:
rem  1) cd /d "%~dp0"   double-click starts in another directory
rem  2) chcp 65001      let Python's UTF-8 output render correctly
rem  3) pause on error  otherwise the window closes instantly
rem ============================================================
chcp 65001 >nul 2>nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
title LongShot2PDF

rem Try, in order: project venv -> py launcher -> python on PATH
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
    where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)

if not defined PY (
    echo.
    echo  [x] Python not found.
    echo.
    echo      Install Python 3.9+ first:  https://www.python.org/downloads/
    echo      During setup, tick "Add python.exe to PATH".
    echo.
    pause
    exit /b 2
)

%PY% "run.py" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo  [x] Exit code %RC%
    rem Only pause when launched by double-click (no arguments),
    rem so scripts calling this file are not blocked.
    if "%~1"=="" pause
)

endlocal & exit /b %RC%
