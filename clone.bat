@echo off
title DigiCrawler - first-time setup

where git >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Git is not installed. Please install it from:
  echo  https://git-scm.com/downloads/win
  echo  Then double-click this file again.
  echo.
  pause
  exit /b 1
)

echo.
echo  Downloading DigiCrawler...
echo.
git clone https://github.com/digi-infotech/DigiCrawler.git
if errorlevel 1 (
  echo.
  echo  Something went wrong. Please send a screenshot of this window to the developer.
  pause
  exit /b 1
)

echo.
echo  Done. Open the new "DigiCrawler" folder and double-click start.bat.
pause
