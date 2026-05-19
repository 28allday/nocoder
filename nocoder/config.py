"""Shared user-config file at ``$XDG_CONFIG_HOME/nocoder/config.json``.

Multiple parts of the app persist values here (hwaccel pick from
``hwaccel.py``; UI prefs from the settings pane). To avoid one writer
clobbering another's keys, every write goes through ``update_config()`` which
reads the current contents, merges in the updates, and writes back atomically.

Schema (all keys optional):
    {
        "hwaccel": "cuda" | "qsv" | "vaapi" | "none",
        "out_dir": "/home/.../Footage/prores",
        "profile": "hq",
        "naming":  "suffix" | "keep",
        "audio_bits": 16 | 24,
        "auto_reveal": false,
        "cpu_pane_expanded": true
    }
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

CONFIG_PATH = (
    Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    / "nocoder"
    / "config.json"
)

# Serialise reads + writes from any thread (settings panel runs on the GTK
# main loop; hwaccel.get_hwaccel can run from an encode worker on the first
# probe). All access goes through one of the public functions below.
_lock = threading.Lock()


def load_config() -> dict:
    """Return the persisted config as a dict, or {} if missing/corrupt."""
    with _lock:
        return _read_locked()


def update_config(updates: dict) -> None:
    """Read-modify-write: merge `updates` into the persisted config."""
    if not updates:
        return
    with _lock:
        data = _read_locked()
        data.update(updates)
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = CONFIG_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2) + "\n")
            tmp.replace(CONFIG_PATH)  # atomic on POSIX
        except OSError:
            pass


def _read_locked() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
