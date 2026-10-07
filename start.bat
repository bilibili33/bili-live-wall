@echo off
chcp 65001 >nul
cd /d "%~dp0"
title B站直播监控墙

set "PYC=%~dp0venv\Scripts\python.exe"
if not exist "%PYC%" set "PYC=python"

"%PYC%" "%~dp0main.py" %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" echo 服务异常退出，错误码 %RC%
echo 服务已停止。
pause
