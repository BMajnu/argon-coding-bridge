@echo off
title Argon Plugin Sync
echo ==============================================================
echo   Argon Coding Bridge - Plugin Sync
echo ==============================================================
echo Brings every installed copy of this plugin in step with the
echo repository: the skill files, the reference docs, the companion
echo extension and the plugin manifests.
echo.
echo It never touches the live account pool, never creates a generic
echo plugin.json inside a MiniMax root, and never deletes anything.
echo.
echo Extra arguments are passed straight through, for example:
echo   Open_Argon_Update.bat -Check
echo   Open_Argon_Update.bat -Repo "D:\my\argon-coding-bridge"
set "SCRIPTS_DIR=%~dp0skills\argon-coding-bridge\scripts"
if not exist "%SCRIPTS_DIR%\sync_plugin.py" (
    echo [ERROR] Could not locate sync_plugin.py at "%SCRIPTS_DIR%\sync_plugin.py"
    echo         Run this from the plugin root, or use the repository copy.
    pause
    exit /b 1
)
set "PS_EXE=pwsh"
where pwsh >nul 2>nul
if errorlevel 1 (
    set "PS_EXE=powershell"
    where powershell >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] Neither pwsh nor powershell could be found on PATH.
        pause
        exit /b 1
    )
)
echo Running Sync-ArgonPlugin.ps1 with %PS_EXE% ...
echo.
"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%SCRIPTS_DIR%\Sync-ArgonPlugin.ps1" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
    echo [ERROR] Sync-ArgonPlugin.ps1 finished with exit code %RC%.
) else (
    echo [SUCCESS] Every installed plugin root is now in step with the repo.
)
pause
exit /b %RC%
