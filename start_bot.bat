@echo off
cd /d C:\tg_bot_webinars

:loop
wmic process where "name='python.exe'" get commandline 2>nul | find /I "main.py" >nul 2>&1
if %ERRORLEVEL% == 0 (
    echo Webinars bot already running, waiting...
    timeout /t 30 /nobreak >nul
    goto loop
)

echo Starting webinars bot...
"C:\Users\vedam\AppData\Local\Programs\Python\Python314\python.exe" main.py

echo Bot crashed or stopped, restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto loop
