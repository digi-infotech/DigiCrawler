@echo off
title DigiCrawler
cd /d "%~dp0"

docker info >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Docker Desktop is not running.
  echo  Please open Docker Desktop, wait until it says "Engine running",
  echo  then double-click start.bat again.
  echo.
  pause
  exit /b 1
)

if not exist .env copy .env.example .env >nul

echo.
echo  Checking for updates...
git pull --ff-only >nul 2>&1
if errorlevel 1 (
  echo  Could not check for updates ^(no internet, or a local change is in the way^).
  echo  Starting with the version already on this computer.
)

echo.
echo  Starting DigiCrawler... (the first time can take a few minutes)
echo.
docker compose up -d --build
if errorlevel 1 (
  echo.
  echo  Something went wrong. Please send a screenshot of this window to the developer.
  pause
  exit /b 1
)

set PORT=3003
for /f "usebackq tokens=1,* delims==" %%a in (".env") do if /i "%%a"=="PORT" set PORT=%%b

echo.
echo  DigiCrawler is running:  http://localhost:%PORT%
echo  Opening it in your browser...
start "" http://localhost:%PORT%
timeout /t 5 >nul
