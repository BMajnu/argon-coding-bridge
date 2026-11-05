@echo off
title Argon Account Pool Login Assistant
echo ==============================================================
echo   Gemini 4 Argon Multi-Account Pool Login Assistant
echo ==============================================================
echo   GUI Actions:
echo     - Login This Account      (sign in the currently displayed slot)
echo     - Auto Login All          (sign in every account in the pool)
echo     - Add Account             (add a single email + password)
echo     - Import Box              (paste one email:password per line; # comments ignored)
echo     - Load from .txt          (load email:password lines from a .txt file)
echo     - Import ^& Auto Login     (validate, save to accounts_pool.json, and sign in)
echo   CLI Options:
echo     Open_Argon_Login.bat --import-file accounts.txt [--auto-login] [--slot /u/0/]
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
