# NO-CODER

A native GTK4 + libadwaita batch transcoder for Omarchy. Drop video files (or whole camera cards) onto the window, choose a ProRes profile, hit Encode. Output is editorial-ready Apple ProRes `.mov` ready for DaVinci Resolve, Premiere, FCP, Avid.

## Features

- **Real ffmpeg encode** — `prores_ks` (with fallback to plain `prores`), live progress bar parsed from `-progress pipe:1`, cancelable, serial queue with disk-space pre-check.
- **GPU decode auto-probe** — installer tests `cuda` → `qsv` → `vaapi` and pins the working one to `~/.config/nocoder/config.json`. ProRes encoding stays on CPU (no vendor ships a GPU ProRes encoder), but offloading the *decode* side cuts wall time by 25-40% on H.264 / HEVC / AV1 sources.
- **Theme-aware** — palette tracks the active Omarchy theme on every launch (parses `colors.toml` / `ghostty.conf` / `alacritty.toml` / `kitty.conf` in priority order). 34 stock + custom themes verified.
- **Pro camera ready** — `.MXF` from Canon XF / Sony XDCAM / Panasonic AVC-Intra, with proxy-directory pruning so dropping a Sony XAVC card maps only the masters in `CLIP/` and not the low-res duplicates in `SUB/`.
- **Multi-track audio preserved** — Canon C300/C500 records 4 mono PCM streams; all four land in the output `.mov` as separate tracks. Optional 24-bit toggle for pro delivery.
- **Live encode-speed indicator** — footer shows real `1.5×` throughput from ffmpeg and refines the ETA from actual measured rate, not a fixed heuristic. After the batch finishes, the footer also reports total wall time alongside succeeded / failed / output size.
- **Per-core CPU visualizer** — collapsible btop-style pane between the queue and the footer, polling `/proc/stat` once a second and painting one vertical bar per logical core in the live theme accent. Useful for seeing at a glance whether the chosen profile / hwaccel combo is saturating the box or leaving headroom. Expand/collapse state persists across launches.
- **Hyprland-aware install** — registers a `.desktop` entry with the walker, installs the icon at six hicolor sizes, appends a windowrule that floats and centres the app at 1280×880.

## Supported source formats

`.mov` `.mp4` `.m4v` `.mkv` `.avi` `.mts` `.m2ts` `.webm` `.mpeg` `.mpg` `.3gp` `.3g2` `.mxf`

**Not supported** (proprietary RAW; ffmpeg has no decoder without vendor SDKs): `.crm` (Canon Cinema RAW Light), `.braw` (Blackmagic), `.r3d` (RED), `.ari` (Arri). Pre-transcode those via Canon Cinema RAW Development / Blackmagic RAW Player / REDCINE-X / ARRI Meta Extract first, then bring the resulting MXF or MOV into NO-CODER.

## Install

Targets Arch / Omarchy specifically.

```sh
git clone https://git.no-signal.uk/nosignal/nocoder.git
cd nocoder
bash install.sh
```

The installer:

1. Verifies pacman is present, fails fast otherwise.
2. Installs missing pacman packages: `python python-gobject gtk4 libadwaita ffmpeg`.
3. Installs Inter and JetBrains Mono fonts to `~/.local/share/fonts/` (per-user, no sudo).
4. Probes GPU decode and pins the working backend to `~/.config/nocoder/config.json`.
5. Copies the source tree to `~/.local/share/nocoder/` so you can delete this clone afterward.
6. Writes a launcher to `~/.local/bin/nocoder`.
7. Drops the `.desktop` file and PNG icons into the right XDG locations.
8. Appends Hyprland windowrules (float, centre, 1280×880) inside a marked block in `~/.config/hypr/windows.conf`.
9. Restarts walker so the entry appears immediately.

After install, **Super+Space → "no"** launches it. Or `nocoder` from a shell.

## Updating

```sh
cd nocoder
git pull
bash install.sh
```

Re-running the installer wipes and re-copies the live install dir — files removed upstream propagate cleanly.

## Uninstall

```sh
bash uninstall.sh
```

Removes the installed app tree, launcher, desktop entry, all six icon sizes, the Hyprland windowrules block, and the per-user config. Pacman packages and fonts are left in place (other apps may need them).

## Hardware

- **Required:** anything that runs Omarchy / Hyprland.
- **Recommended:** a GPU with ffmpeg-supported decode (NVIDIA NVDEC, Intel QSV, AMD VAAPI). The probe falls back to CPU decode on systems without; everything still works, just slower on camera-native sources.
- **No upper limit on cores** — `prores_ks` is well-parallelised.

## Configuration

`~/.config/nocoder/config.json` is read at launch and merged on every settings change. All keys are optional:

```json
{
  "hwaccel":            "cuda" | "qsv" | "vaapi" | "none",
  "profile":            "proxy" | "lt" | "standard" | "hq" | "4444" | "4444xq",
  "naming":             "keep" | "suffix",
  "out_dir":            "/absolute/path/to/output/folder",
  "audio_bits":         16 | 24,
  "auto_reveal":        true | false,
  "cpu_pane_expanded":  true | false
}
```

You can edit by hand to override the auto-probed `hwaccel` or to seed defaults; everything else just reflects what you've picked in the UI most recently.

## Known gaps

- "Reveal in Files" opens the output folder but doesn't *select* the specific file.
- Per-row remove button isn't keyboard-accessible (mouse-hover only, by design — keeps tab order clean).
- No live theme-change pickup — theme swaps apply on next launch, not immediately.

## License

Not yet specified. The app wraps `ffmpeg` and depends on GTK4 / libadwaita; check those licenses for the redistributable parts.

## Credits

Born as a rewrite of `prowrap-yad.sh` (a yad-based ProRes batch transcoder), rebranded NO-CODER to lean into the visual identity. The encoding logic from the original bash script is preserved verbatim.
