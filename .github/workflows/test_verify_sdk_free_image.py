from __future__ import annotations

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from verify_sdk_free_image import scan_image


SDK_NAME = "NkImgSDK.dll"
SDK_PAYLOAD = b"synthetic Nikon SDK payload"


def add_bytes(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(payload)
    archive.addfile(member, io.BytesIO(payload))


def image_archive(path: Path, members: list[tuple[str, bytes | None]]) -> None:
    layer_buffer = io.BytesIO()
    with tarfile.open(fileobj=layer_buffer, mode="w") as layer:
        for name, payload in members:
            if payload is None:
                member = tarfile.TarInfo(name)
                member.type = tarfile.SYMTYPE
                member.linkname = "/private/" + SDK_NAME
                layer.addfile(member)
            else:
                add_bytes(layer, name, payload)

    with tarfile.open(path, mode="w") as image:
        add_bytes(
            image,
            "manifest.json",
            json.dumps([{"Config": "config.json", "Layers": ["layer.tar"]}]).encode(),
        )
        add_bytes(image, "config.json", b"{}")
        add_bytes(image, "layer.tar", layer_buffer.getvalue())


class SdkFreeImageScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manifest = self.root / "sdk.sha256"
        digest = hashlib.sha256(SDK_PAYLOAD).hexdigest()
        self.manifest.write_text(f"{digest}  Bin/x64/Release/{SDK_NAME}\n")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def scan(self, members: list[tuple[str, bytes | None]]) -> list[str]:
        image = self.root / "image.tar"
        image_archive(image, members)
        return scan_image(image, self.manifest)

    def test_forbidden_basename_catches_non_regular_member(self) -> None:
        matches = self.scan([(f"opt/nikon/{SDK_NAME}", None)])
        self.assertEqual(matches, [f"layer.tar:opt/nikon/{SDK_NAME}"])

    def test_known_hash_catches_renamed_payload(self) -> None:
        matches = self.scan([("opt/runtime/renamed.bin", SDK_PAYLOAD)])
        self.assertEqual(matches, ["layer.tar:opt/runtime/renamed.bin"])

    def test_clean_image_with_manifest_metadata_passes(self) -> None:
        metadata = self.manifest.read_bytes()
        matches = self.scan(
            [("usr/share/nef-watch/nikon-sdk-v1.46.sha256", metadata)]
        )
        self.assertEqual(matches, [])


if __name__ == "__main__":
    unittest.main()
