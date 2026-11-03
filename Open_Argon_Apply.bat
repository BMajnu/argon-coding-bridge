@echo off
title Argon Patch Runner
echo ==============================================================
echo   Gemini 4 Argon Patch Runner
echo ==============================================================
echo Finds the apply_changes.ps1 delivered by Gemini 4 Argon,
echo validates it and applies it to the target project.
echo Extra arguments are passed straight through, for example:
echo   Open_Argon_Apply.bat -ProjectPath "C:\MyProject" -DryRun
set "SCRIPTS_DIR=%~dp0skills\argon-coding-bridge\scripts"
if not exist "%SCRIPTS_DIR%" (
    echo [ERROR] Could not locate the scripts folder at "%SCRIPTS_DIR%"
    pause
    exit /b 1
)
if not exist "%SCRIPTS_DIR%\Run-ArgonPatch.ps1" (
    echo [ERROR] Could not locate Run-ArgonPatch.ps1 at "%SCRIPTS_DIR%"
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
echo Running Run-ArgonPatch.ps1 with %PS_EXE% ...
echo.
"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%SCRIPTS_DIR%\Run-ArgonPatch.ps1" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
    echo [ERROR] Run-ArgonPatch.ps1 finished with exit code %RC%.
) else (
    echo [SUCCESS] Run-ArgonPatch.ps1 finished with exit code 0.
)
pause
exit /b %RC%
