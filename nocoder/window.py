"""Main window: headerbar + horizontal split (queue, settings) + footer.

Owns the state machine (empty|ready|encoding|complete), file list, and the
background encode worker thread.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
from gi.repository import Adw, GdkPixbuf, Gio, GLib, Gtk

_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
_LOGO_PATH = _ASSETS_DIR / "logo.png"
_HEADER_LOGO_SIZE = 22

from .data import (
    PROFILES_BY_ID,
    VIDEO_EXTENSIONS,
    estimate_output_bytes,
    format_bytes as _format_bytes,
    is_proxy_dirname,
    is_video_path,
)
from .encoder import (
    EncodeJob,
    SequenceSpec,
    detect_prores_encoder,
    plan_output_path,
    probe_metadata,
    probe_sequence_metadata,
    run_encode,
)
from .sequence_scan import scan_folder, sum_frame_sizes
from .footer import Footer
from .queue_pane import FileEntry, QueuePane
from .config import load_config, update_config
from .settings_pane import Settings, SettingsPane, load_persisted_settings
from .system_pane import SystemPane

WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 880
# Minimum usable size — deliberately small so tiling WMs (Hyprland/Sway/i3)
# can resize us into narrow tiles without pushing the footer off-screen.
# Both panes have internal scrollbars, so the content copes with compression.
WINDOW_MIN_WIDTH = 560
WINDOW_MIN_HEIGHT = 380


class MainWindow(Adw.ApplicationWindow):
    __gtype_name__ = "NoCoderMainWindow"

    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app)
        self.set_title("NO-CODER")
        self.set_default_size(WINDOW_WIDTH, WINDOW_HEIGHT)
        self.set_size_request(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)
        self.add_css_class("nocoder-window")

        # App state
        self._files: list[FileEntry] = []
        self._selected_id: Optional[str] = None
        self._state: str = "empty"
        self._encoder_kind = detect_prores_encoder()
        self._settings = load_persisted_settings()
        self._ensure_out_dir()
        self._encode_thread: Optional[threading.Thread] = None
        self._cancel_event: Optional[threading.Event] = None
        self._active_job: Optional[EncodeJob] = None
        self._current_idx: int = 0
        self._current_speed: Optional[float] = None
        self._encode_start_mono: Optional[float] = None
        self._encode_elapsed: Optional[float] = None

        # Root layout
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(root)

        root.append(self._build_headerbar())

        split = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        split.set_hexpand(True)
        split.set_vexpand(True)
        root.append(split)

        self._queue = QueuePane()
        self._queue.connect("add-files-requested", lambda *_: self._open_files_dialog())
        self._queue.connect("add-folder-requested", lambda *_: self._open_folder_dialog())
        self._queue.connect(
            "add-sequence-folder-requested",
            lambda *_: self._open_sequence_folder_dialog(),
        )
        self._queue.connect("clear-requested", lambda *_: self._clear_files())
        self._queue.connect("files-dropped", self._on_files_dropped)
        self._queue.connect("selection-changed", self._on_selection_changed)
        self._queue.connect("remove-requested", self._on_remove_requested)
        split.append(self._queue)

        self._settings_pane = SettingsPane(self._settings, self._encoder_kind)
        self._settings_pane.connect("settings-changed", lambda *_: self._on_settings_changed())
        self._settings_pane.connect("choose-folder-requested", lambda *_: self._open_out_dir_dialog())
        split.append(self._settings_pane)

        cpu_pane_expanded = bool(load_config().get("cpu_pane_expanded", True))
        self._system_pane = SystemPane(initial_expanded=cpu_pane_expanded)
        root.append(self._system_pane)

        self._footer = Footer()
        self._footer.connect("encode-requested", lambda *_: self._start_encode())
        self._footer.connect("cancel-requested", lambda *_: self._cancel_encode())
        self._footer.connect("reveal-requested", lambda *_: self._reveal_output_dir())
        root.append(self._footer)

        self._refresh_all()
        self.connect("close-request", self._on_close_request)

        # Keyboard shortcut: ⌃F focuses the search entry.
        accel = Gtk.ShortcutController()
        accel.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("<Control>f"),
            Gtk.CallbackAction.new(self._focus_search),
        ))
        self.add_controller(accel)

    # ---------- headerbar ----------

    def _build_headerbar(self) -> Gtk.Widget:
        header = Adw.HeaderBar()
        header.add_css_class("nocoder-headerbar")
        header.set_show_title(True)

        # Left cluster: hamburger menu + search pill
        left = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        hamburger = Gtk.MenuButton()
        hamburger.add_css_class("icon-btn")
        hamburger.set_icon_name("open-menu-symbolic")
        hamburger.set_menu_model(self._build_menu_model())
        left.append(hamburger)
        left.append(self._build_search_pill())
        header.pack_start(left)

        # Center title: logo + app name + status chip
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        title_box.set_valign(Gtk.Align.CENTER)
        title_box.append(_build_header_logo())
        app_name = Gtk.Label(label="NO-CODER")
        app_name.add_css_class("app-title")
        title_box.append(app_name)
        self._status_chip = _StatusChip()
        title_box.append(self._status_chip)
        header.set_title_widget(title_box)

        # Right cluster: toggle-settings button (+ built-in window controls)
        sliders = Gtk.ToggleButton()
        sliders.add_css_class("icon-btn")
        sliders.set_child(Gtk.Image.new_from_icon_name("preferences-system-symbolic"))
        sliders.set_tooltip_text("Show/hide settings pane")
        sliders.set_active(True)
        sliders.connect("toggled", self._on_settings_toggle)
        self._settings_toggle = sliders
        header.pack_end(sliders)
        return header

    def _on_settings_toggle(self, btn: Gtk.ToggleButton) -> None:
        self._settings_pane.set_visible(btn.get_active())

    def _build_menu_model(self) -> Gio.Menu:
        menu = Gio.Menu()
        menu.append("Add files…", "win.add-files")
        menu.append("Add folder…", "win.add-folder")
        menu.append("Clear queue", "win.clear-queue")
        self._install_menu_actions()
        return menu

    def _install_menu_actions(self) -> None:
        def add(name: str, handler):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_: handler())
            self.add_action(action)
        add("add-files", self._open_files_dialog)
        add("add-folder", self._open_folder_dialog)
        add("clear-queue", self._clear_files)

    def _build_search_pill(self) -> Gtk.Widget:
        pill = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        pill.add_css_class("search-pill")
        icon = Gtk.Image.new_from_icon_name("system-search-symbolic")
        icon.set_pixel_size(13)
        pill.append(icon)
        entry = Gtk.Entry()
        entry.set_placeholder_text("Search files in queue…")
        entry.set_has_frame(False)
        entry.set_hexpand(True)
        entry.set_width_chars(22)
        entry.connect("changed", self._on_search_changed)
        self._search_entry = entry

        # Esc clears the filter and drops focus back to the queue.
        esc = Gtk.ShortcutController()
        esc.set_scope(Gtk.ShortcutScope.LOCAL)
        esc.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("Escape"),
            Gtk.CallbackAction.new(self._clear_search_on_escape),
        ))
        entry.add_controller(esc)

        pill.append(entry)
        kbd = Gtk.Label(label="⌃F")
        kbd.add_css_class("search-kbd")
        pill.append(kbd)
        return pill

    def _clear_search_on_escape(self, *_args) -> bool:
        if not hasattr(self, "_search_entry"):
            return False
        if self._search_entry.get_text():
            self._search_entry.set_text("")
        else:
            # Already empty — drop focus so Esc isn't a no-op (lets the user
            # leave the search field with the keyboard).
            self.grab_focus()
        return True

    def _focus_search(self, *_args) -> bool:
        if hasattr(self, "_search_entry"):
            self._search_entry.grab_focus()
            return True
        return False

    def _on_search_changed(self, entry: Gtk.Entry) -> None:
        self._queue.set_search_query(entry.get_text())

    # ---------- state plumbing ----------

    def _compute_state(self) -> str:
        if self._state == "encoding":
            return "encoding"
        if self._state == "complete":
            # Stay in complete until user encodes again or clears.
            return "complete"
        if not self._files:
            return "empty"
        return "ready"

    def _refresh_all(self) -> None:
        self._state = self._compute_state() if self._state not in ("encoding", "complete") else self._state
        self._status_chip.set_state(self._state)
        self._queue.set_files(self._files)
        if self._selected_id is not None:
            self._queue.set_selected(self._selected_id)
        self._queue.set_encoding(self._state == "encoding")
        first = self._files[0] if self._files else None
        if first is not None:
            self._settings_pane.set_first_file_name(
                first.display_name, sequence=first.sequence,
            )
        else:
            self._settings_pane.set_first_file_name(None)
        self._settings_pane.set_encoding(self._state == "encoding")
        self._settings_pane.refresh()
        self._footer.update(
            state="ready" if self._state in ("empty", "ready") else self._state,
            files=self._files,
            profile_id=self._settings.profile,
            overall=self._overall_progress(),
            current_idx=self._current_idx,
            elapsed=self._encode_elapsed,
        )

    def _overall_progress(self) -> float:
        if not self._files:
            return 0.0
        total = 0.0
        for f in self._files:
            if f.status == "done":
                total += 1.0
            elif f.status == "encoding":
                total += max(0.0, min(1.0, f.progress))
        return total / len(self._files)

    def _on_settings_changed(self) -> None:
        # Recompute est_out using the new profile's bitrate.
        mbps = PROFILES_BY_ID[self._settings.profile].mbps
        for f in self._files:
            f.est_out = estimate_output_bytes(f.meta.duration, mbps)
            self._queue.update_file(f)
        # Footer and preview need refresh.
        self._footer.update(
            state="ready" if self._state in ("empty", "ready") else self._state,
            files=self._files,
            profile_id=self._settings.profile,
            overall=self._overall_progress(),
            current_idx=self._current_idx,
        )
        # Persist the new state across launches. Cheap (single small JSON
        # file write) and idempotent — if nothing actually changed we still
        # round-trip the same dict, no harm.
        update_config(self._settings.to_persistable())

    def _ensure_out_dir(self) -> None:
        if not self._settings.out_dir:
            self._settings.out_dir = str(Path.home() / "Footage" / "prores")
        try:
            Path(self._settings.out_dir).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    # ---------- file operations ----------

    def _open_files_dialog(self) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose videos")
        dialog.set_modal(True)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        video_filter = Gtk.FileFilter()
        video_filter.set_name("Video files")
        for ext in VIDEO_EXTENSIONS:
            video_filter.add_pattern(f"*{ext}")
            video_filter.add_pattern(f"*{ext.upper()}")
        filters.append(video_filter)
        any_filter = Gtk.FileFilter()
        any_filter.set_name("All files")
        any_filter.add_pattern("*")
        filters.append(any_filter)
        dialog.set_filters(filters)
        dialog.open_multiple(self, None, self._on_files_chosen)

    def _on_files_chosen(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            model = dialog.open_multiple_finish(result)
        except GLib.Error:
            return
        paths: list[str] = []
        for i in range(model.get_n_items()):
            f = model.get_item(i)
            if f is None:
                continue
            p = f.get_path()
            if p:
                paths.append(p)
        self._add_paths(paths)

    def _open_folder_dialog(self) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose folder")
        dialog.set_modal(True)
        dialog.select_folder(self, None, self._on_folder_chosen)

    def _on_folder_chosen(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            f = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if f is None:
            return
        path = f.get_path()
        if not path:
            return
        paths: list[str] = []
        for root, dirs, files in os.walk(path):
            # Prune proxy / thumbnail / metadata subdirs in-place so os.walk
            # doesn't recurse into them — avoids pulling low-res duplicates
            # from Sony SUB/, Panasonic PROXY/ etc. into the queue alongside
            # the master clips.
            dirs[:] = [d for d in dirs if not is_proxy_dirname(d)]
            for name in files:
                full = os.path.join(root, name)
                if is_video_path(full):
                    paths.append(full)
        paths.sort()
        self._add_paths(paths)

    def _open_sequence_folder_dialog(self) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose image-sequence folder")
        dialog.set_modal(True)
        dialog.select_folder(self, None, self._on_sequence_folder_chosen)

    def _on_sequence_folder_chosen(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            f = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if f is None:
            return
        path = f.get_path()
        if not path:
            return
        specs = scan_folder(path, self._settings.sequence_fps)
        if not specs:
            self._show_error(
                f"No image sequences found in:\n{path}\n\n"
                "Sequences are groups of ≥2 frames sharing a name prefix and "
                "extension, ending in a digit run (e.g. shot_0001.png …)."
            )
            return
        self._add_sequences(specs)

    def _open_out_dir_dialog(self) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose output folder")
        dialog.set_modal(True)
        try:
            dialog.set_initial_folder(Gio.File.new_for_path(self._settings.out_dir))
        except GLib.Error:
            pass
        dialog.select_folder(self, None, self._on_out_dir_chosen)

    def _on_out_dir_chosen(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            f = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if f is None:
            return
        path = f.get_path()
        if path:
            self._settings_pane.set_output_folder(path)

    def _on_files_dropped(self, _pane, paths: list[str]) -> None:
        expanded: list[str] = []
        for p in paths:
            if os.path.isdir(p):
                for root, dirs, files in os.walk(p):
                    # Skip proxy / thumbnail / metadata dirs (Sony SUB,
                    # Panasonic PROXY, etc.) — see data.PROXY_DIRNAMES.
                    dirs[:] = [d for d in dirs if not is_proxy_dirname(d)]
                    for name in files:
                        full = os.path.join(root, name)
                        if is_video_path(full):
                            expanded.append(full)
            elif os.path.isfile(p) and is_video_path(p):
                expanded.append(p)
        self._add_paths(expanded)

    def _add_paths(self, paths: list[str]) -> None:
        # Dedupe by realpath so the same physical file added via different
        # mount points (e.g. /run/media/gav/Card and /run/media/gav/Card1)
        # or symlinks doesn't appear twice.
        existing = {os.path.realpath(f.path) for f in self._files}
        mbps = PROFILES_BY_ID[self._settings.profile].mbps
        added: list[FileEntry] = []
        for p in paths:
            if not p or not os.path.isfile(p):
                continue
            real = os.path.realpath(p)
            if real in existing:
                continue
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            entry = FileEntry(path=p, size=size)
            entry.est_out = 0.0
            self._files.append(entry)
            existing.add(real)
            added.append(entry)
        if not added:
            return
        # Move out of "complete" state when user adds more work.
        if self._state == "complete":
            self._state = "ready"
            self._reset_file_statuses()
        self._refresh_all()
        for entry in added:
            self._probe_async(entry, mbps)

    def _add_sequences(self, specs: list[SequenceSpec]) -> None:
        # Dedupe by (dir + pattern_basename) so re-adding the same folder
        # doesn't queue the same sequence twice. Realpath dedupe is wrong
        # here because two sequences in the same folder share the parent
        # path — the pattern is what makes them distinct.
        existing = {
            (f.sequence.dir, f.sequence.pattern_basename)
            for f in self._files
            if f.sequence is not None
        }
        mbps = PROFILES_BY_ID[self._settings.profile].mbps
        added: list[FileEntry] = []
        for spec in specs:
            key = (spec.dir, spec.pattern_basename)
            if key in existing:
                continue
            first = spec.first_frame_path
            try:
                size = sum_frame_sizes(spec)
            except OSError:
                size = 0
            entry = FileEntry(path=first, size=size, sequence=spec)
            entry.est_out = 0.0
            self._files.append(entry)
            existing.add(key)
            added.append(entry)
        if not added:
            return
        if self._state == "complete":
            self._state = "ready"
            self._reset_file_statuses()
        self._refresh_all()
        for entry in added:
            self._probe_sequence_async(entry, mbps)

    def _probe_sequence_async(self, entry: FileEntry, mbps: int) -> None:
        spec = entry.sequence
        assert spec is not None

        def worker() -> None:
            meta = probe_sequence_metadata(spec)

            def apply() -> bool:
                entry.meta = meta
                entry.est_out = estimate_output_bytes(meta.duration, mbps)
                self._queue.update_file(entry)
                self._footer.update(
                    state="ready" if self._state in ("empty", "ready") else self._state,
                    files=self._files,
                    profile_id=self._settings.profile,
                    overall=self._overall_progress(),
                    current_idx=self._current_idx,
                )
                first = self._files[0] if self._files else None
                if first is not None:
                    self._settings_pane.set_first_file_name(
                        first.display_name, sequence=first.sequence,
                    )
                else:
                    self._settings_pane.set_first_file_name(None)
                return False

            GLib.idle_add(apply)

        t = threading.Thread(target=worker, daemon=True)
        t.start()

    def _probe_async(self, entry: FileEntry, mbps: int) -> None:
        def worker() -> None:
            meta = probe_metadata(entry.path)
            def apply() -> bool:
                entry.meta = meta
                entry.est_out = estimate_output_bytes(meta.duration, mbps)
                self._queue.update_file(entry)
                self._footer.update(
                    state="ready" if self._state in ("empty", "ready") else self._state,
                    files=self._files,
                    profile_id=self._settings.profile,
                    overall=self._overall_progress(),
                    current_idx=self._current_idx,
                )
                first = self._files[0] if self._files else None
                if first is not None:
                    self._settings_pane.set_first_file_name(
                        first.display_name, sequence=first.sequence,
                    )
                else:
                    self._settings_pane.set_first_file_name(None)
                return False
            GLib.idle_add(apply)
        t = threading.Thread(target=worker, daemon=True)
        t.start()

    def _clear_files(self) -> None:
        if self._state == "encoding":
            return
        self._files.clear()
        self._selected_id = None
        self._state = "empty"
        if hasattr(self, "_search_entry"):
            self._search_entry.set_text("")
        self._refresh_all()

    def _reset_file_statuses(self) -> None:
        for f in self._files:
            f.status = "queued"
            f.progress = 0.0
            f.error = None

    def _on_selection_changed(self, _pane, file_id: str) -> None:
        self._selected_id = file_id or None

    def _on_remove_requested(self, _pane, file_id: str) -> None:
        self._files = [f for f in self._files if f.id != file_id]
        if self._selected_id == file_id:
            self._selected_id = None
        if not self._files:
            self._state = "empty"
        self._refresh_all()

    # ---------- encode ----------

    def _start_encode(self) -> None:
        if self._state == "encoding" or not self._files:
            return
        if self._encoder_kind == "none":
            self._show_error("No ProRes encoder found.\nInstall ffmpeg with prores_ks or prores support.")
            return
        try:
            Path(self._settings.out_dir).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._show_error(f"Cannot create output folder:\n{e}")
            return
        if not os.access(self._settings.out_dir, os.W_OK):
            self._show_error(f"No write permission for output folder:\n{self._settings.out_dir}")
            return

        # Disk-space pre-check — sum the estimated output sizes of every
        # queued (or queue-able) file and compare against free bytes on the
        # output volume. Cheap insurance against a half-finished batch when
        # someone forgets the destination is nearly full.
        try:
            need = sum(f.est_out for f in self._files if f.est_out)
            free = shutil.disk_usage(self._settings.out_dir).free
        except OSError:
            need = 0
            free = 0
        if need and free and need > free:
            self._show_error(
                f"Not enough free space in {self._settings.out_dir}\n"
                f"Need ≈{_format_bytes(need)}, available {_format_bytes(free)}."
            )
            return

        self._reset_file_statuses()
        self._state = "encoding"
        self._current_idx = 0
        self._cancel_event = threading.Event()
        self._encode_start_mono = time.monotonic()
        self._encode_elapsed = None
        self._refresh_all()

        self._encode_thread = threading.Thread(target=self._encode_worker, daemon=True)
        self._encode_thread.start()

    def _encode_worker(self) -> None:
        cancel = self._cancel_event
        assert cancel is not None
        for idx, entry in enumerate(list(self._files)):
            if cancel.is_set():
                break
            # Source file may have moved/been-deleted between probe time and
            # now. Mark it failed instead of letting ffmpeg emit a cryptic
            # "no such file" error.
            if not os.path.isfile(entry.path):
                GLib.idle_add(self._finish_file, entry.id, False, "source file is missing")
                continue
            GLib.idle_add(self._set_current_encoding, idx, entry.id)
            stem_override = (
                entry.sequence.stripped_stem if entry.sequence is not None else None
            )
            out_path = plan_output_path(
                entry.path,
                self._settings.out_dir,
                self._settings.naming,
                self._settings.profile,
                stem_override=stem_override,
            )
            done_event = threading.Event()
            result = {"ok": False, "err": None}

            def on_prog(pct: float, _entry=entry) -> None:
                GLib.idle_add(self._apply_file_progress, _entry.id, pct)

            def on_done(ok: bool, err, _entry=entry) -> None:
                result["ok"] = ok
                result["err"] = err
                done_event.set()

            def on_speed(spd: float) -> None:
                GLib.idle_add(self._apply_speed, spd)

            job = EncodeJob(
                src=entry.path,
                out=out_path,
                duration=entry.meta.duration or 0.0,
                on_progress=on_prog,
                on_done=on_done,
                on_speed=on_speed,
                audio_stream_indexes=list(entry.meta.audio_stream_indexes),
                sequence=entry.sequence,
                cancel_event=cancel,
            )
            # Track the active job so _on_close_request can cancel the live
            # ffmpeg child synchronously rather than relying on the worker's
            # next stdout-loop iteration.
            self._active_job = job
            try:
                run_encode(
                    job, self._settings.profile, self._settings.alpha, self._encoder_kind,
                    audio_bits=self._settings.audio_bits,
                )
                done_event.wait(timeout=5)
            finally:
                self._active_job = None

            GLib.idle_add(self._finish_file, entry.id, bool(result["ok"]), result["err"])

        GLib.idle_add(self._finish_encoding)

    def _set_current_encoding(self, idx: int, file_id: str) -> bool:
        self._current_idx = idx
        # Reset the live-speed reading at every file boundary so the footer
        # doesn't briefly show the previous file's speed before ffmpeg
        # publishes the first `speed=` for the new one.
        self._current_speed = None
        for f in self._files:
            if f.id == file_id:
                f.status = "encoding"
                f.progress = 0.0
                self._queue.update_file(f)
                break
        self._footer.update(
            state="encoding",
            files=self._files,
            profile_id=self._settings.profile,
            overall=self._overall_progress(),
            current_idx=self._current_idx,
            speed=self._current_speed,
        )
        return False

    def _apply_file_progress(self, file_id: str, pct: float) -> bool:
        for f in self._files:
            if f.id == file_id:
                f.progress = max(0.0, min(1.0, pct))
                self._queue.update_file(f)
                break
        self._footer.update(
            state="encoding",
            files=self._files,
            profile_id=self._settings.profile,
            overall=self._overall_progress(),
            current_idx=self._current_idx,
            speed=self._current_speed,
        )
        return False

    def _apply_speed(self, speed: float) -> bool:
        self._current_speed = speed
        if self._state == "encoding":
            self._footer.update(
                state="encoding",
                files=self._files,
                profile_id=self._settings.profile,
                overall=self._overall_progress(),
                current_idx=self._current_idx,
                speed=self._current_speed,
            )
        return False

    def _finish_file(self, file_id: str, ok: bool, err) -> bool:
        for f in self._files:
            if f.id == file_id:
                f.status = "done" if ok else "failed"
                f.progress = 1.0 if ok else 0.0
                f.error = None if ok else (err or "encode failed")
                self._queue.update_file(f)
                break
        return False

    def _finish_encoding(self) -> bool:
        cancelled = self._cancel_event is not None and self._cancel_event.is_set()
        self._cancel_event = None
        self._encode_thread = None
        if cancelled:
            # Anything still in 'encoding' status becomes 'queued' again.
            for f in self._files:
                if f.status == "encoding":
                    f.status = "queued"
                    f.progress = 0.0
            self._state = "ready"
        else:
            self._state = "complete"
            if self._encode_start_mono is not None:
                self._encode_elapsed = time.monotonic() - self._encode_start_mono
            # If at least one file landed AND the user opted in, pop the
            # output folder open. Skip on cancel (intent unclear) and on
            # full-batch failure (annoying to be shown an empty folder).
            if self._settings.auto_reveal and any(f.status == "done" for f in self._files):
                self._reveal_output_dir()
        self._encode_start_mono = None
        self._refresh_all()
        return False

    def _cancel_encode(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()

    def _reveal_output_dir(self) -> None:
        try:
            uri = Gio.File.new_for_path(self._settings.out_dir).get_uri()
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error:
            pass

    def _on_close_request(self, *_args) -> bool:
        # Tell the worker to stop iterating.
        if self._cancel_event is not None:
            self._cancel_event.set()
        # Actively terminate the live ffmpeg child — the worker thread is a
        # daemon so it dies with Python on window close, but ffmpeg is its own
        # process and would keep running + writing a partial .mov otherwise.
        job = getattr(self, "_active_job", None)
        if job is not None:
            job.cancel()
        return False

    def _show_error(self, message: str) -> None:
        dialog = Adw.MessageDialog.new(self, "NO-CODER", message)
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.present()


def _build_header_logo() -> Gtk.Widget:
    """22×22 NO-CODER mark shown in the headerbar.

    Pre-scales the PNG to 2× for HiDPI, wraps it in a Gtk.Image and fixes the
    display size via set_pixel_size. Falls back to the previous symbolic icon
    (with the old orange tile styling) if the asset is missing.
    """
    if _LOGO_PATH.exists():
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(_LOGO_PATH), _HEADER_LOGO_SIZE * 2, _HEADER_LOGO_SIZE * 2, True,
            )
            img = Gtk.Image.new_from_pixbuf(pb)
            img.set_pixel_size(_HEADER_LOGO_SIZE)
            img.add_css_class("app-logo-image")
            return img
        except GLib.Error:
            pass
    logo = Gtk.Image.new_from_icon_name("video-x-generic-symbolic")
    logo.add_css_class("app-logo")
    logo.set_pixel_size(14)
    return logo


class _StatusChip(Gtk.Box):
    """Small colored pill with a dot + label. States: idle|ready|encoding|done."""
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.add_css_class("status-chip")
        self.add_css_class("idle")
        self._state = "idle"
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._dot = Gtk.Box()
        self._dot.add_css_class("dot")
        self._dot.set_valign(Gtk.Align.CENTER)
        inner.append(self._dot)
        self._label = Gtk.Label(label="IDLE")
        inner.append(self._label)
        self.append(inner)

    def set_state(self, state: str) -> None:
        mapping = {
            "empty": ("idle", "IDLE"),
            "ready": ("ready", "READY"),
            "encoding": ("encoding", "ENCODING"),
            "complete": ("done", "DONE"),
        }
        cls, text = mapping.get(state, ("idle", "IDLE"))
        for c in ("idle", "ready", "encoding", "done"):
            self.remove_css_class(c)
        self.add_css_class(cls)
        self._label.set_label(text)
        self._state = cls
