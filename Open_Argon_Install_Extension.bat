@echo off
setlocal
title Argon Companion Extension One-Click Installer
echo ==============================================================
echo   Argon Coding Bridge Companion Extension - One-Click Install
echo ==============================================================
echo   Installs skills\argon-coding-bridge\extension via Chrome
echo   DevTools Protocol (Extensions.loadUnpacked), verifies
echo   Extensions.getExtensions + the MV3 service worker target,
echo   and confirms the content-script marker on a Gemini page.
echo ==============================================================
set "SCRIPTS_DIR=%~dp0skills\argon-coding-bridge\scripts"
if not exist "%SCRIPTS_DIR%\dispatch_argon.py" (
    echo [ERROR] Could not locate dispatch_argon.py at "%SCRIPTS_DIR%"
    pause
    exit /b 1
)
cd /d "%SCRIPTS_DIR%"
python dispatch_argon.py --install-extension %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] Companion extension installation or verification failed (exit code %EXIT_CODE%).
    pause
    exit /b %EXIT_CODE%
)
echo.
echo [SUCCESS] Companion extension is installed, verified, and alive on Gemini Web.
exit /b 0