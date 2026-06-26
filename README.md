# nef-watch

![platform](https://img.shields.io/badge/platform-macOS%20·%20Apple%20Silicon-111)
![license](https://img.shields.io/badge/license-MIT-blue)

Batch-convert Nikon **NEF** raw files to **TIFF** with Nikon's native in-camera
rendering baked in — the same look NX Studio produces — by driving Nikon's own
**NEF/NRW Image SDK** headlessly. Point it at a folder and it watches for new NEFs
and converts them; or run it once over a folder you already have. It can also
transcode NEF → **DNG**.

Lightroom and Capture One don't match Nikon's color, and NX Studio has no command
line. `nef-watch` uses Nikon's actual rendering engine, so the output matches NX
Studio's own export to the 8-bit quantization floor (mean abs error ≈ 1.7/255,
visually indistinguishable — see [Validation](#validation)).

## Features

- **Nikon's in-camera look**, headless — Picture Control, white balance, Active
  D-Lighting applied exactly as the camera/NX Studio would, with no GUI.
- **Watch mode** — drop NEFs into a folder, get TIFFs out, unattended.
- **Batch mode** — convert an existing folder in one shot.
- **TIFF or DNG** — `--format tiff` (rendered, Nikon look) and/or `--format dng`
  (raw transcode, editable, via dnglab or Adobe DNG Converter).
- **Parallel**, **idempotent** (skips already-converted files), and **atomic**
  (no half-written outputs on Ctrl-C).
- Output is 8-bit (or 16-bit) LZW TIFF with the Nikon sRGB profile embedded —
  byte-for-byte the format NX Studio writes.

## How it works

A small C++/Objective-C++ helper (`nef_render`) links Nikon's `libImgSDK.dylib`
and renders a NEF to a pixel buffer using `DevelopColorMode = AppliedInCamera`
(the camera-matched pipeline). A Python CLI (`nef_watch.py`) handles folder
watching, batching, parallelism, and encodes the TIFF (Pillow) or routes the NEF
to a DNG transcoder. DNG conversion does **not** use the SDK — it preserves the
raw sensor data and therefore does not bake in the Nikon look (that's the TIFF
path's job).

## Requirements

- **macOS on Apple Silicon** (the SDK ships universal; tested on Apple Silicon).
- **Nikon NEF/NRW Image SDK v1.46+** — you must obtain this yourself from Nikon at
  <https://sdk.nikonimaging.com/> (free, application required). It is proprietary
  and **not** redistributed here. Point the build at it via `SDK_DIR` (see below).
- **Xcode command-line tools** (`clang++`).
- **Python 3** with `pillow` and `numpy` (`tifffile` only for `--bits 16`).
- For DNG: **dnglab** (`brew install dnglab`, default) or **Adobe DNG Converter**
  (`brew install --cask adobe-dng-converter`, for `--dng-engine adobe`).

## Installation

```bash
git clone https://github.com/rohanpandula/nef-watch.git
cd nef-watch
pip install pillow numpy tifffile

# Build the SDK render helper. Point SDK_DIR at your unpacked Nikon Image SDK:
SDK_DIR="/path/to/Image SDK/Library/Mac" bash tool/build.sh
```

`build.sh` compiles `nef_render` and stages the SDK's required `prm.bin` resource
next to it. DNG-only use needs neither the SDK nor this build step.

## Usage

```bash
# Watch a folder — convert each NEF as it lands (Ctrl-C to stop)
tool/nef_watch.py ~/Incoming --out ~/Exports

# One-shot: convert everything already in a folder, then exit
tool/nef_watch.py ~/Shoot --out ~/Shoot/tiff --once

# DNG instead of / in addition to TIFF
tool/nef_watch.py ~/Shoot --out ~/dng --once --format dng
tool/nef_watch.py ~/Shoot --out ~/out --once --format both
```

### Options

| Flag | Default | Meaning |
|------|---------|---------|
| `input` (positional) | — | folder to watch / scan for `.NEF` |
| `--out`, `-o` | *(required)* | output folder |
| `--format` | `tiff` | `tiff` (Nikon look), `dng` (raw transcode), or `both` |
| `--once` | off | convert existing NEFs once, then exit (default: keep watching) |
| `--jobs`, `-j` | `4` | parallel workers |
| `--bits {8,16}` | `8` | TIFF bit depth |
| `--dng-engine` | `dnglab` | DNG backend: `dnglab` or `adobe` |
| `--dng-embed-original` | off | embed the original NEF inside the DNG (much larger) |
| `--recursive`, `-r` | off | scan subfolders too |
| `--overwrite` | off | re-convert even if the output exists |
| `--interval` | `3.0` | watch poll interval (seconds) |
| `--profile` | Nikon sRGB | output ICC profile |

## Validation

The TIFF output was checked against NX Studio's own exports of the same NEFs
(Nikon Z f). Across multiple images the mean absolute error is **≈ 1.74 / 255**
(max 7, PSNR 41.4 dB) — and that residual is *pure 8-bit requantization*, not a
rendering difference: the SDK's 8-bit output is byte-identical to rounding its own
16-bit render down to 8-bit, edge-correlation with the difference is ~0, and no
sharpening/dither/LUT closes it. In other words, it sits at the theoretical
quantization floor. Full method and numbers in [`docs/VALIDATION.md`](docs/VALIDATION.md).

## Performance

A 24 MP develop is ~6 s and the SDK uses only ~1¼ cores, so conversions run in a
small pool (`--jobs`, default 4). The develop is memory-bandwidth-bound, so
throughput plateaus around 3–4 workers (~2.2× vs serial; more oversaturates).
DNG transcoding is much faster (~0.5 s/file with dnglab).

## Limitations

- macOS + Apple Silicon only; Nikon bodies supported by Image SDK v1.46.
- The build bakes the SDK's `Lib/release` path into the helper's `@rpath`; re-run
  `build.sh` if you move the SDK.
- DNG carries no Nikon look by design — use TIFF for the finished render.

## License

MIT — see [LICENSE](LICENSE). This covers only this project's code. The Nikon
Image SDK is Nikon's property under its own license; obtain and use it per Nikon's
terms. DNG conversion relies on [dnglab](https://github.com/dnglab/dnglab) or
Adobe DNG Converter, each under its own license.

## Acknowledgments

- Nikon, for the NEF/NRW Image SDK.
- The r/Nikon thread that surfaced the SDK call-ordering detail (`RawParameterSet`
  before the output profile) that keeps the render correct once adjustments are added.
