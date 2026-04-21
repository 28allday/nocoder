"""Settings pane: profile picker, alpha toggle, naming, output folder, ffmpeg preview.

Emits:
  settings-changed          ()
  choose-folder-requested   ()
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("GObject", "2.0")
from gi.repository import GObject, Gtk, Pango

from .config import load_config
from .data import PROFILES, PROFILES_BY_ID
from .encoder import format_preview_command


def _resolve_theme_hex(widget: Gtk.Widget, name: str, fallback: str) -> str:
    """Look up a libadwaita @named-color from the widget's style context.

    Returns the colour as a `#rrggbb` string. Falls back to `fallback` when
    the name isn't registered (e.g. before the CSS providers are wired up on
    a pre-realised widget, or on a system where Omarchy's theme palette isn't
    loaded).
    """
    try:
        ok, rgba = widget.get_style_context().lookup_color(name)
    except Exception:
        return fallback
    if not ok:
        return fallback
    r = int(round(rgba.red * 255))
    g = int(round(rgba.green * 255))
    b = int(round(rgba.blue * 255))
    return f"#{r:02x}{g:02x}{b:02x}"


class Settings:
    __slots__ = ("profile", "alpha", "naming", "out_dir", "audio_bits", "auto_reveal")

    def __init__(
        self,
        profile: str = "hq",
        alpha: bool = False,
        naming: str = "suffix",
        out_dir: str = "",
        audio_bits: int = 16,
        auto_reveal: bool = False,
    ) -> None:
        self.profile = profile
        self.alpha = alpha
        self.naming = naming
        self.out_dir = out_dir or str(Path.home() / "Footage" / "prores")
        # 16 = pcm_s16le (editorial default, matches prowrap-yad.sh)
        # 24 = pcm_s24le (preserves pro-camera bit depth; ~50% bigger audio)
        self.audio_bits = audio_bits
        # If True, _finish_encoding opens the output folder via Files when the
        # batch completes. Convenient for one-shot transcodes; off by default
        # so the app doesn't surprise users mid-workflow.
        self.auto_reveal = auto_reveal

    def snapshot(self) -> "Settings":
        return Settings(
            self.profile, self.alpha, self.naming, self.out_dir,
            self.audio_bits, self.auto_reveal,
        )

    def to_persistable(self) -> dict:
        """Subset of fields that survives across launches.

        Excludes `alpha` because the alpha toggle is conditional on the
        chosen profile (it auto-clears when a non-4444 profile is selected),
        so persisting it across launches creates more confusion than value.
        """
        return {
            "profile": self.profile,
            "naming": self.naming,
            "out_dir": self.out_dir,
            "audio_bits": self.audio_bits,
            "auto_reveal": self.auto_reveal,
        }


def load_persisted_settings() -> Settings:
    """Construct a Settings populated from `~/.config/nocoder/config.json`.

    Each field is validated against its allowed range; an out-of-range or
    missing entry falls back to the Settings constructor default.
    """
    data = load_config()

    profile = data.get("profile")
    if profile not in PROFILES_BY_ID:
        profile = "hq"

    naming = data.get("naming")
    if naming not in ("keep", "suffix"):
        naming = "suffix"

    audio_bits = data.get("audio_bits")
    if audio_bits not in (16, 24):
        audio_bits = 16

    out_dir = data.get("out_dir")
    if not isinstance(out_dir, str) or not out_dir.strip():
        out_dir = ""

    auto_reveal = bool(data.get("auto_reveal", False))

    return Settings(
        profile=profile,
        alpha=False,
        naming=naming,
        out_dir=out_dir,
        audio_bits=audio_bits,
        auto_reveal=auto_reveal,
    )


class SettingsPane(Gtk.Box):
    __gtype_name__ = "NoCoderSettingsPane"

    __gsignals__ = {
        "settings-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        "choose-folder-requested": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self, settings: Settings, encoder_kind: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("settings-pane")
        self.set_size_request(380, -1)
        self.set_hexpand(False)

        self._settings = settings
        self._encoder_kind = encoder_kind
        self._encoding_locked = False
        self._first_file_name: Optional[str] = None
        self._profile_buttons: dict[str, Gtk.ToggleButton] = {}
        self._profile_rows: dict[str, Gtk.Widget] = {}
        self._profile_radios: dict[str, Gtk.Widget] = {}
        self._profile_handlers: dict[str, int] = {}
        self._alpha_handler_id: int = 0
        self._naming_handler_id: int = 0
        self._cmd_visible = True

        self._build_header()
        self._build_scroll_body()

    # ---------- public ----------

    @property
    def settings(self) -> Settings:
        return self._settings

    def set_encoding(self, encoding: bool) -> None:
        self._encoding_locked = encoding
        # Lock interactive sub-widgets
        for btn in self._profile_buttons.values():
            btn.set_sensitive(not encoding)
        self._alpha_switch.set_sensitive(not encoding and self._alpha_available())
        if hasattr(self, "_audio_bits_switch"):
            self._audio_bits_switch.set_sensitive(not encoding)
        if hasattr(self, "_auto_reveal_switch"):
            self._auto_reveal_switch.set_sensitive(not encoding)
        self._naming_dropdown.set_sensitive(not encoding)
        self._browse_btn.set_sensitive(not encoding)

    def set_first_file_name(self, name: Optional[str]) -> None:
        self._first_file_name = name
        self._update_cmd_preview()

    def refresh(self) -> None:
        """Re-sync all widgets to the current Settings snapshot (accent handled via CSS)."""
        for pid, btn in self._profile_buttons.items():
            selected = (pid == self._settings.profile)
            hid = self._profile_handlers.get(pid, 0)
            if hid:
                btn.handler_block(hid)
            try:
                btn.set_active(selected)
            finally:
                if hid:
                    btn.handler_unblock(hid)
            self._apply_profile_visual(pid, selected)
        self._refresh_alpha_row()
        if self._naming_handler_id:
            self._naming_dropdown.handler_block(self._naming_handler_id)
        try:
            self._naming_dropdown.set_selected(0 if self._settings.naming == "keep" else 1)
        finally:
            if self._naming_handler_id:
                self._naming_dropdown.handler_unblock(self._naming_handler_id)
        self._folder_path.set_text(self._settings.out_dir)
        self._update_cmd_preview()

    # ---------- header ----------

    def _build_header(self) -> None:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        header.add_css_class("pane-header")
        header.set_hexpand(True)

        label = Gtk.Label(label="ENCODE SETTINGS", xalign=0)
        label.add_css_class("pane-label")
        label.set_hexpand(True)
        header.append(label)

        chip_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        chip_box.add_css_class("encoder-chip")
        icon = Gtk.Image.new_from_icon_name("preferences-desktop-apps-symbolic")
        icon.set_pixel_size(12)
        chip_box.append(icon)
        label_text = self._encoder_kind if self._encoder_kind in ("ks", "plain") else "none"
        chip_name = "prores_ks" if label_text == "ks" else ("prores" if label_text == "plain" else "no encoder")
        chip_box.append(Gtk.Label(label=chip_name))
        header.append(chip_box)
        self.append(header)

    # ---------- body ----------

    def _build_scroll_body(self) -> None:
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        body.set_margin_top(14)
        body.set_margin_bottom(120)
        body.set_margin_start(16)
        body.set_margin_end(16)

        body.append(self._build_profile_section())
        body.append(self._build_alpha_section())
        body.append(self._build_audio_bits_section())
        body.append(self._build_auto_reveal_section())
        body.append(self._build_naming_section())
        body.append(self._build_folder_section())
        body.append(self._build_cmd_section())

        scroller.set_child(body)
        self.append(scroller)

    # ---------- profile picker ----------

    def _build_profile_section(self) -> Gtk.Widget:
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        label = Gtk.Label(label="ProRes profile", xalign=0)
        label.add_css_class("section-label")
        section.append(label)
        sub = Gtk.Label(xalign=0)
        sub.add_css_class("section-sublabel")
        sub.set_wrap(True)
        sub.set_max_width_chars(50)
        sub.set_label("Higher bitrates preserve more detail. HQ is the editorial default.")
        section.append(sub)
        section.append(Gtk.Box(height_request=4))

        group_root: Optional[Gtk.ToggleButton] = None
        for profile in PROFILES:
            btn = Gtk.ToggleButton()
            btn.add_css_class("profile-row")
            btn.set_has_frame(False)
            btn.set_active(profile.id == self._settings.profile)
            handler_id = btn.connect("toggled", self._on_profile_toggled, profile.id)
            self._profile_handlers[profile.id] = handler_id
            if group_root is None:
                group_root = btn
            else:
                btn.set_group(group_root)

            inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            # Radio outer
            radio = Gtk.Box()
            radio.add_css_class("profile-radio-outer")
            radio.set_valign(Gtk.Align.CENTER)
            inner.append(radio)
            self._profile_radios[profile.id] = radio

            # Name + desc
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            col.set_hexpand(True)
            name_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            name = Gtk.Label(label=profile.name, xalign=0)
            name.add_css_class("profile-name")
            name_box.append(name)
            if profile.alpha:
                alpha_tag = Gtk.Label(label="+ alpha", xalign=0)
                alpha_tag.add_css_class("alpha-tag")
                name_box.append(alpha_tag)
            col.append(name_box)
            desc = Gtk.Label(label=profile.desc, xalign=0)
            desc.add_css_class("profile-desc")
            desc.set_ellipsize(Pango.EllipsizeMode.END)
            col.append(desc)
            inner.append(col)

            # Badge
            badge = Gtk.Label(label=f"PID {profile.pid}")
            badge.add_css_class("profile-badge")
            badge.set_valign(Gtk.Align.CENTER)
            inner.append(badge)

            btn.set_child(inner)
            self._profile_buttons[profile.id] = btn
            self._profile_rows[profile.id] = btn
            self._apply_profile_visual(profile.id, profile.id == self._settings.profile)
            section.append(btn)

        return section

    def _apply_profile_visual(self, profile_id: str, selected: bool) -> None:
        btn = self._profile_buttons.get(profile_id)
        radio = self._profile_radios.get(profile_id)
        if btn is None or radio is None:
            return
        if selected:
            btn.add_css_class("selected")
            radio.add_css_class("selected")
        else:
            btn.remove_css_class("selected")
            radio.remove_css_class("selected")

    def _on_profile_toggled(self, btn: Gtk.ToggleButton, profile_id: str) -> None:
        if not btn.get_active():
            return
        # Ensure only one row carries the .selected class.
        for pid in self._profile_buttons:
            self._apply_profile_visual(pid, pid == profile_id)
        if self._settings.profile != profile_id:
            self._settings.profile = profile_id
            # Force-off alpha if the new profile can't do it.
            if not PROFILES_BY_ID[profile_id].alpha and self._settings.alpha:
                self._settings.alpha = False
                self._set_alpha_switch_silent(False)
            self._refresh_alpha_row()
            self._update_cmd_preview()
            self.emit("settings-changed")

    # ---------- alpha toggle ----------

    def _build_alpha_section(self) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.add_css_class("toggle-row")
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        col.set_hexpand(True)
        title = Gtk.Label(label="Include alpha channel", xalign=0)
        title.add_css_class("toggle-label")
        col.append(title)
        self._alpha_sub = Gtk.Label(xalign=0)
        self._alpha_sub.add_css_class("toggle-sub")
        col.append(self._alpha_sub)
        row.append(col)

        switch = Gtk.Switch()
        switch.add_css_class("alpha-switch")
        switch.set_valign(Gtk.Align.CENTER)
        switch.set_active(self._settings.alpha)
        self._alpha_switch = switch
        self._alpha_handler_id = switch.connect("state-set", self._on_alpha_toggled)
        row.append(switch)

        self._alpha_row = row
        self._refresh_alpha_row()
        return row

    def _alpha_available(self) -> bool:
        return PROFILES_BY_ID[self._settings.profile].alpha

    def _refresh_alpha_row(self) -> None:
        available = self._alpha_available()
        if available:
            self._alpha_row.remove_css_class("disabled")
            self._alpha_sub.set_label("Available for 4444 and 4444 XQ only")
            self._alpha_switch.set_sensitive(not self._encoding_locked)
        else:
            self._alpha_row.add_css_class("disabled")
            self._alpha_sub.set_label("Requires 4444 or 4444 XQ profile")
            self._alpha_switch.set_sensitive(False)
            self._set_alpha_switch_silent(False)

    def _on_alpha_toggled(self, _switch: Gtk.Switch, state: bool) -> bool:
        if not self._alpha_available():
            return True
        self._settings.alpha = bool(state)
        self._update_cmd_preview()
        self.emit("settings-changed")
        return False

    def _set_alpha_switch_silent(self, on: bool) -> None:
        if self._alpha_handler_id:
            self._alpha_switch.handler_block(self._alpha_handler_id)
        try:
            self._alpha_switch.set_active(on)
        finally:
            if self._alpha_handler_id:
                self._alpha_switch.handler_unblock(self._alpha_handler_id)

    # ---------- audio bit depth toggle ----------

    def _build_audio_bits_section(self) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.add_css_class("toggle-row")
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        col.set_hexpand(True)
        title = Gtk.Label(label="24-bit audio", xalign=0)
        title.add_css_class("toggle-label")
        col.append(title)
        sub = Gtk.Label(
            label="Preserve full dynamic range from pro-camera sources. Off = 16-bit (editorial default, smaller files).",
            xalign=0,
        )
        sub.add_css_class("toggle-sub")
        sub.set_wrap(True)
        sub.set_max_width_chars(40)
        col.append(sub)
        row.append(col)

        switch = Gtk.Switch()
        switch.add_css_class("alpha-switch")  # re-use the accent-tinted style
        switch.set_valign(Gtk.Align.CENTER)
        switch.set_active(self._settings.audio_bits == 24)
        self._audio_bits_switch = switch
        self._audio_bits_handler_id = switch.connect("state-set", self._on_audio_bits_toggled)
        row.append(switch)
        return row

    def _on_audio_bits_toggled(self, _switch: Gtk.Switch, state: bool) -> bool:
        self._settings.audio_bits = 24 if state else 16
        self._update_cmd_preview()
        self.emit("settings-changed")
        return False

    # ---------- auto-reveal toggle ----------

    def _build_auto_reveal_section(self) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.add_css_class("toggle-row")
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        col.set_hexpand(True)
        title = Gtk.Label(label="Open output folder when done", xalign=0)
        title.add_css_class("toggle-label")
        col.append(title)
        sub = Gtk.Label(
            label="Pop the file manager open at the output folder once the queue completes.",
            xalign=0,
        )
        sub.add_css_class("toggle-sub")
        sub.set_wrap(True)
        sub.set_max_width_chars(40)
        col.append(sub)
        row.append(col)

        switch = Gtk.Switch()
        switch.add_css_class("alpha-switch")
        switch.set_valign(Gtk.Align.CENTER)
        switch.set_active(self._settings.auto_reveal)
        self._auto_reveal_switch = switch
        switch.connect("state-set", self._on_auto_reveal_toggled)
        row.append(switch)
        return row

    def _on_auto_reveal_toggled(self, _switch: Gtk.Switch, state: bool) -> bool:
        self._settings.auto_reveal = bool(state)
        self.emit("settings-changed")
        return False

    # ---------- naming ----------

    def _build_naming_section(self) -> Gtk.Widget:
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        label = Gtk.Label(label="Output naming", xalign=0)
        label.add_css_class("section-label")
        section.append(label)

        model = Gtk.StringList.new([
            "Keep original — OriginalName.mov",
            "Append suffix — OriginalName_prores_<profile>.mov",
        ])
        dropdown = Gtk.DropDown.new(model, None)
        dropdown.add_css_class("nocoder-select")
        dropdown.set_selected(0 if self._settings.naming == "keep" else 1)
        self._naming_dropdown = dropdown
        self._naming_handler_id = dropdown.connect("notify::selected", self._on_naming_changed)
        section.append(dropdown)
        return section

    def _on_naming_changed(self, dropdown: Gtk.DropDown, _pspec) -> None:
        idx = dropdown.get_selected()
        new = "keep" if idx == 0 else "suffix"
        if new != self._settings.naming:
            self._settings.naming = new
            self._update_cmd_preview()
            self.emit("settings-changed")

    # ---------- output folder ----------

    def _build_folder_section(self) -> Gtk.Widget:
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        label = Gtk.Label(label="Output folder", xalign=0)
        label.add_css_class("section-label")
        section.append(label)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.add_css_class("folder-row")
        folder_icon = Gtk.Image.new_from_icon_name("folder-symbolic")
        folder_icon.add_css_class("folder-icon")
        folder_icon.set_pixel_size(15)
        row.append(folder_icon)

        path = Gtk.Label(xalign=0)
        path.add_css_class("folder-path")
        path.set_hexpand(True)
        path.set_ellipsize(Pango.EllipsizeMode.START)
        path.set_label(self._settings.out_dir)
        self._folder_path = path
        row.append(path)

        browse = Gtk.Button(label="Browse…")
        browse.add_css_class("folder-browse")
        browse.connect("clicked", lambda _b: self.emit("choose-folder-requested"))
        self._browse_btn = browse
        row.append(browse)

        section.append(row)
        return section

    def set_output_folder(self, path: str) -> None:
        if path and path != self._settings.out_dir:
            self._settings.out_dir = path
            self._folder_path.set_label(path)
            self._update_cmd_preview()
            self.emit("settings-changed")

    # ---------- ffmpeg preview ----------

    def _build_cmd_section(self) -> Gtk.Widget:
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        disclosure = Gtk.Button()
        disclosure.add_css_class("cmd-disclosure")
        disclosure.set_has_frame(False)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._cmd_chevron = Gtk.Image.new_from_icon_name("pan-down-symbolic")
        self._cmd_chevron.set_pixel_size(12)
        row.append(self._cmd_chevron)
        terminal_icon = Gtk.Image.new_from_icon_name("utilities-terminal-symbolic")
        terminal_icon.set_pixel_size(13)
        row.append(terminal_icon)
        row.append(Gtk.Label(label="ffmpeg command preview"))
        disclosure.set_child(row)
        disclosure.connect("clicked", self._on_toggle_cmd)
        section.append(disclosure)

        self._cmd_scroller = Gtk.ScrolledWindow()
        self._cmd_scroller.add_css_class("cmd-box")
        self._cmd_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._cmd_scroller.set_min_content_height(60)
        self._cmd_scroller.set_max_content_height(180)

        self._cmd_view = Gtk.TextView()
        self._cmd_view.set_editable(False)
        self._cmd_view.set_cursor_visible(False)
        self._cmd_view.set_monospace(True)
        self._cmd_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._cmd_view.set_left_margin(0)
        self._cmd_view.set_right_margin(0)
        self._cmd_view.set_top_margin(0)
        self._cmd_view.set_bottom_margin(0)

        self._buffer = self._cmd_view.get_buffer()
        # TextTag foregrounds must be concrete colours (the `foreground` property
        # doesn't understand CSS named colours), so resolve them from the
        # active theme — keyword = accent, flag = warning (ANSI yellow), string
        # = success (ANSI green). Re-resolved on every call in case the widget
        # wasn't realised the first time.
        kw_hex  = _resolve_theme_hex(self._cmd_view, "accent_color",      "#bb9af7")
        fl_hex  = _resolve_theme_hex(self._cmd_view, "warning_bg_color",  "#ff8c42")
        str_hex = _resolve_theme_hex(self._cmd_view, "success_bg_color",  "#9ece6a")
        self._tag_keyword = self._buffer.create_tag("keyword", foreground=kw_hex, weight=Pango.Weight.BOLD)
        self._tag_flag = self._buffer.create_tag("flag", foreground=fl_hex)
        self._tag_string = self._buffer.create_tag("string", foreground=str_hex)

        self._cmd_scroller.set_child(self._cmd_view)
        section.append(self._cmd_scroller)

        self._update_cmd_preview()
        return section

    def _on_toggle_cmd(self, _btn: Gtk.Button) -> None:
        self._cmd_visible = not self._cmd_visible
        self._cmd_scroller.set_visible(self._cmd_visible)
        self._cmd_chevron.set_from_icon_name("pan-down-symbolic" if self._cmd_visible else "pan-end-symbolic")

    def _update_cmd_preview(self) -> None:
        if not hasattr(self, "_buffer"):
            return
        if self._first_file_name:
            stem = Path(self._first_file_name).stem
            suffix = f"_prores_{self._settings.profile}" if self._settings.naming == "suffix" else ""
            out_path = f"{self._settings.out_dir.rstrip('/')}/{stem}{suffix}.mov"
            text = format_preview_command(
                self._first_file_name, out_path, self._settings.profile, self._settings.alpha,
                audio_bits=self._settings.audio_bits,
            )
        else:
            text = "# Add files to see the ffmpeg command"
        self._buffer.set_text(text)
        self._apply_highlighting()

    def _apply_highlighting(self) -> None:
        buf = self._buffer
        start = buf.get_start_iter()
        end = buf.get_end_iter()
        text = buf.get_text(start, end, True)

        # Tag 'ffmpeg' keyword (only the first token)
        m = re.match(r"\s*ffmpeg\b", text)
        if m:
            s = buf.get_iter_at_offset(m.start())
            e = buf.get_iter_at_offset(m.end())
            buf.apply_tag(self._tag_keyword, s, e)

        # Tag flags: -word and -c:v style
        for m in re.finditer(r"(?<!\w)-[A-Za-z][\w:]*", text):
            s = buf.get_iter_at_offset(m.start())
            e = buf.get_iter_at_offset(m.end())
            buf.apply_tag(self._tag_flag, s, e)

        # Tag double-quoted strings
        for m in re.finditer(r'"[^"\n]*"', text):
            s = buf.get_iter_at_offset(m.start())
            e = buf.get_iter_at_offset(m.end())
            buf.apply_tag(self._tag_string, s, e)


