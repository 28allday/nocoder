"""ProRes profile map, video extensions, formatters, size/time estimators.

Mirrors design_handoff_prowrap/src/data.jsx and the profile map from prowrap-yad.sh.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    desc: str
    mbps: int
    pid: int
    alpha: bool = False
    # Relative speed factor used for "estimated encode time" (matches footer.jsx).
    speed_factor: float = 0.9


PROFILES: list[Profile] = [
    Profile("proxy",    "Proxy",    "45 Mb/s — fast, small, offline edit",    45,  0, speed_factor=0.3),
    Profile("lt",       "LT",       "102 Mb/s — lightweight delivery",        102, 1, speed_factor=0.5),
    Profile("standard", "Standard", "147 Mb/s — general mastering",           147, 2, speed_factor=0.7),
    Profile("hq",       "HQ",       "220 Mb/s — high-quality mastering",      220, 3, speed_factor=0.9),
    Profile("4444",     "4444",     "330 Mb/s — 4:4:4 + alpha",               330, 4, alpha=True, speed_factor=1.3),
    Profile("4444xq",   "4444 XQ",  "500 Mb/s — maximum 4:4:4 + alpha",       500, 5, alpha=True, speed_factor=1.7),
]

PROFILES_BY_ID: dict[str, Profile] = {p.id: p for p in PROFILES}


VIDEO_EXTENSIONS: frozenset[str] = frozenset({
    # Common consumer / editorial container formats
    ".mp4", ".mov", ".m4v", ".mkv", ".avi", ".mts", ".m2ts",
    ".webm", ".mpeg", ".mpg", ".3gp", ".3g2",
    # Professional camera container — MXF covers Canon XF-AVC, Sony XDCAM,
    # Panasonic AVC-Intra / P2. ffmpeg decodes these natively on stock builds.
    #
    # NOT in this list (deliberate): .crm (Canon Cinema RAW Light), .braw
    # (Blackmagic RAW), .r3d (RED), .ari (Arri RAW). All are proprietary and
    # require vendor SDKs that ffmpeg does not ship. Including them here
    # would have them land in the queue only to fail at encode with a cryptic
    # decoder error, which is worse UX than ignoring them on drop. Users
    # shooting those formats should first transcode via the vendor tool
    # (Canon Cinema RAW Development, Blackmagic RAW Player, REDCINE-X, Arri
    # Meta Extract) into MXF or ProRes.
    ".mxf",
})


def is_video_path(path: str) -> bool:
    lower = path.lower()
    return any(lower.endswith(ext) for ext in VIDEO_EXTENSIONS)


# Subdirectory names to SKIP when recursively walking a dropped folder or
# camera card. The names match case-insensitively.
#
# Pro cameras write both a master clip and a low-res "proxy" alongside it,
# typically in a sibling directory with the same base filename. If we walk
# into those proxy dirs, the queue fills with low-res duplicates that look
# like real clips but are ~5-10% of the master's bitrate. Users not paying
# attention would transcode the proxies and lose quality.
#
# Known layouts:
#   Sony XAVC:     PRIVATE/M4ROOT/CLIP/*.MXF   + SUB/*.MP4 (proxy)
#                                              + THMBNL/*.JPG (thumbnails)
#                                              + GENERAL/*    (metadata)
#   Canon XF-AVC:  CONTENTS/CLIPS001/*.MXF     + SUB/*.MP4
#   Panasonic P2:  CONTENTS/VIDEO/*.MXF        + PROXY/*.MP4
#                                              + ICON/*.BMP   (thumbs)
#                                              + VOICE/*      (audio notes)
#   Generic DSLR:  DCIM/*                      (nothing to skip)
#
# If a filmmaker intentionally drops the SUB/ directory specifically, it'd
# still work — we only prune when recursing INTO a parent folder.
PROXY_DIRNAMES: frozenset[str] = frozenset({
    # Sony / Canon proxies
    "sub",
    # Panasonic P2 proxies + metadata
    "proxy", "icon", "voice",
    # Thumbnail directories across vendors
    "thmbnl", "thumbs", "thumb", "thumbnail", "thumbnails", "preview", "previews",
})


def is_proxy_dirname(name: str) -> bool:
    return name.lower() in PROXY_DIRNAMES


def pick_pixel_format(profile_id: str, alpha: bool) -> str:
    """yuv422p10le for non-4444; yuv444p10le for 4444; yuva444p10le if 4444+alpha."""
    profile = PROFILES_BY_ID[profile_id]
    if profile.pid >= 4:
        return "yuva444p10le" if alpha else "yuv444p10le"
    return "yuv422p10le"


def format_bytes(b: float) -> str:
    if b < 1024:
        return f"{int(b)} B"
    if b < 1024 * 1024:
        return f"{b / 1024:.0f} KB"
    if b < 1024 * 1024 * 1024:
        return f"{b / (1024 * 1024):.0f} MB"
    return f"{b / (1024 * 1024 * 1024):.2f} GB"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def estimate_output_bytes(duration_s: float, mbps: int) -> float:
    """Video bitrate × duration + PCM 16-bit stereo audio (~1.411 Mb/s)."""
    if not duration_s or duration_s <= 0:
        return 0
    video_bits = mbps * 1_000_000 * duration_s
    audio_bits = 1_411_000 * duration_s
    return (video_bits + audio_bits) / 8


def estimate_encode_seconds(duration_s: float, profile_id: str) -> float:
    """Rough heuristic used for the UI's Est. encode time. Matches footer.jsx."""
    return duration_s * PROFILES_BY_ID[profile_id].speed_factor
