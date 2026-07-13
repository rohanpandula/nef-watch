#!/usr/bin/env python3
"""Exact decoded-TIFF color-equivalence gate.

The TIFF container is deliberately not compared byte-for-byte: compression,
metadata ordering, and embedded thumbnails may differ without changing the
rendered image.  The primary RGB raster and its color interpretation are.

Exit codes:
  0  every candidate is exactly equivalent to the reference
  1  a decoded pixel, raster contract, ICC profile, or corpus file-set differs
  2  an input cannot be opened/decoded or the command is invalid
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence, Tuple

import numpy as np

try:
    import tifffile
except ModuleNotFoundError:  # pragma: no cover - exercised by the CLI environment
    tifffile = None


TIFF_SUFFIXES = {".tif", ".tiff"}
LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
CHANNEL_NAMES = ("R", "G", "B")
ROW_CHUNK = 256


class OperationalError(RuntimeError):
    """An input could not be inspected, so equivalence is unknown."""


@dataclass(frozen=True)
class ArtifactSpec:
    label: str
    path: Path


@dataclass
class TiffRaster:
    label: str
    path: Path
    pixels: Optional[np.ndarray]
    series_index: int
    ignored_series: int
    source_axes: str
    photometric: str
    orientation: int
    bits_per_sample: Tuple[int, ...]
    dtype: str
    icc_profile: Optional[bytes]
    pixel_sha256: Optional[str]
    contract_errors: list[str]

    def summary(self) -> dict[str, Any]:
        shape = list(self.pixels.shape) if self.pixels is not None else None
        return {
            "label": self.label,
            "path": str(self.path),
            "primary_series": self.series_index,
            "ignored_series": self.ignored_series,
            "source_axes": self.source_axes,
            "shape_y_x_rgb": shape,
            "dtype": self.dtype,
            "bits_per_sample": list(self.bits_per_sample),
            "photometric": self.photometric,
            "orientation": self.orientation,
            "pixel_sha256": self.pixel_sha256,
            "icc_sha256": _sha256(self.icc_profile),
            "contract_errors": self.contract_errors,
        }


def _sha256(data: Optional[bytes]) -> Optional[str]:
    if data is None:
        return None
    return hashlib.sha256(data).hexdigest()


def _tag_value(page: Any, code: int, default: Any) -> Any:
    tag = page.tags.get(code)
    return default if tag is None else tag.value


def _enum_name(value: Any) -> str:
    return str(getattr(value, "name", value)).upper()


def _axis_order(axes: str, ndim: int) -> Optional[tuple[int, int, int]]:
    """Return source indices for Y, X, samples, accepting YXS and SYX TIFFs."""
    if len(axes) != ndim or ndim != 3:
        return None
    sample_axes = [axis for axis in ("S", "C") if axes.count(axis) == 1]
    if axes.count("Y") != 1 or axes.count("X") != 1 or len(sample_axes) != 1:
        return None
    sample_axis = sample_axes[0]
    if set(axes) != {"Y", "X", sample_axis}:
        return None
    return axes.index("Y"), axes.index("X"), axes.index(sample_axis)


def _series_area(series: Any) -> Optional[int]:
    order = _axis_order(str(series.axes), len(series.shape))
    if order is None:
        return None
    y_axis, x_axis, sample_axis = order
    if int(series.shape[sample_axis]) != 3:
        return None
    return int(series.shape[y_axis]) * int(series.shape[x_axis])


def _choose_primary_series(tf: Any) -> tuple[int, list[str]]:
    """Select the largest three-sample raster, ignoring NX's thumbnail series."""
    candidates = []
    for index, series in enumerate(tf.series):
        area = _series_area(series)
        if area is not None:
            candidates.append((area, index))
    if not candidates:
        return 0, ["TIFF has no three-sample Y/X raster series"]
    candidates.sort(reverse=True)
    largest_area = candidates[0][0]
    largest = [index for area, index in candidates if area == largest_area]
    errors = []
    if len(largest) != 1:
        errors.append(
            "TIFF has multiple equally large RGB raster series; primary image is ambiguous"
        )
    return min(largest), errors


def _normalise_bits(value: Any, samples: int) -> tuple[int, ...]:
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        bits = tuple(int(item) for item in value)
        return bits
    return (int(value),) * max(samples, 1)


def _validate_icc(profile: Optional[bytes]) -> list[str]:
    if profile is None:
        return ["embedded ICC profile (TIFF tag 34675) is missing"]
    errors = []
    if len(profile) < 128:
        return [f"embedded ICC profile is truncated ({len(profile)} bytes)"]
    declared_size = int.from_bytes(profile[:4], "big")
    if declared_size != len(profile):
        errors.append(
            f"ICC header declares {declared_size} bytes but tag contains {len(profile)}"
        )
    if profile[36:40] != b"acsp":
        errors.append("embedded ICC profile has no 'acsp' signature")
    if profile[16:20] != b"RGB ":
        found = profile[16:20].decode("ascii", "replace")
        errors.append(f"embedded ICC profile color space is {found!r}, not RGB")
    return errors


def _canonical_pixel_hash(pixels: np.ndarray) -> str:
    """Hash decoded values in Y/X/RGB order with encoding-neutral endianness."""
    digest = hashlib.sha256()
    canonical_dtype = np.dtype(f"<u{pixels.dtype.itemsize}")
    for y0 in range(0, pixels.shape[0], ROW_CHUNK):
        block = np.ascontiguousarray(pixels[y0 : y0 + ROW_CHUNK], dtype=canonical_dtype)
        digest.update(memoryview(block).cast("B"))
    return digest.hexdigest()


def inspect_tiff(path: Path, label: str) -> TiffRaster:
    if tifffile is None:
        raise OperationalError(
            "tifffile is required; install requirements.txt or `pip install tifffile imagecodecs`"
        )
    try:
        with tifffile.TiffFile(path) as tf:
            if not tf.series:
                raise OperationalError(f"{path}: TIFF contains no image series")
            series_index, contract_errors = _choose_primary_series(tf)
            series = tf.series[series_index]
            if not series.pages:
                raise OperationalError(f"{path}: primary TIFF series contains no pages")
            page = series.pages[0]
            axes = str(series.axes)
            try:
                raw = np.asarray(series.asarray())
            except Exception as exc:
                raise OperationalError(
                    f"{path}: cannot decode primary raster: {exc}"
                ) from exc

            order = _axis_order(axes, raw.ndim)
            pixels: Optional[np.ndarray]
            if order is None:
                contract_errors.append(
                    f"primary raster axes {axes!r} and shape {tuple(raw.shape)} are not RGB Y/X/samples"
                )
                pixels = None
            else:
                pixels = np.transpose(raw, order)
                if pixels.shape[2] != 3:
                    contract_errors.append(
                        f"primary raster has {pixels.shape[2]} samples per pixel, expected RGB (3)"
                    )
                    pixels = None

            photometric = _enum_name(getattr(page, "photometric", "UNKNOWN"))
            orientation = int(_tag_value(page, 274, 1))
            samples = int(getattr(page, "samplesperpixel", 0) or 0)
            bits = _normalise_bits(getattr(page, "bitspersample", 0), samples)
            dtype = np.dtype(raw.dtype).name
            icc_value = _tag_value(page, 34675, None)
            icc_profile = bytes(icc_value) if icc_value is not None else None

            if len(series.pages) != 1:
                contract_errors.append(
                    f"primary raster spans {len(series.pages)} pages; expected one rendered image"
                )
            if photometric != "RGB":
                contract_errors.append(
                    f"PhotometricInterpretation is {photometric}, expected RGB"
                )
            if samples != 3:
                contract_errors.append(
                    f"SamplesPerPixel is {samples}, expected RGB (3)"
                )
            extrasamples = tuple(getattr(page, "extrasamples", ()) or ())
            if extrasamples:
                contract_errors.append(
                    f"primary raster has extra samples ({', '.join(map(str, extrasamples))})"
                )
            if orientation != 1:
                contract_errors.append(
                    f"Orientation is {orientation}, expected 1 (pixels must already be display-oriented)"
                )
            if raw.dtype.kind != "u" or raw.dtype.itemsize not in (1, 2):
                contract_errors.append(
                    f"sample dtype is {dtype}, expected unsigned 8-bit or 16-bit integer"
                )
            expected_bits = raw.dtype.itemsize * 8
            if bits != (expected_bits,) * 3:
                contract_errors.append(
                    f"BitsPerSample is {bits}, expected {(expected_bits,) * 3} for {dtype}"
                )
            contract_errors.extend(_validate_icc(icc_profile))

            pixel_hash = None
            if (
                pixels is not None
                and raw.dtype.kind == "u"
                and raw.dtype.itemsize in (1, 2)
            ):
                pixel_hash = _canonical_pixel_hash(pixels)

            return TiffRaster(
                label=label,
                path=path,
                pixels=pixels,
                series_index=series_index,
                ignored_series=max(len(tf.series) - 1, 0),
                source_axes=axes,
                photometric=photometric,
                orientation=orientation,
                bits_per_sample=bits,
                dtype=dtype,
                icc_profile=icc_profile,
                pixel_sha256=pixel_hash,
                contract_errors=contract_errors,
            )
    except OperationalError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise OperationalError(f"{path}: cannot open as TIFF: {exc}") from exc


def _pixel_stats(reference: np.ndarray, candidate: np.ndarray) -> dict[str, Any]:
    different_samples = 0
    different_pixels = 0
    absolute_sum = 0
    squared_sum = 0
    signed_sum = np.zeros(3, dtype=np.int64)
    channel_absolute_sum = np.zeros(3, dtype=np.int64)
    channel_max = np.zeros(3, dtype=np.int64)
    max_absolute_delta = 0
    first_mismatch = None

    for y0 in range(0, reference.shape[0], ROW_CHUNK):
        ref = reference[y0 : y0 + ROW_CHUNK]
        cand = candidate[y0 : y0 + ROW_CHUNK]
        unequal = ref != cand
        count = int(np.count_nonzero(unequal))
        if count == 0:
            continue
        different_samples += count
        different_pixels += int(np.count_nonzero(np.any(unequal, axis=2)))
        delta = cand.astype(np.int64) - ref.astype(np.int64)
        absolute = np.abs(delta)
        absolute_sum += int(absolute.sum(dtype=np.int64))
        squared_sum += int(np.square(delta, dtype=np.int64).sum(dtype=np.int64))
        signed_sum += delta.sum(axis=(0, 1), dtype=np.int64)
        channel_absolute_sum += absolute.sum(axis=(0, 1), dtype=np.int64)
        channel_max = np.maximum(channel_max, absolute.max(axis=(0, 1)))
        max_absolute_delta = max(max_absolute_delta, int(absolute.max()))
        if first_mismatch is None:
            y, x, channel = (int(value) for value in np.argwhere(unequal)[0])
            first_mismatch = {
                "x": x,
                "y": y0 + y,
                "channel": CHANNEL_NAMES[channel],
                "reference": int(ref[y, x, channel]),
                "candidate": int(cand[y, x, channel]),
                "signed_delta": int(delta[y, x, channel]),
            }

    total_samples = int(reference.size)
    total_pixels = int(reference.shape[0] * reference.shape[1])
    channel_samples = total_pixels
    mean_squared_delta = squared_sum / total_samples
    peak_value = int(np.iinfo(reference.dtype).max)
    return {
        "equal": different_samples == 0,
        "different_samples": different_samples,
        "total_samples": total_samples,
        "different_sample_fraction": different_samples / total_samples,
        "different_pixels": different_pixels,
        "total_pixels": total_pixels,
        "different_pixel_fraction": different_pixels / total_pixels,
        "mean_absolute_delta": absolute_sum / total_samples,
        "absolute_delta_sum": absolute_sum,
        "mean_squared_delta": mean_squared_delta,
        "squared_delta_sum": squared_sum,
        "psnr_db": (
            None
            if mean_squared_delta == 0
            else 10.0 * math.log10((peak_value * peak_value) / mean_squared_delta)
        ),
        "peak_value": peak_value,
        "max_absolute_delta": max_absolute_delta,
        "per_channel_mean_absolute_delta": {
            CHANNEL_NAMES[index]: int(channel_absolute_sum[index]) / channel_samples
            for index in range(3)
        },
        "per_channel_signed_bias": {
            CHANNEL_NAMES[index]: int(signed_sum[index]) / channel_samples
            for index in range(3)
        },
        "per_channel_max_absolute_delta": {
            CHANNEL_NAMES[index]: int(channel_max[index]) for index in range(3)
        },
        "first_mismatch": first_mismatch,
    }


def compare_rasters(reference: TiffRaster, candidate: TiffRaster) -> dict[str, Any]:
    mismatches = []
    ref_shape = tuple(reference.pixels.shape) if reference.pixels is not None else None
    cand_shape = tuple(candidate.pixels.shape) if candidate.pixels is not None else None
    if ref_shape != cand_shape:
        mismatches.append(f"decoded shape differs: {ref_shape} != {cand_shape}")
    if reference.dtype != candidate.dtype:
        mismatches.append(
            f"decoded dtype differs: {reference.dtype} != {candidate.dtype}"
        )
    if reference.bits_per_sample != candidate.bits_per_sample:
        mismatches.append(
            f"BitsPerSample differs: {reference.bits_per_sample} != {candidate.bits_per_sample}"
        )
    if reference.icc_profile != candidate.icc_profile:
        mismatches.append(
            "embedded ICC profile differs: "
            f"{_sha256(reference.icc_profile)} != {_sha256(candidate.icc_profile)}"
        )

    stats = None
    if (
        reference.pixels is not None
        and candidate.pixels is not None
        and ref_shape == cand_shape
        and reference.dtype == candidate.dtype
    ):
        stats = _pixel_stats(reference.pixels, candidate.pixels)

    contract_errors = [
        *(f"reference: {message}" for message in reference.contract_errors),
        *(f"candidate: {message}" for message in candidate.contract_errors),
    ]
    passed = (
        not contract_errors
        and not mismatches
        and stats is not None
        and bool(stats["equal"])
    )
    return {
        "reference": reference.label,
        "reference_path": str(reference.path),
        "candidate": candidate.label,
        "candidate_path": str(candidate.path),
        "passed": passed,
        "contract_errors": contract_errors,
        "metadata_mismatches": mismatches,
        "pixel_comparison": stats,
    }


def _parse_spec(value: str, index: int) -> ArtifactSpec:
    label = ""
    path_text = value
    if "=" in value:
        possible_label, possible_path = value.split("=", 1)
        if LABEL_RE.fullmatch(possible_label):
            label, path_text = possible_label, possible_path
    path = Path(path_text).expanduser().resolve()
    if not label:
        label = "reference" if index == 0 else (path.stem or f"candidate-{index}")
    return ArtifactSpec(label=label, path=path)


def parse_specs(values: Sequence[str]) -> list[ArtifactSpec]:
    specs = [_parse_spec(value, index) for index, value in enumerate(values)]
    labels = [spec.label for spec in specs]
    if len(labels) != len(set(labels)):
        duplicates = sorted({label for label in labels if labels.count(label) > 1})
        raise OperationalError(
            f"artifact labels must be unique: {', '.join(duplicates)}"
        )
    for spec in specs:
        if not spec.path.exists():
            raise OperationalError(f"{spec.label}: path does not exist: {spec.path}")
    kinds = {"directory" if spec.path.is_dir() else "file" for spec in specs}
    if len(kinds) != 1:
        raise OperationalError(
            "artifacts must be either all TIFF files or all directories"
        )
    if kinds == {"file"}:
        for spec in specs:
            if spec.path.suffix.casefold() not in TIFF_SUFFIXES:
                raise OperationalError(
                    f"{spec.label}: not a .tif/.tiff file: {spec.path}"
                )
    return specs


def _directory_tiffs(root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.casefold() not in TIFF_SUFFIXES:
            continue
        relative = path.relative_to(root).as_posix()
        key = relative.casefold()
        if key in found:
            raise OperationalError(
                f"{root}: TIFF names collide case-insensitively: "
                f"{found[key].relative_to(root)} and {relative}"
            )
        found[key] = path
    return found


def _base_report(mode: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "criterion": "exact-decoded-rgb-with-identical-icc",
        "mode": mode,
        "passed": True,
        "artifacts": [],
        "file_set_mismatches": [],
        "comparisons": [],
        "aggregates": {},
    }


def _label_group(label: str) -> str:
    return label.split(":", 1)[0]


def _add_aggregates(report: dict[str, Any]) -> None:
    accumulators: dict[str, dict[str, Any]] = {}
    for comparison in report["comparisons"]:
        stats = comparison["pixel_comparison"]
        if stats is None:
            continue
        reference = _label_group(comparison["reference"])
        candidate = _label_group(comparison["candidate"])
        key = f"{candidate}_vs_{reference}"
        acc = accumulators.setdefault(
            key,
            {
                "reference": reference,
                "candidate": candidate,
                "image_count": 0,
                "total_samples": 0,
                "different_samples": 0,
                "total_pixels": 0,
                "different_pixels": 0,
                "absolute_delta_sum": 0,
                "squared_delta_sum": 0,
                "max_absolute_delta": 0,
                "peak_value": stats["peak_value"],
                "contract_match_all": True,
                "icc_match_all": True,
                "exact_all": True,
            },
        )
        if acc["peak_value"] != stats["peak_value"]:
            raise OperationalError(
                f"cannot aggregate mixed sample ranges for {candidate} vs {reference}"
            )
        acc["image_count"] += 1
        for field in (
            "total_samples",
            "different_samples",
            "total_pixels",
            "different_pixels",
            "absolute_delta_sum",
            "squared_delta_sum",
        ):
            acc[field] += int(stats[field])
        acc["max_absolute_delta"] = max(
            acc["max_absolute_delta"], int(stats["max_absolute_delta"])
        )
        acc["contract_match_all"] = (
            acc["contract_match_all"] and not comparison["contract_errors"]
        )
        acc["icc_match_all"] = acc["icc_match_all"] and not any(
            "ICC profile differs" in message
            for message in comparison["metadata_mismatches"]
        )
        acc["exact_all"] = acc["exact_all"] and bool(stats["equal"])

    aggregates = {}
    for key, acc in accumulators.items():
        total_samples = acc["total_samples"]
        total_pixels = acc["total_pixels"]
        mse = acc["squared_delta_sum"] / total_samples
        exact_samples = total_samples - acc["different_samples"]
        exact_pixels = total_pixels - acc["different_pixels"]
        aggregates[key] = {
            "reference": acc["reference"],
            "candidate": acc["candidate"],
            "image_count": acc["image_count"],
            "total_samples": total_samples,
            "different_samples": acc["different_samples"],
            "exact_samples": exact_samples,
            "exact_sample_percent": exact_samples / total_samples * 100.0,
            "total_pixels": total_pixels,
            "different_pixels": acc["different_pixels"],
            "exact_pixels": exact_pixels,
            "exact_pixel_percent": exact_pixels / total_pixels * 100.0,
            "mean_absolute_delta": acc["absolute_delta_sum"] / total_samples,
            "mean_squared_delta": mse,
            "max_absolute_delta": acc["max_absolute_delta"],
            "psnr_db": (
                None
                if mse == 0
                else 10.0
                * math.log10((acc["peak_value"] * acc["peak_value"]) / mse)
            ),
            "contract_match_all": acc["contract_match_all"],
            "icc_match_all": acc["icc_match_all"],
            "exact_all": acc["exact_all"],
        }
    report["aggregates"] = aggregates


def validate_files(specs: Sequence[ArtifactSpec]) -> dict[str, Any]:
    report = _base_report("files")
    reference = inspect_tiff(specs[0].path, specs[0].label)
    report["artifacts"].append(reference.summary())
    try:
        for spec in specs[1:]:
            candidate = inspect_tiff(spec.path, spec.label)
            report["artifacts"].append(candidate.summary())
            comparison = compare_rasters(reference, candidate)
            report["comparisons"].append(comparison)
            report["passed"] = report["passed"] and comparison["passed"]
            del candidate
    finally:
        del reference
    _add_aggregates(report)
    return report


def validate_directories(specs: Sequence[ArtifactSpec]) -> dict[str, Any]:
    report = _base_report("directories")
    inventories = {spec.label: _directory_tiffs(spec.path) for spec in specs}
    reference_spec = specs[0]
    reference_files = inventories[reference_spec.label]
    if not reference_files:
        raise OperationalError(
            f"{reference_spec.path}: reference directory has no TIFF files"
        )

    reference_keys = set(reference_files)
    for spec in specs[1:]:
        candidate_keys = set(inventories[spec.label])
        missing = sorted(reference_keys - candidate_keys)
        extra = sorted(candidate_keys - reference_keys)
        if missing or extra:
            report["passed"] = False
            report["file_set_mismatches"].append(
                {"candidate": spec.label, "missing": missing, "extra": extra}
            )

    for key in sorted(reference_keys):
        reference_path = reference_files[key]
        relative = reference_path.relative_to(reference_spec.path).as_posix()
        reference = inspect_tiff(reference_path, f"{reference_spec.label}:{relative}")
        report["artifacts"].append(reference.summary())
        try:
            for spec in specs[1:]:
                candidate_path = inventories[spec.label].get(key)
                if candidate_path is None:
                    continue
                candidate = inspect_tiff(candidate_path, f"{spec.label}:{relative}")
                report["artifacts"].append(candidate.summary())
                comparison = compare_rasters(reference, candidate)
                report["comparisons"].append(comparison)
                report["passed"] = report["passed"] and comparison["passed"]
                del candidate
        finally:
            del reference
    _add_aggregates(report)
    return report


def validate(specs: Sequence[ArtifactSpec]) -> dict[str, Any]:
    if specs[0].path.is_dir():
        return validate_directories(specs)
    return validate_files(specs)


def _format_fraction(count: int, total: int) -> str:
    percent = (count / total * 100.0) if total else 0.0
    return f"{count:,}/{total:,} ({percent:.9f}%)"


def print_human(report: dict[str, Any]) -> None:
    status = "PASS" if report["passed"] else "FAIL"
    print(f"1:1 decoded TIFF color gate: {status}")
    print(f"criterion: {report['criterion']}  mode: {report['mode']}")
    for mismatch in report["file_set_mismatches"]:
        print(f"FAIL {mismatch['candidate']}: corpus file set differs")
        if mismatch["missing"]:
            print(f"  missing: {', '.join(mismatch['missing'])}")
        if mismatch["extra"]:
            print(f"  extra: {', '.join(mismatch['extra'])}")
    for comparison in report["comparisons"]:
        marker = "PASS" if comparison["passed"] else "FAIL"
        print(f"{marker} {comparison['candidate']} vs {comparison['reference']}")
        for message in comparison["contract_errors"]:
            print(f"  contract: {message}")
        for message in comparison["metadata_mismatches"]:
            print(f"  metadata: {message}")
        pixels = comparison["pixel_comparison"]
        if pixels is None:
            print("  pixels: not comparable because shape/layout/dtype differs")
        elif pixels["equal"]:
            print(
                f"  pixels: {pixels['total_samples']:,} decoded samples are identical"
            )
        else:
            print(
                "  differing samples: "
                + _format_fraction(pixels["different_samples"], pixels["total_samples"])
            )
            print(
                "  differing pixels:  "
                + _format_fraction(pixels["different_pixels"], pixels["total_pixels"])
            )
            print(
                f"  mean/max absolute delta: {pixels['mean_absolute_delta']:.12g} / "
                f"{pixels['max_absolute_delta']}"
            )
            first = pixels["first_mismatch"]
            if first:
                print(
                    f"  first mismatch: x={first['x']} y={first['y']} "
                    f"{first['channel']} reference={first['reference']} "
                    f"candidate={first['candidate']} delta={first['signed_delta']:+d}"
                )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Require exact decoded RGB sample equality, orientation 1, and an "
            "identical valid embedded RGB ICC profile. The first artifact is the reference."
        )
    )
    parser.add_argument(
        "artifacts",
        nargs="+",
        metavar="[LABEL=]TIFF_OR_DIR",
        help="two or more TIFF files, or two or more directories matched by relative TIFF name",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit a machine-readable report on stdout",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if len(args.artifacts) < 2:
        parser.error("at least two artifacts are required")
    try:
        specs = parse_specs(args.artifacts)
        report = validate(specs)
    except OperationalError as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "criterion": "exact-decoded-rgb-with-identical-icc",
                        "passed": False,
                        "operational_error": str(exc),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_human(report)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
