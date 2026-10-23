@echo off
setlocal
cd /d "%~dp0"
where cmake >nul 2>nul
if errorlevel 1 (
 echo CMake not found. Launching the optional development tools installer.
 call "%~dp0install_build_tools_windows.bat"
 echo Restart this window and run build_native_windows.bat after installation.
 pause
 exit /b 1
)
echo Building optional native chroma-key engine...
cmake -S native -B native_build -G "Visual Studio 17 2022" -A x64
if errorlevel 1 goto fail
cmake --build native_build --config Release
if errorlevel 1 goto fail
copy /Y "native_build\Release\dt_engine.dll" "native\dt_engine.dll"
if errorlevel 1 goto fail
copy /Y "native_build\Release\rnnoise.dll" "native\rnnoise.dll"
if errorlevel 1 goto fail
copy /Y "native_build\RNNoise-LICENSE.txt" "..\docs\RNNoise-LICENSE.txt"
if errorlevel 1 goto fail
echo Native engines installed. Restart Dynamic Teleprompter.
pause
exit /b 0
:fail
echo Build failed. The application can still run with its NumPy fallback.
pause
exit /b 1
