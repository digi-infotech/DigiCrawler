#!/usr/bin/env sh
# Mac / Linux: start DigiCrawler and open it in the browser.
cd "$(dirname "$0")" || exit 1
if ! docker info >/dev/null 2>&1; then
  echo "Docker Desktop is not running. Open it, wait until it is running, then run ./start.sh again."
  exit 1
fi
[ -f .env ] || cp .env.example .env
docker compose up -d --build || { echo "Something went wrong. Send this output to the developer."; exit 1; }
PORT=$(grep -E '^PORT=' .env | cut -d= -f2); PORT=${PORT:-3003}
echo "DigiCrawler is running: http://localhost:$PORT"
(open "http://localhost:$PORT" 2>/dev/null || xdg-open "http://localhost:$PORT" 2>/dev/null) &
