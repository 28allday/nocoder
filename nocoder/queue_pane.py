"""Queue pane: drop zone (empty) or file list (populated), with action bar.

Emits:
  add-files-requested  ()
  add-folder-requested ()
  clear-requested      ()
  files-dropped        (paths: GLib.Variant[array of str])
  selection-changed    (file_id: str)
  remove-requested     (file_id: str)
"""
from __future__ import annotations

import os
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
_DROP_LOGO_PATH = _ASSETS_DIR / "logo.png"
_DROP_LOGO_SIZE = 88

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("GObject", "2.0")
from gi.repository import GdkPixbuf, GLib, GObject, Gdk, Gio, Gtk

from .data import VIDEO_EXTENSIONS, format_bytes, format_duration
from .encoder import Metadata


@dataclass
class FileEntry:
    path: str
    size: int
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    meta: Metadata = field(default_factory=Metadata)
    est_out: float = 0.0
    status: str = "queued"  # queued | encoding | done | failed
    progress: float = 0.0   # 0..1
    error: Optional[str] = None

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


class QueuePane(Gtk.Box):
    __gtype_name__ = "NoCoderQueuePane"

    __gsignals__ = {
        "add-files-requested": (GObject.SignalFlags.RUN_LAST, None, ()),
        "add-folder-requested": (GObject.SignalFlags.RUN_LAST, None, ()),
        "clear-requested": (GObject.SignalFlags.RUN_LAST, None, ()),
        "files-dropped": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        "selection-changed": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "remove-requested": (GObject.SignalFlags.RUN_LAST, None, (str,)),
    }

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("queue-pane")
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._files: list[FileEntry] = []
        self._selected_id: Optional[str] = None
        self._encoding_locked: bool = False
        self._row_by_id: dict[str, Gtk.ListBoxRow] = {}
        self._body_child: Optional[Gtk.Widget] = None
        self._search_query: str = ""

        self._build_header()
        self._build_action_bar()
        self._build_body_stack()
        self._install_drop_target(self)

    # ---------- header ----------

    def _build_header(self) -> None:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        header.add_css_class("pane-header")
        header.set_hexpand(True)

        label = Gtk.Label(label="QUEUE", xalign=0)
        label.add_css_class("pane-label")
        label.set_hexpand(True)
        header.append(label)

        right = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._count_chip = Gtk.Label(label="0")
        self._count_chip.add_css_class("count-chip")
        right.append(self._count_chip)

        self._size_chip = Gtk.Label(label="")
        self._size_chip.add_css_class("count-chip")
        self._size_chip.add_css_class("secondary")
        self._size_chip.set_visible(False)
        right.append(self._size_chip)

        header.append(right)
        self.append(header)

    def _build_action_bar(self) -> None:
        self._action_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._action_bar.add_css_class("action-bar")
        self._action_bar.set_visible(False)

        add_files = Gtk.Button()
        add_files.add_css_class("muted-btn")
        add_files.set_child(_icon_label("list-add-symbolic", "Add files"))
        add_files.connect("clicked", lambda _b: self.emit("add-files-requested"))
        self._action_bar.append(add_files)

        add_folder = Gtk.Button()
        add_folder.add_css_class("muted-btn")
        add_folder.set_child(_icon_label("folder-symbolic", "Add folder"))
        add_folder.connect("clicked", lambda _b: self.emit("add-folder-requested"))
        self._action_bar.append(add_folder)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        self._action_bar.append(spacer)

        clear = Gtk.Button()
        clear.add_css_class("muted-btn")
        clear.add_css_class("clear-btn")
        clear.set_child(_icon_label("user-trash-symbolic", "Clear"))
        clear.connect("clicked", lambda _b: self.emit("clear-requested"))
        self._clear_btn = clear
        self._action_bar.append(clear)

        self.append(self._action_bar)

    def _build_body_stack(self) -> None:
        # A body container we swap between drop zone and scrolled list.
        self._body_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._body_box.set_vexpand(True)
        self._body_box.set_hexpand(True)
        self.append(self._body_box)
        self._show_drop_zone()

    # ---------- drop zone ----------

    def _show_drop_zone(self) -> None:
        self._clear_body()
        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        wrapper.set_vexpand(True)
        wrapper.set_hexpand(True)

        drop = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        drop.add_css_class("drop-zone")
        drop.set_vexpand(True)
        drop.set_hexpand(True)
        drop.set_halign(Gtk.Align.FILL)
        drop.set_valign(Gtk.Align.FILL)

        # Spacer pushes content to center vertically.
        top_spacer = Gtk.Box()
        top_spacer.set_vexpand(True)
        drop.append(top_spacer)

        drop.append(_build_drop_logo())

        heading = Gtk.Label(label="Drop videos here")
        heading.add_css_class("drop-heading")
        heading.set_halign(Gtk.Align.CENTER)
        drop.append(heading)

        sub = Gtk.Label()
        sub.add_css_class("drop-sub")
        sub.set_halign(Gtk.Align.CENTER)
        sub.set_justify(Gtk.Justification.CENTER)
        sub.set_wrap(True)
        sub.set_max_width_chars(44)
        sub.set_markup(
            'Or <span foreground="#ff8c42" weight="500">browse files</span> to add them to the queue. '
            "Whole folders work too — non-video files are ignored."
        )
        drop.append(sub)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.CENTER)
        primary = Gtk.Button()
        primary.add_css_class("muted-btn")
        primary.add_css_class("accent-outline")
        primary.set_child(_icon_label("list-add-symbolic", "Add files"))
        primary.connect("clicked", lambda _b: self.emit("add-files-requested"))
        buttons.append(primary)

        secondary = Gtk.Button()
        secondary.add_css_class("muted-btn")
        secondary.set_child(_icon_label("folder-symbolic", "Add folder"))
        secondary.connect("clicked", lambda _b: self.emit("add-folder-requested"))
        buttons.append(secondary)
        drop.append(buttons)

        hint = Gtk.Label()
        hint.add_css_class("drop-hint")
        hint.set_halign(Gtk.Align.CENTER)
        hint.set_markup(
            'Accepts <span face="JetBrains Mono">.mov .mp4 .mkv .avi .mxf .mts</span>'
            ' and more. Folders and camera cards are scanned recursively.'
        )
        drop.append(hint)

        bottom_spacer = Gtk.Box()
        bottom_spacer.set_vexpand(True)
        drop.append(bottom_spacer)

        wrapper.append(drop)
        self._body_box.append(wrapper)
        self._body_child = wrapper
        self._drop_widget = drop

    # ---------- list view ----------

    def _show_list(self) -> None:
        self._clear_body()
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_hexpand(True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        listbox = Gtk.ListBox()
        listbox.add_css_class("queue-list")
        listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        listbox.connect("row-activated", self._on_row_activated)
        listbox.connect("row-selected", self._on_row_selected)
        placeholder = Gtk.Label(label="No files match your search.")
        placeholder.add_css_class("queue-empty-matches")
        placeholder.set_halign(Gtk.Align.CENTER)
        placeholder.set_valign(Gtk.Align.CENTER)
        listbox.set_placeholder(placeholder)
        self._listbox = listbox

        scroller.set_child(listbox)
        self._body_box.append(scroller)
        self._body_child = scroller

    def _clear_body(self) -> None:
        if self._body_child is not None:
            self._body_box.remove(self._body_child)
            self._body_child = None
        self._row_by_id.clear()
        self._drop_widget = None

    # ---------- drop target ----------

    def _install_drop_target(self, widget: Gtk.Widget) -> None:
        # Accept several value types — different file managers (Nautilus,
        # Thunar, Files under XWayland, etc.) deliver drops as Gdk.FileList,
        # a single Gio.File, or a text/uri-list string.
        actions = Gdk.DragAction.COPY | Gdk.DragAction.MOVE | Gdk.DragAction.LINK
        target = Gtk.DropTarget.new(Gdk.FileList, actions)
        target.set_gtypes([Gdk.FileList, Gio.File, GObject.TYPE_STRING])
        target.set_preload(True)
        target.connect("drop", self._on_drop)
        target.connect("enter", self._on_drop_enter)
        target.connect("motion", self._on_drop_motion)
        target.connect("leave", self._on_drop_leave)
        widget.add_controller(target)

    def _on_drop(self, _target: Gtk.DropTarget, value, _x: float, _y: float) -> bool:
        paths = _paths_from_drop_value(value)
        self._set_drop_hover(False)
        if not paths:
            return False
        self.emit("files-dropped", paths)
        return True

    def _on_drop_enter(self, _target: Gtk.DropTarget, _x: float, _y: float) -> Gdk.DragAction:
        self._set_drop_hover(True)
        return Gdk.DragAction.COPY

    def _on_drop_motion(self, _target: Gtk.DropTarget, _x: float, _y: float) -> Gdk.DragAction:
        return Gdk.DragAction.COPY

    def _on_drop_leave(self, _target: Gtk.DropTarget) -> None:
        self._set_drop_hover(False)

    def _set_drop_hover(self, on: bool) -> None:
        w = getattr(self, "_drop_widget", None)
        if w is None:
            return
        if on:
            w.add_css_class("drop-hover")
        else:
            w.remove_css_class("drop-hover")

    # ---------- external API ----------

    def set_encoding(self, encoding: bool) -> None:
        self._encoding_locked = encoding
        self._clear_btn.set_sensitive(not encoding)
        for row in self._row_by_id.values():
            btn = getattr(row, "_nocoder_widgets", {}).get("remove")
            if btn is not None:
                btn.set_sensitive(not encoding)

    def set_files(self, files: list[FileEntry]) -> None:
        self._files = list(files)
        self._refresh_header()
        self._action_bar.set_visible(bool(self._files))
        if not self._files:
            self._show_drop_zone()
            return
        self._show_list()
        self._populate_list()
        self._apply_selection()

    def set_search_query(self, query: str) -> None:
        new_q = (query or "").strip().lower()
        if new_q == self._search_query:
            return
        self._search_query = new_q
        if not self._files:
            return
        if getattr(self, "_listbox", None) is None:
            return
        self._populate_list()
        self._apply_selection()

    def _populate_list(self) -> None:
        # Clear existing rows.
        self._row_by_id.clear()
        child = self._listbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._listbox.remove(child)
            child = nxt
        # Append rows that match the current search query (empty = all).
        q = self._search_query
        for entry in self._files:
            if q and q not in entry.name.lower():
                continue
            row = self._build_row(entry)
            self._listbox.append(row)
            self._row_by_id[entry.id] = row

    def update_file(self, entry: FileEntry) -> None:
        """Called when a single file's metadata/progress/status changed. Updates row in place."""
        for i, f in enumerate(self._files):
            if f.id == entry.id:
                self._files[i] = entry
                break
        else:
            return
        row = self._row_by_id.get(entry.id)
        if row is None:
            return
        old_widgets = getattr(row, "_nocoder_widgets", {})
        _populate_row(row, entry, old_widgets)
        self._refresh_header()

    def set_selected(self, file_id: Optional[str]) -> None:
        self._selected_id = file_id
        self._apply_selection()

    # ---------- internals ----------

    def _refresh_header(self) -> None:
        self._count_chip.set_text(str(len(self._files)))
        if self._files:
            total_in = sum(f.size for f in self._files)
            total_out = sum(f.est_out for f in self._files)
            self._size_chip.set_text(f"{format_bytes(total_in)} → {format_bytes(total_out)}")
            self._size_chip.set_visible(True)
        else:
            self._size_chip.set_visible(False)

    def _apply_selection(self) -> None:
        for fid, row in self._row_by_id.items():
            inner = getattr(row, "_nocoder_widgets", {}).get("container")
            if inner is None:
                continue
            if fid == self._selected_id:
                inner.add_css_class("selected")
            else:
                inner.remove_css_class("selected")

    def _on_row_activated(self, _lb, row: Gtk.ListBoxRow) -> None:
        fid = getattr(row, "_nocoder_id", None)
        if fid:
            self._selected_id = fid
            self._apply_selection()
            self.emit("selection-changed", fid)

    def _on_row_selected(self, _lb, row: Optional[Gtk.ListBoxRow]) -> None:
        if row is None:
            return
        fid = getattr(row, "_nocoder_id", None)
        if fid:
            self._selected_id = fid
            self._apply_selection()
            self.emit("selection-changed", fid)

    def _build_row(self, entry: FileEntry) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_activatable(True)
        row.set_selectable(True)
        row._nocoder_id = entry.id
        widgets: dict[str, Gtk.Widget] = {}

        container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        container.add_css_class("file-row")
        widgets["container"] = container

        # Thumbnail
        thumb = Gtk.Image.new_from_icon_name("video-x-generic-symbolic")
        thumb.add_css_class("file-thumb")
        thumb.set_pixel_size(18)
        container.append(thumb)

        # Center: name + meta + progress
        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        center.set_hexpand(True)
        center.set_valign(Gtk.Align.CENTER)
        name = Gtk.Label(xalign=0)
        name.add_css_class("filename")
        name.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        name.set_hexpand(True)
        widgets["name"] = name
        center.append(name)

        meta_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        meta_box.add_css_class("file-meta")
        widgets["meta_box"] = meta_box
        center.append(meta_box)

        progress = Gtk.ProgressBar()
        progress.add_css_class("file-progress")
        progress.set_visible(False)
        widgets["progress"] = progress
        center.append(progress)

        container.append(center)

        # Right: input size + est out
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        right.set_halign(Gtk.Align.END)
        right.set_valign(Gtk.Align.CENTER)
        size_lbl = Gtk.Label(xalign=1.0)
        size_lbl.add_css_class("file-size")
        widgets["size"] = size_lbl
        right.append(size_lbl)
        est_lbl = Gtk.Label(xalign=1.0)
        est_lbl.add_css_class("file-estout")
        widgets["est"] = est_lbl
        right.append(est_lbl)
        container.append(right)

        # Status dot
        dot = Gtk.Box()
        dot.add_css_class("status-dot")
        dot.set_halign(Gtk.Align.CENTER)
        dot.set_valign(Gtk.Align.CENTER)
        widgets["dot"] = dot
        container.append(dot)

        # Remove button (hover-revealed via CSS).
        remove = Gtk.Button()
        remove.add_css_class("file-row-remove")
        remove.set_child(Gtk.Image.new_from_icon_name("window-close-symbolic"))
        remove.set_tooltip_text("Remove from queue")
        remove.set_valign(Gtk.Align.CENTER)
        remove.set_can_focus(False)
        remove.set_sensitive(not self._encoding_locked)
        remove.connect("clicked", lambda _b, fid=entry.id: self.emit("remove-requested", fid))
        widgets["remove"] = remove
        container.append(remove)

        row.set_child(container)
        row._nocoder_widgets = widgets
        _populate_row(row, entry, widgets)
        return row


# ---------- module-level helpers ----------


def _build_drop_logo() -> Gtk.Widget:
    """The NO-CODER brand mark shown above the drop-zone copy.

    Pre-scales the source PNG to 2× the display size so HiDPI stays crisp,
    then wraps it in a Gtk.Image so the rendered size is exactly what we ask
    for (Gtk.Picture's natural size is the source's 800×800 and only acts as
    a minimum, so size_request can't shrink it).
    """
    if _DROP_LOGO_PATH.exists():
        try:
            hidpi = _DROP_LOGO_SIZE * 2
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(_DROP_LOGO_PATH), hidpi, hidpi, True
            )
            img = Gtk.Image.new_from_pixbuf(pb)
            img.set_pixel_size(_DROP_LOGO_SIZE)
            img.set_halign(Gtk.Align.CENTER)
            img.add_css_class("drop-logo")
            return img
        except GLib.Error:
            pass
    icon = Gtk.Image.new_from_icon_name("video-x-generic-symbolic")
    icon.set_pixel_size(40)
    icon.add_css_class("drop-icon")
    icon.set_halign(Gtk.Align.CENTER)
    return icon


def _paths_from_drop_value(value) -> list[str]:
    """Extract local filesystem paths from whatever a Gtk.DropTarget delivered.

    Supports Gdk.FileList (multi-file drops), a single Gio.File, and a
    text/uri-list-style string (lines of file:// URIs or plain paths).
    """
    paths: list[str] = []
    if value is None:
        return paths
    # Gdk.FileList
    if hasattr(value, "get_files"):
        try:
            for f in value.get_files():
                p = f.get_path() if hasattr(f, "get_path") else None
                if p:
                    paths.append(p)
            return paths
        except Exception:
            pass
    # Single Gio.File
    if hasattr(value, "get_path"):
        p = value.get_path()
        if p:
            paths.append(p)
        return paths
    # text/uri-list or raw path string
    if isinstance(value, str):
        for line in value.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("file:"):
                # Let the stdlib handle the host part + %-decoding properly.
                # `file://hostname/path` → `/path`; `file:///path` → `/path`.
                parsed = urllib.parse.urlparse(line)
                line = urllib.request.url2pathname(parsed.path)
            paths.append(line)
    return paths


def _icon_label(icon_name: str, text: str) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    img = Gtk.Image.new_from_icon_name(icon_name)
    img.set_pixel_size(14)
    box.append(img)
    box.append(Gtk.Label(label=text))
    return box


def _populate_row(row: Gtk.ListBoxRow, entry: FileEntry, widgets: dict) -> None:
    widgets["name"].set_text(entry.name)
    # Meta
    meta_box: Gtk.Box = widgets["meta_box"]
    _clear_children(meta_box)
    parts: list[tuple[str, Optional[str]]] = []
    if entry.meta.resolution != "—":
        parts.append((entry.meta.resolution, None))
    if entry.meta.codec:
        parts.append((entry.meta.codec, None))
    if entry.meta.fps:
        parts.append((f"{entry.meta.fps:g}fps", None))
    if entry.meta.duration:
        parts.append((format_duration(entry.meta.duration), None))
    if entry.meta.alpha:
        parts.append(("α", "alpha-mark"))
    if not parts:
        lbl = Gtk.Label(label="probing…", xalign=0)
        meta_box.append(lbl)
    else:
        for i, (text, cls) in enumerate(parts):
            if i > 0:
                sep = Gtk.Label(label="·")
                sep.add_css_class("sep")
                meta_box.append(sep)
            lbl = Gtk.Label(label=text, xalign=0)
            if cls:
                lbl.add_css_class(cls)
            meta_box.append(lbl)

    # Sizes
    widgets["size"].set_text(format_bytes(entry.size) if entry.size else "—")
    widgets["est"].set_text(f"→ {format_bytes(entry.est_out)}" if entry.est_out else "→ —")

    # Progress
    pb: Gtk.ProgressBar = widgets["progress"]
    if entry.status == "encoding":
        pb.set_fraction(min(1.0, max(0.0, entry.progress)))
        pb.set_visible(True)
    else:
        pb.set_visible(False)

    # Status dot
    dot: Gtk.Box = widgets["dot"]
    for cls in ("queued", "encoding", "done", "failed"):
        dot.remove_css_class(cls)
    dot.add_css_class(entry.status)
    _clear_children(dot)
    inner = _status_icon_for(entry.status)
    if inner is not None:
        dot.append(inner)


def _status_icon_for(status: str) -> Optional[Gtk.Widget]:
    if status == "done":
        img = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
        img.set_pixel_size(10)
        return img
    if status == "failed":
        img = Gtk.Image.new_from_icon_name("window-close-symbolic")
        img.set_pixel_size(10)
        return img
    if status == "encoding":
        spinner = Gtk.Spinner()
        spinner.set_size_request(10, 10)
        spinner.set_spinning(True)
        return spinner
    return None


def _clear_children(box: Gtk.Box) -> None:
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt
