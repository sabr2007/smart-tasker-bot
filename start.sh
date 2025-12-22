#!/usr/bin/env bash
set -e

echo "Starting WebApp (FastAPI)..."
python -m uvicorn web.app:app \
  --app-dir src \
  --host 0.0.0.0 \
  --port $PORT &

echo "Starting Telegram Bot..."
python src/main.py
