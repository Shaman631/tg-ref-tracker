@echo off
rem Bot control on Windows:  botctl.cmd start ^| stop ^| status
rem The bot writes its own PID into .bot.pid
setlocal enabledelayedexpansion
cd /d "%~dp0.."

set "PIDFILE=.bot.pid"
set "PY=python"
where %PY% >nul 2>&1 || set "PY=py"

if "%~1"=="start" goto start
if "%~1"=="stop" goto stop
if "%~1"=="status" goto status
echo Usage: %~nx0 start ^| stop ^| status
exit /b 1

:start
if exist "%PIDFILE%" (
    set /p PID=<"%PIDFILE%"
    tasklist /FI "PID eq !PID!" 2>nul | find "!PID!" >nul && (
        echo Bot already running ^(PID !PID!^)
        exit /b 0
    )
)
start "tg-ref-bot" /min %PY% tg_ref.py bot
echo Bot started in a separate window "tg-ref-bot". Check: %~nx0 status
exit /b 0

:stop
if not exist "%PIDFILE%" (
    echo Bot is not running ^(no .bot.pid^)
    exit /b 0
)
set /p PID=<"%PIDFILE%"
taskkill /PID !PID! /F >nul 2>&1
del /q "%PIDFILE%" >nul 2>&1
echo Bot stopped ^(PID !PID!^)
exit /b 0

:status
if not exist "%PIDFILE%" (
    echo Bot is not running ^(no .bot.pid^)
    exit /b 1
)
set /p PID=<"%PIDFILE%"
tasklist /FI "PID eq !PID!" 2>nul | find "!PID!" >nul && (
    echo Bot is running ^(PID !PID!^)
    exit /b 0
)
echo Bot is not running ^(process !PID! not found^)
exit /b 1
