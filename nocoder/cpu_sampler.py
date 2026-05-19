"""Per-core CPU utilisation sampling from /proc/stat.

Pure-Python, no GTK. Keeps a snapshot of the last (idle, total) jiffies per
core so each ``sample()`` returns the delta-derived utilisation since the
previous call. The first call returns zeros (no baseline yet).
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

PROC_STAT = "/proc/stat"


class CpuSampler:
    def __init__(self) -> None:
        self._prev: List[Optional[Tuple[int, int]]] = []

    def sample(self) -> List[float]:
        """Return per-core utilisation 0–100.

        First call returns a list of zeros sized to ``os.cpu_count()`` (or the
        number of per-core lines in /proc/stat, whichever is found).
        """
        snapshot = _read_cpu_jiffies()
        if not self._prev:
            self._prev = [None] * len(snapshot)

        # /proc/stat may grow if a core comes online between calls. Pad prev.
        if len(snapshot) > len(self._prev):
            self._prev.extend([None] * (len(snapshot) - len(self._prev)))

        out: List[float] = []
        for i, cur in enumerate(snapshot):
            prev = self._prev[i] if i < len(self._prev) else None
            if prev is None:
                out.append(0.0)
            else:
                d_idle = cur[0] - prev[0]
                d_total = cur[1] - prev[1]
                if d_total <= 0:
                    out.append(0.0)
                else:
                    busy = d_total - d_idle
                    out.append(max(0.0, min(100.0, 100.0 * busy / d_total)))
            self._prev[i] = cur
        return out


def _read_cpu_jiffies() -> List[Tuple[int, int]]:
    """Parse /proc/stat and return [(idle, total), ...] per core (cpu0..cpuN)."""
    rows: List[Tuple[int, int]] = []
    try:
        with open(PROC_STAT, "r") as f:
            for line in f:
                if not line.startswith("cpu"):
                    break
                parts = line.split()
                name = parts[0]
                if name == "cpu":
                    continue  # aggregate row; skip
                # Fields: user nice system idle iowait irq softirq steal guest guest_nice
                # idle time = idle + iowait
                fields = [int(x) for x in parts[1:]]
                idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
                total = sum(fields)
                rows.append((idle, total))
    except OSError:
        return []
    return rows
