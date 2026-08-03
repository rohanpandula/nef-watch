#!/usr/bin/env python3
"""Verify and stage only the private files needed for runtime acceptance."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import sys
from pathlib import Path

if __package__:
    from . import verify_runtime_acceptance as acceptance
else:  # ``python -I path/to/script.py`` excludes the script directory.
    module_path = Path(__file__).resolve().with_name("verify_runtime_acceptance.py")
    specification = importlib.util.spec_from_file_location(
        "_nef_watch_verify_runtime_acceptance", module_path
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("cannot load the sealed runtime acceptance module")
    acceptance = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = acceptance
    specification.loader.exec_module(acceptance)


def _copy_exact(source: Path, destination: Path, expected_hash: str, limit: int) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, flags)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination_fd = -1
    try:
        before = os.fstat(source_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > limit
        ):
            raise acceptance.OperationalError(
                f"private acceptance input is not a bounded regular file: {source.name}"
            )
        destination_fd = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o400,
        )
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            block = os.read(source_fd, min(1024 * 1024, remaining))
            if not block:
                raise acceptance.OperationalError(
                    f"private acceptance input ended early: {source.name}"
                )
            offset = 0
            while offset < len(block):
                written = os.write(destination_fd, block[offset:])
                if written <= 0:
                    raise acceptance.OperationalError(
                        f"cannot stage private acceptance input: {source.name}"
                    )
                offset += written
            digest.update(block)
            remaining -= len(block)
        if os.read(source_fd, 1):
            raise acceptance.OperationalError(
                f"private acceptance input grew while staging: {source.name}"
            )
        after = os.fstat(source_fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            before.st_nlink,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_nlink,
        ):
            raise acceptance.OperationalError(
                f"private acceptance input changed while staging: {source.name}"
            )
        actual_hash = digest.hexdigest()
        if actual_hash != expected_hash:
            raise acceptance.OperationalError(
                f"private acceptance input hash mismatch: {source.name}"
            )
        os.fsync(destination_fd)
        os.fchmod(destination_fd, 0o444)
        return actual_hash
    finally:
        os.close(source_fd)
        if destination_fd >= 0:
            os.close(destination_fd)


def _seal_directories(root: Path) -> None:
    directories = [root, *(path for path in root.rglob("*") if path.is_dir())]
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        descriptor = os.open(
            directory,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o555)
        finally:
            os.close(descriptor)


def stage(
    *,
    baseline_path: Path,
    source_dir: Path,
    sdk_manifest_path: Path,
    sdk_dir: Path,
    destination: Path,
) -> dict[str, object]:
    baseline_path = baseline_path.expanduser().resolve(strict=True)
    source_dir = source_dir.expanduser().resolve(strict=True)
    sdk_manifest_path = sdk_manifest_path.expanduser().resolve(strict=True)
    sdk_dir = sdk_dir.expanduser().resolve(strict=True)
    requested_destination = destination.expanduser().absolute()
    destination_parent = requested_destination.parent.resolve(strict=True)
    if requested_destination.name in ("", ".", ".."):
        raise acceptance.OperationalError("unsafe acceptance staging destination")
    destination = destination_parent / requested_destination.name
    if destination.exists() or destination.is_symlink():
        raise acceptance.OperationalError("acceptance staging destination must not exist")
    destination.mkdir(mode=0o700)
    try:
        staged_sources = destination / "sources"
        staged_sdk = destination / "sdk"
        staged_sources.mkdir(mode=0o700)
        staged_sdk.mkdir(mode=0o700)

        manifest = acceptance._load_manifest(baseline_path)
        seen_sources: set[str] = set()
        source_records: list[dict[str, str]] = []
        source_total = 0
        for index, image in enumerate(manifest["images"], 1):
            if not isinstance(image, dict):
                raise acceptance.OperationalError(
                    f"baseline image entry {index} is malformed"
                )
            relative = acceptance._safe_relative(
                image.get("source_nef"), f"baseline image {index}: source_nef"
            )
            key = relative.as_posix().casefold()
            expected_hash = image.get("source_sha256")
            if key in seen_sources or not isinstance(expected_hash, str) \
                    or acceptance.SHA256_RE.fullmatch(expected_hash) is None:
                raise acceptance.OperationalError(
                    f"baseline image {index} has duplicate or malformed source identity"
                )
            seen_sources.add(key)
            source = acceptance._contained_file(
                source_dir, relative, f"baseline source {relative}"
            )
            source_total += source.stat().st_size
            if source_total > acceptance.MAX_SOURCE_TOTAL_BYTES:
                raise acceptance.OperationalError(
                    "private acceptance sources exceed the aggregate size limit"
                )
            actual_hash = _copy_exact(
                source,
                staged_sources.joinpath(*relative.parts),
                expected_hash,
                acceptance.MAX_SOURCE_FILE_BYTES,
            )
            source_records.append({"path": relative.as_posix(), "sha256": actual_hash})

        sdk_entries = acceptance._load_sdk_manifest(sdk_manifest_path)
        sdk_records: list[dict[str, str]] = []
        sdk_total = 0
        for relative, expected_hash in sdk_entries:
            source = acceptance._contained_file(
                sdk_dir, relative, f"manifested SDK file {relative}"
            )
            sdk_total += source.stat().st_size
            if sdk_total > acceptance.MAX_SDK_TOTAL_BYTES:
                raise acceptance.OperationalError(
                    "manifested SDK files exceed the aggregate size limit"
                )
            actual_hash = _copy_exact(
                source,
                staged_sdk.joinpath(*relative.parts),
                expected_hash,
                acceptance.MAX_SDK_FILE_BYTES,
            )
            sdk_records.append({"path": relative.as_posix(), "sha256": actual_hash})

        _seal_directories(destination)
        return {
            "schema_version": 1,
            "baseline_sha256": acceptance.file_sha256(
                baseline_path, maximum_bytes=acceptance.MAX_MANIFEST_BYTES
            ),
            "sdk_manifest_sha256": acceptance.file_sha256(
                sdk_manifest_path, maximum_bytes=acceptance.MAX_MANIFEST_BYTES
            ),
            "sources": source_records,
            "sdk_files": sdk_records,
        }
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--sdk-manifest", type=Path, required=True)
    parser.add_argument("--sdk-dir", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = stage(
            baseline_path=args.baseline,
            source_dir=args.source_dir,
            sdk_manifest_path=args.sdk_manifest,
            sdk_dir=args.sdk_dir,
            destination=args.destination,
        )
    except (OSError, ValueError, acceptance.OperationalError) as exc:
        print(f"acceptance staging failed: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(
            f"acceptance staging: PASS ({len(report['sources'])} sources, "
            f"{len(report['sdk_files'])} SDK files)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
