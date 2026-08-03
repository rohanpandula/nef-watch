#!/usr/bin/env python3
"""Validate the retained production-acceptance evidence inside the container.

The production Compose model mounts one root-owned, read-only report at a fixed
path.  This verifier binds that report to identity and fixture files baked into
the exact image, so a raw ``docker compose up`` cannot silently bypass the host
acceptance wrapper.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn


EXIT_CONFIG = 78
GIB = 1024**3
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
IMAGE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SDK_LINE_RE = re.compile(r"^([0-9a-f]{64})  (.+)$")
RASTER_BINDING_FIELDS = {
    "shape_y_x_rgb",
    "dtype",
    "bits_per_sample",
    "photometric",
    "orientation",
}
OUTPUT_FIELDS = {
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
    "file_size",
    "file_sha256",
}
REPORT_FIELDS = {
    "schema_version",
    "criterion",
    "passed",
    "errors",
    "manifest",
    "expected_artifact_label",
    "candidate_engine",
    "sdk_manifest",
    "docker_inspect",
    "sandboxed_tiff_inspection",
    "checked_sources",
    "checked_outputs",
}

REPORT_PATH = Path("/run/nef-watch-acceptance.json")
IDENTITY_PATH = Path("/usr/share/nef-watch/source-identity.json")
BASELINE_PATH = Path("/usr/share/nef-watch/validation-baseline-v1.json")
SDK_MANIFEST_PATH = Path("/usr/share/nef-watch/nikon-sdk-v1.46.sha256")


class AcceptanceMarkerError(RuntimeError):
    """Runtime evidence does not prove this exact image was accepted."""


def _fail(message: str) -> NoReturn:
    raise AcceptanceMarkerError(message)


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_trusted_file(
    path: Path,
    *,
    expected_owner: int,
    maximum_bytes: int,
    exact_mode: int = 0o444,
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AcceptanceMarkerError(f"cannot open acceptance evidence {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != expected_owner
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != exact_mode
            or before.st_size > maximum_bytes
        ):
            _fail(
                f"acceptance evidence must be a bounded, owner-{expected_owner}, "
                f"single-link, mode-{exact_mode:04o} regular file: {path}"
            )
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            block = os.read(descriptor, min(1024 * 1024, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(payload) != before.st_size
            or len(payload) > maximum_bytes
            or _file_identity(before) != _file_identity(after)
        ):
            _fail(f"acceptance evidence changed while it was read: {path}")
        return payload
    finally:
        os.close(descriptor)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"acceptance JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _load_json_bytes(payload: bytes, label: str) -> Any:
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: _fail(
                f"{label} contains non-finite JSON number {value}"
            ),
        )
    except AcceptanceMarkerError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AcceptanceMarkerError(f"cannot parse {label}: {exc}") from exc


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _safe_relative(value: Any, label: str) -> str:
    if not isinstance(value, str):
        _fail(f"{label} path is not a string")
    relative = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or relative.is_absolute()
        or any(part in ("", ".", "..") for part in relative.parts)
    ):
        _fail(f"unsafe {label} path: {value!r}")
    return relative.as_posix()


def _parse_sdk_manifest(payload: bytes) -> list[dict[str, str]]:
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise AcceptanceMarkerError(f"cannot decode SDK manifest: {exc}") from exc
    if not lines or len(lines) > 1024:
        _fail("SDK manifest has an unsafe entry count")
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(lines, 1):
        match = SDK_LINE_RE.fullmatch(line)
        if match is None:
            _fail(f"malformed SDK manifest line {line_number}")
        relative = _safe_relative(match.group(2), "SDK manifest")
        key = relative.casefold()
        if key in seen:
            _fail(f"duplicate SDK manifest path: {relative}")
        seen.add(key)
        entries.append({"path": relative, "sha256": match.group(1)})
    return entries


def _validate_raster(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != RASTER_BINDING_FIELDS:
        _fail(f"{label} must contain the exact raster metadata fields")
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
        _fail(f"{label} violates the raster contract")
    return value


def _validate_report(
    report: Any,
    *,
    image_reference: str,
    fingerprint: str,
    revision: str,
    baseline: Any,
    baseline_sha256: str,
    sdk_manifest_sha256: str,
    sdk_files: list[dict[str, str]],
) -> None:
    if not isinstance(report, dict) or set(report) != REPORT_FIELDS:
        _fail("retained acceptance report has an unexpected schema")
    if (
        report.get("schema_version") != 1
        or report.get("criterion")
        != "private-source-and-sdk-plus-trusted-local-image-and-recorded-linux-pixels"
        or report.get("passed") is not True
        or report.get("errors") != []
        or report.get("expected_artifact_label") != "linux"
        or report.get("manifest")
        != {"file": "baseline-v1.json", "sha256": baseline_sha256}
    ):
        _fail("retained acceptance report is not an exact unqualified local-image pass")

    expected_engine = {
        "identity_mode": "trusted-local-image",
        "docker_image_id": image_reference,
        "docker_source_fingerprint": fingerprint,
        "expected_source_fingerprint": fingerprint,
    }
    if report.get("candidate_engine") != expected_engine:
        _fail("retained report does not bind this exact image and source fingerprint")

    if not isinstance(baseline, dict) or baseline.get("schema_version") != 1:
        _fail("embedded fixture baseline is malformed")
    engines = baseline.get("engines")
    expected_sdk = (
        engines.get("nikon_sdk_manifest_sha256") if isinstance(engines, dict) else None
    )
    if expected_sdk != sdk_manifest_sha256:
        _fail("embedded baseline and SDK manifest do not agree")
    if report.get("sdk_manifest") != {
        "file": "nikon-sdk-v1.46.sha256",
        "sha256": sdk_manifest_sha256,
        "expected_sha256": sdk_manifest_sha256,
        "checked_files": sdk_files,
    }:
        _fail("retained report does not bind every embedded SDK fixture hash")

    inspect = report.get("docker_inspect")
    if (
        not isinstance(inspect, dict)
        or set(inspect)
        != {"file", "sha256", "platform", "revision", "render_jobs"}
        or inspect.get("platform") != "linux/amd64"
        or inspect.get("revision") != revision
        or inspect.get("render_jobs") != 1
        or not isinstance(inspect.get("file"), str)
        or not isinstance(inspect.get("sha256"), str)
        or not SHA256_RE.fullmatch(inspect["sha256"])
    ):
        _fail("retained report does not bind this exact image revision and platform")

    images = baseline.get("images")
    if not isinstance(images, list) or not images or len(images) > 64:
        _fail("embedded fixture baseline has an invalid image set")
    expected_sources: list[dict[str, str]] = []
    expected_outputs: dict[str, dict[str, Any]] = {}
    for entry in images:
        if not isinstance(entry, dict):
            _fail("embedded fixture baseline contains a malformed image")
        name = entry.get("name")
        source = _safe_relative(entry.get("source_nef"), "source fixture")
        source_sha = entry.get("source_sha256")
        output = _safe_relative(entry.get("tiff"), "TIFF fixture")
        artifacts = entry.get("artifacts")
        linux = artifacts.get("linux") if isinstance(artifacts, dict) else None
        raster = _validate_raster(entry.get("raster"), f"baseline raster for {name!r}")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(source_sha, str)
            or not SHA256_RE.fullmatch(source_sha)
            or not isinstance(linux, dict)
            or set(linux) != {"pixel_sha256", "icc_sha256"}
            or not isinstance(linux.get("pixel_sha256"), str)
            or not SHA256_RE.fullmatch(linux["pixel_sha256"])
            or not isinstance(linux.get("icc_sha256"), str)
            or not SHA256_RE.fullmatch(linux["icc_sha256"])
        ):
            _fail("embedded fixture baseline contains malformed image evidence")
        output_key = output.casefold()
        if output_key in expected_outputs:
            _fail(f"embedded fixture baseline contains duplicate TIFF path: {output}")
        expected_sources.append({"name": name, "path": source, "sha256": source_sha})
        expected_outputs[output_key] = {
            "path": output,
            "pixel_sha256": linux["pixel_sha256"],
            "icc_sha256": linux["icc_sha256"],
            "raster": raster,
        }
    if report.get("checked_sources") != expected_sources:
        _fail("retained report does not bind every embedded source-NEF fixture hash")

    outputs = report.get("checked_outputs")
    if not isinstance(outputs, list) or len(outputs) != len(expected_outputs):
        _fail("retained report does not cover the exact TIFF fixture set")
    seen_outputs: set[str] = set()
    for output in outputs:
        if not isinstance(output, dict) or set(output) != OUTPUT_FIELDS:
            _fail("retained report contains malformed TIFF evidence")
        relative = _safe_relative(output.get("path"), "TIFF evidence")
        key = relative.casefold()
        expected = expected_outputs.get(key)
        if expected is None or key in seen_outputs:
            _fail(f"retained report contains unexpected TIFF evidence: {relative}")
        seen_outputs.add(key)
        expected_raster = expected["raster"]
        dtype = output.get("dtype")
        shape = output.get("shape_y_x_rgb")
        if (
            output.get("label") != f"candidate:{relative}"
            or output.get("pixel_sha256") != expected["pixel_sha256"]
            or output.get("icc_sha256") != expected["icc_sha256"]
            or output.get("contract_errors") != []
            or output.get("primary_series") != 0
            or output.get("ignored_series") != 0
            or output.get("source_axes") != "YXS"
            or output.get("bits_per_sample")
            != ([8, 8, 8] if dtype == "uint8" else [16, 16, 16])
            or any(
                output.get(field) != expected_raster[field]
                for field in RASTER_BINDING_FIELDS
            )
            or not isinstance(shape, list)
            or len(shape) != 3
            or shape[2] != 3
            or any(
                not isinstance(item, int) or isinstance(item, bool) or item <= 0
                for item in shape
            )
            or not isinstance(output.get("file_size"), int)
            or isinstance(output.get("file_size"), bool)
            or not 0 < output["file_size"] <= GIB
            or not isinstance(output.get("file_sha256"), str)
            or not SHA256_RE.fullmatch(output["file_sha256"])
        ):
            _fail(f"retained TIFF evidence is not a strict passing artifact: {relative}")

    inspection = report.get("sandboxed_tiff_inspection")
    if (
        not isinstance(inspection, dict)
        or set(inspection) != {"file", "sha256", "output_count"}
        or inspection.get("output_count") != len(expected_outputs)
        or not isinstance(inspection.get("file"), str)
        or not isinstance(inspection.get("sha256"), str)
        or not SHA256_RE.fullmatch(inspection["sha256"])
    ):
        _fail("retained report lacks complete sandboxed TIFF inspection evidence")


def validate_runtime_acceptance(
    report_path: Path,
    *,
    image_reference: str,
    identity_path: Path = IDENTITY_PATH,
    baseline_path: Path = BASELINE_PATH,
    sdk_manifest_path: Path = SDK_MANIFEST_PATH,
    expected_owner: int = 0,
) -> None:
    """Validate a report against immutable evidence shipped by this image."""
    if not IMAGE_RE.fullmatch(image_reference):
        _fail("production image reference must be an exact local sha256 image ID")
    identity_payload = _read_trusted_file(
        identity_path,
        expected_owner=expected_owner,
        maximum_bytes=4096,
    )
    baseline_payload = _read_trusted_file(
        baseline_path,
        expected_owner=expected_owner,
        maximum_bytes=MAX_JSON_BYTES,
    )
    sdk_payload = _read_trusted_file(
        sdk_manifest_path,
        expected_owner=expected_owner,
        maximum_bytes=MAX_MANIFEST_BYTES,
    )
    report_payload = _read_trusted_file(
        report_path,
        expected_owner=expected_owner,
        maximum_bytes=MAX_JSON_BYTES,
    )

    identity = _load_json_bytes(identity_payload, "embedded source identity")
    if (
        not isinstance(identity, dict)
        or set(identity)
        != {"schema_version", "source_fingerprint", "source_revision"}
        or identity.get("schema_version") != 1
        or not isinstance(identity.get("source_fingerprint"), str)
        or not SHA256_RE.fullmatch(identity["source_fingerprint"])
        or not isinstance(identity.get("source_revision"), str)
        or not COMMIT_RE.fullmatch(identity["source_revision"])
    ):
        _fail("embedded source identity is malformed")

    _validate_report(
        _load_json_bytes(report_payload, "retained acceptance report"),
        image_reference=image_reference,
        fingerprint=identity["source_fingerprint"],
        revision=identity["source_revision"],
        baseline=_load_json_bytes(baseline_payload, "embedded fixture baseline"),
        baseline_sha256=_sha256(baseline_payload),
        sdk_manifest_sha256=_sha256(sdk_payload),
        sdk_files=_parse_sdk_manifest(sdk_payload),
    )


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        _fail("runtime acceptance validator accepts no command-line overrides")
    validate_runtime_acceptance(
        REPORT_PATH,
        image_reference=os.environ.get("NEF_WATCH_IMAGE_REFERENCE", ""),
    )
    print("runtime acceptance evidence: PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AcceptanceMarkerError as exc:
        print(f"runtime acceptance evidence failed: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_CONFIG)
