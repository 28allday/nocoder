"""Detect image sequences in a folder.

The "Add image sequence folder…" entry point picks a folder and we look for
groups of numbered frames inside it. A sequence is a set of files sharing a
filename prefix and extension, differing only in a trailing digit run, with
consistent zero-padding (so `shot_001.png` and `shot_0001.png` in the same
folder are treated as two distinct sequences — that matches ffmpeg's `%0Nd`
semantics).

We don't require contiguity: a group with gaps still encodes (`expected_frames
> frame_count` signals the gap so the UI can warn). ffmpeg will halt at the
first missing frame at encode time.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .data import SEQUENCE_EXTENSIONS
from .encoder import SequenceSpec

# Anchor on the *last* digit run in the stem so `scene_01_v2_0001.png` becomes
# (prefix="scene_01_v2_", digits="0001"), not (prefix="scene_", digits="01").
_TRAILING_DIGITS_RE = re.compile(r"^(.*?)(\d+)$")


def scan_folder(directory: str, fps: float) -> list[SequenceSpec]:
    """Return all image sequences found directly in `directory` (no recursion).

    Hidden files (leading dot) and unsupported extensions are ignored. Groups
    with fewer than 2 frames are dropped (a stray `thumb.jpg` next to a video
    folder shouldn't queue itself as a one-frame sequence).
    """
    try:
        entries = os.listdir(directory)
    except OSError:
        return []

    # group_key -> {"frames": [(num_int, name)], "prefix": str, "ext": str, "padding": int}
    groups: dict[tuple[str, str, int], dict] = {}
    for name in entries:
        if name.startswith("."):
            continue
        full = os.path.join(directory, name)
        if not os.path.isfile(full):
            continue
        ext = Path(name).suffix.lower()
        if ext not in SEQUENCE_EXTENSIONS:
            continue
        stem = Path(name).stem
        m = _TRAILING_DIGITS_RE.match(stem)
        if not m:
            continue
        prefix, digits = m.group(1), m.group(2)
        padding = len(digits)
        key = (prefix, ext, padding)
        try:
            num = int(digits)
        except ValueError:
            continue
        g = groups.setdefault(
            key,
            {"frames": [], "prefix": prefix, "ext": ext, "padding": padding},
        )
        g["frames"].append((num, name))

    specs: list[SequenceSpec] = []
    for g in groups.values():
        frames = g["frames"]
        if len(frames) < 2:
            continue
        frames.sort(key=lambda t: t[0])
        nums = [n for n, _ in frames]
        start = nums[0]
        end = nums[-1]
        specs.append(
            SequenceSpec(
                dir=os.path.abspath(directory),
                prefix=g["prefix"],
                ext=g["ext"],
                padding=g["padding"],
                start_frame=start,
                frame_count=len(frames),
                expected_frames=end - start + 1,
                fps=fps,
            )
        )

    specs.sort(key=lambda s: (s.prefix, s.ext, s.padding))
    return specs


def sum_frame_sizes(spec: SequenceSpec) -> int:
    """Total bytes of all frames in the sequence (best-effort)."""
    total = 0
    try:
        for name in os.listdir(spec.dir):
            if name.startswith("."):
                continue
            stem = Path(name).stem
            if Path(name).suffix.lower() != spec.ext:
                continue
            m = _TRAILING_DIGITS_RE.match(stem)
            if not m:
                continue
            prefix, digits = m.group(1), m.group(2)
            if prefix != spec.prefix or len(digits) != spec.padding:
                continue
            try:
                total += os.path.getsize(os.path.join(spec.dir, name))
            except OSError:
                continue
    except OSError:
        return 0
    return total
