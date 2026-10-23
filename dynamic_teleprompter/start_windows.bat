@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "STATE_DIR=%LOCALAPPDATA%\DynamicTeleprompter"
set "SETUP_MARKER=%STATE_DIR%\setup_alpha17_done.txt"
if not exist "%SETUP_MARKER%" (
  echo First launch: checking Dynamic Teleprompter dependencies...
  py -3 setup_windows.py
  if errorlevel 1 (
    echo Setup did not finish successfully. Run setup_windows.bat to retry.
    pause
    exit /b 1
  )
  if not exist "%STATE_DIR%" mkdir "%STATE_DIR%"
  > "%SETUP_MARKER%" echo Setup completed
)
py -3 app.py
if errorlevel 1 echo Application exited with an error. See messages above.
pause
