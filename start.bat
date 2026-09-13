@echo off
rem Запуск wardogs-monitor: pythonw в фоне, полный путь (как требует сторож-логика).
cd /d "%~dp0"
set "PY=C:\Users\Admin\AppData\Local\Programs\Python\Python312\pythonw.exe"
if not exist "%PY%" set "PY=pythonw.exe"
start "" "%PY%" "%~dp0bot.py"