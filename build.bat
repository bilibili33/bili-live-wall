@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 打包 B站直播监控墙

set "PYC=%~dp0venv\Scripts\python.exe"
if not exist "%PYC%" set "PYC=python"

"%PYC%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
  echo 还没装 PyInstaller，先跑：
  echo     "%PYC%" -m pip install pyinstaller
  pause
  exit /b 1
)

echo 开始打包……
"%PYC%" -m PyInstaller --noconfirm --clean ^
  --distpath "%~dp0dist" ^
  --workpath "%~dp0build" ^
  "%~dp0bili-live-wall.spec"
if errorlevel 1 (
  echo.
  echo 打包失败。
  pause
  exit /b 1
)

echo.
echo 打包完成： dist\bili-live-wall\bili-live-wall.exe
echo 整个 dist\bili-live-wall 文件夹一起发出去，不能只拿 exe。
pause
