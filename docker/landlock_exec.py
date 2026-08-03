#!/usr/bin/env python3
"""Execute a command inside an irreversible Landlock and seccomp sandbox.

The container is linux/amd64-only, so using the stable x86-64 syscall numbers
keeps this helper dependency-free.  Both policies are inherited across exec and
by every child process.  Landlock complements Docker's mount policy, while a
small seccomp-BPF filter closes Landlock's unmediated metadata-mutation syscall
families for same-UID host-facing files.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import os
import platform
import stat
import sys
from pathlib import Path


SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
LANDLOCK_SCOPE_SIGNAL = 1 << 1
MINIMUM_LANDLOCK_ABI = 6
PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2

# Linux UAPI constants for classic BPF over ``struct seccomp_data``.
AUDIT_ARCH_X86_64 = 0xC000003E
X32_SYSCALL_BIT = 0x40000000
SECCOMP_DATA_NR_OFFSET = 0
SECCOMP_DATA_ARCH_OFFSET = 4
SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_ALLOW = 0x7FFF0000
BPF_LD_W_ABS = 0x20
BPF_JMP_JEQ_K = 0x15
BPF_JMP_JSET_K = 0x45
BPF_RET_K = 0x06

# Stable Linux x86-64 syscall numbers.  io_uring is denied because its
# SETXATTR/FSETXATTR operations would otherwise bypass a syscall-number filter.
SYS_IO_URING_SETUP = 425
SYS_IO_URING_ENTER = 426
SYS_IO_URING_REGISTER = 427
DENIED_SYSCALLS = (
    ("chmod", 90),
    ("fchmod", 91),
    ("chown", 92),
    ("fchown", 93),
    ("lchown", 94),
    ("utime", 132),
    ("setxattr", 188),
    ("lsetxattr", 189),
    ("fsetxattr", 190),
    ("removexattr", 197),
    ("lremovexattr", 198),
    ("fremovexattr", 199),
    ("utimes", 235),
    ("fchownat", 260),
    ("futimesat", 261),
    ("fchmodat", 268),
    ("utimensat", 280),
    ("io_uring_setup", SYS_IO_URING_SETUP),
    ("io_uring_enter", SYS_IO_URING_ENTER),
    ("io_uring_register", SYS_IO_URING_REGISTER),
    ("fchmodat2", 452),
)

ACCESS_EXECUTE = 1 << 0
ACCESS_WRITE_FILE = 1 << 1
ACCESS_READ_FILE = 1 << 2
ACCESS_READ_DIR = 1 << 3
ACCESS_REMOVE_DIR = 1 << 4
ACCESS_REMOVE_FILE = 1 << 5
ACCESS_MAKE_CHAR = 1 << 6
ACCESS_MAKE_DIR = 1 << 7
ACCESS_MAKE_REG = 1 << 8
ACCESS_MAKE_SOCK = 1 << 9
ACCESS_MAKE_FIFO = 1 << 10
ACCESS_MAKE_BLOCK = 1 << 11
ACCESS_MAKE_SYM = 1 << 12
ACCESS_REFER = 1 << 13
ACCESS_TRUNCATE = 1 << 14
ACCESS_IOCTL_DEV = 1 << 15

BASE_HANDLED = (
    ACCESS_EXECUTE
    | ACCESS_WRITE_FILE
    | ACCESS_READ_FILE
    | ACCESS_READ_DIR
    | ACCESS_REMOVE_DIR
    | ACCESS_REMOVE_FILE
    | ACCESS_MAKE_CHAR
    | ACCESS_MAKE_DIR
    | ACCESS_MAKE_REG
    | ACCESS_MAKE_SOCK
    | ACCESS_MAKE_FIFO
    | ACCESS_MAKE_BLOCK
    | ACCESS_MAKE_SYM
)
BASE_READ = ACCESS_EXECUTE | ACCESS_READ_FILE | ACCESS_READ_DIR
REGULAR_FILE_READ = ACCESS_EXECUTE | ACCESS_READ_FILE
REGULAR_FILE_READ_WRITE = ACCESS_READ_FILE | ACCESS_WRITE_FILE | ACCESS_TRUNCATE
DEVICE_READ = ACCESS_READ_FILE
DEVICE_READ_WRITE = ACCESS_READ_FILE | ACCESS_WRITE_FILE | ACCESS_IOCTL_DEV


class RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        # Kept zero.  Network rules were added after the filesystem ABI and are
        # intentionally outside this helper's narrow responsibility.
        ("handled_access_net", ctypes.c_uint64),
        # ABI 6 added signal scoping.  This prevents an exploited renderer from
        # signalling the same-UID watcher, X server, or sibling render jobs.
        ("scoped", ctypes.c_uint64),
    ]


class PathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
        ("reserved", ctypes.c_uint32),
    ]


class SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    ]


class SockFprog(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_ushort),
        ("filters", ctypes.POINTER(SockFilter)),
    ]


class LandlockError(RuntimeError):
    pass


class SeccompError(RuntimeError):
    pass


def _libc() -> ctypes.CDLL:
    library = ctypes.CDLL(None, use_errno=True)
    library.syscall.restype = ctypes.c_long
    library.prctl.restype = ctypes.c_int
    return library


def _statement(code: int, value: int) -> SockFilter:
    return SockFilter(code=code, jt=0, jf=0, k=value)


def _jump(code: int, value: int, jump_true: int, jump_false: int) -> SockFilter:
    return SockFilter(code=code, jt=jump_true, jf=jump_false, k=value)


def build_metadata_seccomp_filter() -> list[SockFilter]:
    """Build an amd64-only filter denying unmediated metadata mutation."""

    denied = SECCOMP_RET_ERRNO | errno.EPERM
    filters = [
        _statement(BPF_LD_W_ABS, SECCOMP_DATA_ARCH_OFFSET),
        # Any unexpected syscall ABI is a policy bypass attempt.  x32 reports
        # AUDIT_ARCH_X86_64, so its syscall-number bit is checked separately.
        _jump(BPF_JMP_JEQ_K, AUDIT_ARCH_X86_64, 1, 0),
        _statement(BPF_RET_K, SECCOMP_RET_KILL_PROCESS),
        _statement(BPF_LD_W_ABS, SECCOMP_DATA_NR_OFFSET),
        _jump(BPF_JMP_JSET_K, X32_SYSCALL_BIT, 0, 1),
        _statement(BPF_RET_K, denied),
    ]
    for _name, number in DENIED_SYSCALLS:
        filters.extend(
            (
                _jump(BPF_JMP_JEQ_K, number, 0, 1),
                _statement(BPF_RET_K, denied),
            )
        )
    filters.append(_statement(BPF_RET_K, SECCOMP_RET_ALLOW))
    return filters


def install_metadata_seccomp(library: ctypes.CDLL) -> None:
    """Irreversibly install the metadata-mutation filter for this thread."""

    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        raise SeccompError("seccomp launcher requires linux/amd64")
    instructions = build_metadata_seccomp_filter()
    instruction_array = (SockFilter * len(instructions))(*instructions)
    program = SockFprog(
        length=len(instructions),
        filters=ctypes.cast(instruction_array, ctypes.POINTER(SockFilter)),
    )
    if (
        library.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(program), 0, 0)
        != 0
    ):
        error = ctypes.get_errno()
        raise SeccompError(
            f"cannot install metadata seccomp filter: {os.strerror(error)}"
        )


def _syscall(library: ctypes.CDLL, number: int, *arguments: object) -> int:
    result = int(library.syscall(number, *arguments))
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def landlock_abi(library: ctypes.CDLL) -> int:
    try:
        return _syscall(
            library,
            SYS_LANDLOCK_CREATE_RULESET,
            ctypes.c_void_p(),
            ctypes.c_size_t(0),
            ctypes.c_uint32(LANDLOCK_CREATE_RULESET_VERSION),
        )
    except OSError as exc:
        if exc.errno in (errno.ENOSYS, errno.EOPNOTSUPP, errno.EINVAL):
            return 0
        raise


def handled_rights(abi: int) -> int:
    rights = BASE_HANDLED
    if abi >= 2:
        rights |= ACCESS_REFER
    if abi >= 3:
        rights |= ACCESS_TRUNCATE
    if abi >= 5:
        rights |= ACCESS_IOCTL_DEV
    return rights


def _resolved_existing(path_text: str) -> Path:
    try:
        return Path(path_text).resolve(strict=True)
    except OSError as exc:
        raise LandlockError(
            f"allowlisted path is unavailable: {path_text}: {exc}"
        ) from exc


def allowed_access_for_mode(mode: int, *, writable: bool, handled: int) -> int:
    """Return only rights the kernel accepts and this inode type needs."""

    if stat.S_ISDIR(mode):
        rights = handled if writable else BASE_READ
    elif stat.S_ISREG(mode):
        rights = REGULAR_FILE_READ_WRITE if writable else REGULAR_FILE_READ
    elif stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        # IOCTL_DEV is meaningful only for devices.  Do not grant it to regular
        # files, and do not grant execution or truncation to device nodes.
        rights = DEVICE_READ_WRITE if writable else DEVICE_READ
    else:
        raise LandlockError(
            "allowlisted path must be a directory, regular file, or device"
        )
    return rights & handled


def add_path_rule(
    library: ctypes.CDLL,
    ruleset_fd: int,
    path: Path,
    *,
    writable: bool,
    handled: int,
) -> None:
    flags = getattr(os, "O_PATH", 0o10000000) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise LandlockError(f"cannot open allowlisted path {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        # LANDLOCK_RULE_PATH_BENEATH rejects directory-only rights such as
        # MAKE_DIR and REMOVE_DIR when parent_fd names a file or device.
        allowed = allowed_access_for_mode(
            metadata.st_mode, writable=writable, handled=handled
        )
        attribute = PathBeneathAttr(allowed_access=allowed, parent_fd=descriptor)
        _syscall(
            library,
            SYS_LANDLOCK_ADD_RULE,
            ctypes.c_int(ruleset_fd),
            ctypes.c_int(LANDLOCK_RULE_PATH_BENEATH),
            ctypes.byref(attribute),
            ctypes.c_uint32(0),
        )
    except OSError as exc:
        raise LandlockError(f"cannot allowlist {path}: {exc}") from exc
    finally:
        os.close(descriptor)


def restrict_filesystem(read_only: list[str], read_write: list[str]) -> int:
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        raise LandlockError("Landlock launcher requires linux/amd64")
    library = _libc()
    abi = landlock_abi(library)
    if abi <= 0:
        raise LandlockError(
            "Landlock is unavailable (kernel CONFIG_SECURITY_LANDLOCK or Docker "
            "seccomp support is missing)"
        )
    if abi < MINIMUM_LANDLOCK_ABI:
        raise LandlockError(
            f"Landlock ABI {abi} is too old; ABI {MINIMUM_LANDLOCK_ABI} or newer "
            "is required for filesystem truncation and signal isolation"
        )
    handled = handled_rights(abi)
    attribute = RulesetAttr(
        handled_access_fs=handled,
        handled_access_net=0,
        scoped=LANDLOCK_SCOPE_SIGNAL,
    )
    try:
        ruleset_fd = _syscall(
            library,
            SYS_LANDLOCK_CREATE_RULESET,
            ctypes.byref(attribute),
            ctypes.c_size_t(ctypes.sizeof(attribute)),
            ctypes.c_uint32(0),
        )
    except OSError as exc:
        raise LandlockError(f"cannot create Landlock ruleset: {exc}") from exc
    try:
        seen: set[tuple[bool, Path]] = set()
        for writable, values in ((False, read_only), (True, read_write)):
            for value in values:
                path = _resolved_existing(value)
                # A later writable rule deliberately augments an earlier
                # read-only ancestor, so deduplicate only within exact mode.
                key = (writable, path)
                if key in seen:
                    continue
                seen.add(key)
                add_path_rule(
                    library,
                    ruleset_fd,
                    path,
                    writable=writable,
                    handled=handled,
                )
        if library.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise LandlockError(
                f"cannot set no_new_privs before Landlock: {os.strerror(error)}"
            )
        _syscall(
            library,
            SYS_LANDLOCK_RESTRICT_SELF,
            ctypes.c_int(ruleset_fd),
            ctypes.c_uint32(0),
        )
    finally:
        os.close(ruleset_fd)
    # Landlock intentionally does not mediate chmod/chown/xattr/time changes.
    # Stack a process-local filter before exec so same-UID files outside the
    # allowlist cannot be mutated through those syscall families or io_uring.
    install_metadata_seccomp(library)
    return abi


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "run a command with an irreversible Landlock path allowlist and "
            "metadata-mutation seccomp filter"
        )
    )
    parser.add_argument("--ro", action="append", default=[], metavar="PATH")
    parser.add_argument("--rw", action="append", default=[], metavar="PATH")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        print("landlock-exec: command is required after --", file=sys.stderr)
        return 64
    try:
        abi = restrict_filesystem(args.ro, args.rw)
    except (LandlockError, SeccompError, OSError) as exc:
        print(f"landlock-exec: {exc}", file=sys.stderr)
        return 77
    os.environ["NEF_WATCH_LANDLOCK_ABI"] = str(abi)
    try:
        os.execvpe(command[0], command, os.environ)
    except OSError as exc:
        print(f"landlock-exec: cannot execute {command[0]}: {exc}", file=sys.stderr)
        return 126


if __name__ == "__main__":
    raise SystemExit(main())
