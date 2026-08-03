from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tool import stage_runtime_acceptance


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class StageRuntimeAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.sources = self.root / "private-sources"
        self.sdk = self.root / "private-sdk"
        self.sources.mkdir()
        self.sdk.mkdir()
        source = self.sources / "nested" / "sample.NEF"
        source.parent.mkdir()
        source.write_bytes(b"private nef")
        sdk_file = self.sdk / "Bin" / "runtime.dll"
        sdk_file.parent.mkdir()
        sdk_file.write_bytes(b"private sdk")
        self.baseline = self.root / "baseline.json"
        self.baseline.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "images": [
                        {
                            "source_nef": "nested/sample.NEF",
                            "source_sha256": sha256(b"private nef"),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.sdk_manifest = self.root / "sdk.sha256"
        self.sdk_manifest.write_text(
            f"{sha256(b'private sdk')}  Bin/runtime.dll\n", encoding="utf-8"
        )
        self.destination = self.root / "staged"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def stage(self):
        return stage_runtime_acceptance.stage(
            baseline_path=self.baseline,
            source_dir=self.sources,
            sdk_manifest_path=self.sdk_manifest,
            sdk_dir=self.sdk,
            destination=self.destination,
        )

    def test_stages_only_manifested_verified_files(self) -> None:
        (self.sources / "unrelated.NEF").write_bytes(b"do not expose")
        (self.sdk / "unrelated.dll").write_bytes(b"do not expose")
        report = self.stage()
        self.assertEqual(len(report["sources"]), 1)
        self.assertEqual(len(report["sdk_files"]), 1)
        self.assertEqual(
            (self.destination / "sources" / "nested" / "sample.NEF").read_bytes(),
            b"private nef",
        )
        self.assertFalse((self.destination / "sources" / "unrelated.NEF").exists())
        self.assertFalse((self.destination / "sdk" / "unrelated.dll").exists())
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o555)

    def test_hash_mismatch_removes_incomplete_stage(self) -> None:
        (self.sources / "nested" / "sample.NEF").write_bytes(b"changed")
        with self.assertRaisesRegex(Exception, "hash mismatch"):
            self.stage()
        self.assertFalse(self.destination.exists())

    def test_symlinked_private_input_is_rejected(self) -> None:
        source = self.sources / "nested" / "sample.NEF"
        target = self.root / "outside.NEF"
        target.write_bytes(b"private nef")
        source.unlink()
        source.symlink_to(target)
        with self.assertRaisesRegex(Exception, "symbolic-link"):
            self.stage()
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
