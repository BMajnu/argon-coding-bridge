@echo off
REM ============================================================
REM  ROUND 23 - new LANE, HALF-AUTOMATIC
REM
REM  WHAT YOU MUST DO FIRST (by hand, in the browser):
REM    1. open  https://gemini.google.com/u/6/app
REM    2. model picker -> "Code & analysis 4 Argon"   (NOT the Everyday help / Hydrogen one)
REM    3. attach  G:\Developing\argon-coding-bridge\argon_context.zip
REM    4. leave the composer EMPTY - this script pastes the prompt and sends it
REM
REM  If the tab HANGS (no click anywhere):
REM    close that tab, open the chat again, redo steps 2 and 3, then run this file.
REM ============================================================
setlocal
set REPO=G:\Developing\argon-coding-bridge
set CHROME="%ProgramFiles%\Google\Chrome\Application\chrome.exe"
set LANE=new
set SLOT=/u/6/
set CHAT=https://gemini.google.com/u/6/app
set PROMPT=%~dp0new_prompt.md

echo.
echo  lane   : %LANE%
echo  slot   : %SLOT%
echo  chat   : %CHAT%
echo  prompt : %PROMPT%
echo.
echo  Opening the Argon browser and the conversation ...
start "" %CHROME% --user-data-dir="%USERPROFILE%\.gemini\argon_browser_profile" "https://gemini.google.com/u/6/app"
timeout /t 12 /nobreak >nul

echo  Pasting the prompt and sending (mode, model and the attachment must already be set) ...
python "%REPO%\skills\argon-coding-bridge\scripts\dispatch_argon.py" ^
  --project-path "%REPO%" ^
  --slot %SLOT% ^
  --lane new ^
  --manual-setup ^
  --prompt-file "%PROMPT%" ^
  --skip-package ^
  --no-carry-over ^
  --deadline-seconds 5400 ^
  --delivery-dir "%REPO%\downloads\dual\round23\new"
echo.
echo  exit=%ERRORLEVEL%
pause
