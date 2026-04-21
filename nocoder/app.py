"""Adw.Application entry point. Installs the CSS provider and opens the main window."""
from __future__ import annotations

import re
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .window import MainWindow

APP_ID = "dev.nocoder.NoCoder"


# ---------- shared parser helpers ----------

def _iter_lines(path: Path):
    """Yield stripped, non-empty, non-comment lines from `path` (utf-8).

    Returns an empty iterator on OSError so callers don't need their own
    try/except. Lines beginning with `#` are treated as comments.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        yield stripped


def _dequote(v: str) -> str:
    """If `v` is quoted (`"foo"` / `'foo'`), return the content; else return v.

    Used by colors.toml + alacritty.toml + ghostty.conf parsers — every value
    in those files may be quoted, but their hex colours start with `#` which
    we MUST NOT trim as if it were an inline TOML comment when the value is
    quoted.
    """
    if v[:1] in ('"', "'"):
        end = v.find(v[0], 1)
        if end > 0:
            return v[1:end]
    return v


def _fill_accent_fallback(palette: dict) -> None:
    """If `palette` has no `accent` key, fill it from the most useful ANSI
    colour available (blue → magenta → cyan in that order). Mutates in place.
    """
    if "accent" in palette:
        return
    for k in ("color4", "color5", "color6"):
        if k in palette:
            palette["accent"] = palette[k]
            return


def _read_colors_toml(path: Path) -> dict:
    """Minimal parser for Omarchy's colors.toml — flat `key = "value"` lines only.

    Avoids a hard dep on Python 3.11's `tomllib`; the file format here is
    trivial enough to parse directly and the parser doesn't have to handle
    nested tables or arrays (Omarchy's schema is flat).
    """
    result: dict[str, str] = {}
    for line in _iter_lines(path):
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        if v[:1] in ('"', "'"):
            v = _dequote(v)
        elif "#" in v:
            # Unquoted value: trailing `# comment` is real, strip it.
            v = v.split("#", 1)[0].strip()
        if k and v:
            result[k] = v
    return result


_ALACRITTY_NORMAL_TO_ANSI = {
    "black": "color0", "red": "color1", "green": "color2", "yellow": "color3",
    "blue": "color4", "magenta": "color5", "cyan": "color6", "white": "color7",
}

_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?")


def _read_alacritty_palette(path: Path) -> dict:
    """Extract primary bg/fg AND [colors.normal] indices from alacritty.toml.

    Returns keys compatible with `colors.toml`: `background`, `foreground`,
    and `color0`..`color7` (mapped from `red`, `green`, ... inside
    `[colors.normal]`). `[colors.bright]` is used only as a fallback for a
    brighter `accent` pick.
    """
    result: dict[str, str] = {}
    section = None
    for line in _iter_lines(path):
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section not in ("colors.primary", "colors.normal", "colors.bright") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = _dequote(v.strip())
        if not v:
            continue
        if section == "colors.primary" and k in ("background", "foreground"):
            result[k] = v
        elif section == "colors.normal" and k in _ALACRITTY_NORMAL_TO_ANSI:
            result[_ALACRITTY_NORMAL_TO_ANSI[k]] = v
        elif section == "colors.bright" and k in _ALACRITTY_NORMAL_TO_ANSI:
            # Only fill a bright slot if the normal one didn't already land —
            # lets themes that only define bright still produce something.
            result.setdefault(_ALACRITTY_NORMAL_TO_ANSI[k], v)
    _fill_accent_fallback(result)
    return result


def _read_ghostty_palette(path: Path) -> dict:
    """Extract bg / fg / palette[0..15] from a ghostty.conf.

    Format (per Omarchy's template):
        background = #rrggbb
        foreground = #rrggbb
        palette = 0=#rrggbb
        palette = 4=#rrggbb
    """
    result: dict[str, str] = {}
    for line in _iter_lines(path):
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        if k in ("background", "foreground"):
            m = _HEX_COLOR.search(v)
            if m:
                result[k] = m.group(0)
        elif k == "palette":
            # value is "N=#rrggbb"
            idx, _, hexval = v.partition("=")
            idx = idx.strip()
            if idx.isdigit():
                m = _HEX_COLOR.search(hexval.strip())
                if m:
                    result[f"color{idx}"] = m.group(0)
    _fill_accent_fallback(result)
    return result


def _read_kitty_palette(path: Path) -> dict:
    """Extract bg / fg / colorN / active_border_color from a kitty.conf.

    Kitty uses whitespace-separated `key value` lines; Omarchy's template
    additionally sets `active_border_color` to the theme's accent, which we
    mine as the accent if nothing better is available.
    """
    result: dict[str, str] = {}
    for line in _iter_lines(path):
        # Keep the first two whitespace-delimited tokens.
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        k, v = parts[0], parts[1]
        m = _HEX_COLOR.search(v)
        if not m:
            continue
        hexval = m.group(0)
        if k in ("background", "foreground"):
            result[k] = hexval
        elif k == "active_border_color":
            result["accent"] = hexval
        elif k.startswith("color") and k[5:].isdigit():
            result[k] = hexval
    _fill_accent_fallback(result)
    return result


def _contrast_fg(hex_color: str, light: str = "#ffffff", dark: str = "#111111") -> str:
    """Return `light` or `dark` based on perceived luminance of `hex_color`.

    Used to pick accent-fg / destructive-fg / success-fg etc. — a saturated
    accent background needs matching text regardless of whether the theme is
    light or dark overall.
    """
    if not hex_color.startswith("#"):
        return dark
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return dark
    try:
        r = int(h[0:2], 16)
        g = int(h[2:4], 16)
        b = int(h[4:6], 16)
    except ValueError:
        return dark
    # Rec. 601 weighted luminance (simple & good enough for UI contrast).
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return dark if lum > 0.55 else light


def _synthesize_theme_css(palette: dict) -> str:
    """Build a full libadwaita-token CSS from an Omarchy palette.

    Unlike earlier revisions, this now also synthesises `accent_*`,
    `destructive_*`, `success_*`, `warning_*` and `error_*` from the theme's
    own `accent` + ANSI `color0..color7`, so the app's accents and semantic
    colours adhere to whichever theme the user has set.
    """
    bg = palette["background"]
    fg = palette["foreground"]
    # Accent: prefer the theme's own accent, fall back to ANSI blue/magenta/cyan.
    accent = palette.get("accent") or palette.get("color4") or palette.get("color5") or palette.get("color6") or fg
    accent_fg = _contrast_fg(accent, light=fg, dark=bg)
    # Semantic colours — fall back to the accent if a slot is missing so we
    # never fail to define a libadwaita token.
    danger  = palette.get("color1") or accent
    success = palette.get("color2") or accent
    warning = palette.get("color3") or accent
    info    = palette.get("color4") or accent
    # GTK4 CSS `shade()` is reliable on @named-color references but parses
    # inconsistently against inline hex literals. Define a private base token
    # so the subsequent shade() calls get a named reference in all GTK
    # versions — avoids silent fallback to libadwaita defaults for the
    # view/headerbar/card/sidebar bg tokens.
    return f"""
@define-color _nocoder_base {bg};

@define-color window_bg_color {bg};
@define-color window_fg_color {fg};

@define-color view_bg_color shade(@_nocoder_base, 0.93);
@define-color view_fg_color {fg};

@define-color dialog_bg_color {bg};
@define-color dialog_fg_color {fg};

@define-color popover_bg_color {bg};
@define-color popover_fg_color {fg};

@define-color headerbar_bg_color shade(@_nocoder_base, 1.12);
@define-color headerbar_fg_color {fg};

@define-color card_bg_color shade(@_nocoder_base, 0.93);
@define-color card_fg_color {fg};

@define-color sidebar_bg_color shade(@_nocoder_base, 0.93);
@define-color sidebar_fg_color {fg};

@define-color accent_color       {accent};
@define-color accent_bg_color    {accent};
@define-color accent_fg_color    {accent_fg};

@define-color destructive_bg_color {danger};
@define-color destructive_fg_color {_contrast_fg(danger, light=fg, dark=bg)};

@define-color success_bg_color     {success};
@define-color success_fg_color     {_contrast_fg(success, light=fg, dark=bg)};

@define-color warning_bg_color     {warning};
@define-color warning_fg_color     {_contrast_fg(warning, light=fg, dark=bg)};

@define-color error_bg_color       {danger};
@define-color error_fg_color       {_contrast_fg(danger, light=fg, dark=bg)};
"""

# Omarchy's canonical per-theme palette. Every stock theme ships `colors.toml`
# (keys: background, foreground, accent, color0..color15). A handful of custom
# themes (e.g., "lumon") additionally ship a full `gtk.css` with libadwaita
# tokens pre-mapped; when present we prefer that file verbatim. Otherwise we
# synthesize a minimal libadwaita palette from colors.toml below.
#
# Both paths resolve through Omarchy's `current/theme` symlink, so a
# `omarchy-theme-set <name>` followed by an app relaunch picks up the change.
OMARCHY_THEME_DIR = Path.home() / ".config" / "omarchy" / "current" / "theme"
OMARCHY_GTK_CSS = OMARCHY_THEME_DIR / "gtk.css"
OMARCHY_COLORS_TOML = OMARCHY_THEME_DIR / "colors.toml"
OMARCHY_GHOSTTY_CONF = OMARCHY_THEME_DIR / "ghostty.conf"
OMARCHY_ALACRITTY_TOML = OMARCHY_THEME_DIR / "alacritty.toml"
OMARCHY_KITTY_CONF = OMARCHY_THEME_DIR / "kitty.conf"


class NoCoderApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
        )
        self._window: MainWindow | None = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        # Let the Omarchy theme dictate light/dark via its libadwaita tokens
        # rather than forcing dark — the app used to pin FORCE_DARK back when
        # the palette was hardcoded Tokyo Night. Keep DEFAULT so a light theme
        # like catppuccin-latte or flexoki-light renders correctly.
        self._install_omarchy_theme_css()
        self._install_css()
        # If a previous session crashed mid-encode, surface the orphan path so
        # the user knows where the partial .mov sits. We don't auto-delete —
        # could be a real file that happens to share the marker's name.
        from .encoder import check_orphan_encode  # local import to avoid cycle on import order
        orphan = check_orphan_encode()
        if orphan is not None:
            import sys
            print(
                f"[nocoder] previous encode left an unfinished file: {orphan}\n"
                f"          (delete it manually if it's incomplete)",
                file=sys.stderr,
                flush=True,
            )

    def do_activate(self) -> None:
        if self._window is None:
            self._window = MainWindow(self)
        self._window.present()

    def do_open(self, files, _n_files, _hint) -> None:
        self.do_activate()
        if self._window is None:
            return
        paths = []
        for f in files:
            p = f.get_path() if f is not None else None
            if p:
                paths.append(p)
        if paths:
            self._window._add_paths(paths)

    def _install_omarchy_theme_css(self) -> None:
        """Make the app track the active Omarchy theme.

        Strategy:
          1. If the theme provides a full `gtk.css` (rare — only some custom
             themes like "lumon"), load it verbatim.
          2. Otherwise synthesize the libadwaita named tokens from the
             theme's `colors.toml` (shipped by every stock Omarchy theme).
          3. If neither is present, no-op — libadwaita defaults apply.

        The provider is installed at `PRIORITY_THEME`, below our style.css at
        `PRIORITY_APPLICATION`, so our CSS can override anything token-derived
        (the brand accent, semantic colours) while leaving bg / fg / borders
        / popover / dialog chrome cascading from the theme.
        """
        css_text = None
        if OMARCHY_GTK_CSS.exists():
            try:
                css_text = OMARCHY_GTK_CSS.read_text(encoding="utf-8")
            except OSError:
                css_text = None
        if css_text is None and OMARCHY_COLORS_TOML.exists():
            palette = _read_colors_toml(OMARCHY_COLORS_TOML)
            if palette.get("background") and palette.get("foreground"):
                css_text = _synthesize_theme_css(palette)
        # If no colors.toml, try each terminal config in turn — Omarchy
        # generates all three for any themed terminal. A user who's wiped
        # alacritty from their system might still have ghostty or kitty.
        for path, reader in (
            (OMARCHY_GHOSTTY_CONF, _read_ghostty_palette),
            (OMARCHY_ALACRITTY_TOML, _read_alacritty_palette),
            (OMARCHY_KITTY_CONF, _read_kitty_palette),
        ):
            if css_text is not None:
                break
            if not path.exists():
                continue
            palette = reader(path)
            if palette.get("background") and palette.get("foreground"):
                css_text = _synthesize_theme_css(palette)
        if not css_text:
            return
        provider = Gtk.CssProvider()
        try:
            provider.load_from_data(css_text.encode("utf-8"))
        except GLib.Error:
            return
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_THEME,
            )

    def _install_css(self) -> None:
        # Resolve style.css relative to the package root.
        css_path = Path(__file__).resolve().parent.parent / "style.css"
        if not css_path.exists():
            return
        provider = Gtk.CssProvider()
        provider.load_from_path(str(css_path))
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
