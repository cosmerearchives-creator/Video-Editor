@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Dynamic Teleprompter - Install C++ Build Tools
echo ====================================================
echo   Dynamic Teleprompter - Developer Tools Installer
echo ====================================================
echo.
echo These tools are only needed to COMPILE the optional native engine.
echo End users can use the Python fallback without them.
echo Downloads are several GB. Administrator approval may be required.
echo.
where winget >nul 2>&1
if errorlevel 1 (
    echo ERROR: winget was not found. Install/update Windows App Installer first.
    pause
    exit /b 1
)
choice /M "Install CMake and Visual Studio 2022 C++ Build Tools"
if errorlevel 2 exit /b 0
if errorlevel 1 goto install
exit /b 1
:install
where cmake >nul 2>&1
if not errorlevel 1 (
    echo CMake appears to be available. Skipping CMake installation.
) else (
    echo Installing CMake...
    winget install --id Kitware.CMake --exact --accept-package-agreements --accept-source-agreements
    if errorlevel 1 goto cmake_failed
)
goto tools
:cmake_failed
echo WARNING: CMake installer returned an error. Continuing to Build Tools.
:tools
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if exist "%VSWHERE%" (
    "%VSWHERE%" -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath | findstr /R "." >nul
    if not errorlevel 1 (
        echo Visual Studio C++ toolchain detected. Skipping Build Tools installation.
        goto finish
    )
)
echo Installing Visual Studio 2022 C++ Build Tools...
winget install --id Microsoft.VisualStudio.2022.BuildTools --exact --force --accept-package-agreements --accept-source-agreements --override "--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
if errorlevel 1 (
    echo ERROR: Build Tools installer returned an error.
    pause
    exit /b 1
)
:finish
echo.
echo Tool installation attempt completed. Please close and reopen this terminal.
echo Then run build_native_windows.bat to compile the native DLL.
pause
