@echo off
setlocal
cd /d "%~dp0"
title Bloody Control A70 v1.0.1
set "PY=py"
%PY% -c "import sys" >nul 2>nul
if errorlevel 1 set "PY=python"
%PY% -c "import tkinter; import hid" >nul 2>nul
if errorlevel 1 (
  echo Checking required package hidapi...
  %PY% -m pip install --user -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Could not install hidapi. Run manually: %PY% -m pip install -r requirements.txt
    pause
    exit /b 1
  )
)
%PY% BloodyControl_A70.py
if errorlevel 1 (
  echo.
  echo Bloody Control exited with an error. Copy the output for troubleshooting.
  pause
)
endlocal
