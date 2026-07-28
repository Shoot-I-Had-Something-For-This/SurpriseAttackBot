@echo off
cd /d "%~dp0"
set PYTHONUNBUFFERED=1
if not exist ".venv\Scripts\python.exe" (
  echo Creating venv...
  py -3.12 -m venv .venv 2>nul || python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)
echo Starting Surprise Attack bot (do NOT run this if Render already uses the same token)...
".venv\Scripts\python.exe" -u surprise_attack_bot.py
pause
