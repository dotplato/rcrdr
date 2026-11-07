#!/usr/bin/env bash
# Installs Rcrdr for the current user only (no sudo):
#   ~/.local/share/rcrdr/rcrdr/        Python package
#   ~/.local/bin/rcrdr                 launcher
#   ~/.local/share/applications/...    app-grid entry
# Run with --uninstall to remove all three.
set -euo pipefail

APP_ID="io.github.dotplato.Rcrdr"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
LIB_DIR="$DATA_HOME/rcrdr"
BIN_PATH="$HOME/.local/bin/rcrdr"
DESKTOP_PATH="$DATA_HOME/applications/$APP_ID.desktop"

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -rf "$LIB_DIR" "$BIN_PATH" "$DESKTOP_PATH"
    echo "Rcrdr uninstalled. (Settings remain in ~/.config/rcrdr.)"
    exit 0
fi

mkdir -p "$LIB_DIR" "$(dirname "$BIN_PATH")" "$(dirname "$DESKTOP_PATH")"
rm -rf "$LIB_DIR/rcrdr"
cp -r "$SRC_DIR/rcrdr" "$LIB_DIR/"
find "$LIB_DIR" -name "__pycache__" -type d -prune -exec rm -rf {} +

cat > "$BIN_PATH" <<EOF
#!/bin/sh
PYTHONPATH="$LIB_DIR" exec python3 -m rcrdr "\$@"
EOF
chmod +x "$BIN_PATH"

cp "$SRC_DIR/data/$APP_ID.desktop" "$DESKTOP_PATH"
sed -i "s|^Exec=.*|Exec=$BIN_PATH|" "$DESKTOP_PATH"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$(dirname "$DESKTOP_PATH")" || true

echo "Rcrdr installed. Launch it from the app grid or run: rcrdr"
