from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from tool import verify_validation_baseline
from tool import validate_tiffs
from test_validate_tiffs import rgb_icc, write_rgb


IMAGE_ID = "sha256:" + "a" * 64
SOURCE_FINGERPRINT = "b" * 64


class VerifyValidationBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.source_dir = self.root / "source"
        self.source_dir.mkdir()
        self.source = self.source_dir / "sample.NEF"
        self.source.write_bytes(b"same-source-nef")
        self.pixels = np.arange(4 * 5 * 3, dtype=np.uint8).reshape(4, 5, 3)
        self.icc = rgb_icc()
        self.artifact_dirs = {}
        pixel_hash = validate_tiffs._canonical_pixel_hash(self.pixels)
        icc_hash = hashlib.sha256(self.icc).hexdigest()
        artifacts = {}
        for label in ("nx", "mac", "linux"):
            directory = self.root / label
            directory.mkdir()
            write_rgb(directory / "sample.TIF", self.pixels, icc=self.icc)
            self.artifact_dirs[label] = directory
            artifacts[label] = {
                "pixel_sha256": pixel_hash,
                "icc_sha256": icc_hash,
            }
        manifest = {
            "schema_version": 1,
            "artifact_labels": ["nx", "mac", "linux"],
            "engines": {
                "docker_image_id": IMAGE_ID,
                "docker_source_fingerprint": SOURCE_FINGERPRINT,
            },
            "images": [
                {
                    "name": "sample",
                    "source_nef": "sample.NEF",
                    "source_sha256": hashlib.sha256(
                        self.source.read_bytes()
                    ).hexdigest(),
                    "tiff": "sample.TIF",
                    "artifacts": artifacts,
                }
            ],
        }
        self.manifest = self.root / "baseline.json"
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def command(self) -> list[str]:
        return [
            str(self.manifest),
            "--source-dir",
            str(self.source_dir),
            "--docker-image-id",
            IMAGE_ID,
            "--docker-source-fingerprint",
            SOURCE_FINGERPRINT,
            *(f"{label}={path}" for label, path in self.artifact_dirs.items()),
        ]

    def test_exact_provenance_and_artifacts_pass(self) -> None:
        self.assertEqual(verify_validation_baseline.main(self.command()), 0)

    def test_changed_source_fails_even_when_tiffs_match(self) -> None:
        self.source.write_bytes(b"different-source-nef")
        self.assertEqual(verify_validation_baseline.main(self.command()), 1)

    def test_placeholder_engine_identity_fails_provenance(self) -> None:
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["engines"]["docker_image_id"] = "sha256:unknown"
        manifest["engines"]["docker_source_fingerprint"] = "compose-direct-unverified"
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")

        command = self.command()
        command[command.index(IMAGE_ID)] = "sha256:unknown"
        command[command.index(SOURCE_FINGERPRINT)] = "compose-direct-unverified"
        self.assertEqual(verify_validation_baseline.main(command), 1)


if __name__ == "__main__":
    unittest.main()
