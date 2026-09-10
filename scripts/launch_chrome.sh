#!/usr/bin/env bash
# navegador externo (linux/macos)
# Lanza un Chrome "externo" con debugging remoto (CDP). El scraper se conecta
# a el: la vida del navegador es independiente de la del script.
set -euo pipefail

# Linux: google-chrome | chromium
# macOS: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_BIN="${CHROME_BIN:-google-chrome}"
PORT="${CDP_PORT:-9222}"
PROFILE_DIR="${PROFILE_DIR:-$HOME/.g2-scraper/chrome-profile}"

mkdir -p "$PROFILE_DIR"
echo "Chrome : $CHROME_BIN"
echo "Perfil : $PROFILE_DIR (persistente)"
echo "CDP    : http://localhost:$PORT"

exec "$CHROME_BIN" \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$PROFILE_DIR" \
  --no-first-run \
  --no-default-browser-check \
  --disable-blink-features=AutomationControlled \
  --window-size=1440,900 \
  about:blank