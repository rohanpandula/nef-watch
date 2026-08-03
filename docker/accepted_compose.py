#!/usr/bin/env python3
"""Run production Compose only for the exact, retained, accepted image.

The reviewed tree, build state, acceptance report, Docker image, effective
Compose model, resource limits, and storage topology are re-checked on every
command that can create or start a container.  A small emergency command set
remains available so a broken deployment can always be stopped and inspected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn


EXIT_CONFIG = 78
GIB = 1024**3
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_DOTENV_BYTES = 64 * 1024
MAX_REVIEWED_TREE_ENTRIES = 8192
IMAGE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
DOTENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
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

MIN_MEMORY_BYTES = 2 * GIB
MAX_MEMORY_BYTES = 16 * GIB
MIN_CPUS = Decimal("1")
MAX_CPUS = Decimal("16")
MIN_PIDS = 128
MAX_PIDS = 1024
MIN_TEMP_CAPACITY_BYTES = 8 * GIB
MAX_TEMP_CAPACITY_BYTES = 64 * GIB
MIN_TEMP_FREE_BYTES = 8 * GIB
MIN_RENDER_FILE_SIZE_MIB = 768
MAX_RENDER_FILE_SIZE_MIB = 2048
MIN_WATCH_INTERVAL_SECONDS = Decimal("0.1")
MAX_WATCH_INTERVAL_SECONDS = Decimal("840")
MIN_HEALTH_MAX_AGE_SECONDS = Decimal("60")
MAX_HEALTH_MAX_AGE_SECONDS = Decimal("900")
HEALTH_INTERVAL_MARGIN_SECONDS = Decimal("60")

START_COMMANDS = {"up"}
EMERGENCY_COMMANDS = {
    "config",
    "down",
    "events",
    "images",
    "kill",
    "logs",
    "ls",
    "pause",
    "port",
    "ps",
    "rm",
    "stop",
    "top",
    "version",
    "wait",
}
FORBIDDEN_COMMANDS = {
    "alpha",
    "bridge",
    "build",
    "cp",
    "create",
    "exec",
    "publish",
    "pull",
    "push",
    "restart",
    "run",
    "start",
    "unpause",
    "watch",
}
FORBIDDEN_ARGUMENTS = {
    "--ansi",
    "--build",
    "--compatibility",
    "--dry-run",
    "--env-file",
    "--file",
    "--parallel",
    "--profile",
    "--progress",
    "--project-directory",
    "--project-name",
    "--pull",
    "-p",
}


class AcceptanceError(RuntimeError):
    """Production startup is not bound to passing acceptance evidence."""


def _fail(message: str) -> NoReturn:
    raise AcceptanceError(message)


def _sha256(path: Path, *, maximum_bytes: int = MAX_JSON_BYTES) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AcceptanceError(f"cannot open trusted file {path}: {exc}") from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum_bytes:
            _fail(f"trusted file is not a bounded regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        after = os.fstat(descriptor)
        def identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
            return (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )
        if identity(before) != identity(after):
            _fail(f"trusted file changed while it was hashed: {path}")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AcceptanceError(f"JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _load_json(path: Path, *, maximum_bytes: int = MAX_JSON_BYTES) -> Any:
    try:
        metadata = path.stat()
        if metadata.st_size > maximum_bytes:
            _fail(f"JSON exceeds the {maximum_bytes}-byte limit: {path}")
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_fail(f"non-finite JSON number {value}")),
        )
    except AcceptanceError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"cannot read trusted JSON {path}: {exc}") from exc


def _validate_trusted_directory(path: Path, *, anchor: Path | None = None) -> os.stat_result:
    try:
        if not path.is_absolute() or path != path.resolve(strict=True):
            _fail(f"trusted directory is not an absolute canonical path: {path}")
        metadata = path.lstat()
    except OSError as exc:
        raise AcceptanceError(f"trusted directory is missing: {path}: {exc}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        _fail(f"trusted path is not a real directory: {path}")
    if metadata.st_uid != 0 or metadata.st_mode & 0o022:
        _fail(f"trusted directory must be root-owned and not group/world writable: {path}")
    if anchor is not None:
        try:
            relative = path.relative_to(anchor)
        except ValueError as exc:
            raise AcceptanceError(f"trusted directory escapes {anchor}: {path}") from exc
        current = anchor
        anchor_device = anchor.lstat().st_dev
        for component in relative.parts:
            current /= component
            entry = current.lstat()
            if (
                not stat.S_ISDIR(entry.st_mode)
                or stat.S_ISLNK(entry.st_mode)
                or entry.st_uid != 0
                or entry.st_mode & 0o022
                or entry.st_dev != anchor_device
            ):
                _fail(f"unsafe trusted-directory component: {current}")
    return metadata


def _validate_trusted_file(
    path: Path,
    *,
    owner: int = 0,
    exact_mode: int | None = None,
    same_device: int | None = None,
    maximum_bytes: int = MAX_JSON_BYTES,
) -> os.stat_result:
    try:
        requested = path.absolute()
        metadata = requested.lstat()
        if requested != requested.resolve(strict=True):
            _fail(f"trusted file is not canonical: {path}")
    except OSError as exc:
        raise AcceptanceError(f"trusted file is missing: {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        _fail(f"trusted path is not a regular file: {path}")
    if metadata.st_uid != owner or metadata.st_nlink != 1:
        _fail(f"trusted file has unsafe ownership or link count: {path}")
    if exact_mode is not None and stat.S_IMODE(metadata.st_mode) != exact_mode:
        _fail(f"trusted file must have mode {exact_mode:04o}: {path}")
    if exact_mode is None and metadata.st_mode & 0o022:
        _fail(f"trusted file is group/world writable: {path}")
    if same_device is not None and metadata.st_dev != same_device:
        _fail(f"trusted file is on the wrong filesystem: {path}")
    if metadata.st_size > maximum_bytes:
        _fail(f"trusted file exceeds the {maximum_bytes}-byte limit: {path}")
    return metadata


def _validate_sealed_tree(root: Path) -> None:
    """Require every reviewed-tree entry to remain root-owned and non-writable."""
    root_device = root.lstat().st_dev
    seen = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = os.scandir(directory)
        except OSError as exc:
            raise AcceptanceError(f"cannot enumerate sealed reviewed tree: {exc}") from exc
        with entries:
            for entry in entries:
                seen += 1
                if seen > MAX_REVIEWED_TREE_ENTRIES:
                    _fail("sealed reviewed tree exceeds the bounded entry count")
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise AcceptanceError(
                        f"cannot inspect sealed reviewed-tree entry {entry.path}: {exc}"
                    ) from exc
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or metadata.st_uid != 0
                    or metadata.st_mode & 0o022
                    or metadata.st_dev != root_device
                ):
                    _fail(f"unsafe ownership, mode, link, or device in reviewed tree: {entry.path}")
                if stat.S_ISDIR(metadata.st_mode):
                    stack.append(Path(entry.path))
                elif not stat.S_ISREG(metadata.st_mode):
                    _fail(f"non-file entry in sealed reviewed tree: {entry.path}")


def parse_build_state(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise AcceptanceError(f"cannot read exact-build state: {exc}") from exc
    expected = {
        "IMAGE_ID",
        "REVIEWED_COMMIT",
        "EXPECTED_SOURCE_FINGERPRINT",
        "REVIEWED_TREE",
    }
    values: dict[str, str] = {}
    for line in lines:
        if line.count("=") != 1:
            _fail("exact-build state contains a malformed line")
        key, value = line.split("=", 1)
        if key not in expected or key in values or not value:
            _fail(f"unexpected or duplicate exact-build state field: {key!r}")
        values[key] = value
    if set(values) != expected:
        _fail("exact-build state is incomplete")
    if not IMAGE_RE.fullmatch(values["IMAGE_ID"]):
        _fail("exact-build state contains a malformed image ID")
    if not COMMIT_RE.fullmatch(values["REVIEWED_COMMIT"]):
        _fail("exact-build state contains a malformed reviewed commit")
    if not SHA256_RE.fullmatch(values["EXPECTED_SOURCE_FINGERPRINT"]):
        _fail("exact-build state contains a malformed source fingerprint")
    return values


def parse_dotenv(path: Path) -> dict[str, str]:
    try:
        if path.stat().st_size > MAX_DOTENV_BYTES:
            _fail("docker/.env is unexpectedly large")
        lines = path.read_text(encoding="utf-8").splitlines()
    except AcceptanceError:
        raise
    except (OSError, UnicodeError) as exc:
        raise AcceptanceError(f"cannot read docker/.env: {exc}") from exc
    result: dict[str, str] = {}
    for line_number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            _fail(f"docker/.env line {line_number} is malformed")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not DOTENV_KEY_RE.fullmatch(key) or key in result:
            _fail(f"docker/.env line {line_number} has an invalid or duplicate key")
        result[key] = value
    return result


def _parse_memory(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([bBkKmMgGtT](?:[iI]?[bB])?)?", value)
    if match is None:
        _fail("MEMORY_LIMIT must be a positive finite byte quantity such as 4g")
    suffix = (match.group(2) or "b").casefold()
    power = {"b": 0, "k": 1, "kb": 1, "kib": 1, "m": 2, "mb": 2, "mib": 2,
             "g": 3, "gb": 3, "gib": 3, "t": 4, "tb": 4, "tib": 4}[suffix]
    return int(match.group(1)) * 1024**power


def validate_declared_resources(
    dotenv: dict[str, str], image_id: str, acceptance_report: str
) -> dict[str, int | Decimal]:
    required = {
        "ACCEPTANCE_REPORT",
        "IMAGE",
        "MEMORY_LIMIT",
        "CPU_LIMIT",
        "PIDS_LIMIT",
        "RENDER_FILE_SIZE_MIB",
        "TEMP_CAPACITY_BYTES",
        "TEMP_MIN_FREE_BYTES",
        "INTERVAL",
        "HEALTH_MAX_AGE_SECONDS",
    }
    missing = sorted(required - set(dotenv))
    if missing:
        _fail("docker/.env is missing required production fields: " + ", ".join(missing))
    if dotenv["IMAGE"] != image_id:
        _fail("docker/.env IMAGE is not the exact accepted image ID")
    if dotenv["ACCEPTANCE_REPORT"] != acceptance_report:
        _fail("docker/.env ACCEPTANCE_REPORT is not the exact retained report")
    memory = _parse_memory(dotenv["MEMORY_LIMIT"])
    try:
        cpus = Decimal(dotenv["CPU_LIMIT"])
        interval = Decimal(dotenv["INTERVAL"])
        health_max_age = Decimal(dotenv["HEALTH_MAX_AGE_SECONDS"])
    except InvalidOperation as exc:
        raise AcceptanceError(
            "CPU_LIMIT, INTERVAL, and HEALTH_MAX_AGE_SECONDS must be decimals"
        ) from exc
    if not cpus.is_finite() or not interval.is_finite() or not health_max_age.is_finite():
        _fail("CPU_LIMIT, INTERVAL, and HEALTH_MAX_AGE_SECONDS must be finite")
    try:
        pids = int(dotenv["PIDS_LIMIT"], 10)
        render_file_size_mib = int(dotenv["RENDER_FILE_SIZE_MIB"], 10)
        temp_capacity = int(dotenv["TEMP_CAPACITY_BYTES"], 10)
        temp_free = int(dotenv["TEMP_MIN_FREE_BYTES"], 10)
    except ValueError as exc:
        raise AcceptanceError(
            "PID, render-file, and temporary-storage limits must be base-10 integers"
        ) from exc
    if not MIN_MEMORY_BYTES <= memory <= MAX_MEMORY_BYTES:
        _fail("MEMORY_LIMIT must be between 2 GiB and 16 GiB")
    if not MIN_CPUS <= cpus <= MAX_CPUS:
        _fail("CPU_LIMIT must be between 1 and 16 CPUs")
    if not MIN_PIDS <= pids <= MAX_PIDS:
        _fail("PIDS_LIMIT must be between 128 and 1024")
    if not MIN_RENDER_FILE_SIZE_MIB <= render_file_size_mib <= MAX_RENDER_FILE_SIZE_MIB:
        _fail("RENDER_FILE_SIZE_MIB must be between 768 and 2048")
    if not MIN_TEMP_CAPACITY_BYTES <= temp_capacity <= MAX_TEMP_CAPACITY_BYTES:
        _fail("TEMP_CAPACITY_BYTES must be between 8 GiB and 64 GiB")
    if not MIN_TEMP_FREE_BYTES <= temp_free <= temp_capacity:
        _fail("TEMP_MIN_FREE_BYTES must be at least 8 GiB and no larger than capacity")
    if not MIN_WATCH_INTERVAL_SECONDS <= interval <= MAX_WATCH_INTERVAL_SECONDS:
        _fail("INTERVAL must be between 0.1 and 840 seconds in production")
    if not MIN_HEALTH_MAX_AGE_SECONDS <= health_max_age <= MAX_HEALTH_MAX_AGE_SECONDS:
        _fail("HEALTH_MAX_AGE_SECONDS must be between 60 and 900 seconds")
    if health_max_age < interval + HEALTH_INTERVAL_MARGIN_SECONDS:
        _fail("HEALTH_MAX_AGE_SECONDS must exceed INTERVAL by at least 60 seconds")
    return {
        "memory": memory,
        "cpus": cpus,
        "pids": pids,
        "render_file_size_mib": render_file_size_mib,
        "temp_capacity": temp_capacity,
        "temp_free": temp_free,
        "interval": interval,
        "health_max_age": health_max_age,
    }


def _safe_manifest_path(value: str, label: str) -> str:
    relative = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or relative.is_absolute()
        or any(part in ("", ".", "..") for part in relative.parts)
    ):
        _fail(f"unsafe {label} path: {value!r}")
    return relative.as_posix()


def _expected_raster_binding(value: Any, label: str) -> dict[str, Any]:
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
        _fail(f"{label} is malformed or violates the raster contract")
    return value


def parse_sdk_manifest(path: Path) -> list[dict[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise AcceptanceError(f"cannot read SDK manifest: {exc}") from exc
    if not lines or len(lines) > 1024:
        _fail("SDK manifest has an unsafe entry count")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(lines, 1):
        match = SDK_LINE_RE.fullmatch(line)
        if match is None:
            _fail(f"malformed SDK manifest line {line_number}")
        relative = _safe_manifest_path(match.group(2), "SDK manifest")
        key = relative.casefold()
        if key in seen:
            _fail(f"duplicate SDK manifest path: {relative}")
        seen.add(key)
        result.append({"path": relative, "sha256": match.group(1)})
    return result


def validate_sdk_directory(root: Path, entries: list[dict[str, str]]) -> None:
    """Re-hash the production SDK source against the accepted manifest."""
    try:
        requested_root = root.expanduser().absolute()
        resolved_root = requested_root.resolve(strict=True)
    except OSError as exc:
        raise AcceptanceError(f"production SDK directory is unavailable: {exc}") from exc
    if requested_root != resolved_root or not resolved_root.is_dir():
        _fail("production SDK path must be a canonical directory without symbolic links")
    total = 0
    for entry in entries:
        relative = PurePosixPath(entry["path"])
        candidate = resolved_root
        try:
            for component in relative.parts:
                candidate /= component
                metadata = candidate.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    _fail(f"production SDK path contains a symbolic link: {relative}")
        except OSError as exc:
            raise AcceptanceError(f"production SDK fixture is missing: {relative}: {exc}") from exc
        metadata = candidate.stat()
        if not stat.S_ISREG(metadata.st_mode):
            _fail(f"production SDK fixture is not a regular file: {relative}")
        total += metadata.st_size
        if total > 2 * GIB:
            _fail("production SDK fixtures exceed the 2 GiB aggregate limit")
        actual = _sha256(candidate, maximum_bytes=512 * 1024 * 1024)
        if actual != entry["sha256"]:
            _fail(f"production SDK fixture hash changed after acceptance: {relative}")


def validate_acceptance_report(
    report: Any,
    *,
    image_id: str,
    fingerprint: str,
    revision: str,
    baseline: dict[str, Any],
    baseline_sha256: str,
    sdk_manifest_sha256: str,
    sdk_files: list[dict[str, str]],
) -> None:
    if not isinstance(report, dict) or set(report) != REPORT_FIELDS:
        _fail("retained acceptance report has an unexpected schema")
    if report.get("schema_version") != 1:
        _fail("retained acceptance report has an unsupported schema")
    if report.get("criterion") != "private-source-and-sdk-plus-trusted-local-image-and-recorded-linux-pixels":
        _fail("retained report was not produced by the private local-image gate")
    if report.get("passed") is not True or report.get("errors") != []:
        _fail("retained acceptance report is not an unqualified pass")
    if report.get("expected_artifact_label") != "linux":
        _fail("retained report does not bind the Linux artifact baseline")
    if report.get("manifest") != {"file": "baseline-v1.json", "sha256": baseline_sha256}:
        _fail("retained report does not bind the current fixture baseline bytes")

    engine = report.get("candidate_engine")
    expected_engine = {
        "identity_mode": "trusted-local-image",
        "docker_image_id": image_id,
        "docker_source_fingerprint": fingerprint,
        "expected_source_fingerprint": fingerprint,
    }
    if engine != expected_engine:
        _fail("retained report does not bind the exact image and source fingerprint")

    engines = baseline.get("engines")
    expected_sdk = engines.get("nikon_sdk_manifest_sha256") if isinstance(engines, dict) else None
    if expected_sdk != sdk_manifest_sha256:
        _fail("current fixture baseline and SDK manifest no longer agree")
    sdk = report.get("sdk_manifest")
    if sdk != {
        "file": "nikon-sdk-v1.46.sha256",
        "sha256": sdk_manifest_sha256,
        "expected_sha256": sdk_manifest_sha256,
        "checked_files": sdk_files,
    }:
        _fail("retained report does not bind every current SDK fixture hash")

    inspect = report.get("docker_inspect")
    if (
        not isinstance(inspect, dict)
        or set(inspect) != {"file", "sha256", "platform", "revision", "render_jobs"}
        or inspect.get("platform") != "linux/amd64"
        or inspect.get("revision") != revision
        or inspect.get("render_jobs") != 1
        or not isinstance(inspect.get("file"), str)
        or not isinstance(inspect.get("sha256"), str)
        or not SHA256_RE.fullmatch(inspect["sha256"])
    ):
        _fail("retained report has invalid exact-image inspection evidence")

    images = baseline.get("images")
    if not isinstance(images, list) or not images or len(images) > 64:
        _fail("current fixture baseline has an invalid image set")
    expected_sources: list[dict[str, str]] = []
    expected_outputs: dict[str, dict[str, Any]] = {}
    for entry in images:
        if not isinstance(entry, dict):
            _fail("current fixture baseline contains a malformed image")
        name = entry.get("name")
        source = entry.get("source_nef")
        source_sha = entry.get("source_sha256")
        tiff = entry.get("tiff")
        artifacts = entry.get("artifacts")
        linux = artifacts.get("linux") if isinstance(artifacts, dict) else None
        raster = _expected_raster_binding(entry.get("raster"), f"{name}: raster")
        if (
            not isinstance(name, str)
            or not isinstance(source, str)
            or not isinstance(tiff, str)
            or not isinstance(source_sha, str)
            or not SHA256_RE.fullmatch(source_sha)
            or not isinstance(linux, dict)
            or not isinstance(linux.get("pixel_sha256"), str)
            or not SHA256_RE.fullmatch(linux["pixel_sha256"])
            or not isinstance(linux.get("icc_sha256"), str)
            or not SHA256_RE.fullmatch(linux["icc_sha256"])
        ):
            _fail(f"current fixture baseline entry is malformed: {name!r}")
        source = _safe_manifest_path(source, "source fixture")
        tiff = _safe_manifest_path(tiff, "TIFF fixture")
        expected_sources.append({"name": name, "path": source, "sha256": source_sha})
        if tiff.casefold() in expected_outputs:
            _fail(f"current fixture baseline duplicates TIFF path {tiff}")
        expected_outputs[tiff.casefold()] = {
            "pixel_sha256": linux["pixel_sha256"],
            "icc_sha256": linux["icc_sha256"],
            "raster": raster,
        }
    if report.get("checked_sources") != expected_sources:
        _fail("retained report does not bind every source NEF fixture hash")

    outputs = report.get("checked_outputs")
    if not isinstance(outputs, list) or len(outputs) != len(expected_outputs):
        _fail("retained report does not cover the exact TIFF fixture set")
    seen_outputs: set[str] = set()
    for output in outputs:
        if not isinstance(output, dict) or set(output) != OUTPUT_FIELDS:
            _fail("retained report contains malformed TIFF evidence")
        relative = _safe_manifest_path(output["path"], "TIFF evidence")
        key = relative.casefold()
        expected = expected_outputs.get(key)
        if expected is None or key in seen_outputs:
            _fail(f"retained report contains unexpected TIFF evidence: {relative}")
        seen_outputs.add(key)
        expected_raster = expected["raster"]
        dtype = output.get("dtype")
        expected_bits = [8, 8, 8] if dtype == "uint8" else [16, 16, 16]
        shape = output.get("shape_y_x_rgb")
        if (
            output.get("label") != f"candidate:{relative}"
            or output.get("pixel_sha256") != expected["pixel_sha256"]
            or output.get("icc_sha256") != expected["icc_sha256"]
            or output.get("contract_errors") != []
            or output.get("primary_series") != 0
            or output.get("ignored_series") != 0
            or output.get("source_axes") != "YXS"
            or output.get("photometric") != "RGB"
            or output.get("orientation") != 1
            or dtype not in {"uint8", "uint16"}
            or output.get("bits_per_sample") != expected_bits
            or any(
                output.get(field) != expected_raster[field]
                for field in RASTER_BINDING_FIELDS
            )
            or not isinstance(shape, list)
            or len(shape) != 3
            or shape[2] != 3
            or any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in shape)
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


def validate_image_inspect(payload: Any, *, image_id: str, fingerprint: str, revision: str) -> None:
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        _fail("Docker returned malformed image-inspect evidence")
    image = payload[0]
    config = image.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    environment = config.get("Env") if isinstance(config, dict) else None
    render_jobs = (
        [item for item in environment if isinstance(item, str) and item.startswith("NEF_WATCH_RENDER_JOBS=")]
        if isinstance(environment, list)
        else []
    )
    if (
        image.get("Id") != image_id
        or image.get("Os") != "linux"
        or image.get("Architecture") != "amd64"
        or not isinstance(labels, dict)
        or labels.get("io.nef-watch.source.fingerprint") != fingerprint
        or labels.get("org.opencontainers.image.revision") != revision
        or labels.get("io.nef-watch.nikon-sdk.bundled") != "false"
        or render_jobs != ["NEF_WATCH_RENDER_JOBS=1"]
    ):
        _fail("the current local Docker image no longer matches accepted identity")


def _numeric_config(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        _fail(f"effective Compose {field} is absent or non-numeric")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise AcceptanceError(f"effective Compose {field} is malformed") from exc
    if not number.is_finite() or number <= 0:
        _fail(f"effective Compose {field} must be positive and finite")
    return number


def _volume_map(service: dict[str, Any]) -> dict[str, dict[str, Any]]:
    volumes = service.get("volumes")
    if not isinstance(volumes, list) or any(not isinstance(item, dict) for item in volumes):
        _fail("effective Compose volumes are malformed")
    result: dict[str, dict[str, Any]] = {}
    for volume in volumes:
        target = volume.get("target")
        if not isinstance(target, str) or target in result:
            _fail("effective Compose contains a duplicate or malformed volume target")
        result[target] = volume
    return result


def _command_option(service: dict[str, Any], option: str) -> str:
    command = service.get("command")
    if not isinstance(command, list) or any(not isinstance(item, str) for item in command):
        _fail("effective Compose watcher command is malformed")
    positions = [index for index, item in enumerate(command) if item == option]
    if len(positions) != 1 or positions[0] + 1 >= len(command):
        _fail(f"effective Compose watcher command must set {option} exactly once")
    return command[positions[0] + 1]


def validate_compose_config(
    model: Any,
    *,
    image_id: str,
    declared: dict[str, int | Decimal],
    acceptance_report: str,
) -> dict[str, str]:
    if not isinstance(model, dict) or model.get("name") != "docker":
        _fail("effective Compose project identity is not fixed to 'docker'")
    services = model.get("services")
    if not isinstance(services, dict) or set(services) != {"nef-watch", "nef-watch-init"}:
        _fail("effective Compose service set differs from the reviewed deployment")
    for name, service in services.items():
        if not isinstance(service, dict):
            _fail(f"effective Compose service {name} is malformed")
        memory = _numeric_config(service.get("mem_limit"), f"{name}.mem_limit")
        swap = _numeric_config(service.get("memswap_limit"), f"{name}.memswap_limit")
        cpus = _numeric_config(service.get("cpus"), f"{name}.cpus")
        pids = _numeric_config(service.get("pids_limit"), f"{name}.pids_limit")
        environment = service.get("environment")
        ulimits = service.get("ulimits")
        if (
            memory != Decimal(declared["memory"])
            or swap != memory
            or cpus != declared["cpus"]
            or pids != Decimal(declared["pids"])
            or service.get("image") != image_id
            or service.get("platform") != "linux/amd64"
            or service.get("pull_policy") != "never"
            or service.get("network_mode") != "none"
            or service.get("read_only") is not True
            or service.get("privileged") not in (None, False)
            or service.get("cap_drop") != ["ALL"]
            or service.get("security_opt") != ["no-new-privileges:true"]
            or ulimits
            != {
                "fsize": {"hard": 2147483648, "soft": 2147483648},
                "nofile": {"hard": 512, "soft": 512},
            }
            or not isinstance(environment, dict)
            or environment.get("NEF_WATCH_IMAGE_REFERENCE") != image_id
            or environment.get("NEF_WATCH_REQUIRE_ACCEPTANCE_REPORT") != "1"
            or environment.get("NEF_WATCH_PRE_ACCEPTANCE_FIXTURE_MODE") != "0"
            or environment.get("NEF_WATCH_REQUIRE_LANDLOCK") != "1"
            or environment.get("NEF_WATCH_RENDER_JOBS") != "1"
            or _numeric_config(
                environment.get("NEF_WATCH_RENDER_FILE_SIZE_MIB"),
                f"{name}.NEF_WATCH_RENDER_FILE_SIZE_MIB",
            )
            != Decimal(declared["render_file_size_mib"])
            or _numeric_config(
                environment.get("NEF_WATCH_INTERVAL_SECONDS"),
                f"{name}.NEF_WATCH_INTERVAL_SECONDS",
            )
            != declared["interval"]
            or _numeric_config(
                environment.get("NEF_WATCH_HEALTH_MAX_AGE"),
                f"{name}.NEF_WATCH_HEALTH_MAX_AGE",
            )
            != declared["health_max_age"]
            or environment.get("NEF_WATCH_TEMP_CAPACITY_BYTES") != str(declared["temp_capacity"])
            or environment.get("NEF_WATCH_TEMP_MIN_FREE_BYTES") != str(declared["temp_free"])
        ):
            _fail(f"effective Compose security or resource model changed for {name}")

    runtime = services["nef-watch"]
    initializer = services["nef-watch-init"]
    allowed_init_caps = {"CHOWN", "DAC_OVERRIDE", "FOWNER", "KILL", "SETGID", "SETPCAP", "SETUID"}
    if runtime.get("user") != "99:100" or runtime.get("cap_add") not in (None, []):
        _fail("long-running watcher is not fixed to unprivileged uid/gid with no capabilities")
    if _numeric_config(
        _command_option(runtime, "--interval"), "nef-watch.command.--interval"
    ) != declared["interval"]:
        _fail("effective Compose watcher interval differs from docker/.env")
    if initializer.get("user") not in (None, "") or set(initializer.get("cap_add", [])) != allowed_init_caps:
        _fail("one-shot initializer capability boundary changed")

    runtime_volumes = _volume_map(runtime)
    init_volumes = _volume_map(initializer)
    acceptance_target = "/run/nef-watch-acceptance.json"
    if set(runtime_volumes) != {
        "/input",
        "/output",
        "/work/nef-watch",
        "/var/lib/nef-watch",
        acceptance_target,
    }:
        _fail("long-running watcher mount set changed or gained SDK access")
    if set(init_volumes) != {
        "/input",
        "/output",
        "/nikon-sdk",
        "/work/nef-watch",
        "/var/lib/nef-watch",
        acceptance_target,
    }:
        _fail("initializer mount set changed")
    for target in ("/input", "/output", "/work/nef-watch", acceptance_target):
        if runtime_volumes[target].get("type") != "bind" or init_volumes[target].get("type") != "bind":
            _fail(f"{target} is no longer a host bind mount")
        if runtime_volumes[target].get("source") != init_volumes[target].get("source"):
            _fail(f"runtime and initializer disagree about {target}")
        if (
            runtime_volumes[target].get("bind") != {"create_host_path": False}
            or init_volumes[target].get("bind") != {"create_host_path": False}
        ):
            _fail(f"{target} may no longer auto-create its host bind source")
    if runtime_volumes["/input"].get("read_only") is not True:
        _fail("runtime input mount must be read-only")
    if init_volumes["/input"].get("read_only") is not True or init_volumes["/output"].get("read_only") is not True:
        _fail("initializer photo mounts must be read-only")
    sdk = init_volumes["/nikon-sdk"]
    if (
        sdk.get("type") != "bind"
        or sdk.get("read_only") is not True
        or sdk.get("bind") != {"create_host_path": False}
    ):
        _fail("initializer SDK mount must remain a read-only bind")
    report_mount = runtime_volumes[acceptance_target]
    if (
        report_mount.get("source") != acceptance_report
        or report_mount.get("read_only") is not True
        or init_volumes[acceptance_target].get("read_only") is not True
    ):
        _fail("runtime acceptance report must be the exact read-only retained file")
    for volumes in (runtime_volumes, init_volumes):
        state = volumes["/var/lib/nef-watch"]
        if state.get("type") != "volume" or state.get("source") != "nef-watch-state":
            _fail("persistent state must remain the dedicated named volume")
    return {
        "input": runtime_volumes["/input"]["source"],
        "output": runtime_volumes["/output"]["source"],
        "temp": runtime_volumes["/work/nef-watch"]["source"],
        "sdk": sdk["source"],
        "acceptance_report": report_mount["source"],
    }


def classify_command(arguments: list[str]) -> bool:
    """Return True when acceptance is mandatory; reject unsafe wrapper bypasses."""
    if not arguments:
        _fail("a Docker Compose command is required")
    if arguments[0] in {"--help", "--version"}:
        return False
    if arguments[0].startswith("-"):
        _fail("Compose global options are fixed by the accepted deployment wrapper")
    command = arguments[0]
    for argument in arguments:
        if (argument == "-f" and command != "logs") or argument in FORBIDDEN_ARGUMENTS or any(
            argument.startswith(prefix + "=") for prefix in FORBIDDEN_ARGUMENTS if prefix.startswith("--")
        ):
            _fail(f"Compose option is fixed or forbidden: {argument}")
    if command in FORBIDDEN_COMMANDS:
        _fail(f"Compose command is forbidden in production: {command}")
    if command in START_COMMANDS:
        if arguments not in (["up"], ["up", "-d"], ["up", "--detach"]):
            _fail("production up accepts only optional -d/--detach and always starts the full reviewed project")
        return True
    if command in EMERGENCY_COMMANDS:
        return False
    _fail(f"unrecognized Compose command is denied: {command}")


def command_touches_docker_daemon(arguments: list[str]) -> bool:
    """Return whether an already-classified Compose command reaches Docker."""
    if not arguments or arguments[0] in {"--help", "--version", "config", "version"}:
        return False
    return True


def _run_bounded(arguments: list[str], *, environment: dict[str, str], cwd: Path) -> str:
    try:
        result = subprocess.run(
            arguments,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AcceptanceError(f"trusted command failed to run: {arguments[0]}: {exc}") from exc
    if len(result.stdout) > MAX_JSON_BYTES or len(result.stderr) > MAX_JSON_BYTES:
        _fail(f"trusted command emitted more than {MAX_JSON_BYTES} bytes")
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        _fail(f"trusted command failed: {detail or result.returncode}")
    return result.stdout.decode("utf-8", "strict")


def _json_text(payload: str, label: str) -> Any:
    try:
        return json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_fail(f"non-finite {label} number {value}")),
        )
    except AcceptanceError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"{label} returned malformed JSON: {exc}") from exc


def _compose_prefix(tree: Path, trust_root: Path) -> tuple[list[str], dict[str, str], Path]:
    docker_dir = tree / "docker"
    environment = {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/root",
        "LC_ALL": "C",
        "LANG": "C",
        "DOCKER_CONFIG": str(trust_root / "docker-config"),
        "DOCKER_CONTEXT": "default",
        "DOCKER_HOST": "unix:///var/run/docker.sock",
        "COMPOSE_DISABLE_ENV_FILE": "1",
    }
    prefix = [
        "/usr/bin/docker",
        "compose",
        "--file",
        str(docker_dir / "compose.yaml"),
        "--env-file",
        str(docker_dir / ".env"),
        "--project-directory",
        str(docker_dir),
        "--project-name",
        "docker",
    ]
    return prefix, environment, docker_dir


def _validate_docker_boundary(environment: dict[str, str], cwd: Path) -> None:
    socket_path = Path("/var/run/docker.sock")
    try:
        metadata = socket_path.lstat()
    except OSError as exc:
        raise AcceptanceError(f"Docker socket is unavailable: {exc}") from exc
    if not stat.S_ISSOCK(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != 0:
        _fail("Docker socket must be a real root-owned Unix socket")
    context = _run_bounded(["/usr/bin/docker", "context", "show"], environment=environment, cwd=cwd).strip()
    if context != "default":
        _fail("Docker context must be exactly 'default'")
    endpoint = _run_bounded(
        [
            "/usr/bin/docker",
            "context",
            "inspect",
            "default",
            "--format",
            '{{(index .Endpoints "docker").Host}}',
        ],
        environment=environment,
        cwd=cwd,
    ).strip()
    if endpoint != "unix:///var/run/docker.sock":
        _fail("Docker default context is not the local Unix socket")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run reviewed Compose with an exact retained acceptance report."
    )
    parser.add_argument("--trust-root", type=Path, required=True)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="validate the complete production gate without running Compose",
    )
    parser.add_argument("compose_arguments", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    compose_arguments = args.compose_arguments
    if compose_arguments[:1] == ["--"]:
        compose_arguments = compose_arguments[1:]
    if args.verify_only:
        if compose_arguments:
            _fail("--verify-only cannot be combined with a Compose command")
        requires_acceptance = True
    else:
        requires_acceptance = classify_command(compose_arguments)
    if os.geteuid() != 0:
        _fail("the accepted production wrapper must run as root")

    script = Path(__file__).absolute()
    if script.is_symlink():
        _fail("accepted production wrapper must not be invoked through a symbolic link")
    reviewed_tree = script.parent.parent
    scratch = args.trust_root.absolute().parent
    scratch_metadata = _validate_trusted_directory(scratch)
    trust_root = args.trust_root.absolute()
    _validate_trusted_directory(trust_root, anchor=scratch)
    tree_metadata = _validate_trusted_directory(reviewed_tree, anchor=trust_root)
    docker_config = trust_root / "docker-config"
    _validate_trusted_directory(docker_config, anchor=trust_root)
    acceptance_reports = trust_root / "acceptance-reports"
    _validate_trusted_directory(acceptance_reports, anchor=trust_root)

    state_path = trust_root / "build-state.env"
    _validate_trusted_file(
        state_path,
        exact_mode=0o400,
        same_device=scratch_metadata.st_dev,
        maximum_bytes=4096,
    )
    state = parse_build_state(state_path)
    expected_tree = trust_root / "reviewed" / state["REVIEWED_COMMIT"]
    if reviewed_tree != expected_tree or state["REVIEWED_TREE"] != str(expected_tree):
        _fail("wrapper path is not the exact sealed reviewed tree in build-state.env")
    if tree_metadata.st_dev != scratch_metadata.st_dev:
        _fail("reviewed tree is not on the trusted scratch filesystem")
    _validate_sealed_tree(reviewed_tree)

    dotenv_path = reviewed_tree / "docker" / ".env"
    _validate_trusted_file(dotenv_path, exact_mode=0o600, maximum_bytes=MAX_DOTENV_BYTES)
    prefix, environment, docker_dir = _compose_prefix(reviewed_tree, trust_root)

    if not requires_acceptance:
        if command_touches_docker_daemon(compose_arguments):
            _validate_docker_boundary(environment, docker_dir)
        os.execve(prefix[0], [*prefix, *compose_arguments], environment)

    report_path = acceptance_reports / f"{state['IMAGE_ID'][7:]}.acceptance.json"
    dotenv = parse_dotenv(dotenv_path)
    declared = validate_declared_resources(
        dotenv, state["IMAGE_ID"], str(report_path)
    )

    fingerprint_output = _run_bounded(
        [
            sys.executable,
            "-I",
            str(reviewed_tree / "docker" / "source_fingerprint.py"),
            "--root",
            str(reviewed_tree),
        ],
        environment=environment,
        cwd=docker_dir,
    ).strip()
    if fingerprint_output != state["EXPECTED_SOURCE_FINGERPRINT"]:
        _fail("sealed source tree no longer matches the accepted source fingerprint")

    baseline_path = reviewed_tree / "validation" / "baseline-v1.json"
    sdk_manifest_path = reviewed_tree / "docker" / "nikon-sdk-v1.46.sha256"
    baseline = _load_json(baseline_path)
    if not isinstance(baseline, dict):
        _fail("fixture baseline is malformed")
    baseline_sha = _sha256(baseline_path)
    sdk_sha = _sha256(sdk_manifest_path, maximum_bytes=1024 * 1024)
    sdk_files = parse_sdk_manifest(sdk_manifest_path)
    _validate_trusted_file(
        report_path,
        exact_mode=0o444,
        same_device=scratch_metadata.st_dev,
        maximum_bytes=MAX_JSON_BYTES,
    )
    report = _load_json(report_path)
    validate_acceptance_report(
        report,
        image_id=state["IMAGE_ID"],
        fingerprint=state["EXPECTED_SOURCE_FINGERPRINT"],
        revision=state["REVIEWED_COMMIT"],
        baseline=baseline,
        baseline_sha256=baseline_sha,
        sdk_manifest_sha256=sdk_sha,
        sdk_files=sdk_files,
    )

    _validate_docker_boundary(environment, docker_dir)
    inspect = _json_text(
        _run_bounded(
            ["/usr/bin/docker", "image", "inspect", state["IMAGE_ID"]],
            environment=environment,
            cwd=docker_dir,
        ),
        "Docker image inspect",
    )
    validate_image_inspect(
        inspect,
        image_id=state["IMAGE_ID"],
        fingerprint=state["EXPECTED_SOURCE_FINGERPRINT"],
        revision=state["REVIEWED_COMMIT"],
    )
    compose_model = _json_text(
        _run_bounded(
            [*prefix, "config", "--format", "json"],
            environment=environment,
            cwd=docker_dir,
        ),
        "Docker Compose config",
    )
    storage = validate_compose_config(
        compose_model,
        image_id=state["IMAGE_ID"],
        declared=declared,
        acceptance_report=str(report_path),
    )
    validate_sdk_directory(Path(storage["sdk"]), sdk_files)
    _run_bounded(
        [
            sys.executable,
            "-I",
            str(reviewed_tree / "docker" / "validate_storage.py"),
            "--input",
            storage["input"],
            "--output",
            storage["output"],
            "--temp",
            storage["temp"],
            "--sdk",
            storage["sdk"],
            "--acceptance-report",
            str(report_path),
            "--trusted-scratch-root",
            str(scratch),
            "--require-unraid-direct-temp",
            "--max-temp-filesystem-bytes",
            str(declared["temp_capacity"]),
            "--min-temp-filesystem-free-bytes",
            str(declared["temp_free"]),
        ],
        environment=environment,
        cwd=docker_dir,
    )
    if args.verify_only:
        print(
            "production acceptance gate: PASS for "
            f"{state['IMAGE_ID']} ({state['EXPECTED_SOURCE_FINGERPRINT']})"
        )
        return 0
    os.execve(prefix[0], [*prefix, *compose_arguments], environment)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AcceptanceError as exc:
        print(f"production acceptance gate: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_CONFIG)
