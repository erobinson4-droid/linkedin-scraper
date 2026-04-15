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
echo "Creating desktop shortcut…"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SHORTCUT="$HOME/Desktop/LinkedIn Scraper.command"
cat > "$SHORTCUT" << EOF
#!/bin/bash
cd "$SCRIPT_DIR"
bash run.sh
EOF
chmod +x "$SHORTCUT"

# Apply custom icon if icon.png exists in the project directory
ICON_PATH="$SCRIPT_DIR/icon.png"
if [ -f "$ICON_PATH" ]; then
    .venv/bin/python3 << PYEOF
import AppKit
img = AppKit.NSImage.alloc().initWithContentsOfFile_("$ICON_PATH")
if img:
    AppKit.NSWorkspace.sharedWorkspace().setIcon_forFile_options_(img, "$SHORTCUT", 0)
    print("✓ Custom icon applied to shortcut.")
else:
    print("⚠ Could not load icon — shortcut created without custom icon.")
PYEOF
fi

echo "✓ Desktop shortcut created — double-click 'LinkedIn Scraper' on your Desktop to launch."

echo ""
echo "✓ Setup complete!"
echo "  Run:  ./run.sh  or double-click the icon on your Desktop."
