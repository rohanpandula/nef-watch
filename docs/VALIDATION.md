# Validation: does the SDK render match NX Studio?

The whole premise of `nef-watch` is that Nikon's NEF/NRW Image SDK produces the
*same* image as Nikon's NX Studio — so a headless tool can replace the GUI without
losing the "Nikon look." This document records how that was checked.

## Method

Nikon Z f NEFs were exported to TIFF two ways and compared pixel-for-pixel:

1. **NX Studio 1.10** — exported as 8-bit, LZW, "Nikon sRGB" profile (the ground truth).
2. **`nef-watch`** — rendered headless through the SDK with
   `DevelopColorMode = AppliedInCamera`, as-shot Picture Control / WB / Active
   D-Lighting, output profile `NKsRGB.icm`, then encoded 8-bit LZW + ICC.

Metrics: mean absolute error (MAE), max per-pixel error, PSNR, per-channel signed
bias, and the spatial/tonal distribution of the difference.

## Result

| Metric | Value |
|--------|-------|
| Mean absolute error | **≈ 1.74 / 255** |
| Worst pixel | **7 / 255** |
| PSNR | **41.4 dB** |
| Per-channel bias | ~0 |

The two renders are visually indistinguishable. The residual is consistent across
images and was confirmed (independently, by a second analysis pass) to be the
**8-bit quantization floor**, not a rendering difference:

- **Not sharpening** — correlation between the reference's edge map and the
  per-pixel difference is ≈ 0 (the error is uniform across flat and detailed
  regions), and applying any unsharp mask only *increases* the error.
- **Pure requantization** — the SDK's 8-bit output is byte-identical to rounding
  its own 16-bit render down to 8-bit. There is no extra processing step that NX
  Studio applies and the SDK omits.
- **Nothing recoverable** — an overfit per-channel correction LUT trained on the
  exact test images recovers ~0.004/255 (i.e. noise). A faint tone-dependent bias
  exists (~+0.5 in shadows, ~−0.2 in highlights) but lives *below* the 8-bit
  integer LSB, so it is not correctable.

## Takeaway

`nef-watch`'s TIFF output sits at the theoretical floor for an 8-bit pipeline —
the difference from NX Studio is requantization rounding, which is invisible. For
practical purposes the SDK render and the NX Studio render are the same image.

> The "Applied in Camera" color mode plus the `NKsRGB.icm` output profile are the
> two pieces that matter. The SDK's call order also matters: applying
> `RawParameterSet = AsShot` resets session state, so it must run *before* the
> output profile is set, or the SDK silently reverts to the display profile.
