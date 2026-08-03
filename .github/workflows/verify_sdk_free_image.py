#!/usr/bin/env python3
"""Fail if a Docker image layer contains a Nikon SDK file or known SDK payload."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
from pathlib import Path, PurePosixPath


MAX_IMAGE_ARCHIVE_BYTES = 6 * 1024**3
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_LAYERS = 256
MAX_LAYER_BYTES = 4 * 1024**3
MAX_LAYER_MEMBERS = 500_000
MAX_MEMBER_BYTES = 2 * 1024**3


def manifest_entries(path: Path) -> tuple[set[str], set[str]]:
    names: set[str] = set()
    hashes: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            digest, relative = line.split(maxsplit=1)
        except ValueError as exc:
            raise ValueError(f"malformed manifest line {line_number}") from exc
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError(f"invalid SHA-256 on manifest line {line_number}")
        hashes.add(digest)
        names.add(PurePosixPath(relative).name.casefold())
    if not names:
        raise ValueError("Nikon SDK manifest is empty")
    return names, hashes


def scan_image(image_path: Path, sdk_manifest: Path) -> list[str]:
    forbidden_names, forbidden_hashes = manifest_entries(sdk_manifest)
    matches: list[str] = []

    if image_path.stat().st_size > MAX_IMAGE_ARCHIVE_BYTES:
        raise ValueError("Docker image archive exceeds the 6 GiB safety limit")

    with tarfile.open(image_path, mode="r:") as image:
        manifest_member = image.getmember("manifest.json")
        if not manifest_member.isfile() or manifest_member.size > MAX_MANIFEST_BYTES:
            raise ValueError("Docker image manifest is missing, unsafe, or too large")
        manifest_stream = image.extractfile(manifest_member)
        if manifest_stream is None:
            raise ValueError("Docker image archive has no manifest.json")
        image_manifest = json.load(manifest_stream)
        if (
            not isinstance(image_manifest, list)
            or len(image_manifest) != 1
            or not isinstance(image_manifest[0], dict)
        ):
            raise ValueError("Docker archive must contain exactly one image")
        layers = image_manifest[0].get("Layers")
        if (
            not isinstance(layers, list)
            or not layers
            or len(layers) > MAX_LAYERS
            or any(not isinstance(layer, str) or not layer for layer in layers)
            or len(set(layers)) != len(layers)
        ):
            raise ValueError("Docker image archive has an unsafe layer list")

        for layer_name in layers:
            try:
                layer_member = image.getmember(layer_name)
            except KeyError as exc:
                raise ValueError(f"Docker image archive is missing {layer_name}") from exc
            if not layer_member.isfile() or layer_member.size > MAX_LAYER_BYTES:
                raise ValueError(f"Docker image layer is unsafe or too large: {layer_name}")
            layer_stream = image.extractfile(layer_member)
            if layer_stream is None:
                raise ValueError(f"Docker image archive is missing {layer_name}")
            with tarfile.open(fileobj=layer_stream, mode="r|*") as layer:
                for member_index, member in enumerate(layer, 1):
                    if member_index > MAX_LAYER_MEMBERS:
                        raise ValueError(f"Docker image layer has too many entries: {layer_name}")
                    if member.size < 0 or member.size > MAX_MEMBER_BYTES:
                        raise ValueError(f"Docker image member is too large: {member.name}")
                    basename = PurePosixPath(member.name).name.casefold()
                    if basename.startswith(".wh."):
                        continue
                    if basename in forbidden_names:
                        matches.append(f"{layer_name}:{member.name}")
                        continue
                    if not member.isfile():
                        continue
                    stream = layer.extractfile(member)
                    if stream is None:
                        continue
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                    if digest in forbidden_hashes:
                        matches.append(f"{layer_name}:{member.name}")
    return matches


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path, help="archive produced by docker save")
    parser.add_argument("sdk_manifest", type=Path, help="Nikon SDK SHA-256 manifest")
    args = parser.parse_args()

    matches = scan_image(args.image, args.sdk_manifest)
    if matches:
        print("Nikon SDK artifact(s) found in image layer(s):")
        for match in matches:
            print(f"  {match}")
        return 1
    print("SDK-free image-layer audit: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
