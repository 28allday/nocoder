"""ffprobe metadata + ffmpeg encode with live -progress parsing.

The encode command mirrors prowrap-yad.sh exactly:
    ffmpeg -hide_banner -loglevel error -y -i SRC \
      -map 0:v:0 -map 0:a? \
      -c:v prores_ks -profile:v <profile> -pix_fmt <pf> [-alpha_bits 16] \
      -c:a pcm_s16le -f mov -movflags +use_metadata_tags OUT
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .data import PROFILES_BY_ID, pick_pixel_format
from .hwaccel import get_hwaccel

FFMPEG = "/usr/bin/ffmpeg"
FFPROBE = "/usr/bin/ffprobe"

# Marker file that records the currently-encoding output path. Created when an
# encode starts, removed on success/failure/cancel. If the app is force-killed
# (SIGKILL, OS crash) mid-encode the marker survives — `check_orphan_encode`
# at startup detects this and surfaces the partial file's path so the user
# can clean up.
ACTIVE_ENCODE_FILE = (
    Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    / "nocoder"
    / "active.json"
)


def _mark_encode_started(out_path: str) -> None:
    try:
        ACTIVE_ENCODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        ACTIVE_ENCODE_FILE.write_text(json.dumps({"out_path": out_path}) + "\n")
    except OSError:
        pass


def _mark_encode_finished() -> None:
    try:
        ACTIVE_ENCODE_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def check_orphan_encode() -> Optional[str]:
    """If a previous encode died ungracefully, return its output path.

    Always clears the marker after inspection so we don't repeatedly warn
    on subsequent launches. Returns None if no marker existed, or if the
    marker pointed at a path that no longer exists (cleanly removed already).
    """
    if not ACTIVE_ENCODE_FILE.exists():
        return None
    out_path = None
    try:
        data = json.loads(ACTIVE_ENCODE_FILE.read_text())
        candidate = data.get("out_path")
        if isinstance(candidate, str) and os.path.isfile(candidate):
            out_path = candidate
    except (OSError, json.JSONDecodeError):
        pass
    _mark_encode_finished()
    return out_path


def detect_prores_encoder() -> str:
    """Return 'ks', 'plain', or 'none' based on available ffmpeg encoders."""
    try:
        out = subprocess.run(
            [FFMPEG, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "none"
    if " prores_ks " in " " + out + " ":
        return "ks"
    # Match either standalone 'prores' or 'prores_aw' (both register as 'prores').
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] in ("prores", "prores_aw"):
            return "plain"
    return "none"


@dataclass
class Metadata:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    codec: str = ""
    fps: float = 0.0
    alpha: bool = False
    # Absolute stream indices (0-based across all streams in the file) of
    # every audio stream with a known codec, in source order. Pro cameras
    # (Canon C300/C500, Sony FX6) record 4 separate mono PCM streams for
    # boom / lav / ambient / scratch — editorial expects all of them
    # preserved as distinct tracks in the output .mov, so we map each by
    # absolute index. iPhone-style sidecar streams (codec_name=unknown) are
    # skipped. Empty list = silent video.
    audio_stream_indexes: list[int] = field(default_factory=list)

    @property
    def resolution(self) -> str:
        if self.width and self.height:
            return f"{self.width}×{self.height}"
        return "—"


@dataclass
class SequenceSpec:
    """A detected image sequence (group of numbered frames in a folder).

    Built by `sequence_scan.scan_folder()`. Drives the ffmpeg image2-demuxer
    input form: `-framerate FPS -start_number N -i <dir>/<prefix>%0Pd<ext>`.
    """
    dir: str             # absolute folder containing the frames
    prefix: str          # stem before the digit run; "" if frames are pure digits
    ext: str             # ".png" / ".exr" / ... (lowercase, leading dot)
    padding: int         # 4 for shot_0001.png; 0 for unpadded
    start_frame: int
    frame_count: int     # frames actually found in the group
    expected_frames: int # max - min + 1; > frame_count means there are gaps
    fps: float           # at queue time, from settings.sequence_fps

    @property
    def pattern_basename(self) -> str:
        digits = f"%0{self.padding}d" if self.padding > 0 else "%d"
        return f"{self.prefix}{digits}{self.ext}"

    @property
    def pattern_path(self) -> str:
        return str(Path(self.dir) / self.pattern_basename)

    @property
    def stripped_stem(self) -> str:
        # Output naming base: trim trailing _ . - left after stripping digits.
        # Pure-digit frames (prefix == "") fall back to the folder name.
        return self.prefix.rstrip("_.-") or Path(self.dir).name

    @property
    def first_frame_path(self) -> str:
        if self.padding > 0:
            name = f"{self.prefix}{self.start_frame:0{self.padding}d}{self.ext}"
        else:
            name = f"{self.prefix}{self.start_frame}{self.ext}"
        return str(Path(self.dir) / name)


def probe_metadata(path: str) -> Metadata:
    """Run ffprobe synchronously. Callers should invoke from a worker thread.

    Walks every stream in the source so we can both:
      - fill Metadata fields from the first video stream (width, height, fps,
        codec, alpha), and
      - find the first *usable* audio stream (known codec_name) so encode
        time can map it by absolute index instead of the positional glob.
    """
    meta = Metadata()
    try:
        proc = subprocess.run(
            [
                FFPROBE, "-v", "error",
                "-show_entries",
                "stream=index,codec_type,codec_name,width,height,r_frame_rate,pix_fmt:format=duration",
                "-of", "json",
                path,
            ],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return meta
    if proc.returncode != 0 or not proc.stdout:
        return meta
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return meta

    fmt = data.get("format") or {}
    try:
        meta.duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        meta.duration = 0.0

    seen_video = False
    for stream in data.get("streams") or []:
        stype = stream.get("codec_type") or ""
        codec = (stream.get("codec_name") or "").strip().lower()

        if stype == "video" and not seen_video:
            seen_video = True
            meta.codec = _human_codec(stream.get("codec_name") or "")
            try:
                meta.width = int(stream.get("width") or 0)
                meta.height = int(stream.get("height") or 0)
            except (TypeError, ValueError):
                pass
            rate = stream.get("r_frame_rate") or "0/1"
            meta.fps = _parse_rate(rate)
            pix_fmt = (stream.get("pix_fmt") or "").lower()
            meta.alpha = _pix_fmt_has_alpha(pix_fmt)

        elif stype == "audio" and codec and codec not in ("unknown", "none"):
            idx = stream.get("index")
            if isinstance(idx, int):
                meta.audio_stream_indexes.append(idx)

    return meta


def probe_sequence_metadata(spec: SequenceSpec) -> Metadata:
    """Build a Metadata for an image sequence without running probe_metadata.

    probe_metadata expects a single demuxable file; the image2-demuxer pattern
    can't be ffprobed directly. We synthesise duration/fps from the spec, then
    ffprobe just the first frame for width/height/pix_fmt (so the alpha flag
    is accurate when the user picks 4444+alpha against a real RGBA EXR/PNG).
    """
    duration = spec.frame_count / spec.fps if spec.fps > 0 else 0.0
    meta = Metadata(
        duration=duration,
        codec=spec.ext.lstrip(".").upper(),
        fps=spec.fps,
        audio_stream_indexes=[],
    )
    first = spec.first_frame_path
    try:
        proc = subprocess.run(
            [
                FFPROBE, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,pix_fmt",
                "-of", "json",
                first,
            ],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return meta
    if proc.returncode != 0 or not proc.stdout:
        return meta
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return meta
    streams = data.get("streams") or []
    if not streams:
        return meta
    stream = streams[0]
    try:
        meta.width = int(stream.get("width") or 0)
        meta.height = int(stream.get("height") or 0)
    except (TypeError, ValueError):
        pass
    pix_fmt = (stream.get("pix_fmt") or "").lower()
    meta.alpha = _pix_fmt_has_alpha(pix_fmt)
    return meta


# Concrete pixel-format tokens that carry an alpha channel. The earlier
# heuristic (`"a" in pix_fmt.split("p", 1)[0]`) misfired on grayscale formats
# because "gray" contains the letter 'a'.
_ALPHA_PIX_FMT_TOKENS = (
    "yuva", "rgba", "argb", "abgr", "bgra", "rgb32", "bgr32",
)


def _pix_fmt_has_alpha(pix_fmt: str) -> bool:
    if not pix_fmt:
        return False
    if any(tok in pix_fmt for tok in _ALPHA_PIX_FMT_TOKENS):
        return True
    # `ya8`, `ya16le`, etc. — grayscale with alpha. Match "ya" followed by a
    # digit so we don't false-positive on "yay" or similar nonsense.
    return len(pix_fmt) > 2 and pix_fmt.startswith("ya") and pix_fmt[2].isdigit()


def _parse_rate(rate: str) -> float:
    try:
        num, den = rate.split("/", 1)
        n, d = float(num), float(den)
        if d == 0:
            return 0.0
        return round(n / d, 3)
    except (ValueError, ZeroDivisionError):
        return 0.0


_CODEC_NAMES = {
    "h264": "H.264", "hevc": "HEVC", "prores": "ProRes", "vp9": "VP9", "av1": "AV1",
    "mpeg4": "MPEG-4", "mpeg2video": "MPEG-2", "mjpeg": "MJPEG", "dnxhd": "DNxHD",
    "vc1": "VC-1", "flv1": "FLV1",
}


def _human_codec(name: str) -> str:
    return _CODEC_NAMES.get(name.lower(), name.upper() if name else "")


def _format_fps(fps: float) -> str:
    """ffmpeg-friendly fps string — int if whole, else 3-decimal."""
    if fps == int(fps):
        return str(int(fps))
    return f"{fps:.3f}".rstrip("0").rstrip(".")


def build_command(
    src: str,
    out: str,
    profile_id: str,
    alpha: bool,
    encoder: str,
    audio_indexes: Optional[list[int]] = None,
    audio_bits: int = 16,
    sequence: Optional[SequenceSpec] = None,
) -> list[str]:
    """Assemble the ffmpeg command list for a single encode.

    `audio_indexes` is the absolute stream indices of every known-codec audio
    track in the source (see `probe_metadata`). Each one is mapped into the
    output as a separate track — pro cameras record 4 separate mono PCM
    streams that editorial wants preserved as distinct tracks, not collapsed.

      audio_indexes == list of ints  → `-map 0:<i>` for each (what we want)
      audio_indexes == []            → silent output (no audio map, no -c:a)
      audio_indexes is None          → fallback `-map 0:a:0?` (first audio,
                                        optional) for ad-hoc callers who
                                        haven't probed yet

    `sequence`, when set, switches the input to the image2 demuxer
    (`-framerate FPS -start_number N -i <pattern>`) and forces no-audio. The
    `src` arg is ignored for sequences; pass the first-frame path or "" for it.
    """
    profile = PROFILES_BY_ID[profile_id]
    pix_fmt = pick_pixel_format(profile_id, alpha)
    cmd: list[str] = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-nostdin",
    ]
    if sequence is not None:
        # image2 demuxer: deterministic frame ordering via %0Nd, no hwaccel
        # (it's decoder-side, irrelevant for still-image input), no audio.
        # -vsync passthrough on EXR/DPX keeps ffmpeg from dropping/duplicating
        # frames when the file mtimes look unusual.
        if sequence.ext in (".exr", ".dpx"):
            cmd += ["-vsync", "passthrough"]
        cmd += [
            "-framerate", _format_fps(sequence.fps),
            "-start_number", str(sequence.start_frame),
            "-i", sequence.pattern_path,
            "-map", "0:v:0",
        ]
        has_audio = False
    else:
        hw = get_hwaccel()
        if hw:
            # ffmpeg silently falls back to CPU decode for codecs the GPU can't
            # handle (MJPEG, ProRes input, etc.), so unconditional -hwaccel is safe.
            cmd += ["-hwaccel", hw]
        cmd += ["-i", src, "-map", "0:v:0"]

        if audio_indexes is None:
            # No probe info → safe fallback (first known audio, optional).
            cmd += ["-map", "0:a:0?"]
            has_audio = True
        elif audio_indexes:
            for idx in audio_indexes:
                cmd += ["-map", f"0:{idx}"]
            has_audio = True
        else:
            has_audio = False

    if encoder == "ks":
        cmd += ["-c:v", "prores_ks", "-profile:v", profile.id, "-pix_fmt", pix_fmt]
        if alpha and profile.pid >= 4:
            cmd += ["-alpha_bits", "16"]
    else:
        cmd += ["-c:v", "prores", "-profile:v", str(profile.pid), "-pix_fmt", pix_fmt]
    if has_audio:
        # Single -c:a spec applies to every mapped audio stream; each stays as
        # its own track in the output .mov, just re-encoded. 24-bit preserves
        # pro-camera dynamic range; 16-bit is the editorial default.
        audio_codec = "pcm_s24le" if audio_bits == 24 else "pcm_s16le"
        cmd += ["-c:a", audio_codec]
    cmd += [
        "-f", "mov",
        "-movflags", "+use_metadata_tags",
        "-progress", "pipe:1",
        out,
    ]
    return cmd


def format_preview_command(
    src_name: str,
    out_path: str,
    profile_id: str,
    alpha: bool,
    audio_bits: int = 16,
    sequence: Optional[SequenceSpec] = None,
) -> str:
    """Pretty multi-line preview for the ffmpeg command box. Uses prores_ks always.

    The real command maps each known audio stream by absolute index; the
    preview shows `0:a?` (glob-all) for brevity — the runtime behaviour is
    equivalent when every audio stream is known-codec.

    When `sequence` is provided, renders the image2-demuxer form (no hwaccel,
    no audio map / codec) using shlex-quoted pattern path.
    """
    profile = PROFILES_BY_ID[profile_id]
    pix_fmt = pick_pixel_format(profile_id, alpha)
    alpha_flag = " -alpha_bits 16" if (alpha and profile.pid >= 4) else ""
    if sequence is not None:
        vsync_line = "  -vsync passthrough \\\n" if sequence.ext in (".exr", ".dpx") else ""
        return (
            "ffmpeg -hide_banner -y \\\n"
            + vsync_line
            + f"  -framerate {_format_fps(sequence.fps)} \\\n"
            + f"  -start_number {sequence.start_frame} \\\n"
            + f"  -i {shlex.quote(sequence.pattern_path)} \\\n"
            "  -map 0:v:0 \\\n"
            f"  -c:v prores_ks -profile:v {profile.id} \\\n"
            f"  -pix_fmt {pix_fmt}{alpha_flag} \\\n"
            "  -movflags +use_metadata_tags \\\n"
            f"  {shlex.quote(out_path)}"
        )
    hw = get_hwaccel()
    hw_line = f"  -hwaccel {hw} \\\n" if hw else ""
    audio_codec = "pcm_s24le" if audio_bits == 24 else "pcm_s16le"
    return (
        "ffmpeg -hide_banner -y \\\n"
        + hw_line
        + f'  -i "{src_name}" \\\n'
        "  -map 0:v:0 -map 0:a? \\\n"
        f"  -c:v prores_ks -profile:v {profile.id} \\\n"
        f"  -pix_fmt {pix_fmt}{alpha_flag} \\\n"
        f"  -c:a {audio_codec} \\\n"
        "  -movflags +use_metadata_tags \\\n"
        f'  "{out_path}"'
    )


def plan_output_path(
    src: str,
    out_dir: str,
    naming: str,
    profile_id: str,
    stem_override: Optional[str] = None,
) -> str:
    """Output path rules from prowrap-yad.sh: keep vs suffix, with ' (N)' disambiguation.

    `stem_override`, when given, replaces the filename-derived stem. Used for
    image-sequence jobs where the "name" comes from a SequenceSpec, not a file.
    """
    stem = stem_override if stem_override is not None else Path(src).stem
    if naming == "suffix":
        base = f"{stem}_prores_{profile_id}"
    else:
        base = stem
    candidate = Path(out_dir) / f"{base}.mov"
    if not candidate.exists():
        return str(candidate)
    n = 1
    while True:
        trial = Path(out_dir) / f"{base} ({n}).mov"
        if not trial.exists():
            return str(trial)
        n += 1


@dataclass
class EncodeJob:
    src: str
    out: str
    duration: float
    on_progress: Callable[[float], None]  # 0..1 (file-local)
    on_done: Callable[[bool, Optional[str]], None]  # (success, error_text)
    # ffmpeg's `-progress` emits `speed=1.5x` every ~1s; this callback
    # surfaces that as a float (1.5 = encoding 1.5 seconds of source per
    # second of wall time). Optional — None = caller doesn't care.
    on_speed: Optional[Callable[[float], None]] = None
    # Resolved at file-add time via probe_metadata. Threaded through so
    # build_command can map each known audio stream by absolute index.
    # Empty list = silent video; None = no probe info (safe fallback applies).
    audio_stream_indexes: Optional[list[int]] = None
    # When set, this is an image-sequence job, not a single-file job. `src`
    # still carries the first-frame path (load-bearing for `_copy_mtime` and
    # the orphan marker), but the ffmpeg input form switches to image2.
    # `duration` should be set to `frame_count / fps` by the caller so the
    # existing -progress parser produces correct percentages.
    sequence: Optional[SequenceSpec] = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    _proc: Optional[subprocess.Popen] = None

    def cancel(self) -> None:
        self.cancel_event.set()
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass


def run_encode(job: EncodeJob, profile_id: str, alpha: bool, encoder: str, audio_bits: int = 16) -> None:
    """Blocking. Runs ffmpeg, streams progress lines, invokes callbacks."""
    if job.cancel_event.is_set():
        job.on_done(False, "cancelled")
        return

    cmd = build_command(
        job.src, job.out, profile_id, alpha, encoder,
        job.audio_stream_indexes, audio_bits,
        sequence=job.sequence,
    )
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as e:
        job.on_done(False, f"ffmpeg not found: {e}")
        return
    job._proc = proc
    _mark_encode_started(job.out)

    duration_us = max(1.0, (job.duration or 0) * 1_000_000)
    last_pct = 0.0
    assert proc.stdout is not None

    try:
        for raw in proc.stdout:
            if job.cancel_event.is_set():
                try:
                    proc.terminate()
                except Exception:
                    pass
                break
            line = raw.strip()
            if not line or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key == "out_time_us" and val.isdigit():
                pct = min(1.0, int(val) / duration_us)
                if pct - last_pct >= 0.005 or pct >= 1.0:
                    last_pct = pct
                    job.on_progress(pct)
            elif key == "out_time_ms" and val.isdigit():
                # out_time_ms is actually in microseconds in ffmpeg (historical naming).
                pct = min(1.0, int(val) / duration_us)
                if pct - last_pct >= 0.005 or pct >= 1.0:
                    last_pct = pct
                    job.on_progress(pct)
            elif key == "speed" and val.endswith("x") and job.on_speed is not None:
                # ffmpeg writes `speed=1.5x` (or `speed=N/A` while warming up).
                try:
                    spd = float(val[:-1])
                except ValueError:
                    pass
                else:
                    job.on_speed(spd)
            elif key == "progress" and val == "end":
                job.on_progress(1.0)

        # If we broke out on cancel, drain any remaining stdout so ffmpeg isn't
        # blocked on a full pipe before it can respond to SIGTERM.
        if job.cancel_event.is_set():
            try:
                proc.stdout.read()
            except Exception:
                pass

        # Short wait after cancel/finish — 5s is plenty. If still alive, SIGKILL
        # and a brief second wait so the zombie is reaped before we inspect
        # returncode.
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

        if job.cancel_event.is_set():
            _safe_unlink(job.out)
            job.on_done(False, "cancelled")
            return

        if proc.returncode == 0 and _nonempty_file(job.out):
            _copy_mtime(job.src, job.out)
            job.on_done(True, None)
        else:
            err = ""
            if proc.stderr is not None:
                try:
                    err = proc.stderr.read() or ""
                except Exception:
                    err = ""
            _safe_unlink(job.out)
            job.on_done(False, (err.strip() or f"ffmpeg exited {proc.returncode}"))
    finally:
        # Always close stdout/stderr so FDs aren't leaked on long queues or
        # mid-stream cancels. Safe to call on already-closed streams.
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
        # Clear the orphan marker — encode reached a terminal state, success
        # or failure. SIGKILL/crash is the only path that leaves it behind.
        _mark_encode_finished()


def _nonempty_file(path: str) -> bool:
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _copy_mtime(src: str, dst: str) -> None:
    try:
        st = os.stat(src)
        os.utime(dst, (st.st_atime, st.st_mtime))
    except OSError:
        pass


def preview_shell_command(src: str, out: str, profile_id: str, alpha: bool, encoder: str) -> str:
    """For copy-to-clipboard style usage; kept simple — not used by UI preview box."""
    return " ".join(shlex.quote(x) for x in build_command(src, out, profile_id, alpha, encoder))
