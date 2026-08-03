#!/usr/bin/env python3
"""Exercise the required Landlock ABI and metadata seccomp boundary."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, NoReturn


EXIT_CONFIG = 78
LAUNCHER = Path("/usr/local/libexec/nef-watch-landlock-exec.py")


class ProbeError(RuntimeError):
    """The running kernel cannot enforce the reviewed sandbox boundary."""


def _fail(message: str) -> NoReturn:
    raise ProbeError(message)


def _expect_eperm(label: str, operation: Callable[[], object]) -> None:
    try:
        operation()
    except OSError as exc:
        if exc.errno == errno.EPERM:
            return
        _fail(f"{label} returned errno {exc.errno}, expected EPERM")
    _fail(f"{label} unexpectedly succeeded")


def _expect_access_denied(label: str, operation: Callable[[], object]) -> None:
    try:
        operation()
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM):
            return
        _fail(f"{label} returned unexpected errno {exc.errno}")
    _fail(f"{label} unexpectedly succeeded")


def confined_probe(allowed: Path, outside: Path) -> int:
    created = allowed / "ordinary-io"
    created.write_bytes(b"landlock-probe")
    if created.read_bytes() != b"landlock-probe":
        _fail("ordinary read/write verification failed inside the writable rule")
    created.unlink()

    outside_create = outside.with_name(outside.name + ".ordinary-io")
    _expect_access_denied(
        "ordinary read outside the Landlock allowlist", outside.read_bytes
    )
    _expect_access_denied(
        "ordinary create/write outside the Landlock allowlist",
        lambda: outside_create.write_bytes(b"landlock-bypass"),
    )

    inside = allowed / "metadata-target"
    for label, target in (("inside", inside), ("outside", outside)):
        _expect_eperm(f"chmod {label}", lambda target=target: os.chmod(target, 0o600))
        _expect_eperm(
            f"chown {label}",
            lambda target=target: os.chown(target, os.getuid(), os.getgid()),
        )
        _expect_eperm(f"utime {label}", lambda target=target: os.utime(target))
        _expect_eperm(
            f"setxattr {label}",
            lambda target=target: os.setxattr(target, b"user.nef_watch_probe", b"1"),
        )
        _expect_eperm(
            f"removexattr {label}",
            lambda target=target: os.removexattr(
                target, b"user.nef_watch_probe_missing"
            ),
        )
    return 0


def _trusted_image_file(path: Path, label: str) -> Path:
    try:
        requested = path.absolute()
        resolved = requested.resolve(strict=True)
        metadata = requested.lstat()
    except OSError as exc:
        raise ProbeError(f"cannot inspect sealed {label}: {exc}") from exc
    if (
        requested != resolved
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o555
    ):
        _fail(f"{label} is not a canonical root-owned single-link mode-0555 file")
    return resolved


def _trusted_launcher() -> Path:
    return _trusted_image_file(LAUNCHER, "Landlock launcher")


def run_probe(work_root: Path) -> int:
    try:
        requested = work_root.absolute()
        resolved = requested.resolve(strict=True)
    except OSError as exc:
        raise ProbeError(f"cannot resolve probe work root: {exc}") from exc
    if requested != resolved or not resolved.is_dir() or not os.access(resolved, os.W_OK):
        _fail("probe work root must be a canonical writable directory")
    launcher = _trusted_launcher()
    probe_path = _trusted_image_file(Path(__file__), "Landlock probe")
    job = Path(tempfile.mkdtemp(prefix=".nef-watch-landlock-probe.", dir=resolved))
    allowed = job / "allowed"
    outside = job / "outside"
    failure: ProbeError | None = None
    try:
        allowed.mkdir(mode=0o700)
        (allowed / "metadata-target").write_bytes(b"inside")
        outside.write_bytes(b"outside")
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                os.fspath(launcher),
                "--ro",
                "/usr",
                "--rw",
                os.fspath(allowed),
                "--",
                sys.executable,
                "-I",
                os.fspath(probe_path),
                "--confined",
                os.fspath(allowed),
                os.fspath(outside),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip()
            _fail(f"Landlock/seccomp child probe failed: {detail or result.returncode}")
    except ProbeError as exc:
        failure = exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        failure = ProbeError(f"cannot run Landlock/seccomp child probe: {exc}")
    cleanup_failure: OSError | None = None
    try:
        if job.exists() or job.is_symlink():
            shutil.rmtree(job)
    except OSError as exc:
        cleanup_failure = exc
    if job.exists() or job.is_symlink():
        cleanup_failure = cleanup_failure or OSError("probe directory remains")
    if failure is not None:
        if cleanup_failure is not None:
            raise ProbeError(f"{failure}; cleanup also failed: {cleanup_failure}") from failure
        raise failure
    if cleanup_failure is not None:
        raise ProbeError(f"Landlock probe cleanup failed: {cleanup_failure}")
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        if len(arguments) == 3 and arguments[0] == "--confined":
            return confined_probe(Path(arguments[1]), Path(arguments[2]))
        if len(arguments) == 1:
            return run_probe(Path(arguments[0]))
        _fail("usage: landlock_probe.py WORK_ROOT")
    except ProbeError as exc:
        print(f"Landlock enforcement probe failed: {exc}", file=sys.stderr)
        return EXIT_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())
