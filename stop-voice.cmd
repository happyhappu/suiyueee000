@echo off
"D:\Python313\python.exe" -c "from pathlib import Path;p=Path(r'%~dp0.runtime')/'voice-stop';p.parent.mkdir(exist_ok=True);p.touch();print('Stop requested. The gateway will unsubscribe audio and exit.')"
if /I not "%~1"=="--no-pause" pause
