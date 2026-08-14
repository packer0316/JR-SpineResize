@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3 main.py
if %ERRORLEVEL% NEQ 0 pause
