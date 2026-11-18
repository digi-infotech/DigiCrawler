@echo off
title DigiCrawler
cd /d "%~dp0"
echo Stopping DigiCrawler...
docker compose down
echo Stopped. You can close this window.
timeout /t 3 >nul
