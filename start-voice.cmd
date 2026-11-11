@echo off
chcp 65001 >nul
cd /d "%~dp0"
"D:\Python313\python.exe" "%~dp0run_voice_gateway.py"
pause

