@echo off
cd /d "%~dp0"
echo Installing optional native-backed VST3 host (Spotify Pedalboard)...
echo VST3 host installation is optional. VST2 plugins are not supported.
py -3 -m pip install "pedalboard>=0.9.20,<1"
if errorlevel 1 (
  echo Installation failed for this Python interpreter.
  echo Pedalboard may not provide wheels for your Python version.
  echo You can continue using built-in effects without Pedalboard.
  pause
  exit /b 1
)
echo Installation completed. Restart Dynamic Teleprompter.
pause
