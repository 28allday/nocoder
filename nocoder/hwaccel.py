"""GPU hardware-accelerated decode selection.

We only accelerate decoding of the input file — ProRes encoding itself always
runs on CPU (no vendor ships a GPU ProRes encoder). Offloading decode from a
handful of the user's cores frees them up for the actual ProRes encode, which
is the typical bottleneck on camera-native (H.264 / HEVC / AV1) sources.

The selected hwaccel is cached at ``$XDG_CONFIG_HOME/nocoder/config.json`` so
we probe once per machine (install-time) rather than on every launch. If the
config is missing, the first encode will lazily re-probe and cache.
"""
from __future__ import annotations

import subprocess
import threading
from typing import Optional

from .config import load_config, update_config

# Re-exported for backward compatibility — users may have referenced this
# constant. Kept as a thin alias to the shared config module.
from .config import CONFIG_PATH  # noqa: F401

# Ordered by vendor preference: NVIDIA > Intel > AMD/generic. ffmpeg silently
# falls back to CPU decode when the source codec can't be GPU-decoded (MJPEG,
# ProRes input, etc.) so picking a hwaccel even on ProRes-only workflows is
# harmless.
_CANDIDATES = ("cuda", "qsv", "vaapi")

_cache: tuple[bool, Optional[str]] = (False, None)
# Guards check-then-set on `_cache` when two worker threads kick off encodes
# before the first-time probe has completed. Probing ffmpeg twice is harmless
# but wasteful and clutters the config-write path.
_cache_lock = threading.Lock()


def get_hwaccel() -> Optional[str]:
    """Return the selected hwaccel name, or None for CPU-only decode.

    Reads from the on-disk config if present; otherwise probes the system,
    writes the result, and returns it. Results are memoised for the process.
    """
    global _cache
    with _cache_lock:
        if _cache[0]:
            return _cache[1]
        choice = _read_configured_hwaccel()
        if choice is _Sentinel.MISSING:
            choice = probe_best_hwaccel()
            save_hwaccel(choice)
        _cache = (True, choice)  # type: ignore[assignment]
        return choice  # type: ignore[return-value]


def probe_best_hwaccel() -> Optional[str]:
    """Return the first hwaccel that actually initialises on this machine."""
    for candidate in _CANDIDATES:
        if _hwaccel_works(candidate):
            return candidate
    return None


def save_hwaccel(hw: Optional[str]) -> None:
    """Persist the selected hwaccel. ``None`` means CPU decode."""
    update_config({"hwaccel": hw or "none"})


class _Sentinel:
    MISSING = object()


def _read_configured_hwaccel():
    """Return the stored hwaccel, None (CPU), or MISSING (no entry yet)."""
    data = load_config()
    if "hwaccel" not in data:
        return _Sentinel.MISSING
    hw = data["hwaccel"]
    if hw in (None, "", "none"):
        return None
    return hw if hw in _CANDIDATES else None


def _hwaccel_works(hwaccel: str) -> bool:
    """Run a throwaway 1-frame pipeline to test that `hwaccel` initialises."""
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-init_hw_device", hwaccel,
                "-f", "lavfi", "-i", "nullsrc=s=32x32",
                "-frames:v", "1",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0
