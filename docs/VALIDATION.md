# Validation: does the SDK render match NX Studio?

The target for `nef-watch` is that Nikon's NEF/NRW Image SDK produce the *same*
image as Nikon's NX Studio, so a headless tool can replace the GUI without losing
the "Nikon look." This document records the exact check and its current failure.

## Method

Nikon Z f NEFs were exported to TIFF two ways and compared pixel-for-pixel:

1. **NX Studio 1.10.1.3002** — exported as 8-bit, LZW, "Nikon sRGB" profile (the ground truth).
2. **`nef-watch`** — rendered headless through the SDK with
   `DevelopColorMode = AppliedInCamera`, as-shot Picture Control / WB / Active
   D-Lighting, output profile `NKsRGB.icm`, then encoded 8-bit LZW + ICC.

Metrics: mean absolute error (MAE), max per-pixel error, PSNR, per-channel signed
bias, and the spatial/tonal distribution of the difference.

## Historical result (visual parity only)

| Metric | Value |
|--------|-------|
| Mean absolute error | **≈ 1.74 / 255** |
| Worst sample | **7 / 255** |
| PSNR | **41.4 dB** |
| Per-channel bias | ~0 |

The two renders are visually indistinguishable, but they are not the same decoded
image. Earlier versions of this document incorrectly described the residual as
the "8-bit quantization floor" and "pure requantization." Those claims are
retracted. Showing that the SDK's 8-bit output equals a rounded copy of its own
16-bit output says nothing about why the SDK output differs from NX Studio.

Fresh exact comparison finds a real, small delta across 99.56% of pixels in the
current `DSC_0723` baseline. The ICC profile matches, and prior edge/LUT tests
help describe the error, but they do not establish its cause. The measurements
above establish visual similarity only. They do **not** satisfy an exact
decoded-pixel requirement: any non-zero MAE or maximum delta fails a 1:1 gate.

## Exact macOS/Linux/Docker parity gate

`tool/validate_tiffs.py` is the executable honesty gate for exact parity. It is
intentionally red on the current NX Studio-versus-macOS SDK baseline, so it is
not yet a passing release gate. It compares TIFF meaning, not TIFF container
bytes: LZW versus another lossless compression, tag order, unrelated EXIF, and
embedded thumbnails may differ.

For every artifact, the gate requires:

- one unambiguous largest, three-channel RGB raster;
- unsigned 8-bit or 16-bit samples, with identical dimensions and bit depth;
- display-oriented pixels (`Orientation = 1`);
- a structurally valid embedded RGB ICC profile, byte-identical across exports;
- **zero differing decoded samples** (MAE 0, maximum delta 0).

Pass three exports of one NEF with the NX Studio result first (the reference):

```bash
python3 tool/validate_tiffs.py \
  nx=/validation/DSC_0001-nx.tif \
  mac=/validation/DSC_0001-mac-sdk.tif \
  linux=/validation/DSC_0001-linux-sdk.tif
```

For a corpus, pass directories. TIFFs are matched recursively by relative path,
case-insensitively, and missing or extra files fail the gate:

```bash
python3 tool/validate_tiffs.py \
  nx=/validation/nx \
  mac=/validation/mac \
  linux=/validation/linux
```

Use `--json` for a machine-readable report. Exit status `0` means exact parity,
`1` means a contract or pixel mismatch, and `2` means an artifact could not be
opened or decoded. Install `tifffile` and `imagecodecs` from `requirements.txt`;
the latter decodes the LZW TIFFs produced by NX Studio and `nef-watch`.

The gate proves equality of the exported TIFFs, not that they came from the same
NEF. `validation/baseline-v1.json` records each source NEF SHA-256 plus the
canonical decoded-pixel and ICC hashes for every NX/macOS/Linux artifact. Verify
that provenance and then run the exact equality gate in one command:

```bash
python3 tool/verify_validation_baseline.py \
  validation/baseline-v1.json \
  --source-dir /validation/nefs \
  --docker-image-id sha256:IMAGE_ID_FROM_DOCKER_INSPECT \
  --docker-source-fingerprint SOURCE_LABEL_FROM_DOCKER_INSPECT \
  nx=/validation/nx \
  mac=/validation/mac \
  linux=/validation/linux
```

Use `--json` to retain a machine-readable audit report. The current manifest is
an evidence snapshot, not a passing golden master: provenance passes for the
recorded files, but the command exits `1` because decoded RGB equality is still
false. A replacement baseline is valid only after all three exports are made
from the recorded source hashes and the strict gate reaches zero differences.

### Current six-image baseline

On 2026-07-13, `DSC_0722` through `DSC_0727` were rendered through the native
macOS SDK and the SDK-free Linux/amd64 Wine wrapper, then compared with their NX
Studio references. Each artifact was 4032×6048 (width×height) RGB uint8 with
Orientation 1 and
the same Nikon sRGB ICC SHA-256
`49caea94c9d36322910350ee37f1fa09629bed70e01cf615a6863ad3f8d1475e`.
The recorded Linux artifacts were reproduced exactly by the public-wrapper
candidate image
`sha256:85e3aa93db238068c75cb3a4b0891ab5af17adaa325077c3967827832ec08b80`
(source fingerprint `ab44e257fd0707f16e705743022ac928a5113302da226cd2ffa1d4c4bc380d3f`).
Its image layers contain no Nikon SDK files. A fresh schema-v2 state volume
validated a read-only SDK mount, compiled from a re-hashed private snapshot, and
rendered all six NEFs under the production container restrictions. All
`438,939,648` decoded samples and every ICC profile were exactly equal to the
earlier Linux baseline (mean and maximum delta `0`). A restart reused that
private state without the SDK mount and passed the container healthcheck.

| Aggregate over six NEFs | macOS SDK vs NX | Docker/Wine SDK vs NX | Docker/Wine vs macOS SDK |
|-------------------------|----------------:|-----------------------:|---------------------------:|
| Mean absolute delta | 1.739329312 / 255 | 1.741117736 / 255 | 0.103505756 / 255 |
| Maximum absolute delta | 7 / 255 | 24 / 255 | 27 / 255 |
| PSNR | 41.375628 dB | 41.369703 dB | 57.936140 dB |
| Exact samples | 17.692466460% | 17.666644914% | 89.697809208% |
| Exact RGB pixels | 0.437670648% | 0.437764282% | 73.597499217% |
| ICC profile | exact | exact | exact |

The maximum Docker deltas are rare outliers; the aggregate MAE better describes
the overall distance. They still count as failures under an exact criterion.
The first two columns are reproducible from the verifier's JSON `aggregates`;
the third comes from a second validator run with the macOS directory first and
the Linux directory second.
Normal and `--deterministic` macOS exports were byte-identical on all six files,
and five Docker renders of `DSC_0723` were byte-identical, so the observed
cross-runtime delta is reproducible rather than run-to-run noise.

This is an intentional failing baseline: matching Linux/Docker to the current
macOS SDK output is necessary but is not sufficient. Both cross-runtime deltas
and the NX Studio-to-SDK delta must reach zero for the stated 1:1 exit criterion.

### Controlled `DSC_0723` experiments

Repeated NX Studio exports of the same `DSC_0723.NEF` are not equal even though
they have the same dimensions, orientation, bit depth, and byte-identical Nikon
sRGB profile. One earlier pair differs at `60,979,500` of `73,156,608` samples
(MAE `2.279305801 / 255`, maximum `11`). A fresh user export on 2026-07-13 differs
from the baseline NX TIFF at `61,809,307` samples (MAE `2.310341794 / 255`,
maximum `11`), while its macOS-SDK distance remains MAE `1.740561728`, maximum
`7`. Per-channel bias is effectively zero. A particular NX Studio TIFF therefore
cannot currently serve as a deterministic exact-pixel target for a fresh export
of that same NEF.

The official Nikon sample requests native 8-bit output, while `nef-watch` asks
the SDK for 16-bit data and rounds it to 8-bit. A controlled native-macOS matrix
tested both paths plus rendering intent, raw quality, and color-process mode:

| Variant vs first NX export | MAE / 255 | Maximum |
|----------------------------|----------:|--------:|
| Current SDK 16-bit, rounded to 8-bit | 1.740660953 | 7 |
| SDK native 8-bit | 2.296727084 | 12 |

- relative and perceptual intent produced identical SDK pixels; the SDK read
  both requests back as perceptual;
- default raw quality was already high, and setting high explicitly made no
  pixel change;
- `AppliedInCamera` and `Latest` changed the getter value but not the pixels for
  this NEF;
- five native-8-bit SDK processes produced an identical pixel payload, including
  with the macOS RNG interposer enabled.

Native SDK 8-bit and both NX exports have nearly identical channel-wise noise
means, standard deviations, and ranges relative to the same SDK 16-bit render,
but their spatial noise correlation is only about `0.05`–`0.10`. The evidence
therefore localizes the remaining NX mismatch to different realizations of an
8-bit-specific noise/dither stage, rather than to a different ICC profile,
rendering intent, raw-quality setting, or color-process setting. This is a
diagnostic result, not a waiver: the exact gate remains failed.

Exporting the SDK paths at 16-bit does not provide a four-way escape hatch.
For `DSC_0723`, native macOS SDK 16-bit versus final Docker SDK 16-bit differs
at `68,230,334` of `73,156,608` samples (`93.266125734%`), with MAE
`25.541960666 / 65535` and maximum delta `2799`. That is the same smaller
cross-runtime render difference exposed at higher precision, so eliminating
the NX 8-bit output stage cannot make macOS SDK equal Linux/Wine SDK.

Run the validator's regression suite with:

```bash
python3 -m unittest discover -s tests -v
```

## Takeaway

The current macOS SDK render is visually very close to NX Studio, but it is not
pixel-identical. The controlled matrix localizes the dominant NX-versus-SDK
difference to the 8-bit noise/dither realization, while a smaller reproducible
Mac-versus-Wine SDK delta also remains. Exact Linux parity requires both Linux
SDK = macOS SDK and macOS SDK = NX Studio; satisfying only visual similarity or
only one equality does not meet the exit criterion.

> The "Applied in Camera" color mode plus the `NKsRGB.icm` output profile are the
> two pieces that matter. The SDK's call order also matters: applying
> `RawParameterSet = AsShot` resets session state, so it must run *before* the
> output profile is set, or the SDK silently reverts to the display profile.
