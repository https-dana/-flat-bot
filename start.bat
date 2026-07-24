@echo off
cd /d "%~dp0"
if not exist .venv (
    echo Створюю віртуальне середовище...
    python -m venv .venv
    .venv\Scripts\pip install -r requirements.txt
)
.venv\Scripts\python bot.py
pause
