@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3 -X utf8 "%~dp0tools\bootstrap.py" --check-env
if errorlevel 9009 python -X utf8 "%~dp0tools\bootstrap.py" --check-env
pause
