@echo off
cd /d "%~dp0"
title SA Website Bridge
echo Starting Surprise Attack website bridge...
echo Keeps https://surprise-attack-leaderboard.vercel.app in sync with Discord.
echo Leave this window open. Ctrl+C to stop.
echo.
python website_bridge.py
pause
