@echo off
REM Builds dist\AmazonMXBot.exe (single file, no console window).
if not exist .venv python -m venv .venv
call .venv\Scripts\activate
pip install -r requirements.txt
pyinstaller --noconfirm --onefile --windowed --name AmazonMXBot app.py
echo.
echo Done: dist\AmazonMXBot.exe
