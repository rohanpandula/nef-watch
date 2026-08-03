#!/usr/bin/env python3
"""Verify a private Unraid render against the recorded Linux golden hashes.

This is deliberately separate from ``verify_validation_baseline.py``.  The
baseline verifier proves the historical evidence snapshot, including its old
image identity.  This verifier accepts a *new* immutable image identity and
proves that it reproduced the same validated Linux pixels from the same NEFs
with the same pinned SDK manifest.

Exit codes:
  0  source provenance, SDK manifest, TIFF contract, ICC, and pixels all match
  1  a supplied artifact differs from the recorded Linux golden hashes
  2  the command or an input is malformed/unreadable
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_REFERENCE_RE = re.compile(r"^[^@\s]+@sha256:([0-9a-f]{64})$")
TIFF_SUFFIXES = {".tif", ".tiff"}
MAX_PROVENANCE_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_BASELINE_IMAGES = 64
MAX_SDK_MANIFEST_ENTRIES = 1024
MAX_SDK_FILE_BYTES = 512 * 1024 * 1024
MAX_SDK_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_OUTPUT_ENTRIES = 128
MAX_OUTPUT_DEPTH = 16
MAX_TIFF_BYTES = 1024 * 1024 * 1024
MAX_TIFF_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_OUTPUT_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_SOURCE_FILE_BYTES = 1024 * 1024 * 1024
MAX_SOURCE_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
RASTER_BINDING_FIELDS = {
    "shape_y_x_rgb",
    "dtype",
    "bits_per_sample",
    "photometric",
    "orientation",
}
MAX_REGISTRY_MANIFEST_BYTES = 1024 * 1024
MAX_DOCKER_INSPECT_BYTES = 2 * 1024 * 1024
MAX_TIFF_INSPECTION_REPORT_BYTES = 1024 * 1024
SINGLE_IMAGE_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
}
CONFIG_MEDIA_TYPES = {
    "application/vnd.docker.container.image.v1+json",
    "application/vnd.oci.image.config.v1+json",
}


class OperationalError(RuntimeError):
    """An acceptance input is malformed, unsafe, or cannot be inspected."""


def _expected_raster_binding(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != RASTER_BINDING_FIELDS:
        raise OperationalError(f"{field} must contain the exact raster metadata fields")
    shape = value.get("shape_y_x_rgb")
    dtype = value.get("dtype")
    expected_bits = [8, 8, 8] if dtype == "uint8" else [16, 16, 16]
    if (
        not isinstance(shape, list)
        or len(shape) != 3
        or shape[2] != 3
        or any(
            not isinstance(item, int) or isinstance(item, bool) or item <= 0
            for item in shape
        )
        or dtype not in {"uint8", "uint16"}
        or value.get("bits_per_sample") != expected_bits
        or value.get("photometric") != "RGB"
        or value.get("orientation") != 1
    ):
        raise OperationalError(f"{field} is malformed or violates the raster contract")
    return value


def _tiff_validator():
    """Load native TIFF dependencies only inside the isolated decoder mode."""
    if __package__:
        from . import validate_tiffs
    else:  # ``python -I path/to/script.py`` excludes the script directory.
        import importlib.util

        module_path = Path(__file__).resolve().with_name("validate_tiffs.py")
        specification = importlib.util.spec_from_file_location(
            "_nef_watch_validate_tiffs", module_path
        )
        if specification is None or specification.loader is None:
            raise OperationalError("cannot load the sealed TIFF validator")
        validate_tiffs = importlib.util.module_from_spec(specification)
        sys.modules[specification.name] = validate_tiffs
        try:
            specification.loader.exec_module(validate_tiffs)
        except ImportError as exc:
            sys.modules.pop(specification.name, None)
            raise OperationalError(
                f"TIFF decoder dependencies are unavailable: {exc}"
            ) from exc
    return validate_tiffs


def file_sha256(path: Path, *, maximum_bytes: int | None = None) -> str:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OperationalError(f"not a regular file: {path}")
        if maximum_bytes is not None and before.st_size > maximum_bytes:
            raise OperationalError(
                f"file exceeds the {maximum_bytes}-byte safety limit: {path.name}"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise OperationalError(f"file changed while hashing: {path.name}")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _safe_relative(value: Any, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise OperationalError(f"{field} is not a safe relative path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise OperationalError(f"{field} is not a safe relative path")
    return relative


def _contained_file(root: Path, relative: PurePosixPath, field: str) -> Path:
    candidate = root.joinpath(*relative.parts)
    current = root
    try:
        for component in relative.parts:
            current = current / component
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise OperationalError(
                    f"{field} contains a symbolic-link component: {candidate}"
                )
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise OperationalError(
            f"{field} is missing or unreadable: {candidate}: {exc}"
        ) from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise OperationalError(
            f"{field} escapes its configured directory: {candidate}"
        ) from exc
    metadata = resolved.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise OperationalError(f"{field} is not a file: {candidate}")
    return resolved


def _tiff_inventory(root: Path) -> dict[str, Path]:
    inventory: dict[str, Path] = {}
    stack: list[tuple[Path, PurePosixPath, int]] = [(root, PurePosixPath(), 0)]
    entry_count = 0
    total_tiff_bytes = 0
    total_output_bytes = 0
    while stack:
        folder, relative_folder, depth = stack.pop()
        if depth > MAX_OUTPUT_DEPTH:
            raise OperationalError(
                f"candidate output nesting exceeds {MAX_OUTPUT_DEPTH} levels"
            )
        try:
            entries = os.scandir(folder)
        except OSError as exc:
            raise OperationalError(
                f"cannot enumerate candidate TIFF directory: {exc}"
            ) from exc
        with entries:
            for entry in entries:
                entry_count += 1
                if entry_count > MAX_OUTPUT_ENTRIES:
                    raise OperationalError(
                        f"candidate output contains more than {MAX_OUTPUT_ENTRIES} entries"
                    )
                relative = relative_folder / entry.name
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise OperationalError(
                        f"cannot stat candidate output entry {relative}: {exc}"
                    ) from exc
                if stat.S_ISLNK(metadata.st_mode):
                    raise OperationalError(
                        f"candidate output contains a symbolic link: {relative}"
                    )
                if stat.S_ISDIR(metadata.st_mode):
                    stack.append((Path(entry.path), relative, depth + 1))
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise OperationalError(
                        f"candidate output contains a non-regular entry: {relative}"
                    )
                total_output_bytes += metadata.st_size
                if total_output_bytes > MAX_OUTPUT_TOTAL_BYTES:
                    raise OperationalError(
                        "candidate output entries exceed the aggregate size limit"
                    )
                if Path(entry.name).suffix.casefold() not in TIFF_SUFFIXES:
                    if (
                        relative != PurePosixPath(".nef-watch-output.lock")
                        or metadata.st_nlink != 1
                        or metadata.st_size != 0
                    ):
                        raise OperationalError(
                            f"candidate output contains an unexpected file: {relative}"
                        )
                    continue
                if metadata.st_nlink != 1:
                    raise OperationalError(
                        f"candidate TIFF has multiple hard links: {relative}"
                    )
                if metadata.st_size > MAX_TIFF_BYTES:
                    raise OperationalError(
                        f"candidate TIFF exceeds {MAX_TIFF_BYTES} bytes: {relative}"
                    )
                total_tiff_bytes += metadata.st_size
                if total_tiff_bytes > MAX_TIFF_TOTAL_BYTES:
                    raise OperationalError(
                        "candidate TIFF files exceed the aggregate size limit"
                    )
                key = relative.as_posix().casefold()
                candidate = Path(entry.path)
                if key in inventory:
                    raise OperationalError(
                        f"candidate TIFF paths collide case-insensitively: "
                        f"{inventory[key].name} and {relative}"
                    )
                inventory[key] = candidate
    return inventory


def create_tiff_inspection_report(output_dir: Path) -> dict[str, Any]:
    """Decode candidate TIFFs in a process that has no private fixture mounts."""
    validator = _tiff_validator()
    output_dir = output_dir.expanduser().resolve(strict=True)
    if not output_dir.is_dir():
        raise OperationalError("--output-dir must be a directory")
    inventory = _tiff_inventory(output_dir)
    outputs: list[dict[str, Any]] = []
    for key, candidate in sorted(inventory.items()):
        relative = candidate.relative_to(output_dir).as_posix()
        try:
            raster = validator.inspect_tiff(
                candidate, f"candidate:{relative}", strict_acceptance=True
            )
        except validator.OperationalError as exc:
            raise OperationalError(str(exc)) from exc
        summary = raster.summary()
        del raster
        summary["path"] = relative
        outputs.append(
            {
                "path": relative,
                "file_size": candidate.stat().st_size,
                "file_sha256": file_sha256(candidate, maximum_bytes=MAX_TIFF_BYTES),
                "summary": summary,
            }
        )
    return {"schema_version": 1, "outputs": outputs}


def write_tiff_inspection_report(report: dict[str, Any], destination: Path) -> None:
    """Write only to the pre-created, single-link report file mounted by the host."""
    payload = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > MAX_TIFF_INSPECTION_REPORT_BYTES:
        raise OperationalError("TIFF inspection report is too large")
    descriptor = os.open(
        destination,
        os.O_WRONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size != 0
            or stat.S_IMODE(before.st_mode) != 0o600
        ):
            raise OperationalError(
                "TIFF inspection report destination is not an empty mode-0600 "
                "single-link regular file"
            )
        if before.st_uid != os.geteuid():
            raise OperationalError(
                "TIFF inspection report destination is not owned by the decoder user"
            )
        os.ftruncate(descriptor, 0)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OperationalError(
                    "cannot write TIFF inspection report"
                )
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validated_inspection_summary(value: Any, relative: PurePosixPath) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OperationalError(
            f"TIFF inspection summary is malformed: {relative}"
        )
    required = {
        "label",
        "path",
        "primary_series",
        "ignored_series",
        "source_axes",
        "shape_y_x_rgb",
        "dtype",
        "bits_per_sample",
        "photometric",
        "orientation",
        "pixel_sha256",
        "icc_sha256",
        "contract_errors",
    }
    if set(value) != required:
        raise OperationalError(
            f"TIFF inspection summary fields are malformed: {relative}"
        )
    expected_path = relative.as_posix()
    if value["path"] != expected_path or value["label"] != f"candidate:{expected_path}":
        raise OperationalError(
            f"TIFF inspection summary path is not canonical: {relative}"
        )
    for field in ("pixel_sha256", "icc_sha256"):
        if not isinstance(value[field], str) or SHA256_RE.fullmatch(value[field]) is None:
            raise OperationalError(
                f"TIFF inspection {field} is malformed: {relative}"
            )
    if (
        not isinstance(value["primary_series"], int)
        or not isinstance(value["ignored_series"], int)
        or not isinstance(value["orientation"], int)
        or not isinstance(value["source_axes"], str)
        or not isinstance(value["dtype"], str)
        or not isinstance(value["photometric"], str)
        or not isinstance(value["shape_y_x_rgb"], list)
        or len(value["shape_y_x_rgb"]) != 3
        or any(not isinstance(item, int) or item <= 0 for item in value["shape_y_x_rgb"])
        or not isinstance(value["bits_per_sample"], list)
        or any(not isinstance(item, int) for item in value["bits_per_sample"])
        or not isinstance(value["contract_errors"], list)
        or any(not isinstance(item, str) or len(item) > 4096 for item in value["contract_errors"])
    ):
        raise OperationalError(
            f"TIFF inspection summary values are malformed: {relative}"
        )
    expected_bits = [8, 8, 8] if value["dtype"] == "uint8" else [16, 16, 16]
    if (
        value["primary_series"] != 0
        or value["ignored_series"] != 0
        or value["source_axes"] != "YXS"
        or value["shape_y_x_rgb"][2] != 3
        or value["dtype"] not in {"uint8", "uint16"}
        or value["bits_per_sample"] != expected_bits
        or value["photometric"] != "RGB"
        or value["orientation"] != 1
    ):
        raise OperationalError(
            f"TIFF inspection summary violates the strict raster contract: {relative}"
        )
    return value


def _load_tiff_inspection_report(
    path: Path, inventory: dict[str, Path]
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    try:
        requested = path.expanduser().absolute()
        requested_metadata = requested.lstat()
        if stat.S_ISLNK(requested_metadata.st_mode):
            raise OperationalError(
                "TIFF inspection report must not be a symbolic link"
            )
        resolved = requested.resolve(strict=True)
        metadata = resolved.stat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > MAX_TIFF_INSPECTION_REPORT_BYTES
        ):
            raise OperationalError(
                "TIFF inspection report is not a bounded single-link regular file"
            )
        report = json.loads(resolved.read_text(encoding="utf-8"))
    except OperationalError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OperationalError(
            f"cannot read TIFF inspection report: {exc}"
        ) from exc
    if (
        not isinstance(report, dict)
        or set(report) != {"schema_version", "outputs"}
        or report.get("schema_version") != 1
        or not isinstance(report.get("outputs"), list)
        or len(report["outputs"]) > MAX_BASELINE_IMAGES
    ):
        raise OperationalError("TIFF inspection report is malformed")

    summaries: dict[str, dict[str, Any]] = {}
    for entry in report["outputs"]:
        if not isinstance(entry, dict) or set(entry) != {
            "path", "file_size", "file_sha256", "summary"
        }:
            raise OperationalError("TIFF inspection entry is malformed")
        relative = _safe_relative(entry["path"], "TIFF inspection path")
        key = relative.as_posix().casefold()
        if key in summaries or key not in inventory:
            raise OperationalError(
                f"TIFF inspection path is duplicate or unexpected: {relative}"
            )
        file_size = entry["file_size"]
        expected_hash = entry["file_sha256"]
        if (
            not isinstance(file_size, int)
            or file_size < 0
            or file_size > MAX_TIFF_BYTES
            or not isinstance(expected_hash, str)
            or SHA256_RE.fullmatch(expected_hash) is None
        ):
            raise OperationalError(
                f"TIFF inspection file identity is malformed: {relative}"
            )
        candidate = inventory[key]
        if candidate.stat().st_size != file_size or file_sha256(
            candidate, maximum_bytes=MAX_TIFF_BYTES
        ) != expected_hash:
            raise OperationalError(
                f"TIFF changed after sandboxed inspection: {relative}"
            )
        summary = _validated_inspection_summary(entry["summary"], relative)
        summary["file_size"] = file_size
        summary["file_sha256"] = expected_hash
        summaries[key] = summary
    if set(summaries) != set(inventory):
        raise OperationalError(
            "TIFF inspection report does not cover the exact candidate TIFF set"
        )
    evidence = {
        "file": resolved.name,
        "sha256": file_sha256(
            resolved, maximum_bytes=MAX_TIFF_INSPECTION_REPORT_BYTES
        ),
        "output_count": len(summaries),
    }
    return summaries, evidence


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise OperationalError("acceptance manifest is too large")
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperationalError(
            f"cannot read acceptance manifest {path}: {exc}"
        ) from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or not isinstance(manifest.get("images"), list)
        or not manifest["images"]
        or len(manifest["images"]) > MAX_BASELINE_IMAGES
    ):
        raise OperationalError("unsupported or malformed acceptance manifest")
    return manifest


def _load_sdk_manifest(path: Path) -> list[tuple[PurePosixPath, str]]:
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise OperationalError("SDK manifest is too large")
        lines = path.read_text(encoding="utf-8").splitlines()
    except OperationalError:
        raise
    except (OSError, UnicodeError) as exc:
        raise OperationalError(f"cannot read SDK manifest: {exc}") from exc
    if not lines or len(lines) > MAX_SDK_MANIFEST_ENTRIES:
        raise OperationalError("SDK manifest entry count is unsafe")
    entries: list[tuple[PurePosixPath, str]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(lines, 1):
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if match is None:
            raise OperationalError(
                f"malformed SDK manifest line {line_number}"
            )
        relative = _safe_relative(match.group(2), f"SDK manifest line {line_number}")
        key = relative.as_posix().casefold()
        if key in seen:
            raise OperationalError(
                f"duplicate SDK manifest path: {relative}"
            )
        seen.add(key)
        entries.append((relative, match.group(1)))
    return entries


def _verify_sdk_files(
    sdk_root: Path, entries: list[tuple[PurePosixPath, str]]
) -> tuple[list[dict[str, str]], list[str]]:
    checked: list[dict[str, str]] = []
    errors: list[str] = []
    total = 0
    for relative, expected_hash in entries:
        candidate = _contained_file(sdk_root, relative, f"SDK file {relative}")
        size = candidate.stat().st_size
        total += size
        if total > MAX_SDK_TOTAL_BYTES:
            raise OperationalError(
                "manifested SDK files exceed the aggregate size limit"
            )
        actual_hash = file_sha256(candidate, maximum_bytes=MAX_SDK_FILE_BYTES)
        checked.append({"path": relative.as_posix(), "sha256": actual_hash})
        if actual_hash != expected_hash:
            errors.append(
                f"SDK file {relative} SHA-256 {actual_hash} != {expected_hash}"
            )
    return checked, errors


def _validate_identity(
    image_id: str,
    image_reference: str | None,
    source_fingerprint: str,
    expected_source_fingerprint: str,
    *,
    trusted_local_image: bool,
) -> str | None:
    if not image_id.startswith("sha256:") or not SHA256_RE.fullmatch(
        image_id.removeprefix("sha256:")
    ):
        raise OperationalError(
            "--docker-image-id must be an immutable sha256 image ID"
        )
    if not SHA256_RE.fullmatch(source_fingerprint):
        raise OperationalError(
            "--docker-source-fingerprint must be the image's SHA-256 source label"
        )
    if not SHA256_RE.fullmatch(expected_source_fingerprint):
        raise OperationalError(
            "--expected-source-fingerprint must be the audited checkout's SHA-256 fingerprint"
        )
    if trusted_local_image:
        if image_reference is not None:
            raise OperationalError(
                "--trusted-local-image cannot be combined with "
                "--docker-image-reference"
            )
        return None
    if image_reference is None:
        raise OperationalError(
            "registry acceptance requires --docker-image-reference"
        )
    reference_match = IMAGE_REFERENCE_RE.fullmatch(image_reference)
    if reference_match is None:
        raise OperationalError(
            "--docker-image-reference must be an exact registry reference ending in "
            "@sha256:<64 lowercase hex characters>"
        )
    return reference_match.group(1)


def _verified_provenance(path: Path, expected_digest: str) -> dict[str, Any]:
    """Validate and summarize JSON emitted by ``gh attestation verify``.

    Signature and certificate policy enforcement is performed by the trusted
    ``gh`` invocation. This second check makes the retained acceptance report
    fail closed unless that verified output names the exact candidate digest.
    """

    try:
        resolved = path.expanduser().resolve(strict=True)
        if resolved.stat().st_size > MAX_PROVENANCE_BYTES:
            raise OperationalError(
                "verified provenance output is unexpectedly large"
            )
        entries = json.loads(resolved.read_text(encoding="utf-8"))
    except OperationalError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OperationalError(
            f"cannot read verified provenance output {path}: {exc}"
        ) from exc
    if not isinstance(entries, list) or not entries:
        raise OperationalError(
            "verified provenance output must contain at least one attestation"
        )

    matching = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        verification = entry.get("verificationResult")
        statement = verification.get("statement") if isinstance(verification, dict) else None
        subjects = statement.get("subject") if isinstance(statement, dict) else None
        if not isinstance(subjects, list):
            continue
        for subject in subjects:
            digest = subject.get("digest") if isinstance(subject, dict) else None
            if isinstance(digest, dict) and digest.get("sha256") == expected_digest:
                matching += 1
                break
    if matching == 0:
        raise OperationalError(
            "verified provenance does not name the exact candidate registry digest"
        )
    return {
        "file": resolved.name,
        "sha256": file_sha256(resolved),
        "matching_attestations": matching,
    }


def _verified_registry_manifest(
    path: Path, expected_digest: str, expected_image_id: str
) -> dict[str, Any]:
    try:
        resolved = path.expanduser().resolve(strict=True)
        if resolved.stat().st_size > MAX_REGISTRY_MANIFEST_BYTES:
            raise OperationalError("registry manifest is too large")
        payload = resolved.read_bytes()
        manifest = json.loads(payload)
    except OperationalError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OperationalError(
            f"cannot read exact registry manifest: {exc}"
        ) from exc
    actual_digest = hashlib.sha256(payload).hexdigest()
    if actual_digest != expected_digest:
        raise OperationalError(
            "registry manifest bytes do not match the exact candidate digest"
        )
    config = manifest.get("config") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("schemaVersion") != 2
        or manifest.get("mediaType") not in SINGLE_IMAGE_MEDIA_TYPES
        or "manifests" in manifest
        or not isinstance(config, dict)
        or not isinstance(manifest.get("layers"), list)
        or not manifest["layers"]
    ):
        raise OperationalError(
            "candidate digest must identify one image manifest, not an OCI index"
        )
    if (
        config.get("mediaType") not in CONFIG_MEDIA_TYPES
        or config.get("digest") != expected_image_id
        or not isinstance(config.get("size"), int)
        or not 0 < config["size"] <= MAX_DOCKER_INSPECT_BYTES
    ):
        raise OperationalError(
            "registry manifest config is malformed or does not match the local image ID"
        )
    return {
        "file": resolved.name,
        "sha256": actual_digest,
        "media_type": manifest["mediaType"],
        "config_digest": config["digest"],
        "layers": len(manifest["layers"]),
    }


def _verified_docker_inspect(
    path: Path,
    *,
    image_id: str,
    image_reference: str | None,
    source_fingerprint: str,
    expected_revision: str,
) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{40}", expected_revision) is None:
        raise OperationalError(
            "--expected-revision must be a full 40-character lowercase Git commit"
        )
    try:
        resolved = path.expanduser().resolve(strict=True)
        if resolved.stat().st_size > MAX_DOCKER_INSPECT_BYTES:
            raise OperationalError("Docker inspect evidence is too large")
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except OperationalError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OperationalError(
            f"cannot read Docker inspect evidence: {exc}"
        ) from exc
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise OperationalError(
            "Docker inspect evidence must contain exactly one image"
        )
    image = payload[0]
    config = image.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    environment = config.get("Env") if isinstance(config, dict) else None
    repo_digests = image.get("RepoDigests")
    if image.get("Id") != image_id:
        raise OperationalError(
            "Docker inspect image ID does not match the supplied exact image ID"
        )
    if image.get("Os") != "linux" or image.get("Architecture") != "amd64":
        raise OperationalError(
            "accepted image must be exactly linux/amd64"
        )
    if image_reference is not None:
        if not isinstance(repo_digests, list) or image_reference not in repo_digests:
            raise OperationalError(
                "Docker inspect evidence does not name the exact registry digest"
            )
    if not isinstance(labels, dict):
        raise OperationalError("Docker image labels are missing")
    if labels.get("io.nef-watch.source.fingerprint") != source_fingerprint:
        raise OperationalError(
            "Docker inspect source label does not match the supplied fingerprint"
        )
    if labels.get("org.opencontainers.image.revision") != expected_revision:
        raise OperationalError(
            "Docker image revision label does not match the reviewed commit"
        )
    if labels.get("io.nef-watch.nikon-sdk.bundled") != "false":
        raise OperationalError(
            "Docker image does not carry the required SDK-free label"
        )
    environment_is_valid = isinstance(environment, list) and all(
        isinstance(value, str) for value in environment
    )
    render_job_entries = (
        [
            value
            for value in environment
            if isinstance(value, str) and value.startswith("NEF_WATCH_RENDER_JOBS=")
        ]
        if environment_is_valid
        else []
    )
    if not environment_is_valid or render_job_entries != [
        "NEF_WATCH_RENDER_JOBS=1"
    ]:
        raise OperationalError(
            "Docker image must set exactly NEF_WATCH_RENDER_JOBS=1"
        )
    return {
        "file": resolved.name,
        "sha256": file_sha256(resolved, maximum_bytes=MAX_DOCKER_INSPECT_BYTES),
        "platform": "linux/amd64",
        "revision": expected_revision,
        "render_jobs": 1,
    }


def verify(
    *,
    manifest_path: Path,
    source_dir: Path,
    output_dir: Path,
    sdk_dir: Path,
    sdk_manifest: Path,
    expected_label: str,
    image_id: str,
    image_reference: str | None = None,
    source_fingerprint: str,
    expected_source_fingerprint: str,
    expected_revision: str,
    provenance_path: Path | None = None,
    registry_manifest_path: Path | None = None,
    docker_inspect_path: Path,
    tiff_inspection_report_path: Path,
    trusted_local_image: bool = False,
) -> tuple[dict[str, Any], int]:
    registry_digest = _validate_identity(
        image_id,
        image_reference,
        source_fingerprint,
        expected_source_fingerprint,
        trusted_local_image=trusted_local_image,
    )
    registry_arguments = {
        "--verified-provenance": provenance_path,
        "--registry-manifest": registry_manifest_path,
    }
    if trusted_local_image:
        supplied = [name for name, value in registry_arguments.items() if value is not None]
        if supplied:
            raise OperationalError(
                "--trusted-local-image cannot be combined with " + ", ".join(supplied)
            )
    else:
        missing = [name for name, value in registry_arguments.items() if value is None]
        if missing:
            raise OperationalError(
                "registry acceptance requires " + ", ".join(missing)
            )
    manifest_path = manifest_path.expanduser().resolve(strict=True)
    source_dir = source_dir.expanduser().resolve(strict=True)
    output_dir = output_dir.expanduser().resolve(strict=True)
    sdk_dir = sdk_dir.expanduser().resolve(strict=True)
    sdk_manifest = sdk_manifest.expanduser().resolve(strict=True)
    if not source_dir.is_dir() or not output_dir.is_dir() or not sdk_dir.is_dir():
        raise OperationalError(
            "--source-dir, --output-dir, and --sdk-dir must be directories"
        )

    manifest = _load_manifest(manifest_path)
    engines = manifest.get("engines")
    if not isinstance(engines, dict):
        raise OperationalError("manifest engine identity is missing")
    expected_sdk_hash = engines.get("nikon_sdk_manifest_sha256")
    if not isinstance(expected_sdk_hash, str) or not SHA256_RE.fullmatch(expected_sdk_hash):
        raise OperationalError(
            "manifest Nikon SDK fingerprint is missing or malformed"
        )
    actual_sdk_hash = file_sha256(sdk_manifest, maximum_bytes=MAX_MANIFEST_BYTES)
    sdk_entries = _load_sdk_manifest(sdk_manifest)
    checked_sdk_files, sdk_file_errors = _verify_sdk_files(sdk_dir, sdk_entries)
    provenance = None
    registry_manifest = None
    if not trusted_local_image:
        assert provenance_path is not None
        assert registry_manifest_path is not None
        assert registry_digest is not None
        provenance = _verified_provenance(provenance_path, registry_digest)
        registry_manifest = _verified_registry_manifest(
            registry_manifest_path, registry_digest, image_id
        )
    docker_inspect = _verified_docker_inspect(
        docker_inspect_path,
        image_id=image_id,
        image_reference=image_reference,
        source_fingerprint=source_fingerprint,
        expected_revision=expected_revision,
    )

    declared_labels = manifest.get("artifact_labels")
    if not isinstance(declared_labels, list) or expected_label not in declared_labels:
        raise OperationalError(
            f"artifact label {expected_label!r} is not present in the manifest"
        )

    inventory = _tiff_inventory(output_dir)
    inspected_tiffs, inspection_evidence = _load_tiff_inspection_report(
        tiff_inspection_report_path, inventory
    )
    errors: list[str] = []
    errors.extend(sdk_file_errors)
    if source_fingerprint != expected_source_fingerprint:
        errors.append(
            "candidate source fingerprint "
            f"{source_fingerprint} != audited checkout {expected_source_fingerprint}"
        )
    if actual_sdk_hash != expected_sdk_hash:
        errors.append(
            f"SDK manifest SHA-256 {actual_sdk_hash} != {expected_sdk_hash}"
        )

    expected_tiffs: set[str] = set()
    seen_sources: set[str] = set()
    checked_sources: list[dict[str, str]] = []
    checked_outputs: list[dict[str, Any]] = []

    for index, image in enumerate(manifest["images"]):
        if not isinstance(image, dict):
            raise OperationalError(
                f"manifest image entry {index + 1} is not an object"
            )
        name = image.get("name")
        if not isinstance(name, str) or not name:
            raise OperationalError(
                f"manifest image entry {index + 1} has no name"
            )
        source_relative = _safe_relative(image.get("source_nef"), f"{name}: source_nef")
        source_key = source_relative.as_posix().casefold()
        if source_key in seen_sources:
            raise OperationalError(
                f"manifest source path is duplicated case-insensitively: {source_relative}"
            )
        seen_sources.add(source_key)
        source_expected = image.get("source_sha256")
        if not isinstance(source_expected, str) or not SHA256_RE.fullmatch(source_expected):
            raise OperationalError(f"{name}: source SHA-256 is malformed")
        source_path = _contained_file(source_dir, source_relative, f"{name}: source NEF")
        source_actual = file_sha256(
            source_path, maximum_bytes=MAX_SOURCE_FILE_BYTES
        )
        checked_sources.append(
            {
                "name": name,
                "path": source_relative.as_posix(),
                "sha256": source_actual,
            }
        )
        if source_actual != source_expected:
            errors.append(f"{name}: source SHA-256 {source_actual} != {source_expected}")

        tiff_relative = _safe_relative(image.get("tiff"), f"{name}: tiff")
        tiff_key = tiff_relative.as_posix().casefold()
        if tiff_key in expected_tiffs:
            raise OperationalError(
                f"manifest TIFF path is duplicated case-insensitively: {tiff_relative}"
            )
        expected_tiffs.add(tiff_key)
        candidate = inventory.get(tiff_key)
        if candidate is None:
            errors.append(f"{name}: candidate TIFF is missing: {tiff_relative}")
            continue

        artifacts = image.get("artifacts")
        expected = artifacts.get(expected_label) if isinstance(artifacts, dict) else None
        if not isinstance(expected, dict):
            raise OperationalError(
                f"{name}: manifest has no {expected_label!r} artifact hashes"
            )
        expected_pixel = expected.get("pixel_sha256")
        expected_icc = expected.get("icc_sha256")
        if not isinstance(expected_pixel, str) or not SHA256_RE.fullmatch(expected_pixel):
            raise OperationalError(f"{name}: expected pixel hash is malformed")
        if not isinstance(expected_icc, str) or not SHA256_RE.fullmatch(expected_icc):
            raise OperationalError(f"{name}: expected ICC hash is malformed")
        expected_raster = _expected_raster_binding(
            image.get("raster"), f"{name}: raster"
        )

        summary = inspected_tiffs[tiff_key]
        checked_outputs.append(summary)
        if summary["contract_errors"]:
            errors.extend(
                f"{name}: TIFF contract: {message}"
                for message in summary["contract_errors"]
            )
        if summary["pixel_sha256"] != expected_pixel:
            errors.append(
                f"{name}: decoded pixel SHA-256 {summary['pixel_sha256']} != {expected_pixel}"
            )
        if summary["icc_sha256"] != expected_icc:
            errors.append(
                f"{name}: ICC SHA-256 {summary['icc_sha256']} != {expected_icc}"
            )
        for field in sorted(RASTER_BINDING_FIELDS):
            if summary.get(field) != expected_raster[field]:
                errors.append(
                    f"{name}: raster {field} {summary.get(field)!r} "
                    f"!= {expected_raster[field]!r}"
                )

    missing = sorted(expected_tiffs - set(inventory))
    extra = sorted(set(inventory) - expected_tiffs)
    if missing or extra:
        errors.append(f"candidate TIFF set differs; missing={missing} extra={extra}")

    candidate_engine: dict[str, Any] = {
        "identity_mode": "trusted-local-image" if trusted_local_image else "registry",
        "docker_image_id": image_id,
        "docker_source_fingerprint": source_fingerprint,
        "expected_source_fingerprint": expected_source_fingerprint,
    }
    if not trusted_local_image:
        assert image_reference is not None
        assert registry_digest is not None
        candidate_engine.update(
            {
                "docker_image_reference": image_reference,
                "registry_digest": f"sha256:{registry_digest}",
            }
        )

    report: dict[str, Any] = {
        "schema_version": 1,
        "criterion": (
            "private-source-and-sdk-plus-trusted-local-image-and-recorded-linux-pixels"
            if trusted_local_image
            else "private-source-and-sdk-provenance-plus-recorded-linux-pixels"
        ),
        "passed": not errors,
        "errors": errors,
        "manifest": {
            "file": manifest_path.name,
            "sha256": file_sha256(manifest_path, maximum_bytes=MAX_MANIFEST_BYTES),
        },
        "expected_artifact_label": expected_label,
        "candidate_engine": candidate_engine,
        "sdk_manifest": {
            "file": sdk_manifest.name,
            "sha256": actual_sdk_hash,
            "expected_sha256": expected_sdk_hash,
            "checked_files": checked_sdk_files,
        },
        "docker_inspect": docker_inspect,
        "sandboxed_tiff_inspection": inspection_evidence,
        "checked_sources": checked_sources,
        "checked_outputs": checked_outputs,
    }
    if not trusted_local_image:
        report["verified_provenance"] = provenance
        report["registry_manifest"] = registry_manifest
    return report, 0 if not errors else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a new private Linux/Unraid render against recorded source, "
            "SDK, decoded-pixel, and ICC hashes."
        )
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--sdk-dir", type=Path, required=True)
    parser.add_argument("--sdk-manifest", type=Path, required=True)
    parser.add_argument("--expected-label", default="linux")
    parser.add_argument(
        "--docker-image-id",
        required=True,
        help="exact local Docker image config ID, sha256:<64 lowercase hex>",
    )
    identity_mode = parser.add_mutually_exclusive_group(required=True)
    identity_mode.add_argument(
        "--docker-image-reference",
        help="exact registry reference ending in @sha256:<64 lowercase hex>",
    )
    identity_mode.add_argument(
        "--trusted-local-image",
        action="store_true",
        help=(
            "trust host-generated Docker inspect evidence for the exact local image ID; "
            "registry manifest and provenance arguments are forbidden"
        ),
    )
    parser.add_argument("--docker-source-fingerprint", required=True)
    parser.add_argument("--expected-source-fingerprint", required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument(
        "--verified-provenance",
        type=Path,
        help=(
            "registry mode only: JSON produced by a policy-constrained "
            "'gh attestation verify --format json'"
        ),
    )
    parser.add_argument(
        "--registry-manifest",
        type=Path,
        help="registry mode only: exact raw single-image manifest returned by the registry",
    )
    parser.add_argument(
        "--docker-inspect",
        type=Path,
        required=True,
        help="JSON emitted by 'docker image inspect' for the exact digest",
    )
    parser.add_argument(
        "--tiff-inspection-report",
        type=Path,
        required=True,
        help=(
            "JSON emitted by --emit-tiff-inspection in an isolated container "
            "that cannot see the private NEFs or SDK"
        ),
    )
    parser.add_argument("--json", action="store_true")
    return parser


def build_inspection_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Decode runtime TIFFs and write a bounded inspection report."
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--emit-tiff-inspection"]:
        inspection_args = build_inspection_parser().parse_args(arguments[1:])
        try:
            inspection_report = create_tiff_inspection_report(
                inspection_args.output_dir
            )
            write_tiff_inspection_report(inspection_report, inspection_args.report)
        except (OSError, ValueError, OperationalError) as exc:
            print(f"TIFF inspection failed: {exc}", file=sys.stderr)
            return 2
        print(
            f"TIFF inspection: COMPLETE ({len(inspection_report['outputs'])} files)"
        )
        return 0

    args = build_parser().parse_args(arguments)
    try:
        report, exit_code = verify(
            manifest_path=args.manifest,
            source_dir=args.source_dir,
            output_dir=args.output_dir,
            sdk_dir=args.sdk_dir,
            sdk_manifest=args.sdk_manifest,
            expected_label=args.expected_label,
            image_id=args.docker_image_id,
            image_reference=args.docker_image_reference,
            source_fingerprint=args.docker_source_fingerprint,
            expected_source_fingerprint=args.expected_source_fingerprint,
            expected_revision=args.expected_revision,
            provenance_path=args.verified_provenance,
            registry_manifest_path=args.registry_manifest,
            docker_inspect_path=args.docker_inspect,
            tiff_inspection_report_path=args.tiff_inspection_report,
            trusted_local_image=args.trusted_local_image,
        )
    except (OSError, ValueError, OperationalError) as exc:
        if args.json:
            print(json.dumps({"passed": False, "operational_error": str(exc)}, indent=2))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("runtime acceptance: " + ("PASS" if report["passed"] else "FAIL"))
        print(f"  image: {report['candidate_engine']['docker_image_id']}")
        if report["candidate_engine"]["identity_mode"] == "registry":
            print(f"  registry: {report['candidate_engine']['docker_image_reference']}")
        else:
            print("  identity: trusted local Docker image config")
        print(
            "  source fingerprint: "
            f"{report['candidate_engine']['docker_source_fingerprint']}"
        )
        print(f"  sources checked: {len(report['checked_sources'])}")
        print(f"  TIFFs checked: {len(report['checked_outputs'])}")
        for error in report["errors"]:
            print(f"  {error}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
