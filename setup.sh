#!/usr/bin/env bash
set -e

echo "=== LinkedIn Scraper Setup ==="
cd "$(dirname "$0")"

# Create venv if it doesn't exist
if [ ! -d ".venv" ]; then
  echo "Creating virtual environment…"
  python3 -m venv .venv
fi

echo "Installing Python packages…"
.venv/bin/pip install -q -r requirements.txt

echo "Installing Playwright browsers (Chromium)…"
.venv/bin/playwright install chromium

echo ""
echo "✓ Setup complete!"
echo "  Run:  ./run.sh"
