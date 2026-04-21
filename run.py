#!/usr/bin/env python3
"""NO-CODER launcher.

Usage:
    python3 run.py [FILE_OR_DIR ...]

Optional positional args are treated as files/directories to pre-populate the queue.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running from anywhere — make this directory importable.
_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from nocoder.app import NoCoderApplication  # noqa: E402


def main() -> int:
    app = NoCoderApplication()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
