#!/usr/bin/env python3
"""Validate and privately stage Nikon's Windows SDK on first container start."""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


EXPECTED_MANIFEST_SHA256 = (
    "0e8aba70b296966c03c407c8dd77ddee5c073924b9fb38f988881c366a1fbc51"
)
BOOTSTRAP_SCHEMA = "nef-watch-sdk-bootstrap-v2"
MANIFEST_LINE = re.compile(r"^([0-9a-f]{64})  ([^\r\n]+)$")
SDK_BINARY_PREFIX = Path("Bin/x64/Release")
SDK_HEADER = Path("Include/Nkfl_Interface.h")
SDK_PROFILE_PREFIX = Path("Profiles")


class BootstrapError(RuntimeError):
    """The private Nikon runtime could not be established safely."""


@dataclass(frozen=True)
class BootstrapConfig:
    sdk_root: Path
    state_root: Path
    manifest: Path
    expected_manifest_sha256: str
    adapter_source: Path
    compiler: Path
    require_read_only_mount: bool = True


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_manifest(path: Path) -> list[tuple[str, Path]]:
    entries: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BootstrapError(f"cannot read Nikon SDK manifest {path}: {exc}") from exc
    for line_number, line in enumerate(lines, 1):
        match = MANIFEST_LINE.fullmatch(line)
        if not match:
            raise BootstrapError(f"malformed Nikon SDK manifest line {line_number}")
        relative = Path(match.group(2))
        if relative.is_absolute() or ".." in relative.parts or relative in seen:
            raise BootstrapError(f"unsafe or duplicate Nikon SDK path: {relative}")
        if not (
            relative == SDK_HEADER
            or relative.parent == SDK_BINARY_PREFIX
            or relative.parent == SDK_PROFILE_PREFIX
        ):
            raise BootstrapError(f"unexpected Nikon SDK manifest path: {relative}")
        seen.add(relative)
        entries.append((match.group(1), relative))
    if SDK_HEADER not in seen:
        raise BootstrapError("Nikon SDK manifest does not include Nkfl_Interface.h")
    if not any(path.parent == SDK_BINARY_PREFIX for _, path in entries):
        raise BootstrapError("Nikon SDK manifest has no Windows runtime files")
    if not any(path.parent == SDK_PROFILE_PREFIX for _, path in entries):
        raise BootstrapError("Nikon SDK manifest has no color profiles")
    return entries


def compiler_fingerprint(compiler: Path) -> str:
    try:
        version = subprocess.run(
            [str(compiler), "--version"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BootstrapError(f"MinGW compiler is unavailable: {compiler}") from exc
    resolved = compiler.resolve()
    material = (
        BOOTSTRAP_SCHEMA.encode()
        + b"\0"
        + version.encode()
        + b"\0"
        + file_sha256(resolved).encode()
    )
    return hashlib.sha256(material).hexdigest()


def ensure_read_only_mount(path: Path) -> None:
    try:
        result = subprocess.run(
            ["findmnt", "--target", str(path), "--noheadings", "--output", "OPTIONS"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BootstrapError(
            f"cannot verify Nikon SDK read-only mount at {path}"
        ) from exc
    options = {item.strip() for item in result.stdout.strip().split(",")}
    if "ro" not in options:
        raise BootstrapError(f"Nikon SDK root must be a read-only mount: {path}")


def runtime_destination(relative: Path) -> Path | None:
    if relative == SDK_HEADER:
        return None
    if relative.parent == SDK_BINARY_PREFIX:
        return Path(relative.name)
    if relative.parent == SDK_PROFILE_PREFIX:
        return Path("Profiles") / relative.name
    raise BootstrapError(f"cannot stage unexpected Nikon SDK path: {relative}")


def create_verified_snapshot(
    sdk_root: Path,
    snapshot_root: Path,
    entries: list[tuple[str, Path]],
) -> None:
    """Copy every manifest entry privately, then verify the closed snapshot."""
    snapshot_root.mkdir(mode=0o700)
    for _, relative in entries:
        target = snapshot_root / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(sdk_root / relative, target)
        target.chmod(0o400)

    # The host can replace files backing a read-only bind mount. Re-hashing the
    # private copy closes the gap between source verification and later use.
    for expected, relative in entries:
        actual = file_sha256(snapshot_root / relative)
        if actual != expected:
            raise BootstrapError(
                f"Nikon SDK snapshot hash mismatch for {relative}: "
                f"{actual} != {expected}"
            )


def activate_runtime(runtime_root: Path, version_name: str) -> Path:
    current = runtime_root / "current"
    link = runtime_root / f".current-{os.getpid()}"
    link.unlink(missing_ok=True)
    link.symlink_to(version_name)
    os.replace(link, current)
    return current.resolve()


def ready_runtime_is_valid(
    version_dir: Path,
    entries: list[tuple[str, Path]],
    expected_marker: dict[str, str],
) -> bool:
    marker_path = version_dir / ".nef-watch-sdk-ready.json"
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(marker, dict):
        return False
    if any(marker.get(key) != value for key, value in expected_marker.items()):
        return False
    adapter = version_dir / "nef_render.exe"
    try:
        if file_sha256(adapter) != marker["adapter_executable_sha256"]:
            return False
        for expected, relative in entries:
            destination = runtime_destination(relative)
            if destination is not None and file_sha256(version_dir / destination) != expected:
                return False
    except (OSError, KeyError):
        return False
    return True


def bootstrap_locked(
    config: BootstrapConfig,
    *,
    manifest_hash: str,
    entries: list[tuple[str, Path]],
    adapter_hash: str,
    toolchain_hash: str,
    bootstrap_hash: str,
    runtime_root: Path,
) -> Path:
    version_name = f"sdk-{manifest_hash[:16]}-bootstrap-{bootstrap_hash[:16]}"
    version_dir = runtime_root / version_name
    runtime_root.mkdir(parents=True, exist_ok=True, mode=0o750)
    expected_marker = {
        "schema": BOOTSTRAP_SCHEMA,
        "sdk_manifest_sha256": manifest_hash,
        "adapter_source_sha256": adapter_hash,
        "compiler_fingerprint": toolchain_hash,
    }

    if ready_runtime_is_valid(version_dir, entries, expected_marker):
        return activate_runtime(runtime_root, version_name)

    if not config.sdk_root.is_dir():
        raise BootstrapError(
            f"Nikon SDK root is required on first start: {config.sdk_root}"
        )
    if config.require_read_only_mount:
        ensure_read_only_mount(config.sdk_root)
    for expected, relative in entries:
        source = config.sdk_root / relative
        if not source.is_file():
            raise BootstrapError(f"Nikon SDK file is missing: {relative}")
        actual = file_sha256(source)
        if actual != expected:
            raise BootstrapError(
                f"Nikon SDK hash mismatch for {relative}: {actual} != {expected}"
            )

    if version_dir.exists() or version_dir.is_symlink():
        if version_dir.is_dir() and not version_dir.is_symlink():
            shutil.rmtree(version_dir)
        else:
            version_dir.unlink()
    temp_dir = Path(tempfile.mkdtemp(prefix=".bootstrap-", dir=runtime_root))
    snapshot = temp_dir / "snapshot"
    staged = temp_dir / "runtime"
    try:
        create_verified_snapshot(config.sdk_root, snapshot, entries)
        (staged / "Profiles").mkdir(parents=True, mode=0o750)
        staged.chmod(0o750)
        for _, relative in entries:
            destination = runtime_destination(relative)
            if destination is None:
                continue
            target = staged / destination
            shutil.copyfile(snapshot / relative, target)
            target.chmod(0o444)

        output = staged / "nef_render.exe"
        command = [
            str(config.compiler),
            "-std=c++17",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wno-attributes",
            "-static",
            "-static-libgcc",
            "-static-libstdc++",
            f"-I{snapshot / 'Include'}",
            str(config.adapter_source),
            "-o",
            str(output),
        ]
        try:
            subprocess.run(command, check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise BootstrapError("failed to compile Nikon SDK adapter with MinGW") from exc
        if not output.is_file() or output.stat().st_size == 0:
            raise BootstrapError("MinGW produced no Nikon SDK adapter executable")
        output.chmod(0o555)
        marker = {
            **expected_marker,
            "adapter_executable_sha256": file_sha256(output),
        }
        marker_path = staged / ".nef-watch-sdk-ready.json"
        marker_path.write_text(
            json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8"
        )
        marker_path.chmod(0o600)
        os.replace(staged, version_dir)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return activate_runtime(runtime_root, version_name)


def bootstrap(config: BootstrapConfig) -> Path:
    manifest_hash = file_sha256(config.manifest)
    if manifest_hash != config.expected_manifest_sha256:
        raise BootstrapError(
            "Nikon SDK manifest fingerprint mismatch: "
            f"{manifest_hash} != {config.expected_manifest_sha256}"
        )
    entries = parse_manifest(config.manifest)
    adapter_hash = file_sha256(config.adapter_source)
    toolchain_hash = compiler_fingerprint(config.compiler)
    bootstrap_hash = hashlib.sha256(
        f"{manifest_hash}\n{adapter_hash}\n{toolchain_hash}\n".encode()
    ).hexdigest()
    runtime_root = config.state_root / "nikon-runtime"
    runtime_root.mkdir(parents=True, exist_ok=True, mode=0o750)
    lock_path = runtime_root / ".bootstrap.lock"
    with lock_path.open("a+b") as lock:
        lock_path.chmod(0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return bootstrap_locked(
            config,
            manifest_hash=manifest_hash,
            entries=entries,
            adapter_hash=adapter_hash,
            toolchain_hash=toolchain_hash,
            bootstrap_hash=bootstrap_hash,
            runtime_root=runtime_root,
        )


def default_config() -> BootstrapConfig:
    compiler = shutil.which("x86_64-w64-mingw32-g++")
    return BootstrapConfig(
        sdk_root=Path(os.environ.get("NIKON_SDK_DIR", "/nikon-sdk")),
        state_root=Path(os.environ.get("NEF_WATCH_STATE_DIR", "/var/lib/nef-watch")),
        manifest=Path("/usr/share/nef-watch/nikon-sdk-v1.46.sha256"),
        expected_manifest_sha256=EXPECTED_MANIFEST_SHA256,
        adapter_source=Path("/usr/src/nef-watch/nef_render_win.cpp"),
        compiler=Path(compiler or "/usr/bin/x86_64-w64-mingw32-g++"),
        require_read_only_mount=True,
    )


def main() -> int:
    try:
        runtime = bootstrap(default_config())
    except (BootstrapError, OSError) as exc:
        print(f"Nikon SDK bootstrap failed: {exc}", file=sys.stderr)
        return 70
    print(runtime)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
