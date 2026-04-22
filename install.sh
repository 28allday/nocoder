#!/usr/bin/env bash
# install.sh — integrate NO-CODER into Omarchy.
#
# Installs pacman dependencies, drops a launcher into ~/.local/bin, registers
# a .desktop entry so the walker finds it, installs the app icon into the
# hicolor theme, and appends Hyprland windowrules so the window always floats
# centered on launch.
#
# Safe to re-run — the Hyprland rules live inside a marked block that is
# replaced (not duplicated) on every install.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$SCRIPT_DIR"
PKG_DIR="$SCRIPT_DIR/packaging"

APP_ID="dev.nocoder.NoCoder"
LAUNCHER_NAME="nocoder"

BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"
HICOLOR_DIR="$HOME/.local/share/icons/hicolor"
INSTALL_DIR="$HOME/.local/share/nocoder"
HYPR_CONF="$HOME/.config/hypr/windows.conf"

MARK_BEGIN="# >>> nocoder windowrules begin"
MARK_END="# <<< nocoder windowrules end"

GREEN=$'\e[32m'; YELLOW=$'\e[33m'; RED=$'\e[31m'; DIM=$'\e[2m'; RESET=$'\e[0m'
say()  { printf '%s==>%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '%s[!]%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die()  { printf '%s[x]%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

# ---------- environment checks ----------

[[ -f "$SRC_DIR/run.py" ]] || die "run.py not found next to install.sh (SRC_DIR=$SRC_DIR)"
[[ -f "$PKG_DIR/$APP_ID.desktop" ]] || die "missing $PKG_DIR/$APP_ID.desktop"
[[ -f "$PKG_DIR/$APP_ID.png" ]]     || die "missing $PKG_DIR/$APP_ID.png"

# Guard against running install.sh from inside the install target itself — the
# clean-and-copy step would remove its own script mid-execution.
if [[ "$SRC_DIR" == "$INSTALL_DIR" ]]; then
  die "Don't run install.sh from $INSTALL_DIR — run it from your git clone."
fi

if ! command -v pacman >/dev/null 2>&1; then
  die "pacman not found — this installer targets Arch/Omarchy only."
fi
if [[ ! -d "$HOME/.local/share/omarchy" ]]; then
  warn "$HOME/.local/share/omarchy not found — are you sure this is Omarchy?"
fi
if [[ ! -f "$HOME/.config/hypr/hyprland.conf" ]]; then
  die "Hyprland config not found at ~/.config/hypr/hyprland.conf."
fi

# ---------- pacman deps (non-font) ----------

PACMAN_PKGS=(
  python
  python-gobject
  gtk4
  libadwaita
  ffmpeg
)

# Only invoke sudo/pacman when something is actually missing.
MISSING_PKGS=()
for p in "${PACMAN_PKGS[@]}"; do
  pacman -Q "$p" &>/dev/null || MISSING_PKGS+=("$p")
done

if ((${#MISSING_PKGS[@]} == 0)); then
  say "All required pacman packages already installed."
else
  say "Installing missing pacman packages: ${MISSING_PKGS[*]}"
  if command -v omarchy-pkg-add >/dev/null 2>&1; then
    omarchy-pkg-add "${MISSING_PKGS[@]}"
  else
    sudo pacman -S --noconfirm --needed "${MISSING_PKGS[@]}"
  fi
fi

# ---------- fonts (per-user, no sudo) ----------

install_font_from_github() {
  # $1 friendly name, $2 github repo "owner/name", $3 fc-list match pattern,
  # $4 subdir under ~/.local/share/fonts/
  local name="$1" repo="$2" fc_pattern="$3" subdir="$4"
  # Read fc-list into a var rather than piping to grep -q — with `set -o pipefail`
  # grep's early exit gives fc-list a SIGPIPE (141), poisoning the pipeline.
  local _fc_all
  _fc_all=$(fc-list)
  if grep -iqE "$fc_pattern" <<<"$_fc_all"; then
    say "$name already available — skipping."
    return 0
  fi
  say "Installing $name to $HOME/.local/share/fonts/$subdir (per-user, no sudo)"
  local url
  url=$(curl -fsSL "https://api.github.com/repos/$repo/releases/latest" \
    | grep -oE '"browser_download_url":[[:space:]]*"[^"]*\.zip"' \
    | head -1 | sed -E 's/.*"([^"]*)".*/\1/') || true
  if [[ -z "$url" ]]; then
    warn "Could not resolve latest $name release — skipping font install."
    return 0
  fi
  local tmpdir
  tmpdir=$(mktemp -d)
  curl -fsSL -o "$tmpdir/pkg.zip" "$url" || { warn "Download failed: $url"; rm -rf "$tmpdir"; return 0; }
  unzip -oq "$tmpdir/pkg.zip" -d "$tmpdir/extract" || { warn "Unzip failed for $name."; rm -rf "$tmpdir"; return 0; }
  mkdir -p "$HOME/.local/share/fonts/$subdir"
  find "$tmpdir/extract" -type f \( -name "*.otf" -o -name "*.ttf" \) \
    -exec cp -f {} "$HOME/.local/share/fonts/$subdir/" \;
  rm -rf "$tmpdir"
}

install_font_from_github "Inter"          "rsms/inter"              '^[^:]*inter[^:]*:' inter
install_font_from_github "JetBrains Mono" "JetBrains/JetBrainsMono" 'jetbrains mono'    jetbrains-mono

if command -v fc-cache >/dev/null 2>&1; then
  fc-cache -f "$HOME/.local/share/fonts/" >/dev/null 2>&1 || true
fi

# ---------- import smoke test ----------

say "Verifying Python imports"
if ! python3 - <<PY
import sys
sys.path.insert(0, "$SRC_DIR")
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa
from nocoder.app import NoCoderApplication  # noqa
PY
then
  die "Python import check failed. A required module (python-gobject / gtk4 / libadwaita) may not be installed properly."
fi

# ---------- copy source tree into $INSTALL_DIR ----------

# Copy runtime files into a stable location so the user can delete the git
# clone after install. Re-runs wipe the target first to purge files removed
# upstream (e.g., from a git pull) before copying fresh.
#
# Pre-flight: verify every source item exists BEFORE wiping the target. A
# missing item post-wipe would leave the user with no installed app.
for item in run.py style.css nocoder assets; do
  [[ -e "$SRC_DIR/$item" ]] || die "missing $SRC_DIR/$item — can't install from an incomplete clone"
done

say "Installing source tree to $INSTALL_DIR"
rm -rf "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cp -r \
  "$SRC_DIR/run.py" \
  "$SRC_DIR/style.css" \
  "$SRC_DIR/nocoder" \
  "$SRC_DIR/assets" \
  "$INSTALL_DIR/"
# Strip any __pycache__ copied from the source tree — they'd go stale anyway
# and Python will regenerate them as needed.
find "$INSTALL_DIR" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# ---------- GPU decode probe ----------

# Test which ffmpeg -hwaccel actually initialises on this box (CUDA on NVIDIA,
# QSV on Intel with intel-media-driver, VAAPI on AMD / Intel fallback) and
# pin the result into ~/.config/nocoder/config.json so the app doesn't re-probe
# on every launch. Decode side only — ProRes encode is always CPU.
say "Probing GPU decode"
# A future regression in hwaccel.py would otherwise abort the whole installer
# post-copy — degrade gracefully to CPU decode so the user still ends up with
# a working app they can inspect.
HW_CHOICE="none"
if HW_OUTPUT="$(python3 - <<PY 2>/dev/null
import sys
sys.path.insert(0, "$INSTALL_DIR")
from nocoder.hwaccel import probe_best_hwaccel, save_hwaccel
choice = probe_best_hwaccel()
save_hwaccel(choice)
print(choice or "none")
PY
)"; then
  HW_CHOICE="${HW_OUTPUT:-none}"
else
  warn "hwaccel probe failed — defaulting to CPU decode. Run the app once to re-probe."
fi

if [[ "$HW_CHOICE" == "none" ]]; then
  say "  No GPU decode available — decodes will run on CPU."
else
  say "  Selected: $HW_CHOICE"
fi

# ---------- launcher script in ~/.local/bin ----------

mkdir -p "$BIN_DIR"
LAUNCHER="$BIN_DIR/$LAUNCHER_NAME"
say "Writing launcher to $LAUNCHER"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
# NO-CODER launcher (installed by install.sh — do not edit by hand).
# Skip the xdg-desktop-portal file chooser so our app's CSS theme applies to
# file dialogs too. Safe on Omarchy — we don't need portal sandboxing.
export GTK_USE_PORTAL=0
exec python3 "$INSTALL_DIR/run.py" "\$@"
EOF
chmod +x "$LAUNCHER"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not in your PATH — add it to your shell rc for CLI use (the .desktop launcher already uses an absolute path indirectly)." ;;
esac

# ---------- icon ----------

# Drop any previously-installed icons under the old/alternate theme locations,
# so the walker doesn't end up picking a stale version.
rm -f "$HICOLOR_DIR/scalable/apps/$APP_ID.svg"
for sz in 48 64 96 128 256 512; do
  rm -f "$HICOLOR_DIR/${sz}x${sz}/apps/$APP_ID.png"
done

# Pick the best downscaler available — ImageMagick (modern "magick" or legacy
# "convert") gives crisp per-size PNGs. Fallback: install source at 256×256
# and let GTK scale on demand.
resize_png() {
  local src="$1" dst="$2" size="$3"
  if command -v magick >/dev/null 2>&1; then
    magick "$src" -resize "${size}x${size}" "$dst"
  elif command -v convert >/dev/null 2>&1; then
    convert "$src" -resize "${size}x${size}" "$dst"
  else
    install -m 0644 "$src" "$dst"
  fi
}

for sz in 48 64 96 128 256 512; do
  dir="$HICOLOR_DIR/${sz}x${sz}/apps"
  mkdir -p "$dir"
  resize_png "$PKG_DIR/$APP_ID.png" "$dir/$APP_ID.png" "$sz"
done
say "Installed icons under $HICOLOR_DIR/{48,64,96,128,256,512}x*/apps/"

if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  # hicolor/ without an index.theme won't regenerate a useful cache — ignore
  # the "invalid" report. The PNGs are still discovered by direct lookup.
  gtk-update-icon-cache -q -t "$HICOLOR_DIR" >/dev/null 2>&1 || true
fi

# ---------- .desktop file ----------

# The template uses @LAUNCHER@ in Exec= so we can substitute the absolute path
# to the user's launcher. Walker (and systemd-launched GUIs in general) runs
# with a minimal PATH that doesn't include ~/.local/bin, so a bare "Exec=nocoder"
# fails silently from the menu.
mkdir -p "$DESKTOP_DIR"
sed "s|@LAUNCHER@|$LAUNCHER|g" "$PKG_DIR/$APP_ID.desktop" > "$DESKTOP_DIR/$APP_ID.desktop"
chmod 0644 "$DESKTOP_DIR/$APP_ID.desktop"
say "Installed desktop entry to $DESKTOP_DIR/$APP_ID.desktop"
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database -q "$DESKTOP_DIR" || true
fi
if command -v desktop-file-validate >/dev/null 2>&1; then
  desktop-file-validate "$DESKTOP_DIR/$APP_ID.desktop" || warn "desktop-file-validate reported warnings."
fi

# ---------- Hyprland windowrules ----------

say "Registering Hyprland windowrules in $HYPR_CONF"
mkdir -p "$(dirname "$HYPR_CONF")"
touch "$HYPR_CONF"

# Strip any previous block (idempotent) — but only if both markers are
# present as a closed pair. An unclosed BEGIN (from a crashed prior run)
# would otherwise cause awk to eat every subsequent line to EOF, including
# hand-edited rules beneath. Leave it alone and warn instead; the user can
# resolve manually, and the fresh block we append below still takes effect.
if grep -qxF "$MARK_BEGIN" "$HYPR_CONF" && ! grep -qxF "$MARK_END" "$HYPR_CONF"; then
  warn "found unclosed '$MARK_BEGIN' block in $HYPR_CONF — leaving it intact (remove it manually if stale)."
elif grep -qxF "$MARK_BEGIN" "$HYPR_CONF"; then
  tmp="$(mktemp)"
  awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
    $0 == b { skip = 1; next }
    skip && $0 == e { skip = 0; next }
    !skip { print }
  ' "$HYPR_CONF" > "$tmp"
  mv "$tmp" "$HYPR_CONF"
fi

# Append fresh block.
cat >> "$HYPR_CONF" <<EOF
$MARK_BEGIN
# NO-CODER — float, centered, at its design size.
windowrule = float on,  match:class ^(dev\\.nocoder\\.NoCoder)$
windowrule = center on, match:class ^(dev\\.nocoder\\.NoCoder)$
windowrule = size 1280 880, match:class ^(dev\\.nocoder\\.NoCoder)$
$MARK_END
EOF

# Pick up the new windowrules. Hyprland auto-reloads on file save in most
# cases, but an atomic `mv` over the existing file can miss the inotify
# watcher on some setups — plus the user might have run this from SSH/TTY
# where $HYPRLAND_INSTANCE_SIGNATURE isn't set. So we try every angle:
#
# 1. Find the live Hyprland instance socket (works from SSH too — we only
#    need *any* one running session for this user).
# 2. Explicit `hyprctl -i $inst reload`; silent no-op if nothing's running.
# 3. Touch the conf so Hyprland's file watcher fires even if reload was
#    missed.
#
# If all three fail (e.g. Hyprland isn't running at all), the rules will
# take effect next time Hyprland starts — still correct, just not instant.
if command -v hyprctl >/dev/null 2>&1; then
  HYPR_INST=""
  if [[ -n "${HYPRLAND_INSTANCE_SIGNATURE:-}" ]]; then
    HYPR_INST="$HYPRLAND_INSTANCE_SIGNATURE"
  else
    # Pick the first live instance socket from $XDG_RUNTIME_DIR/hypr/.
    hypr_root="${XDG_RUNTIME_DIR:-/run/user/$UID}/hypr"
    shopt -s nullglob
    for sock in "$hypr_root"/*/.socket.sock; do
      inst_dir="${sock%/.socket.sock}"
      HYPR_INST="${inst_dir##*/}"
      break
    done
    shopt -u nullglob
  fi
  if [[ -n "$HYPR_INST" ]]; then
    say "Reloading Hyprland (instance $HYPR_INST)"
    HYPRLAND_INSTANCE_SIGNATURE="$HYPR_INST" hyprctl reload >/dev/null 2>&1 || warn "hyprctl reload returned an error — try 'hyprctl reload' manually if rules don't take effect."
  else
    warn "No running Hyprland instance detected — windowrules will load on next Hyprland session."
  fi
fi
# Belt-and-braces: update mtime so Hyprland's inotify-based auto-reload
# notices the change if the explicit reload above was somehow a no-op.
touch "$HYPR_CONF" 2>/dev/null || true

# Walker caches its app list — restart so new installs show up immediately.
# (Omarchy ships a helper that restarts elephant.service + walker in one go.)
if command -v omarchy-restart-walker >/dev/null 2>&1; then
  say "Restarting walker so the new entry is discoverable"
  omarchy-restart-walker >/dev/null 2>&1 || true
fi

cat <<EOF

${GREEN}NO-CODER installed.${RESET}
 ${DIM}•${RESET} App files:  $INSTALL_DIR
 ${DIM}•${RESET} Launcher:   $LAUNCHER
 ${DIM}•${RESET} Desktop:    $DESKTOP_DIR/$APP_ID.desktop
 ${DIM}•${RESET} Icon:       $HICOLOR_DIR/{48,64,96,128,256,512}x*/apps/$APP_ID.png
 ${DIM}•${RESET} Windowrules appended to $HYPR_CONF

Open the walker (Super+Space) and search for "NO-CODER".

If the window opens fullscreen instead of floating centered at 1280×880,
the Hyprland reload didn't pick up our rules — run this once to fix:
    hyprctl reload

Your git clone is no longer needed — feel free to delete it, or keep it to
'git pull && bash install.sh' for updates.
EOF
