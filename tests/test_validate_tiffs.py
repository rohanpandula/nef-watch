from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import numpy as np
import tifffile

from tool import validate_tiffs


def rgb_icc(marker: int = 0) -> bytes:
    """Small structurally valid RGB ICC payload for TIFF contract tests."""
    profile = bytearray(132)
    profile[0:4] = len(profile).to_bytes(4, "big")
    profile[12:16] = b"mntr"
    profile[16:20] = b"RGB "
    profile[20:24] = b"XYZ "
    profile[36:40] = b"acsp"
    profile[-1] = marker
    return bytes(profile)


def write_rgb(
    path: Path,
    pixels: np.ndarray,
    *,
    icc: bytes | None = None,
    compression: str | None = None,
    planar: bool = False,
    orientation: int = 1,
    description: str | None = None,
) -> None:
    data = np.moveaxis(pixels, 2, 0) if planar else pixels
    tags = [(274, "H", 1, orientation, False)]
    if icc is not None:
        tags.append((34675, 7, len(icc), icc, False))
    tifffile.imwrite(
        path,
        data,
        photometric="rgb",
        planarconfig="separate" if planar else "contig",
        compression=compression,
        metadata=None,
        description=description,
        extratags=tags,
    )


class ValidateTiffsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.pixels = np.arange(6 * 7 * 3, dtype=np.uint8).reshape(6, 7, 3)
        self.icc = rgb_icc()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def specs(self, *values: str) -> list[validate_tiffs.ArtifactSpec]:
        return validate_tiffs.parse_specs(values)

    def test_exact_pixels_pass_despite_storage_and_metadata_differences(self) -> None:
        reference = self.root / "nx.tif"
        candidate = self.root / "linux.tif"
        write_rgb(
            reference,
            self.pixels,
            icc=self.icc,
            compression=None,
            description="NX metadata",
        )
        write_rgb(
            candidate,
            self.pixels,
            icc=self.icc,
            compression="deflate",
            planar=True,
            description="unrelated Docker metadata",
        )

        report = validate_tiffs.validate(
            self.specs(f"nx={reference}", f"linux={candidate}")
        )

        self.assertTrue(report["passed"])
        comparison = report["comparisons"][0]
        self.assertTrue(comparison["pixel_comparison"]["equal"])
        self.assertEqual(comparison["pixel_comparison"]["different_samples"], 0)
        self.assertEqual(
            report["artifacts"][0]["pixel_sha256"],
            report["artifacts"][1]["pixel_sha256"],
        )
        self.assertEqual(report["artifacts"][1]["source_axes"], "SYX")

    def test_one_changed_sample_fails_with_exact_diagnostics(self) -> None:
        reference = self.root / "nx.tif"
        candidate = self.root / "linux.tif"
        changed = self.pixels.copy()
        changed[2, 4, 1] += 2
        write_rgb(reference, self.pixels, icc=self.icc)
        write_rgb(candidate, changed, icc=self.icc)

        report = validate_tiffs.validate(
            self.specs(f"nx={reference}", f"linux={candidate}")
        )

        self.assertFalse(report["passed"])
        stats = report["comparisons"][0]["pixel_comparison"]
        self.assertEqual(stats["different_samples"], 1)
        self.assertEqual(stats["different_pixels"], 1)
        self.assertEqual(stats["max_absolute_delta"], 2)
        self.assertEqual(stats["mean_absolute_delta"], 2 / self.pixels.size)
        self.assertEqual(stats["mean_squared_delta"], 4 / self.pixels.size)
        self.assertEqual(
            stats["first_mismatch"],
            {
                "x": 4,
                "y": 2,
                "channel": "G",
                "reference": int(self.pixels[2, 4, 1]),
                "candidate": int(changed[2, 4, 1]),
                "signed_delta": 2,
            },
        )
        aggregate = report["aggregates"]["linux_vs_nx"]
        self.assertEqual(aggregate["image_count"], 1)
        self.assertEqual(aggregate["different_samples"], 1)
        self.assertEqual(aggregate["max_absolute_delta"], 2)
        self.assertFalse(aggregate["exact_all"])

    def test_identical_samples_with_different_icc_fail_color_gate(self) -> None:
        reference = self.root / "nx.tif"
        candidate = self.root / "linux.tif"
        write_rgb(reference, self.pixels, icc=rgb_icc(1))
        write_rgb(candidate, self.pixels, icc=rgb_icc(2))

        report = validate_tiffs.validate(
            self.specs(f"nx={reference}", f"linux={candidate}")
        )

        self.assertFalse(report["passed"])
        comparison = report["comparisons"][0]
        self.assertTrue(comparison["pixel_comparison"]["equal"])
        self.assertIn(
            "embedded ICC profile differs", comparison["metadata_mismatches"][0]
        )

    def test_missing_icc_and_non_normal_orientation_fail_contract(self) -> None:
        reference = self.root / "nx.tif"
        candidate = self.root / "linux.tif"
        write_rgb(reference, self.pixels, icc=self.icc)
        write_rgb(candidate, self.pixels, orientation=6)

        report = validate_tiffs.validate(
            self.specs(f"nx={reference}", f"linux={candidate}")
        )

        self.assertFalse(report["passed"])
        errors = report["comparisons"][0]["contract_errors"]
        self.assertTrue(any("Orientation is 6" in error for error in errors))
        self.assertTrue(
            any("ICC profile" in error and "missing" in error for error in errors)
        )

    def test_largest_rgb_series_is_selected_and_thumbnail_ignored(self) -> None:
        reference = self.root / "nx.tif"
        candidate = self.root / "linux.tif"
        thumbnail_a = np.zeros((2, 3, 3), dtype=np.uint8)
        thumbnail_b = np.full((2, 3, 3), 255, dtype=np.uint8)
        with tifffile.TiffWriter(reference) as writer:
            writer.write(
                self.pixels,
                photometric="rgb",
                extratags=[(34675, 7, len(self.icc), self.icc, False)],
            )
            writer.write(thumbnail_a, photometric="rgb")
        with tifffile.TiffWriter(candidate) as writer:
            writer.write(thumbnail_b, photometric="rgb")
            writer.write(
                self.pixels,
                photometric="rgb",
                extratags=[(34675, 7, len(self.icc), self.icc, False)],
            )

        report = validate_tiffs.validate(
            self.specs(f"nx={reference}", f"linux={candidate}")
        )

        self.assertTrue(report["passed"])
        self.assertEqual(report["artifacts"][0]["primary_series"], 0)
        self.assertEqual(report["artifacts"][1]["primary_series"], 1)
        self.assertEqual(report["artifacts"][0]["ignored_series"], 1)

    def test_strict_acceptance_rejects_extra_series_and_unapproved_metadata(self) -> None:
        candidate = self.root / "candidate.tif"
        thumbnail = np.zeros((2, 3, 3), dtype=np.uint8)
        with tifffile.TiffWriter(candidate) as writer:
            writer.write(
                self.pixels,
                photometric="rgb",
                compression="lzw",
                metadata=None,
                description="covert metadata",
                extratags=[
                    (274, "H", 1, 1, False),
                    (34675, 7, len(self.icc), self.icc, False),
                ],
            )
            writer.write(thumbnail, photometric="rgb", metadata=None)

        raster = validate_tiffs.inspect_tiff(
            candidate, "candidate", strict_acceptance=True
        )

        self.assertTrue(any("exactly one image series" in item for item in raster.contract_errors))
        self.assertTrue(any("unapproved top-level tags" in item for item in raster.contract_errors))

    def test_strict_acceptance_rejects_unreferenced_trailing_payload(self) -> None:
        candidate = self.root / "candidate.tif"
        write_rgb(candidate, self.pixels, icc=self.icc, compression="lzw")
        with candidate.open("ab") as stream:
            stream.write(b"private trailing payload")

        raster = validate_tiffs.inspect_tiff(
            candidate, "candidate", strict_acceptance=True
        )

        self.assertTrue(any("trailing payload" in item for item in raster.contract_errors))

    def test_directory_mode_requires_same_relative_tiff_set(self) -> None:
        reference = self.root / "nx"
        candidate = self.root / "linux"
        (reference / "nested").mkdir(parents=True)
        (candidate / "nested").mkdir(parents=True)
        write_rgb(reference / "nested" / "A.TIF", self.pixels, icc=self.icc)
        write_rgb(candidate / "nested" / "a.tif", self.pixels, icc=self.icc)
        write_rgb(candidate / "extra.tif", self.pixels, icc=self.icc)

        report = validate_tiffs.validate(
            self.specs(f"nx={reference}", f"linux={candidate}")
        )

        self.assertFalse(report["passed"])
        self.assertEqual(
            report["file_set_mismatches"],
            [{"candidate": "linux", "missing": [], "extra": ["extra.tif"]}],
        )
        self.assertTrue(report["comparisons"][0]["passed"])

    def test_json_cli_exit_codes_distinguish_mismatch_and_decode_error(self) -> None:
        reference = self.root / "nx.tif"
        candidate = self.root / "linux.tif"
        changed = self.pixels.copy()
        changed[0, 0, 0] += 1
        write_rgb(reference, self.pixels, icc=self.icc)
        write_rgb(candidate, changed, icc=self.icc)

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            mismatch_code = validate_tiffs.main(
                ["--json", f"nx={reference}", f"linux={candidate}"]
            )
        self.assertEqual(mismatch_code, 1)
        self.assertFalse(json.loads(stdout.getvalue())["passed"])

        broken = self.root / "broken.tif"
        broken.write_bytes(b"not a TIFF")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            error_code = validate_tiffs.main(
                ["--json", f"nx={reference}", f"broken={broken}"]
            )
        self.assertEqual(error_code, 2)
        self.assertIn("operational_error", json.loads(stdout.getvalue()))

    def test_uint16_exact_values_are_supported(self) -> None:
        reference = self.root / "nx16.tif"
        candidate = self.root / "linux16.tif"
        pixels = (self.pixels.astype(np.uint16) * 257).astype(np.uint16)
        write_rgb(reference, pixels, icc=self.icc)
        write_rgb(candidate, pixels, icc=self.icc, compression="deflate")

        report = validate_tiffs.validate(
            self.specs(f"nx={reference}", f"linux={candidate}")
        )

        self.assertTrue(report["passed"])
        self.assertEqual(report["artifacts"][0]["dtype"], "uint16")
        self.assertEqual(report["artifacts"][0]["bits_per_sample"], [16, 16, 16])

    def test_declared_raster_size_is_bounded_before_decode(self) -> None:
        class HostileSeries:
            axes = "YXS"
            shape = (100_001, 1_000, 3)
            dtype = np.dtype("uint8")

        with self.assertRaisesRegex(validate_tiffs.OperationalError, "pixels"):
            validate_tiffs._validate_series_bounds(
                HostileSeries(), self.root / "hostile.tif"
            )


if __name__ == "__main__":
    unittest.main()
