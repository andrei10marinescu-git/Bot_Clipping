@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Actualizez yt-dlp (YouTube/Kick se schimba des - ruleaza asta daca descarcarile nu mai merg)...
".venv\Scripts\python.exe" -m pip install -U "yt-dlp[default,curl-cffi]"
pause
