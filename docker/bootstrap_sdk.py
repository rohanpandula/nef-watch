#!/usr/bin/env python3
"""Validate and privately stage Nikon's Windows SDK on first container start."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


EXPECTED_MANIFEST_SHA256 = (
    "0e8aba70b296966c03c407c8dd77ddee5c073924b9fb38f988881c366a1fbc51"
)
BOOTSTRAP_SCHEMA = "nef-watch-sdk-bootstrap-v3-attested-adapter"
MANIFEST_LINE = re.compile(r"^([0-9a-f]{64})  ([^\r\n]+)$")
RUNTIME_NAME = re.compile(
    r"^sdk-([0-9a-f]{16})-bootstrap-([0-9a-f]{16})$"
)
SDK_BINARY_PREFIX = Path("Bin/x64/Release")
SDK_HEADER = Path("Include/Nkfl_Interface.h")
SDK_PROFILE_PREFIX = Path("Profiles")
PROBE_TIMEOUT_SECONDS = 10
COMPILE_TIMEOUT_SECONDS = 120


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
    require_immutable_tools: bool = True


def file_sha256(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BootstrapError(f"cannot safely open regular file {path}: {exc}") from exc
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise BootstrapError(f"expected a regular file: {path}")
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_private_directory(path: Path, *, create: bool, mode: int = 0o750) -> None:
    if create and not path.exists() and not path.is_symlink():
        try:
            path.mkdir(mode=mode)
        except FileExistsError:
            pass
        except OSError as exc:
            raise BootstrapError(f"cannot create private directory {path}: {exc}") from exc
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise BootstrapError(f"cannot inspect private directory {path}: {exc}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise BootstrapError(f"private path must be a real directory, not a link: {path}")
    if metadata.st_uid != os.geteuid():
        raise BootstrapError(f"private directory is not owned by uid {os.geteuid()}: {path}")
    if metadata.st_mode & 0o022:
        raise BootstrapError(f"private directory is group/world writable: {path}")


def ensure_regular_path_beneath(root: Path, relative: Path) -> Path:
    """Reject symlinks in every manifest-controlled path component."""
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise BootstrapError(f"cannot inspect Nikon SDK directory {current}: {exc}") from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise BootstrapError(f"Nikon SDK directory is not a real directory: {current}")
    candidate = root / relative
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise BootstrapError(f"Nikon SDK file is missing: {relative}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise BootstrapError(f"Nikon SDK file is not a real regular file: {relative}")
    return candidate


def ensure_immutable_tool(path: Path, label: str, *, owner: int = 0) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.lstat()
    except OSError as exc:
        raise BootstrapError(f"cannot inspect immutable {label} {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise BootstrapError(f"immutable {label} is not a regular file: {resolved}")
    # Runtime bootstrap now executes as uid 0 before irrevocably dropping to the
    # watcher uid.  os.access(..., W_OK) alone therefore reports root-owned image
    # files as writable even when Docker mounted the root filesystem read-only.
    # Accept that narrowly defined case; ordinary callers still have to lack
    # write access by DAC.
    image_owned_on_read_only_mount = (
        metadata.st_uid == owner
        and not (metadata.st_mode & 0o022)
        and mount_is_read_only(resolved)
    )
    if metadata.st_nlink != 1 and not image_owned_on_read_only_mount:
        raise BootstrapError(
            f"immutable {label} has multiple writable hard links: {resolved}"
        )
    if os.access(resolved, os.W_OK) and not image_owned_on_read_only_mount:
        raise BootstrapError(
            f"immutable {label} is writable by runtime uid {os.geteuid()}: {resolved}"
        )
    return resolved


def copy_regular_file(source: Path, target: Path, *, mode: int) -> str:
    """Copy without following a source/target symlink and hash bytes as read."""
    read_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    write_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        source_fd = os.open(source, read_flags)
    except OSError as exc:
        raise BootstrapError(f"cannot safely open SDK file {source}: {exc}") from exc
    try:
        source_metadata = os.fstat(source_fd)
        if not stat.S_ISREG(source_metadata.st_mode):
            raise BootstrapError(f"SDK path is not a regular file: {source}")
        try:
            target_fd = os.open(target, write_flags, mode)
        except OSError as exc:
            raise BootstrapError(f"cannot safely create runtime file {target}: {exc}") from exc
        digest = hashlib.sha256()
        try:
            while True:
                block = os.read(source_fd, 1024 * 1024)
                if not block:
                    break
                digest.update(block)
                view = memoryview(block)
                while view:
                    written = os.write(target_fd, view)
                    if written <= 0:
                        raise BootstrapError(f"short write while staging {target}")
                    view = view[written:]
            os.fchmod(target_fd, mode)
            os.fsync(target_fd)
        finally:
            os.close(target_fd)
    finally:
        os.close(source_fd)
    return digest.hexdigest()


def fsync_directory(path: Path) -> None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BootstrapError(f"cannot open directory for durable sync {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise BootstrapError(f"durable sync target is not a directory: {path}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
        resolved = compiler.resolve(strict=True)
    except OSError as exc:
        raise BootstrapError(f"MinGW compiler is unavailable: {compiler}") from exc
    try:
        version = subprocess.run(
            [str(resolved), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        ).stdout
    except subprocess.TimeoutExpired as exc:
        raise BootstrapError(
            f"MinGW compiler version probe timed out after {PROBE_TIMEOUT_SECONDS}s"
        ) from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BootstrapError(f"MinGW compiler is unavailable: {compiler}") from exc
    material = (
        BOOTSTRAP_SCHEMA.encode()
        + b"\0"
        + version.encode()
        + b"\0"
        + file_sha256(resolved).encode()
    )
    return hashlib.sha256(material).hexdigest()


def mount_options(path: Path) -> set[str]:
    try:
        result = subprocess.run(
            ["findmnt", "--target", str(path), "--noheadings", "--output", "OPTIONS"],
            check=True,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise BootstrapError(
            f"mount probe timed out after {PROBE_TIMEOUT_SECONDS}s: {path}"
        ) from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BootstrapError(f"cannot inspect mount options at {path}") from exc
    return {item.strip() for item in result.stdout.strip().split(",")}


def mount_is_read_only(path: Path) -> bool:
    try:
        return "ro" in mount_options(path)
    except BootstrapError:
        return False


def ensure_read_only_mount(path: Path) -> None:
    try:
        options = mount_options(path)
    except BootstrapError as exc:
        raise BootstrapError(
            f"cannot verify Nikon SDK read-only mount at {path}"
        ) from exc
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


def _require_sealed_directory(path: Path, *, owner: int) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise BootstrapError(f"cannot inspect sealed Nikon runtime directory {path}: {exc}") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != owner
        or metadata.st_mode & 0o022
    ):
        raise BootstrapError(f"Nikon runtime directory is not sealed: {path}")


def _require_sealed_file(path: Path, *, owner: int) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise BootstrapError(f"cannot inspect sealed Nikon runtime file {path}: {exc}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != owner
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o022
    ):
        raise BootstrapError(f"Nikon runtime file is not sealed: {path}")


def resolve_active_runtime(config: BootstrapConfig, *, owner: int = 0) -> Path:
    """Resolve and fully validate the runtime activated by the init container.

    The long-running container has no SDK mount and cannot repair state.  It
    therefore accepts only the exact direct ``current`` link and sealed payload
    that this image's bootstrap inputs would have produced.
    """

    if config.require_immutable_tools:
        ensure_immutable_tool(config.adapter_source, "renderer source")
        ensure_immutable_tool(config.compiler, "MinGW compiler")
    manifest_hash = file_sha256(config.manifest)
    if manifest_hash != config.expected_manifest_sha256:
        raise BootstrapError(
            "Nikon SDK manifest fingerprint mismatch: "
            f"{manifest_hash} != {config.expected_manifest_sha256}"
        )
    entries = parse_manifest(config.manifest)
    adapter_source_hash = file_sha256(config.adapter_source)
    toolchain_hash = compiler_fingerprint(config.compiler)
    bootstrap_hash = hashlib.sha256(
        f"{manifest_hash}\n{adapter_source_hash}\n{toolchain_hash}\n".encode()
    ).hexdigest()
    version_name = f"sdk-{manifest_hash[:16]}-bootstrap-{bootstrap_hash[:16]}"
    if RUNTIME_NAME.fullmatch(version_name) is None:
        raise BootstrapError("computed Nikon runtime name is malformed")

    runtime_root = config.state_root / "nikon-runtime"
    _require_sealed_directory(config.state_root, owner=owner)
    _require_sealed_directory(runtime_root, owner=owner)
    current = runtime_root / "current"
    try:
        current_metadata = current.lstat()
        link_target = os.readlink(current)
    except OSError as exc:
        raise BootstrapError(f"cannot inspect active Nikon runtime link {current}: {exc}") from exc
    if (
        not stat.S_ISLNK(current_metadata.st_mode)
        or current_metadata.st_uid != owner
        or link_target != version_name
    ):
        raise BootstrapError(
            "active Nikon runtime must be a root-owned direct link to the exact sdk-* payload"
        )

    version_dir = runtime_root / version_name
    _require_sealed_directory(version_dir, owner=owner)
    try:
        resolved = version_dir.resolve(strict=True)
        expected_resolved = runtime_root.resolve(strict=True) / version_name
    except OSError as exc:
        raise BootstrapError(f"cannot resolve active Nikon runtime: {exc}") from exc
    if resolved != expected_resolved:
        raise BootstrapError("active Nikon runtime escapes its sealed runtime root")

    expected_directories = {
        Path("."),
        Path("Profiles"),
        Path(".attestation"),
        Path(".attestation/Include"),
    }
    expected_files: dict[Path, str] = {}
    for expected_hash, relative in entries:
        destination = (
            Path(".attestation") / relative
            if relative == SDK_HEADER
            else runtime_destination(relative)
        )
        assert destination is not None
        if destination in expected_files:
            raise BootstrapError(f"duplicate Nikon runtime destination: {destination}")
        expected_files[destination] = expected_hash

    marker_path = version_dir / ".nef-watch-sdk-ready.json"
    renderer_path = version_dir / "nef_render.exe"
    _require_sealed_file(marker_path, owner=owner)
    _require_sealed_file(renderer_path, owner=owner)
    marker = read_ready_marker(marker_path)
    renderer_hash = file_sha256(renderer_path)
    expected_marker = {
        "schema": BOOTSTRAP_SCHEMA,
        "sdk_manifest_sha256": manifest_hash,
        "adapter_source_sha256": adapter_source_hash,
        "compiler_fingerprint": toolchain_hash,
        "adapter_executable_sha256": renderer_hash,
    }
    if marker != expected_marker:
        raise BootstrapError("active Nikon runtime attestation marker does not match this image")
    expected_files[Path(".nef-watch-sdk-ready.json")] = file_sha256(marker_path)
    expected_files[Path("nef_render.exe")] = renderer_hash

    actual_directories: set[Path] = set()
    actual_files: set[Path] = set()
    for folder_text, directories, files in os.walk(version_dir, followlinks=False):
        folder = Path(folder_text)
        relative_folder = folder.relative_to(version_dir)
        actual_directories.add(relative_folder if relative_folder.parts else Path("."))
        for name in directories:
            candidate = folder / name
            _require_sealed_directory(candidate, owner=owner)
        for name in files:
            candidate = folder / name
            relative = candidate.relative_to(version_dir)
            _require_sealed_file(candidate, owner=owner)
            actual_files.add(relative)
    if actual_directories != expected_directories or actual_files != set(expected_files):
        raise BootstrapError("active Nikon runtime contains unexpected or missing entries")

    for relative, expected_hash in expected_files.items():
        candidate = version_dir / relative
        if file_sha256(candidate) != expected_hash:
            raise BootstrapError(f"active Nikon runtime hash mismatch: {relative}")
    return resolved


def create_verified_snapshot(
    sdk_root: Path,
    snapshot_root: Path,
    entries: list[tuple[str, Path]],
) -> None:
    """Copy every manifest entry privately, then verify the closed snapshot."""
    snapshot_root.mkdir(mode=0o700)
    for expected, relative in entries:
        target = snapshot_root / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        source = ensure_regular_path_beneath(sdk_root, relative)
        actual = copy_regular_file(source, target, mode=0o400)
        if actual != expected:
            raise BootstrapError(
                f"Nikon SDK snapshot hash mismatch for {relative}: "
                f"{actual} != {expected}"
            )

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
    try:
        current_metadata = current.lstat()
    except FileNotFoundError:
        current_metadata = None
    except OSError as exc:
        raise BootstrapError(f"cannot inspect Nikon runtime link {current}: {exc}") from exc
    if current_metadata is not None and not stat.S_ISLNK(current_metadata.st_mode):
        raise BootstrapError(f"Nikon runtime current path is not a symbolic link: {current}")
    link = runtime_root / (
        f".current-{os.getpid()}-{secrets.token_hex(8)}"
    )
    try:
        link.symlink_to(version_name)
        os.replace(link, current)
        fsync_directory(runtime_root)
    finally:
        try:
            link.unlink()
        except FileNotFoundError:
            pass
    version_dir = runtime_root / version_name
    ensure_private_directory(version_dir, create=False)
    return version_dir.resolve(strict=True)


def prune_inactive_runtimes(runtime_root: Path, active: Path) -> None:
    """Remove only inactive, owned version directories while holding the lock."""
    active = active.resolve(strict=True)
    changed = False
    for candidate in runtime_root.iterdir():
        if not candidate.name.startswith("sdk-"):
            continue
        try:
            metadata = candidate.lstat()
        except OSError as exc:
            raise BootstrapError(f"cannot inspect inactive Nikon runtime {candidate}: {exc}") from exc
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise BootstrapError(f"unsafe inactive Nikon runtime path: {candidate}")
        resolved = candidate.resolve(strict=True)
        if resolved == active:
            continue
        shutil.rmtree(candidate)
        changed = True
    if changed:
        fsync_directory(runtime_root)


def prune_interrupted_staging(runtime_root: Path) -> None:
    """Remove abandoned managed staging trees while holding the bootstrap lock."""
    changed = False
    for candidate in runtime_root.iterdir():
        if not candidate.name.startswith((".attest-", ".bootstrap-")):
            continue
        try:
            metadata = candidate.lstat()
        except OSError as exc:
            raise BootstrapError(
                f"cannot inspect interrupted Nikon staging path {candidate}: {exc}"
            ) from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o022
        ):
            raise BootstrapError(
                f"unsafe interrupted Nikon staging path: {candidate}"
            )
        shutil.rmtree(candidate)
        changed = True
    if changed:
        fsync_directory(runtime_root)


def activate_and_prune(runtime_root: Path, version_name: str) -> Path:
    active = activate_runtime(runtime_root, version_name)
    prune_inactive_runtimes(runtime_root, active)
    return active


def sdk_header_hash(entries: list[tuple[str, Path]]) -> str:
    return next(expected for expected, relative in entries if relative == SDK_HEADER)


def compile_attested_adapter(
    config: BootstrapConfig,
    *,
    header_source: Path,
    expected_header_hash: str,
    expected_adapter_source_hash: str,
    build_root: Path,
) -> tuple[Path, str]:
    """Compile from freshly verified copies so the output is source-derived."""
    inputs = build_root / "inputs"
    include = inputs / "Include"
    include.mkdir(parents=True, mode=0o700)
    header_copy = include / SDK_HEADER.name
    actual_header_hash = copy_regular_file(header_source, header_copy, mode=0o400)
    if actual_header_hash != expected_header_hash:
        raise BootstrapError(
            "attested Nikon SDK header hash mismatch: "
            f"{actual_header_hash} != {expected_header_hash}"
        )
    adapter_copy = inputs / "nef_render_win.cpp"
    actual_source_hash = copy_regular_file(
        config.adapter_source, adapter_copy, mode=0o400
    )
    if actual_source_hash != expected_adapter_source_hash:
        raise BootstrapError(
            "renderer source changed during bootstrap: "
            f"{actual_source_hash} != {expected_adapter_source_hash}"
        )

    output = build_root / "nef_render.exe"
    try:
        compiler = config.compiler.resolve(strict=True)
    except OSError as exc:
        raise BootstrapError(f"MinGW compiler is unavailable: {config.compiler}") from exc
    command = [
        str(compiler),
        "-std=c++17",
        "-O2",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-Wno-attributes",
        "-frandom-seed=nef-watch-adapter-v3",
        "-static",
        "-static-libgcc",
        "-static-libstdc++",
        "-Wl,--no-insert-timestamp",
        f"-I{include}",
        str(adapter_copy),
        "-o",
        str(output),
    ]
    try:
        compiler_process = subprocess.Popen(command, start_new_session=True)
    except OSError as exc:
        raise BootstrapError("failed to start Nikon SDK adapter compiler") from exc
    try:
        return_code = compiler_process.wait(timeout=COMPILE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(compiler_process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        compiler_process.wait()
        raise BootstrapError(
            f"Nikon SDK adapter compilation timed out after "
            f"{COMPILE_TIMEOUT_SECONDS}s"
        ) from exc
    if return_code != 0:
        raise BootstrapError("failed to compile Nikon SDK adapter with MinGW")
    try:
        output_metadata = output.lstat()
    except OSError as exc:
        raise BootstrapError("MinGW produced no Nikon SDK adapter executable") from exc
    if (
        not stat.S_ISREG(output_metadata.st_mode)
        or output_metadata.st_nlink != 1
        or output_metadata.st_size == 0
    ):
        raise BootstrapError("MinGW produced an unsafe Nikon SDK adapter executable")
    output.chmod(0o555)
    with output.open("rb") as executable:
        os.fsync(executable.fileno())
    return output, file_sha256(output)


def read_ready_marker(path: Path) -> dict[str, str] | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            return None
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            marker = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return marker if isinstance(marker, dict) else None


def write_ready_marker(
    version_dir: Path, expected_marker: dict[str, str], adapter_sha256: str
) -> None:
    marker = {
        **expected_marker,
        "adapter_executable_sha256": adapter_sha256,
    }
    marker_path = version_dir / ".nef-watch-sdk-ready.json"
    temporary = version_dir / f".ready-{os.getpid()}-{secrets.token_hex(8)}"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(temporary, flags, 0o600)
        try:
            payload = (json.dumps(marker, sort_keys=True) + "\n").encode("utf-8")
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise BootstrapError(f"short write while creating {temporary}")
                view = view[written:]
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, marker_path)
        fsync_directory(version_dir)
    finally:
        temporary.unlink(missing_ok=True)


def runtime_payload_is_valid(
    version_dir: Path,
    entries: list[tuple[str, Path]],
) -> bool:
    try:
        ensure_private_directory(version_dir, create=False)
        ensure_private_directory(version_dir / "Profiles", create=False)
        ensure_private_directory(version_dir / ".attestation", create=False, mode=0o700)
        ensure_private_directory(
            version_dir / ".attestation" / "Include", create=False, mode=0o700
        )
        for expected, relative in entries:
            if relative == SDK_HEADER:
                path = version_dir / ".attestation" / relative
            else:
                destination = runtime_destination(relative)
                assert destination is not None
                path = version_dir / destination
            if file_sha256(path) != expected:
                return False
    except (BootstrapError, OSError):
        return False
    return True


def attest_or_repair_ready_runtime(
    config: BootstrapConfig,
    *,
    version_dir: Path,
    entries: list[tuple[str, Path]],
    expected_marker: dict[str, str],
    adapter_hash: str,
    runtime_root: Path,
) -> bool:
    if not runtime_payload_is_valid(version_dir, entries):
        return False
    attestation_root = Path(
        tempfile.mkdtemp(prefix=".attest-", dir=runtime_root)
    )
    try:
        build_root = attestation_root / "build"
        build_root.mkdir(mode=0o700)
        candidate, candidate_hash = compile_attested_adapter(
            config,
            header_source=version_dir / ".attestation" / SDK_HEADER,
            expected_header_hash=sdk_header_hash(entries),
            expected_adapter_source_hash=adapter_hash,
            build_root=build_root,
        )
        marker = read_ready_marker(version_dir / ".nef-watch-sdk-ready.json")
        adapter_matches = False
        try:
            adapter_matches = file_sha256(version_dir / "nef_render.exe") == candidate_hash
        except (BootstrapError, OSError):
            pass
        marker_matches = marker is not None and all(
            marker.get(key) == value
            for key, value in {
                **expected_marker,
                "adapter_executable_sha256": candidate_hash,
            }.items()
        )
        if not adapter_matches or not marker_matches:
            print(
                "Nikon renderer attestation mismatch; rebuilding from verified source",
                file=sys.stderr,
            )
            candidate.chmod(0o555)
            os.replace(candidate, version_dir / "nef_render.exe")
            write_ready_marker(version_dir, expected_marker, candidate_hash)
            fsync_directory(version_dir)
        return True
    finally:
        shutil.rmtree(attestation_root, ignore_errors=True)


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
    expected_marker = {
        "schema": BOOTSTRAP_SCHEMA,
        "sdk_manifest_sha256": manifest_hash,
        "adapter_source_sha256": adapter_hash,
        "compiler_fingerprint": toolchain_hash,
    }

    if attest_or_repair_ready_runtime(
        config,
        version_dir=version_dir,
        entries=entries,
        expected_marker=expected_marker,
        adapter_hash=adapter_hash,
        runtime_root=runtime_root,
    ):
        return activate_and_prune(runtime_root, version_name)

    try:
        sdk_root_metadata = config.sdk_root.lstat()
    except OSError:
        sdk_root_metadata = None
    if sdk_root_metadata is None or not stat.S_ISDIR(sdk_root_metadata.st_mode):
        raise BootstrapError(
            f"Nikon SDK root is required on first start: {config.sdk_root}"
        )
    if config.require_read_only_mount:
        ensure_read_only_mount(config.sdk_root)
    for expected, relative in entries:
        source = ensure_regular_path_beneath(config.sdk_root, relative)
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
        (staged / ".attestation" / "Include").mkdir(
            parents=True, mode=0o700
        )
        (staged / ".attestation").chmod(0o700)
        (staged / ".attestation" / "Include").chmod(0o700)
        staged.chmod(0o750)
        for expected, relative in entries:
            destination = (
                Path(".attestation") / relative
                if relative == SDK_HEADER
                else runtime_destination(relative)
            )
            assert destination is not None
            target = staged / destination
            actual = copy_regular_file(
                snapshot / relative,
                target,
                mode=0o400 if relative == SDK_HEADER else 0o444,
            )
            if actual != expected:
                raise BootstrapError(
                    f"verified snapshot changed while staging {relative}"
                )

        build_root = temp_dir / "build"
        build_root.mkdir(mode=0o700)
        output, output_hash = compile_attested_adapter(
            config,
            header_source=snapshot / SDK_HEADER,
            expected_header_hash=sdk_header_hash(entries),
            expected_adapter_source_hash=adapter_hash,
            build_root=build_root,
        )
        os.replace(output, staged / "nef_render.exe")
        write_ready_marker(staged, expected_marker, output_hash)
        fsync_directory(staged / "Profiles")
        fsync_directory(staged / ".attestation" / "Include")
        fsync_directory(staged / ".attestation")
        fsync_directory(staged)
        os.replace(staged, version_dir)
        fsync_directory(runtime_root)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return activate_and_prune(runtime_root, version_name)


def bootstrap(config: BootstrapConfig) -> Path:
    if config.require_immutable_tools:
        ensure_immutable_tool(config.adapter_source, "renderer source")
        ensure_immutable_tool(config.compiler, "MinGW compiler")
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
    ensure_private_directory(config.state_root, create=True)
    runtime_root = config.state_root / "nikon-runtime"
    ensure_private_directory(runtime_root, create=True)
    lock_path = runtime_root / ".bootstrap.lock"
    lock_flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        lock_descriptor = os.open(lock_path, lock_flags, 0o600)
    except OSError as exc:
        raise BootstrapError(f"cannot safely open bootstrap lock {lock_path}: {exc}") from exc
    with os.fdopen(lock_descriptor, "a+b") as lock:
        lock_metadata = os.fstat(lock.fileno())
        if (
            not stat.S_ISREG(lock_metadata.st_mode)
            or lock_metadata.st_nlink != 1
            or lock_metadata.st_uid != os.geteuid()
        ):
            raise BootstrapError(f"bootstrap lock is not a private regular file: {lock_path}")
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        prune_interrupted_staging(runtime_root)
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
    return BootstrapConfig(
        sdk_root=Path(os.environ.get("NIKON_SDK_DIR", "/nikon-sdk")),
        state_root=Path(os.environ.get("NEF_WATCH_STATE_DIR", "/var/lib/nef-watch")),
        manifest=Path("/usr/share/nef-watch/nikon-sdk-v1.46.sha256"),
        expected_manifest_sha256=EXPECTED_MANIFEST_SHA256,
        adapter_source=Path("/usr/src/nef-watch/nef_render_win.cpp"),
        # Do not consult caller-controlled PATH for the executable that forms
        # the persisted adapter's root of trust.
        compiler=Path("/usr/bin/x86_64-w64-mingw32-g++"),
        require_read_only_mount=True,
        require_immutable_tools=True,
    )


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        if arguments == ["--resolve-active-runtime"]:
            runtime = resolve_active_runtime(default_config())
        elif arguments:
            print(
                "usage: nef-watch-bootstrap-sdk.py [--resolve-active-runtime]",
                file=sys.stderr,
            )
            return 64
        else:
            runtime = bootstrap(default_config())
    except (BootstrapError, OSError) as exc:
        print(f"Nikon SDK bootstrap failed: {exc}", file=sys.stderr)
        return 70
    print(runtime)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
