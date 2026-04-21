#!/usr/bin/env bash
# uninstall.sh — reverse what install.sh did.
# Leaves pacman packages alone (they may be needed by other apps).

set -euo pipefail

APP_ID="dev.nocoder.NoCoder"
LAUNCHER_NAME="nocoder"

BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"
HICOLOR_DIR="$HOME/.local/share/icons/hicolor"
INSTALL_DIR="$HOME/.local/share/nocoder"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/nocoder"
HYPR_CONF="$HOME/.config/hypr/windows.conf"

MARK_BEGIN="# >>> nocoder windowrules begin"
MARK_END="# <<< nocoder windowrules end"

GREEN=$'\e[32m'; DIM=$'\e[2m'; RESET=$'\e[0m'
say() { printf '%s==>%s %s\n' "$GREEN" "$RESET" "$*"; }

say "Removing installed app tree, launcher, desktop entry, icons, config"
rm -rf "$INSTALL_DIR"
rm -rf "$CONFIG_DIR"
rm -f "$BIN_DIR/$LAUNCHER_NAME"
rm -f "$DESKTOP_DIR/$APP_ID.desktop"
# Old SVG location (pre-2026-04-21 rebrand) and current multi-size PNGs.
rm -f "$HICOLOR_DIR/scalable/apps/$APP_ID.svg"
for sz in 48 64 96 128 256 512; do
  rm -f "$HICOLOR_DIR/${sz}x${sz}/apps/$APP_ID.png"
done

command -v update-desktop-database >/dev/null 2>&1 && \
  update-desktop-database -q "$DESKTOP_DIR" || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
  gtk-update-icon-cache -q -t "$HICOLOR_DIR" || true

if [[ -f "$HYPR_CONF" ]]; then
  # Only strip if both markers are present (closed block). An unclosed BEGIN
  # would otherwise make awk eat everything to EOF, including user edits.
  if grep -qxF "$MARK_BEGIN" "$HYPR_CONF" && ! grep -qxF "$MARK_END" "$HYPR_CONF"; then
    echo "!! unclosed '$MARK_BEGIN' block in $HYPR_CONF — not touching it."
  elif grep -qxF "$MARK_BEGIN" "$HYPR_CONF"; then
    say "Stripping Hyprland windowrules block"
    tmp="$(mktemp)"
    awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
      $0 == b { skip = 1; next }
      skip && $0 == e { skip = 0; next }
      !skip { print }
    ' "$HYPR_CONF" > "$tmp"
    mv "$tmp" "$HYPR_CONF"
  fi

  if command -v hyprctl >/dev/null 2>&1 && [[ -n "${HYPRLAND_INSTANCE_SIGNATURE:-}" ]]; then
    hyprctl reload >/dev/null
  fi
fi

echo "${DIM}Pacman packages left in place. Remove manually if desired.${RESET}"
echo "Done."
