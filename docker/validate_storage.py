#!/usr/bin/env python3
"""Fail closed when storage roots overlap or alias each other.

The host mode validates the paths from ``docker/.env`` before Compose starts.
The container mode additionally compares the backing roots exposed by
``/proc/self/mountinfo`` so two different container paths cannot conceal the
same (or nested) host bind mount.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


EXIT_CONFIG = 78
MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")
GIB = 1024**3
MIN_TEMP_FILESYSTEM_BYTES = GIB
MAX_TEMP_FILESYSTEM_BYTES = 64 * GIB


class StorageIsolationError(RuntimeError):
    """Configured storage roots are not independent."""


@dataclass(frozen=True)
class MountInfo:
    device: str
    root: PurePosixPath
    mountpoint: PurePosixPath
    filesystem: str
    source: str


@dataclass(frozen=True)
class FilesystemCapacity:
    total_bytes: int
    available_bytes: int


def _unescape_mount_field(value: str) -> str:
    return MOUNT_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


def _parse_bounded_temp_bytes(value: str | int, option: str) -> int:
    try:
        capacity = int(str(value), 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{option} must be an integer number of bytes"
        ) from exc
    if not MIN_TEMP_FILESYSTEM_BYTES <= capacity <= MAX_TEMP_FILESYSTEM_BYTES:
        raise argparse.ArgumentTypeError(
            f"{option} must be between "
            f"{MIN_TEMP_FILESYSTEM_BYTES} and {MAX_TEMP_FILESYSTEM_BYTES} bytes "
            "(1-64 GiB)"
        )
    return capacity


def parse_temp_filesystem_bytes(value: str | int) -> int:
    return _parse_bounded_temp_bytes(value, "max-temp-filesystem-bytes")


def parse_temp_minimum_free_bytes(value: str | int) -> int:
    return _parse_bounded_temp_bytes(
        value, "min-temp-filesystem-free-bytes"
    )


def validate_temp_filesystem_capacity(
    temp_path: Path,
    maximum_bytes: int,
    *,
    statvfs_fn=None,
    minimum_free_bytes: int | None = None,
) -> FilesystemCapacity:
    """Require temp to reside on a genuinely bounded filesystem.

    A directory on a large NVMe pool is not an enforceable storage ceiling.
    ``statvfs`` must report that the entire mounted filesystem is no larger than
    the configured maximum.  ``minimum_free_bytes`` is optional so an existing
    caller-side free-space policy can be enforced without a second filesystem
    probe.
    """
    try:
        maximum = parse_temp_filesystem_bytes(maximum_bytes)
    except argparse.ArgumentTypeError as exc:
        raise StorageIsolationError(str(exc)) from exc
    if minimum_free_bytes is not None:
        try:
            minimum_free = parse_temp_minimum_free_bytes(minimum_free_bytes)
        except argparse.ArgumentTypeError as exc:
            raise StorageIsolationError(str(exc)) from exc
        if minimum_free > maximum:
            raise StorageIsolationError(
                "min-temp-filesystem-free-bytes must be no larger than "
                "max-temp-filesystem-bytes"
            )
    else:
        minimum_free = None

    probe = os.statvfs if statvfs_fn is None else statvfs_fn
    try:
        filesystem = probe(temp_path)
        fragment_size = int(filesystem.f_frsize)
        total_blocks = int(filesystem.f_blocks)
        available_blocks = int(filesystem.f_bavail)
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise StorageIsolationError(
            f"cannot determine temporary filesystem capacity for {temp_path}: {exc}"
        ) from exc
    if fragment_size <= 0 or total_blocks <= 0 or available_blocks < 0:
        raise StorageIsolationError(
            f"temporary filesystem returned invalid statvfs values for {temp_path}"
        )
    if available_blocks > total_blocks:
        raise StorageIsolationError(
            f"temporary filesystem reports more available than total blocks: {temp_path}"
        )

    # Compare before multiplying so an enormous/malicious block count fails at
    # the configured boundary without constructing an unbounded-size integer.
    if total_blocks > maximum // fragment_size:
        raise StorageIsolationError(
            f"temporary filesystem for {temp_path} exceeds the configured "
            f"{maximum}-byte ceiling; use a dedicated bounded filesystem or tmpfs"
        )
    total_bytes = total_blocks * fragment_size
    available_bytes = available_blocks * fragment_size
    if minimum_free is not None and available_bytes < minimum_free:
        raise StorageIsolationError(
            f"temporary filesystem has {available_bytes} bytes available; "
            f"at least {minimum_free} are required"
        )
    return FilesystemCapacity(
        total_bytes=total_bytes, available_bytes=available_bytes
    )


def parse_mountinfo(lines: Iterable[str]) -> list[MountInfo]:
    mounts: list[MountInfo] = []
    for line_number, line in enumerate(lines, 1):
        fields = line.rstrip("\n").split()
        try:
            separator = fields.index("-")
            if separator < 6 or len(fields) < separator + 4:
                raise ValueError
            mounts.append(
                MountInfo(
                    device=fields[2],
                    root=PurePosixPath(_unescape_mount_field(fields[3])),
                    mountpoint=PurePosixPath(_unescape_mount_field(fields[4])),
                    filesystem=fields[separator + 1],
                    source=_unescape_mount_field(fields[separator + 2]),
                )
            )
        except (IndexError, ValueError) as exc:
            raise StorageIsolationError(
                f"malformed /proc/self/mountinfo line {line_number}"
            ) from exc
    if not mounts:
        raise StorageIsolationError("/proc/self/mountinfo contains no mounts")
    return mounts


def _is_exact_mountpoint(
    path: Path, mountinfo_path: Path = Path("/proc/self/mountinfo")
) -> bool:
    try:
        with mountinfo_path.open("r", encoding="utf-8") as stream:
            mounts = parse_mountinfo(stream)
    except OSError as exc:
        raise StorageIsolationError(
            f"cannot inspect host mount table {mountinfo_path}: {exc}"
        ) from exc
    expected = PurePosixPath(path)
    return any(mount.mountpoint == expected for mount in mounts)


def _is_within_or_equal(path: PurePosixPath, root: PurePosixPath) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _overlap(left: PurePosixPath, right: PurePosixPath) -> bool:
    return _is_within_or_equal(left, right) or _is_within_or_equal(right, left)


def _resolve_existing(
    label: str, value: Path, *, allow_generic_host_paths: bool = False
) -> Path:
    requested = value.expanduser()
    absolute = Path(os.path.abspath(os.fspath(requested)))
    try:
        resolved = requested.resolve(strict=True)
    except OSError as exc:
        raise StorageIsolationError(f"{label} path is missing or unreadable: {value}: {exc}") from exc
    if not allow_generic_host_paths and (
        not requested.is_absolute() or requested != absolute or resolved != absolute
    ):
        raise StorageIsolationError(
            f"{label} path must be absolute, canonical, and contain no symbolic-link component: {value}"
        )
    if not resolved.is_dir():
        raise StorageIsolationError(f"{label} path is not a directory: {resolved}")
    return resolved


def _resolve_existing_file(
    label: str, value: Path, *, allow_generic_host_paths: bool = False
) -> Path:
    requested = value.expanduser()
    absolute = Path(os.path.abspath(os.fspath(requested)))
    try:
        resolved = requested.resolve(strict=True)
        metadata = resolved.lstat()
    except OSError as exc:
        raise StorageIsolationError(
            f"{label} file is missing or unreadable: {value}: {exc}"
        ) from exc
    if not allow_generic_host_paths and (
        not requested.is_absolute() or requested != absolute or resolved != absolute
    ):
        raise StorageIsolationError(
            f"{label} file must be absolute, canonical, and contain no symbolic-link component: {value}"
        )
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise StorageIsolationError(f"{label} is not a regular file: {resolved}")
    return resolved


def _unraid_view(value: Path) -> tuple[str, str] | None:
    """Return (view, share) for /mnt/user/<share> or /mnt/<pool>/<share>."""
    absolute = PurePosixPath(os.path.abspath(os.fspath(value.expanduser())))
    parts = absolute.parts
    if len(parts) < 4 or parts[:2] != ("/", "mnt"):
        return None
    if parts[2] == "user":
        return "user", parts[3]
    if parts[2] not in {"disks", "remotes"}:
        return "direct", parts[3]
    return None


def _require_direct_unraid_path(label: str, value: Path) -> None:
    parts = PurePosixPath(value).parts
    reserved = {"user", "user0", "disks", "remotes"}
    if (
        len(parts) < 4
        or parts[:2] != ("/", "mnt")
        or parts[2].casefold() in reserved
    ):
        raise StorageIsolationError(
            f"{label} must use a canonical direct Unraid pool path "
            f"/mnt/<pool>/..., never /mnt/user, /mnt/user0, a remote, or an unassigned disk: {value}"
        )


def validate_unraid_temp_binding(
    temporary: Path,
    *,
    trusted_scratch_root: Path,
    acceptance_report: Path,
    mountpoint_check=None,
) -> tuple[Path, Path, Path]:
    """Bind production temp and retained evidence to one direct pool mount."""
    temp = _resolve_existing("temporary", temporary)
    scratch = _resolve_existing("trusted scratch", trusted_scratch_root)
    report = _resolve_existing_file("acceptance report", acceptance_report)
    _require_direct_unraid_path("temporary storage", temp)
    _require_direct_unraid_path("trusted scratch", scratch)
    try:
        temp.relative_to(scratch)
        report.relative_to(scratch)
    except ValueError as exc:
        raise StorageIsolationError(
            "temporary storage and acceptance report must both be beneath the trusted scratch mount"
        ) from exc
    if report.is_relative_to(temp):
        raise StorageIsolationError(
            "acceptance evidence must not be stored inside watcher-writable temporary storage"
        )
    is_mount = _is_exact_mountpoint if mountpoint_check is None else mountpoint_check
    if not is_mount(scratch):
        raise StorageIsolationError(
            f"trusted scratch root is not its own mounted filesystem or dataset: {scratch}"
        )
    identities = {
        temp.stat().st_dev,
        scratch.stat().st_dev,
        report.stat().st_dev,
    }
    if len(identities) != 1:
        raise StorageIsolationError(
            "temporary storage and acceptance report are not backed by the trusted scratch filesystem"
        )
    return temp, scratch, report


def validate_host_paths(
    paths: dict[str, Path], *, allow_generic_host_paths: bool = False
) -> dict[str, Path]:
    if len(paths) < 2:
        raise StorageIsolationError("at least two storage paths are required")
    resolved = {
        label: _resolve_existing(
            label, value, allow_generic_host_paths=allow_generic_host_paths
        )
        for label, value in paths.items()
    }
    items = list(resolved.items())
    original_items = list(paths.items())
    for index, (left_label, left) in enumerate(items):
        for right_label, right in items[index + 1 :]:
            if _overlap(PurePosixPath(left), PurePosixPath(right)):
                raise StorageIsolationError(
                    f"{left_label} and {right_label} storage overlap: {left} <> {right}"
                )

    # A non-exclusive Unraid user share and its direct pool/disk view can refer
    # to the same data while reporting different devices/inodes. Reject mixing
    # those views for the same share even when the selected subdirectories differ.
    for index, (left_label, left) in enumerate(original_items):
        left_view = _unraid_view(left)
        if left_view is None:
            continue
        for right_label, right in original_items[index + 1 :]:
            right_view = _unraid_view(right)
            if (
                right_view is not None
                and left_view[1] == right_view[1]
                and left_view[0] != right_view[0]
            ):
                raise StorageIsolationError(
                    f"{left_label} and {right_label} mix /mnt/user and direct views "
                    f"of Unraid share {left_view[1]!r}"
                )
    return resolved


def _mount_for(path: PurePosixPath, mounts: list[MountInfo]) -> MountInfo:
    candidates = [mount for mount in mounts if _is_within_or_equal(path, mount.mountpoint)]
    if not candidates:
        raise StorageIsolationError(f"cannot identify backing mount for {path}")
    return max(candidates, key=lambda mount: len(mount.mountpoint.parts))


def _backing_path(path: PurePosixPath, mount: MountInfo) -> PurePosixPath:
    relative = path.relative_to(mount.mountpoint)
    return mount.root.joinpath(*relative.parts)


def validate_container_mounts(
    paths: dict[str, Path],
    *,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
    acceptance_report: Path | None = None,
) -> dict[str, Path]:
    resolved = validate_host_paths(paths)
    try:
        with mountinfo_path.open("r", encoding="utf-8") as stream:
            mounts = parse_mountinfo(stream)
    except OSError as exc:
        raise StorageIsolationError(f"cannot read {mountinfo_path}: {exc}") from exc

    backing: dict[str, tuple[tuple[str, str, str], PurePosixPath]] = {}
    for label, value in resolved.items():
        container_path = PurePosixPath(value)
        mount = _mount_for(container_path, mounts)
        identity = (mount.device, mount.filesystem, mount.source)
        backing[label] = (identity, _backing_path(container_path, mount))

    items = list(backing.items())
    for index, (left_label, (left_identity, left_path)) in enumerate(items):
        for right_label, (right_identity, right_path) in items[index + 1 :]:
            if left_identity == right_identity and _overlap(left_path, right_path):
                raise StorageIsolationError(
                    f"{left_label} and {right_label} bind mounts alias on the host: "
                    f"{left_path} <> {right_path}"
                )
    if acceptance_report is not None:
        report = _resolve_existing_file("acceptance report", acceptance_report)
        report_mount = _mount_for(PurePosixPath(report), mounts)
        report_identity = (
            report_mount.device,
            report_mount.filesystem,
            report_mount.source,
        )
        try:
            temp_identity = backing["temporary"][0]
        except KeyError as exc:
            raise StorageIsolationError(
                "temporary storage is required when binding acceptance-report backing"
            ) from exc
        if report_identity != temp_identity:
            raise StorageIsolationError(
                "temporary storage and retained acceptance report no longer share the accepted backing filesystem"
            )
    return resolved


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="verify that NEF input, TIFF output, SDK, and temp roots are separate"
    )
    parser.add_argument("--input", type=Path, default=Path(os.environ.get("NEF_INPUT", "/input")))
    parser.add_argument("--output", type=Path, default=Path(os.environ.get("NEF_OUTPUT", "/output")))
    parser.add_argument("--temp", type=Path, default=Path(os.environ.get("NEF_TEMP_DIR", "/work/nef-watch")))
    parser.add_argument("--sdk", type=Path, default=Path(os.environ.get("NIKON_SDK_DIR", "/nikon-sdk")))
    parser.add_argument("--state", type=Path)
    parser.add_argument(
        "--acceptance-report",
        type=Path,
        help="retained acceptance report whose backing filesystem must match temp",
    )
    parser.add_argument(
        "--trusted-scratch-root",
        type=Path,
        help="mounted direct Unraid scratch filesystem containing temp and the report",
    )
    host_mode = parser.add_mutually_exclusive_group()
    host_mode.add_argument(
        "--require-unraid-direct-temp",
        action="store_true",
        help="require the production direct-pool scratch and report topology",
    )
    host_mode.add_argument(
        "--allow-generic-host-paths",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--max-temp-filesystem-bytes",
        type=parse_temp_filesystem_bytes,
        default=os.environ.get("NEF_WATCH_TEMP_CAPACITY_BYTES"),
        help="require the entire temporary filesystem to be no larger than "
        "this byte ceiling (env NEF_WATCH_TEMP_CAPACITY_BYTES)",
    )
    parser.add_argument(
        "--min-temp-filesystem-free-bytes",
        type=parse_temp_minimum_free_bytes,
        default=os.environ.get("NEF_WATCH_TEMP_MIN_FREE_BYTES"),
        help="require this many bytes to be available to the container user "
        "(env NEF_WATCH_TEMP_MIN_FREE_BYTES)",
    )
    parser.add_argument("--container", action="store_true", help="also verify backing bind-mount roots")
    parser.add_argument(
        "--runtime",
        action="store_true",
        help="validate the unprivileged production runtime mounts (the host SDK is intentionally absent)",
    )
    parser.add_argument("--mountinfo", type=Path, default=Path("/proc/self/mountinfo"), help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = {
        "input": args.input,
        "output": args.output,
        "temporary": args.temp,
    }
    if not args.runtime:
        paths["Nikon SDK"] = args.sdk
    if args.state is not None:
        paths["state"] = args.state
    try:
        if args.runtime and not args.container:
            raise StorageIsolationError("runtime mode requires container mount validation")
        if args.container:
            if args.require_unraid_direct_temp or args.allow_generic_host_paths:
                raise StorageIsolationError(
                    "host path-mode flags cannot be used inside the container"
                )
            if args.runtime and args.acceptance_report is None:
                raise StorageIsolationError(
                    "runtime mode requires the retained acceptance report backing check"
                )
            resolved = validate_container_mounts(
                paths,
                mountinfo_path=args.mountinfo,
                acceptance_report=args.acceptance_report,
            )
        else:
            if not args.require_unraid_direct_temp and not args.allow_generic_host_paths:
                raise StorageIsolationError(
                    "host validation must explicitly require direct Unraid temp or mark a generic fixture"
                )
            resolved = validate_host_paths(
                paths,
                allow_generic_host_paths=args.allow_generic_host_paths,
            )
            if args.require_unraid_direct_temp:
                if args.trusted_scratch_root is None or args.acceptance_report is None:
                    raise StorageIsolationError(
                        "direct Unraid validation requires --trusted-scratch-root and --acceptance-report"
                    )
                validate_unraid_temp_binding(
                    resolved["temporary"],
                    trusted_scratch_root=args.trusted_scratch_root,
                    acceptance_report=args.acceptance_report,
                )
        if (
            args.min_temp_filesystem_free_bytes is not None
            and args.max_temp_filesystem_bytes is None
        ):
            raise StorageIsolationError(
                "min-temp-filesystem-free-bytes requires "
                "max-temp-filesystem-bytes"
            )
        if args.max_temp_filesystem_bytes is not None:
            validate_temp_filesystem_capacity(
                resolved["temporary"],
                args.max_temp_filesystem_bytes,
                minimum_free_bytes=args.min_temp_filesystem_free_bytes,
            )
    except StorageIsolationError as exc:
        print(f"storage isolation check failed: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    print("storage isolation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
