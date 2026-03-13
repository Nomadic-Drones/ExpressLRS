#!/usr/bin/env bash
# EP2 Flash Tool — launcher
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"

# Create venv if it doesn't exist
if [ ! -d "$VENV" ]; then
  echo "Creating virtual environment…"
  python3 -m venv "$VENV"
fi

# Install/upgrade dependencies
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$SCRIPT_DIR/requirements.txt"

echo ""
echo "  ┌─────────────────────────────────────┐"
echo "  │  EP2 Flash Tool                     │"
echo "  │  Open http://localhost:5173          │"
echo "  └─────────────────────────────────────┘"
echo ""

# Open browser (macOS)
if command -v open &>/dev/null; then
  (sleep 1 && open "http://localhost:5173") &
fi

"$VENV/bin/python" "$SCRIPT_DIR/app.py"
