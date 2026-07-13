# nef-watch

![platform](https://img.shields.io/badge/platform-macOS%20arm64%20·%20Linux%20amd64%20Docker-111)
![license](https://img.shields.io/badge/license-MIT-blue)

Batch-convert Nikon **NEF** raw files to **TIFF** with Nikon's in-camera
rendering baked in — targeting the look NX Studio produces — by driving Nikon's own
**NEF/NRW Image SDK** headlessly. Point it at a folder and it watches for new NEFs
and converts them; or run it once over a folder you already have. It can also
produce **JPEG** for sharing, and transcode NEF → **DNG**.

Lightroom and Capture One don't match Nikon's color, and NX Studio has no command
line. `nef-watch` uses Nikon's actual rendering engine, so the output is visually
very close to NX Studio (mean abs error ≈ 1.7/255), but current exports are not
pixel-identical — see [Validation](#validation).

## Features

- **Nikon's in-camera look**, headless — Picture Control, white balance, and
  Active D-Lighting applied by Nikon's SDK, with no GUI.
- **Watch mode** — drop NEFs into a folder, get TIFFs out, unattended. A failed
  render is retried (up to 3×, with backoff) instead of being blacklisted, and
  a file that fails while still uploading (e.g. mid-FTP) is retried immediately
  once its size/mtime settle. Give-ups are logged clearly and re-armed if the
  file changes later.
- **Batch mode** — convert an existing folder in one shot.
- **TIFF, JPEG, and/or DNG** — `--format tiff` (rendered, Nikon look),
  `--format jpeg` (rendered, smaller, for share/gallery use), and/or
  `--format dng` (raw transcode, editable, via dnglab or Adobe DNG Converter).
  Combine any of them: `--format tiff,jpeg`.
- **EXIF carried over** — GPS and makernotes from the source NEF are copied onto
  every TIFF/JPEG output (via exiftool).
- **Recursive watch mirrors the input tree** — with `-r`, output subfolders match
  input subfolders, so same-named files from different folders don't collide.
- **Parallel**, **idempotent** (skips already-converted files), and **atomic**
  (no half-written outputs on Ctrl-C). In `--once` mode, Ctrl-C finishes
  in-flight conversions, cancels the rest, and prints a partial summary.
- Output is 8-bit (or 16-bit) LZW TIFF (or JPEG) with the Nikon sRGB profile
  embedded and the source NEF's EXIF carried over. The current decoded-pixel
  difference from NX Studio is measured in [Validation](#validation).
- Inputs larger than 512 MiB and rendered rasters above 100 MP are rejected by
  default before they can exhaust a long-running container; the input limit is
  adjustable for a known-valid larger file.

## TIFF, JPEG, or DNG — which do I want?

- **TIFF** — the finished photo *with* the Nikon-rendered look, lossless. Most
  people want this for archival/editing.
- **JPEG** — the same rendered look, much smaller. Good for sharing, galleries,
  or anywhere you don't need a lossless master.
- **DNG** — the raw, for re-editing later. It does **not** carry the Nikon look:
  opened in Lightroom / Apple Photos / Capture One it renders with *their* color
  science, so **it will look different from NX Studio** — the exact mismatch this
  tool exists to avoid. Use DNG to keep raw latitude, not for the look.

## How it works

A small native helper (`nef_render`) loads Nikon's Image SDK and renders a NEF
to a pixel buffer using `DevelopColorMode = AppliedInCamera`
(the camera-matched pipeline), with `--exp-comp` applied during that develop if
given. A Python CLI (`nef_watch.py`) handles folder watching, batching,
parallelism, and encodes the TIFF/JPEG (Pillow) or routes the NEF to a DNG
transcoder. Orientation on rendered output is normalized to `1`, because the SDK
already emits display-oriented, pre-rotated pixels — leaving the EXIF tag at its
original value would double-rotate the image in viewers that respect it. If
`exiftool` is installed, the source NEF's full EXIF (GPS, makernotes included) is
then copied onto the output. DNG conversion does **not** use the SDK — it
preserves the raw sensor data and therefore does not bake in the Nikon look
(that's the TIFF/JPEG path's job).

## Requirements

- **macOS on Apple Silicon**, or **Linux x86-64 with Docker/Wine** (tested on
  Unraid 7.3.1). Nikon does not ship or support a native Linux SDK; the Docker
  path runs Nikon's unmodified Windows x64 SDK under Wine.
- **Nikon NEF/NRW Image SDK v1.46.0** for the validated Docker baseline — you
  must obtain this yourself from Nikon at
  <https://sdk.nikonimaging.com/> (free, application required). It is proprietary
  and **not** redistributed here. Mount its `Image SDK/Library/win` directory
  read-only when the Linux container starts for the first time.
- Native macOS builds need **Xcode command-line tools** (`clang++`), **Python
  3.9+** with the packages in `requirements.txt`, and optionally **ExifTool**
  (`brew install exiftool`) for EXIF carry-over.
- The public Linux image bundles Python, TIFF/JPEG dependencies, Wine, Xvfb,
  ExifTool, and the MinGW toolchain used for first-start adapter compilation.
  It contains no Nikon SDK files.
- For DNG on macOS: **dnglab** (`brew install dnglab`, default) or **Adobe DNG
  Converter** (`brew install --cask adobe-dng-converter`, for
  `--dng-engine adobe`). DNG tooling is not bundled in the Linux image.

On Homebrew/system Python, `pip install` may refuse with an "externally managed
environment" error (PEP 668). Use a venv, `pipx`, `uv pip install`, or skip the
install step entirely with `uv run` (below).

## Installation

```bash
git clone https://github.com/rohanpandula/nef-watch.git
cd nef-watch

# Build the SDK render helper. Point SDK_DIR at your unpacked Nikon Image SDK:
SDK_DIR="/path/to/Image SDK/Library/Mac" bash tool/build.sh
```

For Linux or Unraid, pull the ready-to-run SDK-free image; users do not build it:

```bash
docker pull ghcr.io/rohanpandula/nef-watch:linux-amd64
```

At first start, mount your Nikon Windows SDK directory at `/nikon-sdk:ro` and a
persistent Docker volume at `/var/lib/nef-watch`. The container verifies every
SDK file, compiles the open wrapper, and stages the licensed runtime only in
that private volume. See [Linux and Unraid Docker deployment](docs/DOCKER.md)
for the complete `docker run` and Compose setup.

`build.sh` compiles `nef_render` and stages two SDK resources next to it: the
required `prm.bin` runtime resource, and the `NKsRGB.icm` profile that becomes
`nef_watch.py`'s default `--profile` (no more machine-specific default — it just
works after a build). DNG-only use needs neither the SDK nor this build step.

For the Python side, either install dependencies normally:

```bash
pip install pillow numpy tifffile imagecodecs
```

or skip that entirely — `nef_watch.py` carries inline PEP 723 script metadata, so
[`uv`](https://github.com/astral-sh/uv) resolves and runs it in an ephemeral
environment with zero setup:

```bash
uv run tool/nef_watch.py ~/Incoming --out ~/Exports
```

## Usage

```bash
# Watch a folder — convert each NEF as it lands (Ctrl-C to stop)
tool/nef_watch.py ~/Incoming --out ~/Exports

# One-shot: convert everything already in a folder, then exit
tool/nef_watch.py ~/Shoot --out ~/Shoot/tiff --once

# JPEG instead of / in addition to TIFF
tool/nef_watch.py ~/Shoot --out ~/jpg --once --format jpeg --quality 90
tool/nef_watch.py ~/Shoot --out ~/out --once --format tiff,jpeg

# DNG instead of / in addition to TIFF
tool/nef_watch.py ~/Shoot --out ~/dng --once --format dng
tool/nef_watch.py ~/Shoot --out ~/out --once --format tiff,dng

# Convert a single file (no folder needed)
tool/nef_watch.py ~/Shoot/DSC_0001.NEF --out ~/Shoot/tiff --once

# .NRW (Coolpix raw) is matched everywhere .NEF is — no separate flag
tool/nef_watch.py ~/Shoot --out ~/Shoot/tiff --once

# Native macOS only: byte-reproducible output (pins the SDK's dither)
tool/nef_watch.py ~/Shoot --out ~/tiff --once --deterministic
```

### Options

| Flag | Default | Meaning |
|------|---------|---------|
| `input` (positional) | — | folder to watch/scan, or a single `.NEF`/`.NRW` file to convert once |
| `--out`, `-o` | *(required)* | output folder |
| `--format` | `tiff` | comma-separated: any of `tiff`, `jpeg`, `dng` (e.g. `tiff,dng`); `both` = legacy alias for `tiff,dng` |
| `--quality` | `90` | JPEG quality (1–100) |
| `--once` | off | convert existing files once, then exit |
| `--jobs`, `-j` | `4` | parallel workers |
| `--bits {8,16}` | `8` | TIFF bit depth |
| `--exp-comp` | `0.0` | exposure compensation in EV applied during the SDK develop (tiff/jpeg) |
| `--deterministic` | off | byte-reproducible TIFF/JPEG on native macOS — pin the SDK's `rand()`-seeded dither (same look; see [Limitations](#limitations)) |
| `--dng-engine` | `dnglab` | `dnglab` or `adobe` |
| `--dng-embed-original` | off | embed the original NEF inside the DNG |
| `--recursive`, `-r` | off | scan subfolders; output mirrors the input folder structure |
| `--overwrite` | off | re-convert even if outputs exist |
| `--interval` | `3.0` | watch poll interval (seconds) |
| `--max-input-mib` | `512` | reject larger NEF/NRW files before invoking the proprietary SDK; raise only for a known-valid larger file |
| `--profile` | staged Nikon sRGB | ICC profile; default `tool/Contents/Resources/NKsRGB.icm` (staged from your SDK by `build.sh`) |
| `--log-file` | — | also append timestamped log lines to this file |
| `--render-bin` | `tool/nef_render` | path to the render helper |

## Run unattended (launchd)

To run `nef-watch` continuously in the background — surviving logout/login and
restarting itself if it crashes — install it as a
[launchd](https://www.launchd.info/) LaunchAgent:

```bash
cp contrib/com.nef-watch.plist ~/Library/LaunchAgents/
```

Edit the placeholders in `~/Library/LaunchAgents/com.nef-watch.plist` (marked
with XML comments): the absolute path to a `python3` that has pillow/numpy
installed (launchd does **not** inherit your shell `PATH` — a bare `python3`
resolves to Apple's system Python and crash-loops under `KeepAlive`; use the
output of `which python3`), the absolute path to `nef_watch.py`, the watch
folder, the output folder, and your `--log-file` path. Then load it:

```bash
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.nef-watch.plist
```

To stop and unload it:

```bash
launchctl bootout gui/$UID ~/Library/LaunchAgents/com.nef-watch.plist
```

(On older macOS, or if `bootstrap`/`bootout` aren't available, the legacy
equivalents are `launchctl load -w` / `launchctl unload -w` on the same plist
path.)

Two independent log streams land in `~/Library/Logs/`: `nef-watch.log` (the
app's own timestamped conversion log, from `--log-file` in the plist) and
`nef-watch.out.log` / `nef-watch.err.log` (raw stdout/stderr captured by
launchd itself — useful if the process fails before it even gets to logging).

If the watched folder is a network mount (e.g. an SMB share) that drops, the
watcher doesn't exit — it warns, keeps polling, and logs recovery once the
mount reappears, so the LaunchAgent doesn't need `KeepAlive` to survive a
flaky mount.

## Validation

The TIFF output was checked against NX Studio's own exports of the same Nikon Z f
NEFs. Across multiple images the mean absolute error is **≈ 1.74 / 255** (max 7,
PSNR 41.4 dB), which is visually negligible but is a real decoded-pixel delta.
An earlier claim that this was a pure 8-bit quantization effect was incorrect and
has been retracted. Two supplied NX Studio exports of the same NEF also differ
from each other (MAE ≈ 2.28/255, max 11); controlled native-8-bit, intent,
quality, and color-process tests localize that mismatch to the 8-bit noise/dither
realization but do not make it exact. The exact validator intentionally fails the
current baseline; full method, current counts, and the macOS/Linux/Docker gate are in
[`docs/VALIDATION.md`](docs/VALIDATION.md).

## Performance

A 24 MP develop is ~6 s and the SDK uses only ~1¼ cores, so conversions run in a
small pool (`--jobs`, default 4). The develop is memory-bandwidth-bound, so
throughput plateaus around 3–4 workers (~2.2× vs serial; more oversaturates).
DNG transcoding is much faster (~0.5 s/file with dnglab).

## Limitations

- Native mode is macOS/Apple Silicon. Linux support is an experimental
  `linux/amd64` Wine container; Nikon does not officially support this runtime.
- The build bakes the SDK's `Lib/release` path into the helper's `@rpath`; re-run
  `build.sh` if you move the SDK.
- DNG carries no Nikon look by design — use TIFF/JPEG for the finished render.
- EXIF carry-over requires `exiftool` on `PATH`; without it, TIFF/JPEG outputs
  have no EXIF at all (not even the basics Pillow would otherwise write).
- Orientation on TIFF/JPEG output is always normalized to `1` — the pixels are
  already rotated to display orientation by the SDK, so this is correct, but it
  means the output's Orientation tag does not simply mirror the source NEF's.
- **Some NEFs render slightly differently every time** (not bit-reproducible).
  Convert the same file twice and the two outputs won't be a byte-for-byte match
  — they differ by a fine, invisible speckle across the frame (MAE ≈ 5/255, max
  ≈ 35). You can't see it; it only matters if you rely on the files being
  *exactly* identical (hash checks, deduplication, reproducible pipelines).

  *What causes the run-to-run delta:* Nikon's SDK adds a little intentional
  **dither** — the same trick newspapers use to print smooth gray skies out of
  tiny dots; a controlled
  speckle that stops smooth areas from banding. The SDK picks that speckle
  pattern from the C library's `rand()`, **re-seeded from the clock on every
  run**, so it lands differently each time. (Traced by interposing `rand()`:
  forcing a fixed sequence makes two native macOS renders byte-identical.) It's
  applied only on some files — many are perfectly stable — and which ones get it
  tracks the shot's in-camera tone processing (adaptive-tone / Active
  D-Lighting-style stages). It is **not** the lens, Picture Control, ISO, or
  firmware: each was ruled out by finding same-setting files on both sides.

  *The repeatability fix:* on native macOS, pass **`--deterministic`** to pin the
  dither to a fixed pattern, so the same NEF produces the same bytes. This only
  addresses repeatability; it does not close or explain the NX Studio-to-SDK
  delta. The option injects the small `rand_freeze.dylib` built by `build.sh` and
  is intentionally unavailable in Docker.

## License

MIT — see [LICENSE](LICENSE). This covers only this project's code. The Nikon
Image SDK is Nikon's property under its own license; obtain and use it per Nikon's
terms. DNG conversion relies on [dnglab](https://github.com/dnglab/dnglab) or
Adobe DNG Converter, each under its own license. The public Linux image contains
Debian-packaged Wine, Xvfb, MinGW, ExifTool, and their dependencies, plus
Microsoft's Visual C++ Redistributable. Those components retain their respective
licenses and are not relicensed by this repository's MIT license. Nikon files
are supplied by the user at runtime and remain in the user's private volume.

## Acknowledgments

- Nikon, for the NEF/NRW Image SDK.
- The r/Nikon thread that surfaced the SDK call-ordering detail (`RawParameterSet`
  before the output profile) that keeps the render correct once adjustments are added.
