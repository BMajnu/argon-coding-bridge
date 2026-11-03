@echo off
title Argon Account Pool Login Assistant
echo ==============================================================
echo   Gemini 4 Argon Multi-Account Pool Login Assistant
echo ==============================================================
echo Launching genuine Google Chrome and Login Assistant GUI...
set "SCRIPTS_DIR=%~dp0skills\argon-coding-bridge\scripts"
if not exist "%SCRIPTS_DIR%\login_pool_accounts.py" (
    echo [ERROR] Could not locate login_pool_accounts.py at "%SCRIPTS_DIR%"
    pause
    exit /b 1
)
cd /d "%SCRIPTS_DIR%"
python login_pool_accounts.py %*
pause
