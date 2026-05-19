"""SystemPane — collapsible per-core CPU visualizer (btop-style).

A thin row sandwiched between the main split and the footer. Header shows a
disclosure toggle plus a "CPU" label and an aggregate percentage; the body is
a ``Gtk.DrawingArea`` rendering one vertical bar per logical core using Cairo.

Polls /proc/stat once a second via ``GLib.timeout_add_seconds``. The timer
returns ``False`` (auto-stops) once the widget is detached from any root, so
no explicit teardown signal wiring is needed.
"""
from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("GObject", "2.0")
from gi.repository import GLib, GObject, Gtk

from .config import update_config
from .cpu_sampler import CpuSampler


_SAMPLE_INTERVAL_SECONDS = 1
_BAR_MIN_WIDTH = 4
_BAR_MAX_WIDTH = 14
_BAR_GAP = 2
_AREA_MIN_HEIGHT = 56


class SystemPane(Gtk.Box):
    __gtype_name__ = "NoCoderSystemPane"

    def __init__(self, *, initial_expanded: bool = True) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("system-pane")
        self.set_hexpand(True)

        self._sampler = CpuSampler()
        self._last: list[float] = []

        # ---- Header row: toggle ▾/▸  +  "CPU" label  +  aggregate %  ----
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.add_css_class("system-pane-header")

        self._toggle = Gtk.ToggleButton()
        self._toggle.add_css_class("flat")
        self._toggle.add_css_class("system-pane-toggle")
        self._toggle.set_active(initial_expanded)
        self._toggle_label = Gtk.Label()
        self._toggle.set_child(self._toggle_label)
        self._toggle.connect("toggled", self._on_toggled)
        header.append(self._toggle)

        title = Gtk.Label(label="CPU", xalign=0)
        title.add_css_class("system-pane-title")
        header.append(title)

        self._agg_label = Gtk.Label(label="—", xalign=1.0)
        self._agg_label.add_css_class("system-pane-agg")
        self._agg_label.set_hexpand(True)
        header.append(self._agg_label)

        self.append(header)

        # ---- Body: revealer wrapping the cairo bar canvas ----
        self._revealer = Gtk.Revealer()
        self._revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._revealer.set_transition_duration(150)
        self._revealer.set_reveal_child(initial_expanded)

        self._area = Gtk.DrawingArea()
        self._area.add_css_class("cpu-bar-area")
        self._area.set_content_height(_AREA_MIN_HEIGHT)
        self._area.set_hexpand(True)
        self._area.set_draw_func(self._draw, None)
        self._revealer.set_child(self._area)
        self.append(self._revealer)

        self._refresh_toggle_label()

        # Kick off the polling timer. Returns False to auto-cleanup when the
        # pane is no longer rooted (e.g. window closed).
        GLib.timeout_add_seconds(_SAMPLE_INTERVAL_SECONDS, self._tick)
        # Render an initial frame immediately so bars aren't blank for a second.
        self._tick()

    # ---------- timer ----------

    def _tick(self) -> bool:
        if self.get_root() is None:
            return False
        self._last = self._sampler.sample()
        if self._last:
            avg = sum(self._last) / len(self._last)
            self._agg_label.set_text(f"{avg:5.1f}%  ·  {len(self._last)} threads")
        self._area.queue_draw()
        return True

    # ---------- toggle ----------

    def _on_toggled(self, _btn: Gtk.ToggleButton) -> None:
        expanded = self._toggle.get_active()
        self._revealer.set_reveal_child(expanded)
        self._refresh_toggle_label()
        update_config({"cpu_pane_expanded": expanded})

    def _refresh_toggle_label(self) -> None:
        self._toggle_label.set_text("▾" if self._toggle.get_active() else "▸")

    # ---------- draw ----------

    def _draw(self, area: Gtk.DrawingArea, cr, w: int, h: int, _user) -> None:
        n = len(self._last)
        if n <= 0 or w <= 0 or h <= 0:
            return

        # The widget's foreground colour (set via CSS to the theme accent)
        # paints the active portion of each bar.
        accent = area.get_color()

        # Bar width adapts to fit N cores across the canvas. Cap so a 4-core
        # box doesn't get giant bars and a 64-core box doesn't get hairlines.
        bar_w = (w - _BAR_GAP * (n - 1)) / n
        bar_w = max(_BAR_MIN_WIDTH, min(_BAR_MAX_WIDTH, bar_w))
        total_w = bar_w * n + _BAR_GAP * (n - 1)
        x0 = (w - total_w) / 2  # centre the row

        for i, pct in enumerate(self._last):
            x = x0 + i * (bar_w + _BAR_GAP)
            # Muted track
            cr.set_source_rgba(accent.red, accent.green, accent.blue, 0.14)
            cr.rectangle(x, 0, bar_w, h)
            cr.fill()
            # Filled portion (bottom-up)
            fill_h = max(1.0, h * (pct / 100.0))
            cr.set_source_rgba(accent.red, accent.green, accent.blue, accent.alpha)
            cr.rectangle(x, h - fill_h, bar_w, fill_h)
            cr.fill()
