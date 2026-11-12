@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Cam to Drone - εκκίνηση server...
start "" http://localhost:8080/
python server\app.py --port 8080
pause
