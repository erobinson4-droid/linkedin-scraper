#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Virtual environment not found. Running setup first…"
  bash setup.sh
fi

echo "Starting LinkedIn Scraper at http://localhost:5050"
open "http://localhost:5050" 2>/dev/null || true
.venv/bin/python app.py
