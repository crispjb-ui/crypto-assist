@echo off
rem crypto-assist dashboard launcher - double-click to update and start.
rem Make a desktop shortcut to this file and never touch the terminal.
cd /d "%USERPROFILE%\crypto-assist"
echo Updating...
git pull
start "" http://localhost:8537
echo Dashboard starting - keep this window open (minimize it). Close it to stop.
python -m src.onchain.server
pause
