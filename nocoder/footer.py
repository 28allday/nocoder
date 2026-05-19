"""Footer / action bar. Three variants: ready, encoding, complete.

Emits:
  encode-requested   ()
  cancel-requested   ()
  reveal-requested   ()
"""
from __future__ import annotations

from typing import Optional

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("GObject", "2.0")
from gi.repository import GObject, Gtk, Pango

from .data import (
    PROFILES_BY_ID,
    estimate_encode_seconds,
    format_bytes,
    format_duration,
)
from .queue_pane import FileEntry


class Footer(Gtk.Box):
    __gtype_name__ = "NoCoderFooter"

    __gsignals__ = {
        "encode-requested": (GObject.SignalFlags.RUN_LAST, None, ()),
        "cancel-requested": (GObject.SignalFlags.RUN_LAST, None, ()),
        "reveal-requested": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.add_css_class("footer-bar")
        self.set_hexpand(True)

        self._state = "ready"  # ready | encoding | complete
        self._files: list[FileEntry] = []
        self._profile_id = "hq"
        self._overall = 0.0
        self._current_idx = 0
        self._speed: Optional[float] = None
        self._elapsed: Optional[float] = None

        self._build()

    # ---------- external API ----------

    def update(self, state: str, files: list[FileEntry], profile_id: str,
               overall: float, current_idx: int,
               speed: Optional[float] = None,
               elapsed: Optional[float] = None) -> None:
        self._state = state
        self._files = files
        self._profile_id = profile_id
        self._overall = max(0.0, min(1.0, overall))
        self._current_idx = current_idx
        self._speed = speed
        self._elapsed = elapsed
        self._render()

    # ---------- build ----------

    def _build(self) -> None:
        # Build both variants once, toggle visibility in _render.
        self._ready_box = self._build_ready()
        self._encoding_box = self._build_encoding()
        self._complete_box = self._build_complete()

        self.append(self._ready_box)
        self.append(self._encoding_box)
        self.append(self._complete_box)
        self._render()

    def _build_ready(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
        box.set_hexpand(True)

        stats = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=28)
        stats.set_hexpand(True)

        self._stat_files = _make_stat("Files", "0")
        stats.append(self._stat_files.root)
        stats.append(_divider())

        self._stat_dur = _make_stat("Total duration", "—", small=True)
        stats.append(self._stat_dur.root)
        stats.append(_divider())

        self._stat_out_box = _make_io_stat("Estimated output")
        stats.append(self._stat_out_box.root)
        stats.append(_divider())

        self._stat_eta = _make_stat("Est. encode time", "—", small=True, with_clock=True)
        stats.append(self._stat_eta.root)

        box.append(stats)

        self._encode_btn = Gtk.Button()
        self._encode_btn.add_css_class("encode-cta")
        self._encode_btn.set_child(_icon_label_light("media-playback-start-symbolic", "Encode"))
        self._encode_btn.connect("clicked", lambda _b: self.emit("encode-requested"))
        box.append(self._encode_btn)
        return box

    def _build_encoding(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        box.set_hexpand(True)

        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        center.set_hexpand(True)

        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self._enc_title = Gtk.Label(xalign=0)
        self._enc_title.add_css_class("progress-title")
        self._enc_title.set_ellipsize(Pango.EllipsizeMode.END)
        self._enc_title.set_hexpand(True)
        title_row.append(self._enc_title)
        self._enc_pct = Gtk.Label(xalign=1.0)
        self._enc_pct.add_css_class("progress-title")
        self._enc_pct.add_css_class("pct")
        title_row.append(self._enc_pct)
        center.append(title_row)

        self._enc_progress = Gtk.ProgressBar()
        self._enc_progress.add_css_class("overall-progress")
        center.append(self._enc_progress)

        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self._enc_status_left = Gtk.Label(xalign=0)
        self._enc_status_left.add_css_class("progress-status")
        self._enc_status_left.set_use_markup(True)
        self._enc_status_left.set_hexpand(True)
        status_row.append(self._enc_status_left)
        self._enc_eta = Gtk.Label(xalign=1.0)
        self._enc_eta.add_css_class("progress-status")
        status_row.append(self._enc_eta)
        center.append(status_row)

        box.append(center)

        cancel = Gtk.Button()
        cancel.add_css_class("cancel-btn")
        cancel.set_child(_icon_label("process-stop-symbolic", "Cancel"))
        cancel.connect("clicked", lambda _b: self.emit("cancel-requested"))
        box.append(cancel)
        return box

    def _build_complete(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
        box.set_hexpand(True)

        stats = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=28)
        stats.set_hexpand(True)
        self._stat_ok = _make_stat("Succeeded", "0")
        self._stat_ok.value.add_css_class("success")
        stats.append(self._stat_ok.root)
        stats.append(_divider())
        self._stat_fail = _make_stat("Failed", "0")
        stats.append(self._stat_fail.root)
        stats.append(_divider())
        self._stat_out = _make_stat("Output size", "—", small=True)
        stats.append(self._stat_out.root)
        stats.append(_divider())
        self._stat_total_time = _make_stat("Total time", "—", small=True, with_clock=True)
        stats.append(self._stat_total_time.root)
        box.append(stats)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        reveal = Gtk.Button()
        reveal.add_css_class("reveal-btn")
        reveal.set_child(_icon_label("folder-symbolic", "Reveal in Files"))
        reveal.connect("clicked", lambda _b: self.emit("reveal-requested"))
        actions.append(reveal)
        again = Gtk.Button()
        again.add_css_class("encode-cta")
        again.set_child(_icon_label_light("media-playback-start-symbolic", "Encode again"))
        again.connect("clicked", lambda _b: self.emit("encode-requested"))
        actions.append(again)
        box.append(actions)
        return box

    # ---------- render ----------

    def _render(self) -> None:
        self._ready_box.set_visible(self._state == "ready")
        self._encoding_box.set_visible(self._state == "encoding")
        self._complete_box.set_visible(self._state == "complete")

        if self._state == "ready":
            self._render_ready()
        elif self._state == "encoding":
            self._render_encoding()
        else:
            self._render_complete()

    def _render_ready(self) -> None:
        files = self._files
        total_in = sum(f.size for f in files)
        total_out = sum(f.est_out for f in files)
        total_dur = sum((f.meta.duration or 0) for f in files)
        est_sec = estimate_encode_seconds(total_dur, self._profile_id)

        self._stat_files.value.set_text(str(len(files)))
        self._stat_dur.value.set_text(format_duration(total_dur) if total_dur else "—")
        self._stat_out_box.in_lbl.set_text(format_bytes(total_in) if total_in else "—")
        self._stat_out_box.out_lbl.set_text(format_bytes(total_out) if total_out else "—")
        self._stat_eta.value.set_text(f"~{format_duration(est_sec)}" if est_sec else "—")

        can_encode = len(files) > 0
        self._encode_btn.set_sensitive(can_encode)
        child = self._encode_btn.get_child()
        # Replace the label text based on file count.
        n = len(files)
        text = f"Encode {n} file{'s' if n != 1 else ''}" if n else "Encode"
        _set_icon_label_text(child, text)

    def _render_encoding(self) -> None:
        files = self._files
        if not files:
            self._enc_title.set_text("")
            self._enc_pct.set_text("0%")
            self._enc_progress.set_fraction(0)
            return
        idx = max(0, min(self._current_idx, len(files) - 1))
        f = files[idx]
        self._enc_title.set_markup(
            f'<span foreground="#7982a9">[{idx + 1}/{len(files)}]</span> {GLib_markup_escape(f.name)}'
        )
        pct = int(round(self._overall * 100))
        self._enc_pct.set_text(f"{pct}%")
        self._enc_progress.set_fraction(self._overall)

        done = sum(1 for x in files if x.status == "done")
        failed = sum(1 for x in files if x.status == "failed")
        queued = len(files) - done - failed - (1 if f.status == "encoding" else 0)
        queued = max(0, queued)
        parts = [f'<span class="ok" foreground="#9ece6a">● {done} done</span>']
        if failed:
            parts.append(f'<span class="fail" foreground="#e06c75">● {failed} failed</span>')
        parts.append(f'● {queued} queued')
        self._enc_status_left.set_markup("  ".join(parts))

        # ETA estimate. If ffmpeg has reported a real speed, refine the
        # remaining-time estimate from actual throughput rather than the
        # profile-specific heuristic — much closer to real once the encode
        # is past its first second or so.
        total_dur = sum((x.meta.duration or 0) for x in files)
        if self._speed and self._speed > 0:
            remaining_src_sec = total_dur * (1 - self._overall)
            remaining = remaining_src_sec / self._speed
        else:
            est_total = estimate_encode_seconds(total_dur, self._profile_id)
            remaining = max(0.0, est_total * (1 - self._overall))
        eta_text = f"~{format_duration(remaining)} remaining"
        if self._speed:
            eta_text += f"  ·  {self._speed:.2f}×"
        self._enc_eta.set_text(eta_text)

    def _render_complete(self) -> None:
        files = self._files
        ok = sum(1 for f in files if f.status == "done")
        fail = sum(1 for f in files if f.status == "failed")
        total_out = sum(f.est_out for f in files if f.status == "done")
        self._stat_ok.value.set_text(str(ok))
        self._stat_fail.value.set_text(str(fail))
        if fail > 0:
            self._stat_fail.value.add_css_class("danger")
        else:
            self._stat_fail.value.remove_css_class("danger")
        self._stat_out.value.set_text(format_bytes(total_out) if total_out else "—")
        if self._elapsed is not None and self._elapsed > 0:
            self._stat_total_time.value.set_text(format_duration(self._elapsed))
        else:
            self._stat_total_time.value.set_text("—")


# ---------- helpers ----------


class _Stat:
    __slots__ = ("root", "value")

    def __init__(self, root: Gtk.Widget, value: Gtk.Label) -> None:
        self.root = root
        self.value = value


class _IOStat:
    __slots__ = ("root", "in_lbl", "arrow_lbl", "out_lbl")

    def __init__(self, root: Gtk.Widget, in_lbl: Gtk.Label, arrow_lbl: Gtk.Label, out_lbl: Gtk.Label) -> None:
        self.root = root
        self.in_lbl = in_lbl
        self.arrow_lbl = arrow_lbl
        self.out_lbl = out_lbl


def _make_stat(label: str, value: str, *, small: bool = False, with_clock: bool = False) -> _Stat:
    col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    lbl = Gtk.Label(label=label.upper(), xalign=0)
    lbl.add_css_class("stat-label")
    col.append(lbl)
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
    if with_clock:
        icon = Gtk.Image.new_from_icon_name("preferences-system-time-symbolic")
        icon.set_pixel_size(11)
        row.append(icon)
    val = Gtk.Label(label=value, xalign=0)
    val.add_css_class("stat-value-sm" if small else "stat-value")
    row.append(val)
    col.append(row)
    return _Stat(col, val)


def _make_io_stat(label: str) -> _IOStat:
    col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    lbl = Gtk.Label(label=label.upper(), xalign=0)
    lbl.add_css_class("stat-label")
    col.append(lbl)
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    in_lbl = Gtk.Label(label="—", xalign=0)
    in_lbl.add_css_class("stat-value-sm")
    in_lbl.add_css_class("stat-in")
    row.append(in_lbl)
    arrow = Gtk.Label(label="→", xalign=0)
    arrow.add_css_class("stat-value-sm")
    arrow.add_css_class("stat-arrow")
    row.append(arrow)
    out_lbl = Gtk.Label(label="—", xalign=0)
    out_lbl.add_css_class("stat-value-sm")
    out_lbl.add_css_class("stat-out")
    row.append(out_lbl)
    col.append(row)
    return _IOStat(col, in_lbl, arrow, out_lbl)


def _divider() -> Gtk.Widget:
    div = Gtk.Box()
    div.add_css_class("footer-divider")
    return div


def _icon_label(icon_name: str, text: str) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    img = Gtk.Image.new_from_icon_name(icon_name)
    img.set_pixel_size(14)
    box.append(img)
    lbl = Gtk.Label(label=text)
    box.append(lbl)
    box._nocoder_label = lbl  # type: ignore[attr-defined]
    return box


def _icon_label_light(icon_name: str, text: str) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    img = Gtk.Image.new_from_icon_name(icon_name)
    img.set_pixel_size(14)
    box.append(img)
    lbl = Gtk.Label(label=text)
    box.append(lbl)
    box._nocoder_label = lbl  # type: ignore[attr-defined]
    return box


def _set_icon_label_text(widget: Optional[Gtk.Widget], text: str) -> None:
    if widget is None:
        return
    lbl = getattr(widget, "_nocoder_label", None)
    if lbl is not None:
        lbl.set_label(text)


def GLib_markup_escape(s: str) -> str:
    # Small helper so we don't have to import GLib just for this.
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
    )
