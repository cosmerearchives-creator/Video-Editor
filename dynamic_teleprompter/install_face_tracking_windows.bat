@echo off
cd /d "%~dp0"
echo Installing optional webcam face tracking dependency...
py -m pip install "opencv-python>=4.10,<5"
if errorlevel 1 (echo Failed. Try: python -m pip install opencv-python)
pause
