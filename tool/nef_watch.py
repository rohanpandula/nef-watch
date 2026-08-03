#!/usr/bin/env python3
"""
nef-watch — watch a folder for Nikon NEF files and convert each to a TIFF with
Nikon's native in-camera rendering, via the Nikon Image SDK.

Render core: the compiled `nef_render` helper (next to this script). Each NEF is
~6 s of (lightly-threaded) SDK development, so conversions run in a small worker
pool to use the otherwise-idle cores. TIFF encode, watching, and routing live here.

Examples:
  # watch ~/Incoming, write TIFFs to ~/Exports (Ctrl-C to stop)
  ./nef_watch.py ~/Incoming --out ~/Exports

  # one-shot: convert everything already in a folder, then exit
  ./nef_watch.py ~/Shoot --out ~/Shoot/tiff --once -j 6
"""
# /// script
# requires-python = ">=3.9"
# dependencies = ["pillow", "numpy", "tifffile", "imagecodecs"]
# ///
import argparse
from collections import OrderedDict
import concurrent.futures as cf
import ctypes
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import selectors
import signal
import shutil
import sqlite3
import stat as stat_module
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


# This entry point intentionally lives before third-party imports.  Every
# external helper is wrapped in a fresh copy of this lightweight Linux
# subreaper, so its timeout must not include importing image/array libraries.
_EARLY_MAX_SUPERVISED_PROCESSES = 4096
_PR_SET_DUMPABLE = 4
_SUPERVISOR_REGISTRY_LOCK = threading.Lock()
_ACTIVE_SUPERVISORS = set()


def _set_linux_dumpable(enabled):
    if not sys.platform.startswith("linux"):
        raise OSError(errno.ENOSYS, "process dumpability is a Linux facility")
    library = ctypes.CDLL(None, use_errno=True)
    library.prctl.restype = ctypes.c_int
    if library.prctl(_PR_SET_DUMPABLE, int(bool(enabled)), 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _linux_process_start_ticks(pid=None):
    if not sys.platform.startswith("linux"):
        return None
    process = os.getpid() if pid is None else int(pid)
    payload = Path(f"/proc/{process}/stat").read_text(encoding="ascii")
    delimiter = payload.rfind(") ")
    if delimiter < 0:
        raise OSError(errno.EIO, "Linux process stat has an invalid shape")
    fields_from_state = payload[delimiter + 2 :].split()
    # fields_from_state[0] is field 3 (state); process start time is field 22.
    if len(fields_from_state) <= 19:
        raise OSError(errno.EIO, "Linux process stat is truncated")
    ticks = int(fields_from_state[19])
    if ticks <= 0:
        raise OSError(errno.EIO, "Linux process start time is invalid")
    return ticks


def harden_watcher_process():
    """Hide watcher memory from same-UID helpers and adopt escaped descendants."""
    if not sys.platform.startswith("linux"):
        return
    _set_linux_dumpable(False)
    _enable_linux_child_subreaper()
    _require_linux_children_interface()


def _enable_linux_child_subreaper():
    if not sys.platform.startswith("linux"):
        raise OSError(errno.ENOSYS, "child subreapers require Linux")
    library = ctypes.CDLL(None, use_errno=True)
    library.prctl.restype = ctypes.c_int
    if library.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _require_linux_children_interface():
    path = Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children")
    try:
        path.read_text(encoding="ascii")
    except OSError as error:
        raise OSError(
            error.errno or errno.ENOSYS,
            "the Linux /proc children interface is required for descendant cleanup",
        ) from error


def _linux_direct_children(pid):
    task_root = Path(f"/proc/{pid}/task")
    try:
        tasks = tuple(task_root.iterdir())
    except (FileNotFoundError, ProcessLookupError, OSError):
        return ()
    children = set()
    for task in tasks:
        try:
            payload = (task / "children").read_text(encoding="ascii").strip()
        except (FileNotFoundError, ProcessLookupError, OSError):
            continue
        for value in payload.split():
            try:
                child = int(value)
            except ValueError:
                continue
            if child > 1 and child != os.getpid():
                children.add(child)
    return tuple(sorted(children))


def _linux_descendants(pid=None):
    root = os.getpid() if pid is None else int(pid)
    pending = list(_linux_direct_children(root))
    seen = set()
    preorder = []
    while pending:
        child = pending.pop()
        if child in seen:
            continue
        seen.add(child)
        preorder.append(child)
        if len(preorder) >= _EARLY_MAX_SUPERVISED_PROCESSES:
            break
        pending.extend(_linux_direct_children(child))
    return tuple(reversed(preorder))


def _reap_adopted_children():
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except (ChildProcessError, InterruptedError):
            return
        if pid <= 0:
            return


def _signal_supervised_descendants(signum):
    descendants = _linux_descendants()
    for pid in descendants:
        descriptor = -1
        try:
            if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
                descriptor = os.pidfd_open(pid)
                signal.pidfd_send_signal(descriptor, signum)
            else:
                os.kill(pid, signum)
        except (ProcessLookupError, PermissionError):
            continue
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    return descendants


def _drain_supervised_descendants(grace_seconds):
    deadline = time.monotonic() + max(0.0, float(grace_seconds))
    while True:
        descendants = _signal_supervised_descendants(signal.SIGTERM)
        _reap_adopted_children()
        if not descendants:
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
    kill_deadline = time.monotonic() + 1.0
    while True:
        descendants = _signal_supervised_descendants(signal.SIGKILL)
        _reap_adopted_children()
        if not descendants:
            break
        if time.monotonic() >= kill_deadline:
            raise OSError(
                errno.EBUSY,
                "a supervised helper descendant survived SIGKILL",
            )
        time.sleep(0.01)
    _reap_adopted_children()


def _signal_process_ids(process_ids, signum):
    remaining = []
    for pid in process_ids:
        descriptor = -1
        try:
            if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
                descriptor = os.pidfd_open(pid)
                signal.pidfd_send_signal(descriptor, signum)
            else:
                os.kill(pid, signum)
            remaining.append(pid)
        except (ProcessLookupError, PermissionError):
            continue
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    return tuple(remaining)


def _reap_process_ids(process_ids):
    for pid in process_ids:
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, InterruptedError):
            continue


def _unowned_watcher_descendants():
    # Supervisor creation and registration use this same lock.  Taking the
    # live registry while classifying descendants therefore closes both gaps:
    # a freshly spawned supervisor can never be visible to /proc before it is
    # protected, and a drain never relies on a registry snapshot that becomes
    # stale while other jobs start.
    with _SUPERVISOR_REGISTRY_LOCK:
        descendants = _linux_descendants()
        all_descendants = set(descendants)
        protected = set()
        for supervisor in _ACTIVE_SUPERVISORS:
            protected.add(supervisor)
            protected.update(_linux_descendants(supervisor))
        return tuple(
            pid for pid in descendants if pid in all_descendants - protected
        )


def _drain_unowned_watcher_descendants(grace_seconds):
    deadline = time.monotonic() + max(0.0, float(grace_seconds))
    while True:
        descendants = _unowned_watcher_descendants()
        _signal_process_ids(descendants, signal.SIGTERM)
        _reap_process_ids(descendants)
        if not descendants:
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
    kill_deadline = time.monotonic() + 1.0
    while True:
        descendants = _unowned_watcher_descendants()
        _signal_process_ids(descendants, signal.SIGKILL)
        _reap_process_ids(descendants)
        if not descendants:
            return
        if time.monotonic() >= kill_deadline:
            raise OSError(
                errno.EBUSY,
                "an adopted helper descendant survived SIGKILL",
            )
        time.sleep(0.01)


def _process_supervisor_main(argv):
    if len(argv) < 3 or argv[1] != "--":
        print("process supervisor: invalid invocation", file=sys.stderr)
        return 64
    try:
        grace_seconds = max(0.0, float(argv[0]))
    except ValueError:
        print("process supervisor: invalid grace period", file=sys.stderr)
        return 64
    command = argv[2:]
    if not command:
        print("process supervisor: command is required", file=sys.stderr)
        return 64
    try:
        # The long-running watcher is deliberately non-dumpable. Helpers need
        # their ordinary self-inspection semantics without regaining access to
        # the watcher's memory, so reset only this freshly exec'd supervisor.
        _set_linux_dumpable(True)
        _enable_linux_child_subreaper()
        _require_linux_children_interface()
    except OSError as error:
        print(f"process supervisor: cannot become subreaper: {error}", file=sys.stderr)
        return 125

    requested_signal = [None]

    def request_stop(signum, _frame):
        requested_signal[0] = signum

    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, request_stop)
    try:
        try:
            child = subprocess.Popen(command, start_new_session=True)
        except OSError as error:
            print(f"process supervisor: cannot execute helper: {error}", file=sys.stderr)
            return 126
        while child.poll() is None and requested_signal[0] is None:
            try:
                child.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                pass
        returncode = child.poll()
        try:
            _drain_supervised_descendants(grace_seconds)
        except OSError as error:
            print(f"process supervisor: descendant cleanup failed: {error}", file=sys.stderr)
            return 125
        try:
            if returncode is None:
                returncode = child.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            try:
                child.kill()
            except ProcessLookupError:
                pass
            returncode = child.wait()
        if requested_signal[0] is not None:
            return 128 + int(requested_signal[0])
        # 125 is reserved for the containment layer itself so the parent can
        # distinguish a cleanup failure from an ordinary tool error.
        return 124 if int(returncode) == 125 else int(returncode)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__" and sys.argv[1:2] == ["--_process-supervisor"]:
    raise SystemExit(_process_supervisor_main(sys.argv[2:]))


import numpy as np  # noqa: E402 - supervisor exits before third-party imports
from PIL import Image  # noqa: E402 - supervisor exits before third-party imports

HERE = Path(__file__).resolve().parent
DEFAULT_RENDER_BIN = HERE / "nef_render"
STAGED_PROFILE = HERE / "Contents" / "Resources" / "NKsRGB.icm"
DEFAULT_PROFILE = STAGED_PROFILE
RAW_SUFFIXES = {".nef", ".nrw"}
FORMAT_ORDER = ("tiff", "jpeg", "dng")
RASTER_FORMATS = {"tiff", "jpeg"}
EXTS = {"tiff": ".tif", "jpeg": ".jpg", "dng": ".dng"}
MAX_RENDER_PIXELS = 100_000_000
MAX_RENDER_BYTES = 800_000_000
MAX_RASTER_OUTPUT_BYTES = 1024 * 1024 * 1024
MAX_INPUT_MIB = 4096
DEFAULT_MAX_INPUT_MIB = 512
MAX_DNG_CONTAINER_OVERHEAD_BYTES = 64 * 1024 * 1024
MAX_DNG_OUTPUT_BYTES = (
    MAX_RASTER_OUTPUT_BYTES
    + MAX_DNG_CONTAINER_OVERHEAD_BYTES
    + MAX_INPUT_MIB * 1024 * 1024
)
MAX_NKRAW_HEADER = 256
MAX_PROCESS_OUTPUT_BYTES = 64 * 1024
MAX_JOBS = 32
MAX_PENDING = 1024
DEFAULT_MAX_SCAN_ENTRIES = 100_000
MAX_SCAN_ENTRIES = 1_000_000
MAX_UNSAFE_WARNING_CACHE = 1024
DEFAULT_RECOVERY_BATCH_ENTRIES = 2048
MAX_RECOVERY_SWEEP_ENTRIES = 1_000_000
RECOVERY_INTERVAL_SECONDS = 60.0
STATE_RETENTION_SECONDS = 90 * 24 * 60 * 60
STATE_PRUNE_INTERVAL_SECONDS = 60 * 60
MAX_STATE_PRUNE_ROWS = 1024
DEFAULT_MAX_STATE_SOURCES = 100_000
MAX_STATE_SOURCES = 1_000_000
DEFAULT_STATE_MAX_MIB = 512
MIN_STATE_MAX_MIB = 32
MAX_STATE_MAX_MIB = 4096
MAX_FAILURE_TEXT_BYTES = 4096
MAX_FAILURE_CODE_BYTES = 128
MAX_OUTPUT_METADATA_BYTES = 64 * 1024
MAX_PUBLICATION_PLAN_BYTES = 64 * 1024
MAX_PENDING_PUBLICATIONS = MAX_PENDING * 2
MAX_INTERVAL_SECONDS = 3600.0
STATE_SCHEMA = 3
HEALTH_SCHEMA = 2
CONFIG_SCHEMA = 2
LEGACY_BASELINE_MARKER = ".nef-watch-skip-existing-seeded"
MAX_LEGACY_MARKER_BYTES = 16 * 1024 * 1024
MAX_LEGACY_ARCHIVE_ATTEMPTS = 32
SOURCE_KEY_PREFIX = "source-relative-v1:"
Image.MAX_IMAGE_PIXELS = MAX_RENDER_PIXELS
LOG_FILE = None
LOG_LOCK = threading.Lock()
STOP_EVENT = threading.Event()
FATAL_STOP_EVENT = threading.Event()
UNSAFE_PATH_WARNED = OrderedDict()
ARTIFACT_DIR_NAME = ".nef-watch-artifacts"
ARTIFACT_STAGE_DIR = "staged"
ARTIFACT_BACKUP_DIR = "rollback"
ARTIFACT_TRASH_DIR = "trash"
ARTIFACT_ROLES = frozenset(
    {ARTIFACT_STAGE_DIR, ARTIFACT_BACKUP_DIR, ARTIFACT_TRASH_DIR}
)
ARTIFACT_UUID_RE = re.compile(r"^[0-9a-f]{32}$")
DEFAULT_LANDLOCK_EXEC = Path(
    "/usr/local/libexec/nef-watch-landlock-exec.py"
)


def _request_fatal_stop():
    FATAL_STOP_EVENT.set()
    STOP_EVENT.set()


class ConversionError(RuntimeError):
    """A conversion failure with an explicit retry policy."""

    permanent = False
    code = "conversion"


class PermanentConversionError(ConversionError):
    permanent = True


class TransientConversionError(ConversionError):
    pass


class SourceChangedError(TransientConversionError):
    code = "source-changed"


class ProcessTimeoutError(TransientConversionError):
    code = "timeout"


class ProcessContainmentError(PermanentConversionError):
    code = "containment"


class InvalidRawError(PermanentConversionError):
    code = "invalid-input"


class OutputCollisionError(PermanentConversionError):
    code = "output-collision"


class ScanLimitError(RuntimeError):
    """A directory scan exceeded its configured work and memory ceiling."""


class IncompleteScanError(OSError):
    """A directory scan could not prove a complete view of the input tree."""


class UnsafeFileError(OSError):
    """A path resolved to something other than the bounded regular file expected."""


class StorageLayoutError(ValueError):
    """Configured durable, disposable, and input storage overlap unsafely."""


class StateCapacityError(RuntimeError):
    """Durable state reached its explicit row or byte ceiling."""


class PublicationRecoveryError(RuntimeError):
    """A durable output transaction could not be reconciled safely."""


class FailureDetail(str):
    """String-compatible failure details used by the durable retry policy."""

    def __new__(cls, message, *, permanent=False, code="conversion"):
        obj = super().__new__(cls, message)
        obj.permanent = bool(permanent)
        obj.code = code
        return obj


class SkipDetail(str):
    """A skip reason carrying output identity from the validating descriptors."""

    def __new__(cls, message, *, output_signature=None):
        obj = super().__new__(cls, message)
        obj.output_signature = output_signature
        return obj


class ActionDetail(str):
    """A source decision carrying the exact outputs it may safely replace."""

    def __new__(cls, message, *, owned_outputs=()):
        obj = super().__new__(cls, message)
        obj.owned_outputs = tuple(owned_outputs)
        return obj


class StagedConversionDetail(str):
    """A validated render awaiting main-thread durable publication."""

    def __new__(cls, message, *, staged=(), owned_outputs=()):
        obj = super().__new__(cls, message)
        obj.staged = tuple(staged)
        obj.owned_outputs = tuple(owned_outputs)
        return obj


@dataclass
class InputSnapshot:
    source: Path
    path: Path
    fingerprint: str
    stat_key: tuple


@dataclass
class PendingJob:
    source: Path
    canonical_source: str
    snapshot: InputSnapshot
    fingerprint: str
    config_fingerprint: str
    force: bool
    source_key: str = None
    owned_outputs: tuple = ()


@dataclass
class PublicationPlan:
    transaction_id: str
    source_key: str
    fingerprint: str
    config_fingerprint: str
    output_root: Path
    outputs: tuple

    def to_json(self):
        durable_output_keys = (
            "kind",
            "staged_path",
            "final_path",
            "new_identity",
            "backup_path",
            "old_identity",
        )
        payload = {
            "schema": 2,
            "transaction_id": self.transaction_id,
            "source_key": self.source_key,
            "fingerprint": self.fingerprint,
            "config_fingerprint": self.config_fingerprint,
            "output_root": lexical_path(self.output_root),
            "outputs": [
                {key: output[key] for key in durable_output_keys}
                for output in self.outputs
            ],
        }
        encoded = _json_dumps(payload)
        if len(encoded.encode("utf-8")) > MAX_PUBLICATION_PLAN_BYTES:
            raise StateCapacityError("publication plan exceeds its durable size ceiling")
        return encoded

    @classmethod
    def from_json(cls, payload):
        if not isinstance(payload, str) or len(payload.encode("utf-8")) > MAX_PUBLICATION_PLAN_BYTES:
            raise PublicationRecoveryError("publication plan is missing or oversized")
        try:
            value = json.loads(payload)
        except (TypeError, ValueError) as error:
            raise PublicationRecoveryError("publication plan is not valid JSON") from error
        required = {
            "schema", "transaction_id", "source_key", "fingerprint",
            "config_fingerprint", "output_root", "outputs",
        }
        if (
            not isinstance(value, dict)
            or set(value) != required
            or value["schema"] != 2
            or not isinstance(value["outputs"], list)
            or not 1 <= len(value["outputs"]) <= len(FORMAT_ORDER)
        ):
            raise PublicationRecoveryError("publication plan has an invalid structure")
        for key in required - {"schema", "outputs"}:
            if not isinstance(value[key], str):
                raise PublicationRecoveryError("publication plan has an invalid field")
        outputs = []
        output_keys = {
            "kind", "staged_path", "final_path", "new_identity",
            "backup_path", "old_identity",
        }
        for entry in value["outputs"]:
            if not isinstance(entry, dict) or set(entry) != output_keys:
                raise PublicationRecoveryError("publication output entry is invalid")
            if entry["kind"] not in FORMAT_ORDER:
                raise PublicationRecoveryError("publication output format is invalid")
            for path_key in ("staged_path", "final_path"):
                if not isinstance(entry[path_key], str):
                    raise PublicationRecoveryError("publication output path is invalid")
            if entry["backup_path"] is not None and not isinstance(
                entry["backup_path"], str
            ):
                raise PublicationRecoveryError("publication backup path is invalid")
            for identity_key in ("new_identity", "old_identity"):
                identity = entry[identity_key]
                if identity is None and identity_key == "old_identity":
                    continue
                if (
                    not isinstance(identity, list)
                    or len(identity) != 5
                    or any(not isinstance(item, int) for item in identity[:4])
                    or not isinstance(identity[4], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", identity[4])
                ):
                    raise PublicationRecoveryError("publication identity is invalid")
            outputs.append(dict(entry))
        return cls(
            value["transaction_id"],
            value["source_key"],
            value["fingerprint"],
            value["config_fingerprint"],
            Path(value["output_root"]),
            tuple(outputs),
        )


def canonical_path(path):
    return str(Path(path).resolve(strict=False))


def lexical_path(path):
    """Absolute normalized spelling without following the final path as an output."""
    return os.path.abspath(os.fspath(path))


def reservation_key(path):
    # SMB exports and macOS clients are commonly case-insensitive even when the
    # backing Unraid filesystem is not. The spelling is intentionally lexical:
    # resolving a not-yet-created output would let a raced symlink change which
    # durable reservation row is consulted.
    return unicodedata.normalize("NFC", lexical_path(path)).casefold()


def source_state_key(path, input_root):
    """Return an immutable, mount-namespace-independent source identity."""
    root = Path(lexical_path(input_root))
    candidate = Path(lexical_path(path))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise UnsafeFileError(
            errno.EXDEV, f"source path escapes configured input root: {path}"
        ) from exc
    if not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
        raise UnsafeFileError(errno.EINVAL, f"unsafe source-relative path: {path}")
    key = SOURCE_KEY_PREFIX + relative.as_posix()
    try:
        key.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise UnsafeFileError(
            errno.EILSEQ, f"source name is not valid UTF-8: {os.fsencode(path)!r}"
        ) from error
    return key


def _path_has_strict_utf8_name(path):
    try:
        os.fspath(path).encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return True


def _safe_path_display(path):
    """Render arbitrary Unix directory bytes without emitting surrogates."""
    try:
        return os.fsencode(path).decode("utf-8", errors="backslashreplace")
    except (TypeError, ValueError):
        return repr(path)


def _stored_source_key(value):
    """Accept new opaque relative keys while retaining old direct API callers."""
    text = os.fspath(value)
    stored = text if text.startswith(SOURCE_KEY_PREFIX) else canonical_path(value)
    try:
        stored.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise UnsafeFileError(
            errno.EILSEQ, f"source name is not valid UTF-8: {os.fsencode(value)!r}"
        ) from error
    return stored


def path_is_within(path, root):
    try:
        Path(path).resolve(strict=False).relative_to(Path(root).resolve(strict=False))
        return True
    except ValueError:
        return False


def path_has_symlink_component(path, root):
    path = Path(path)
    root = Path(root)
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _directory_identity(path):
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat_module.S_ISDIR(metadata.st_mode):
            raise UnsafeFileError(errno.ENOTDIR, f"not a directory: {path}")
        return metadata.st_dev, metadata.st_ino
    finally:
        os.close(descriptor)


def _open_regular_fd(
    path, *, root=None, root_identity=None, require_single_link=False,
):
    """Open a regular file without following any component below an optional root."""
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    opened_directories = []
    descriptor = -1
    try:
        if root is None:
            descriptor = os.open(path, file_flags)
        else:
            root_path = Path(lexical_path(root))
            candidate = Path(lexical_path(path))
            try:
                relative = candidate.relative_to(root_path)
            except ValueError as exc:
                raise UnsafeFileError(
                    errno.EXDEV, f"path escapes configured input root: {path}"
                ) from exc
            if not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
                raise UnsafeFileError(errno.EINVAL, f"unsafe relative path: {path}")

            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            current = os.open(root_path, directory_flags)
            opened_directories.append(current)
            root_stat = os.fstat(current)
            if not stat_module.S_ISDIR(root_stat.st_mode):
                raise UnsafeFileError(errno.ENOTDIR, f"input root is not a directory: {root}")
            if root_identity is not None and (root_stat.st_dev, root_stat.st_ino) != tuple(
                root_identity
            ):
                raise UnsafeFileError(errno.ESTALE, f"input root identity changed: {root}")
            for component in relative.parts[:-1]:
                current = os.open(component, directory_flags, dir_fd=current)
                opened_directories.append(current)
            descriptor = os.open(relative.parts[-1], file_flags, dir_fd=current)

        metadata = os.fstat(descriptor)
        if not stat_module.S_ISREG(metadata.st_mode):
            raise UnsafeFileError(errno.EINVAL, f"not a regular file: {path}")
        if require_single_link and metadata.st_nlink != 1:
            raise UnsafeFileError(errno.EMLINK, f"file has multiple hard links: {path}")
        return descriptor, metadata
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        if not isinstance(error, UnsafeFileError) and error.errno in {
            errno.ELOOP, errno.ENOTDIR, errno.EISDIR,
        }:
            raise UnsafeFileError(
                error.errno, f"unsafe path component or file type: {path}"
            ) from error
        raise
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    finally:
        for directory in reversed(opened_directories):
            os.close(directory)


def _output_root_parameters(args=None, out_dir=None, path=None):
    """Resolve the configured output anchor without resolving an output target."""
    ownership_lock = getattr(args, "output_lock", None) if args is not None else None
    if ownership_lock is not None:
        ownership_lock.assert_owned()
    configured = getattr(args, "output_root", None) if args is not None else None
    root = Path(configured if configured is not None else out_dir or Path(path).parent)
    root = Path(lexical_path(root))
    identity = (
        getattr(args, "output_root_identity", None) if args is not None else None
    )
    if identity is None:
        identity = _directory_identity(root)
    return root, tuple(identity)


def _open_output_parent(path, output_root, root_identity, *, create=False):
    """Open an output parent beneath a stable root, rejecting every symlink."""
    root = Path(lexical_path(output_root))
    candidate = Path(lexical_path(path))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise UnsafeFileError(
            errno.EXDEV, f"output path escapes configured output root: {path}"
        ) from exc
    if not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
        raise UnsafeFileError(errno.EINVAL, f"unsafe output-relative path: {path}")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(root, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat_module.S_ISDIR(metadata.st_mode):
            raise UnsafeFileError(errno.ENOTDIR, f"output root is not a directory: {root}")
        if (metadata.st_dev, metadata.st_ino) != tuple(root_identity):
            raise UnsafeFileError(errno.ESTALE, f"output root identity changed: {root}")
        for component in relative.parts[:-1]:
            if create:
                try:
                    os.mkdir(component, mode=0o750, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor, relative.parts[-1]
    except OSError as error:
        os.close(descriptor)
        if not isinstance(error, UnsafeFileError) and error.errno in {
            errno.ELOOP, errno.ENOTDIR, errno.EISDIR,
        }:
            raise UnsafeFileError(
                error.errno, f"unsafe output path component: {path}"
            ) from error
        raise
    except Exception:
        os.close(descriptor)
        raise


def _artifact_component_key(value):
    return unicodedata.normalize("NFC", os.fspath(value)).casefold()


def _reject_reserved_output_path(path, output_root):
    """Keep user-visible mappings out of the private transaction namespace."""
    root = Path(lexical_path(output_root))
    candidate = Path(lexical_path(path))
    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise UnsafeFileError(
            errno.EXDEV, f"output path escapes configured output root: {path}"
        ) from error
    reserved = _artifact_component_key(ARTIFACT_DIR_NAME)
    if any(_artifact_component_key(part) == reserved for part in relative.parts):
        raise OutputCollisionError(
            f"requested output uses reserved component {ARTIFACT_DIR_NAME}: {path}"
        )


def _open_authenticated_artifact_directory(parent_fd, name):
    """Open/create one private, same-filesystem watcher-owned directory."""
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        metadata = os.fstat(descriptor)
        parent = os.fstat(parent_fd)
        if (
            not stat_module.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat_module.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_dev != parent.st_dev
        ):
            raise UnsafeFileError(
                errno.EPERM,
                f"transaction artifact directory is not private and authenticated: {name}",
            )
        if created:
            os.fsync(parent_fd)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _artifact_path_for_parent(
    parent, role, output_root, root_identity, *, identifier=None,
):
    if role not in ARTIFACT_ROLES:
        raise ValueError(f"invalid transaction artifact role: {role}")
    parent = Path(parent)
    marker = parent / ".nef-watch-parent-probe"
    parent_fd, _ = _open_output_parent(
        marker, output_root, root_identity, create=True
    )
    artifact_fd = role_fd = -1
    try:
        artifact_fd = _open_authenticated_artifact_directory(
            parent_fd, ARTIFACT_DIR_NAME
        )
        role_fd = _open_authenticated_artifact_directory(artifact_fd, role)
    finally:
        if role_fd >= 0:
            os.close(role_fd)
        if artifact_fd >= 0:
            os.close(artifact_fd)
        os.close(parent_fd)
    name = identifier or uuid.uuid4().hex
    if not ARTIFACT_UUID_RE.fullmatch(name):
        raise ValueError("transaction artifact identifier must be UUID hex")
    return parent / ARTIFACT_DIR_NAME / role / name


def _artifact_path_for_final(
    final_path, role, output_root, root_identity, *, identifier=None,
):
    final_path = Path(final_path)
    _reject_reserved_output_path(final_path, output_root)
    return _artifact_path_for_parent(
        final_path.parent,
        role,
        output_root,
        root_identity,
        identifier=identifier,
    )


def _artifact_path_matches(path, final_parent, role):
    path = Path(path)
    expected_parent = Path(final_parent) / ARTIFACT_DIR_NAME / role
    return (
        path.parent == expected_parent
        and ARTIFACT_UUID_RE.fullmatch(path.name) is not None
    )


def _open_output_regular_fd(
    path, output_root, root_identity, *, writable=False, require_single_link=True,
):
    parent_fd, name = _open_output_parent(path, output_root, root_identity)
    descriptor = -1
    try:
        flags = (
            (os.O_RDWR if writable else os.O_RDONLY)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        metadata = os.fstat(descriptor)
        if not stat_module.S_ISREG(metadata.st_mode):
            raise UnsafeFileError(errno.EINVAL, f"not a regular output file: {path}")
        if require_single_link and metadata.st_nlink != 1:
            raise UnsafeFileError(errno.EMLINK, f"output has multiple hard links: {path}")
        return descriptor, metadata
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        if not isinstance(error, UnsafeFileError) and error.errno in {
            errno.ELOOP, errno.ENOTDIR, errno.EISDIR,
        }:
            raise UnsafeFileError(
                error.errno, f"unsafe output file or ancestor: {path}"
            ) from error
        raise
    finally:
        os.close(parent_fd)


def _create_output_regular_fd(path, output_root, root_identity):
    parent_fd, name = _open_output_parent(
        path, output_root, root_identity, create=True
    )
    descriptor = -1
    try:
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
        metadata = os.fstat(descriptor)
        if not stat_module.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise UnsafeFileError(errno.EINVAL, f"unsafe staged output: {path}")
        return descriptor
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    finally:
        os.close(parent_fd)


def _output_exists(path, output_root, root_identity):
    try:
        descriptor, _ = _open_output_regular_fd(path, output_root, root_identity)
    except FileNotFoundError:
        return False
    except UnsafeFileError:
        # The lexical entry exists but cannot safely be treated as a file.
        # Classify it as occupied so callers force a secured conversion path;
        # later anchored validation/publication records the concrete failure.
        return True
    else:
        os.close(descriptor)
        return True


def _unlink_output(path, output_root, root_identity, *, missing_ok=False):
    parent_fd, name = _open_output_parent(path, output_root, root_identity)
    try:
        try:
            os.unlink(name, dir_fd=parent_fd)
        except FileNotFoundError:
            if not missing_ok:
                raise
            return False
        os.fsync(parent_fd)
        return True
    finally:
        os.close(parent_fd)


def _replace_output(source, target, output_root, root_identity):
    source_parent, source_name = _open_output_parent(
        source, output_root, root_identity
    )
    target_parent = -1
    try:
        target_parent, target_name = _open_output_parent(
            target, output_root, root_identity, create=True
        )
        os.replace(
            source_name,
            target_name,
            src_dir_fd=source_parent,
            dst_dir_fd=target_parent,
        )
        try:
            os.fsync(source_parent)
            if target_parent != source_parent:
                os.fsync(target_parent)
        except OSError as error:
            durability_error = OSError(
                error.errno,
                f"output rename completed but directory sync failed: {error}",
            )
            durability_error.output_mutation_completed = True
            raise durability_error from error
    finally:
        if target_parent >= 0:
            os.close(target_parent)
        os.close(source_parent)


def _fsync_output_file(path, output_root, root_identity):
    descriptor, _ = _open_output_regular_fd(
        path, output_root, root_identity, writable=True
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def output_size_limit(kind, args):
    """Return the maximum durable artifact size for one configured format.

    A DNG may contain an uncompressed bounded raw raster plus container
    metadata.  Embedding the original is permitted one additional copy of the
    already-bounded source.  This makes the ceiling follow --max-input-mib
    without granting every DNG the global 4 GiB input maximum.
    """
    if kind in RASTER_FORMATS:
        return MAX_RASTER_OUTPUT_BYTES
    if kind != "dng":
        raise ValueError(f"unsupported output kind: {kind}")
    embedded_source = (
        getattr(args, "max_input_bytes", DEFAULT_MAX_INPUT_MIB * 1024 * 1024)
        if getattr(args, "dng_embed_original", False)
        else 0
    )
    return (
        MAX_RASTER_OUTPUT_BYTES
        + MAX_DNG_CONTAINER_OVERHEAD_BYTES
        + embedded_source
    )


def _kind_from_output_path(path):
    suffix = Path(path).suffix.lower()
    for kind, extension in EXTS.items():
        if suffix == extension:
            return kind
    raise ValueError(f"cannot determine output format from {path}")


def _copy_to_staged_output(source, final_path, args, *, kind=None):
    """Copy a completed scratch artifact into an anchored same-filesystem stage."""
    kind = kind or _kind_from_output_path(final_path)
    maximum_bytes = output_size_limit(kind, args)
    output_root, root_identity = _output_root_parameters(
        args=args, path=final_path
    )
    staged_path = unique_partial(
        Path(final_path),
        output_root=output_root,
        root_identity=root_identity,
    )
    source_fd, before = _open_regular_fd(source, require_single_link=True)
    target_fd = -1
    try:
        if before.st_size > maximum_bytes:
            raise UnsafeFileError(
                errno.EFBIG,
                f"{kind} output exceeds {maximum_bytes} byte safety limit: {source}",
            )
        target_fd = _create_output_regular_fd(
            staged_path, output_root, root_identity
        )
        copied = 0
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            if copied > maximum_bytes - len(chunk):
                raise UnsafeFileError(
                    errno.EFBIG,
                    f"{kind} output grew beyond {maximum_bytes} byte safety limit",
                )
            view = memoryview(chunk)
            while view:
                written = os.write(target_fd, view)
                if written <= 0:
                    raise OSError(errno.EIO, "short write while staging output")
                view = view[written:]
            copied += len(chunk)
        after = os.fstat(source_fd)
        if copied != before.st_size or _stable_file_identity(before) != _stable_file_identity(after):
            raise UnsafeFileError(errno.ESTALE, f"scratch output changed while staging: {source}")
        os.fchmod(target_fd, 0o640)
        os.fsync(target_fd)
        return staged_path
    except Exception:
        try:
            _unlink_output(
                staged_path, output_root, root_identity, missing_ok=True
            )
        except OSError:
            pass
        raise
    finally:
        if target_fd >= 0:
            os.close(target_fd)
        os.close(source_fd)


def _read_bounded_regular_file(
    path,
    maximum_bytes,
    *,
    require_single_link=True,
    root=None,
    root_identity=None,
):
    if root is None:
        descriptor, before = _open_regular_fd(
            path, require_single_link=require_single_link
        )
    else:
        descriptor, before = _open_output_regular_fd(
            path,
            root,
            root_identity,
            require_single_link=require_single_link,
        )
    try:
        if before.st_size > maximum_bytes:
            raise UnsafeFileError(
                errno.EFBIG, f"file exceeds {maximum_bytes} bytes: {path}"
            )
        chunks = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(payload) > maximum_bytes:
            raise UnsafeFileError(
                errno.EFBIG, f"file grew beyond {maximum_bytes} bytes: {path}"
            )
        if _stable_file_identity(before) != _stable_file_identity(after):
            raise UnsafeFileError(errno.ESTALE, f"file changed while reading: {path}")
        return payload
    finally:
        os.close(descriptor)


def _paths_overlap(first, second):
    first = Path(first).resolve(strict=False)
    second = Path(second).resolve(strict=False)
    try:
        first.relative_to(second)
        return True
    except ValueError:
        pass
    try:
        second.relative_to(first)
        return True
    except ValueError:
        return False


def validate_storage_layout(input_root, out_dir, state_dir, temp_dir):
    """Keep disposable scratch isolated from source, output, and durable state."""
    roles = {
        "input": Path(input_root).resolve(strict=False),
        "output": Path(out_dir).resolve(strict=False),
        "state": Path(state_dir).resolve(strict=False),
        "temporary": Path(temp_dir).resolve(strict=False),
    }
    for durable_role in ("input", "output", "state"):
        if _paths_overlap(roles["temporary"], roles[durable_role]):
            raise StorageLayoutError(
                f"temporary storage must be separate from {durable_role} storage: "
                f"{roles['temporary']} overlaps {roles[durable_role]}"
            )
    return roles


class OutputRootLock:
    """Lock the stable output inode, plus revalidated public/private sentinels.

    The directory lock is the cross-state-directory authority.  A lock only on
    ``.nef-watch-output.lock`` can be bypassed by unlinking and recreating that
    pathname while the first process still holds the old inode.  The optional
    private state sentinel is keyed to the output device/inode so state remains
    explicit without becoming the cross-process coordination mechanism.
    """

    def __init__(self, out_dir, state_dir=None):
        self.out_dir = Path(lexical_path(out_dir))
        self.state_dir = (
            Path(lexical_path(state_dir)) if state_dir is not None else None
        )
        self.lock_name = ".nef-watch-output.lock"
        self.lock_path = self.out_dir / self.lock_name
        self.root_fd = -1
        self.lock_fd = -1
        self.state_fd = -1
        self.state_lock_fd = -1
        self.root_locked = False
        self.state_lock_locked = False
        self.public_lock_locked = False
        self._closed = False
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        file_flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            self.root_fd = os.open(self.out_dir, directory_flags)
            root_metadata = os.fstat(self.root_fd)
            if not stat_module.S_ISDIR(root_metadata.st_mode):
                raise UnsafeFileError(
                    errno.ENOTDIR, f"output root is not a directory: {self.out_dir}"
                )
            self.root_identity = (root_metadata.st_dev, root_metadata.st_ino)
            # flock on the opened directory is tied to the stable output inode,
            # so a recreated public lock pathname cannot admit a second owner.
            fcntl.flock(self.root_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.root_locked = True

            if self.state_dir is not None:
                self.state_fd = os.open(self.state_dir, directory_flags)
                state_metadata = os.fstat(self.state_fd)
                if not stat_module.S_ISDIR(state_metadata.st_mode):
                    raise UnsafeFileError(
                        errno.ENOTDIR,
                        f"state root is not a directory: {self.state_dir}",
                    )
                self.state_lock_name = (
                    f".output-{root_metadata.st_dev:x}-{root_metadata.st_ino:x}.lock"
                )
                self.state_lock_fd = os.open(
                    self.state_lock_name, file_flags, 0o600, dir_fd=self.state_fd
                )
                private_metadata = os.fstat(self.state_lock_fd)
                if (
                    not stat_module.S_ISREG(private_metadata.st_mode)
                    or private_metadata.st_nlink != 1
                ):
                    raise UnsafeFileError(
                        errno.EINVAL,
                        f"unsafe private output lock: "
                        f"{self.state_dir / self.state_lock_name}",
                    )
                self.state_lock_identity = (
                    private_metadata.st_dev,
                    private_metadata.st_ino,
                )
                fcntl.flock(
                    self.state_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB
                )
                self.state_lock_locked = True

            self.lock_fd = os.open(
                self.lock_name, file_flags, 0o600, dir_fd=self.root_fd
            )
            lock_metadata = os.fstat(self.lock_fd)
            if (
                not stat_module.S_ISREG(lock_metadata.st_mode)
                or lock_metadata.st_nlink != 1
            ):
                raise UnsafeFileError(
                    errno.EINVAL, f"unsafe output lock file: {self.lock_path}"
                )
            self.lock_identity = (lock_metadata.st_dev, lock_metadata.st_ino)
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.public_lock_locked = True
            self.assert_owned()
        except Exception:
            self.close()
            raise

    @staticmethod
    def _assert_sentinel(directory_fd, name, descriptor, expected, label):
        descriptor_metadata = os.fstat(descriptor)
        pathname_metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat_module.S_ISREG(pathname_metadata.st_mode)
            or pathname_metadata.st_nlink != 1
            or descriptor_metadata.st_nlink != 1
            or (descriptor_metadata.st_dev, descriptor_metadata.st_ino) != expected
            or (pathname_metadata.st_dev, pathname_metadata.st_ino) != expected
        ):
            raise UnsafeFileError(errno.ESTALE, f"{label} identity changed")

    def assert_owned(self):
        if self._closed:
            raise UnsafeFileError(errno.EBADF, "output ownership lock is closed")
        root_metadata = os.fstat(self.root_fd)
        pathname_identity = _directory_identity(self.out_dir)
        if (
            not stat_module.S_ISDIR(root_metadata.st_mode)
            or (root_metadata.st_dev, root_metadata.st_ino) != self.root_identity
            or pathname_identity != self.root_identity
        ):
            raise UnsafeFileError(errno.ESTALE, "output root identity changed")
        self._assert_sentinel(
            self.root_fd,
            self.lock_name,
            self.lock_fd,
            self.lock_identity,
            "output lock",
        )
        if self.state_fd >= 0:
            state_metadata = os.fstat(self.state_fd)
            state_path_identity = _directory_identity(self.state_dir)
            if (
                not stat_module.S_ISDIR(state_metadata.st_mode)
                or (state_metadata.st_dev, state_metadata.st_ino)
                != state_path_identity
            ):
                raise UnsafeFileError(errno.ESTALE, "state root identity changed")
            self._assert_sentinel(
                self.state_fd,
                self.state_lock_name,
                self.state_lock_fd,
                self.state_lock_identity,
                "private output lock",
            )

    def close(self):
        if self._closed:
            return
        self._closed = True
        for descriptor, locked in (
            (self.lock_fd, self.public_lock_locked),
            (self.state_lock_fd, self.state_lock_locked),
            (self.state_fd, False),
            (self.root_fd, self.root_locked),
        ):
            if descriptor < 0:
                continue
            if locked:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                os.close(descriptor)
            except OSError:
                pass


def _json_dumps(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _bounded_utf8(value, maximum_bytes, *, marker="[...truncated...] "):
    payload = str(value).encode("utf-8", errors="replace")
    if len(payload) <= maximum_bytes:
        return payload.decode("utf-8")
    prefix = marker.encode("utf-8")
    keep = max(0, maximum_bytes - len(prefix))
    tail = payload[-keep:] if keep else b""
    while tail:
        try:
            return prefix.decode("utf-8") + tail.decode("utf-8")
        except UnicodeDecodeError:
            tail = tail[1:]
    return prefix[:maximum_bytes].decode("utf-8", errors="ignore")


def _bounded_environment_integer(name, default, minimum, maximum):
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise StateCapacityError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise StateCapacityError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


def _utc_iso(epoch=None):
    return datetime.fromtimestamp(epoch or time.time(), timezone.utc).isoformat()


class StateStore:
    """Durable conversion provenance, reservations, and dead-letter state."""

    def __init__(self, state_dir, *, input_root=None):
        self.state_dir = Path(lexical_path(state_dir))
        self.input_root = (
            Path(canonical_path(input_root)) if input_root is not None else None
        )
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.max_state_sources = _bounded_environment_integer(
            "NEF_WATCH_MAX_STATE_SOURCES",
            DEFAULT_MAX_STATE_SOURCES,
            1,
            MAX_STATE_SOURCES,
        )
        self.max_state_mib = _bounded_environment_integer(
            "NEF_WATCH_STATE_MAX_MIB",
            DEFAULT_STATE_MAX_MIB,
            MIN_STATE_MAX_MIB,
            MAX_STATE_MAX_MIB,
        )
        self.lock_name = "watcher.lock"
        self.lock_path = self.state_dir / self.lock_name
        self.state_dir_fd = -1
        self.state_dir_locked = False
        self.lock_file = None
        try:
            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            self.state_dir_fd = os.open(self.state_dir, directory_flags)
            directory_metadata = os.fstat(self.state_dir_fd)
            if not stat_module.S_ISDIR(directory_metadata.st_mode):
                raise UnsafeFileError(
                    errno.ENOTDIR, f"state root is not a directory: {self.state_dir}"
                )
            self.state_dir_identity = (
                directory_metadata.st_dev,
                directory_metadata.st_ino,
            )
            # The stable directory inode is the actual lifetime lock.  A lock
            # file alone can be unlinked and recreated while its first inode is
            # still locked, admitting a second writer to the same SQLite state.
            fcntl.flock(self.state_dir_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.state_dir_locked = True

            lock_flags = (
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            lock_fd = os.open(
                self.lock_name, lock_flags, 0o600, dir_fd=self.state_dir_fd
            )
            try:
                lock_metadata = os.fstat(lock_fd)
                if (
                    not stat_module.S_ISREG(lock_metadata.st_mode)
                    or lock_metadata.st_nlink != 1
                    or lock_metadata.st_uid != os.geteuid()
                ):
                    raise UnsafeFileError(
                        errno.EINVAL, f"unsafe durable state lock: {self.lock_path}"
                    )
                os.fchmod(lock_fd, 0o600)
                self.lock_identity = (
                    lock_metadata.st_dev,
                    lock_metadata.st_ino,
                )
                self.lock_file = os.fdopen(lock_fd, "a+", encoding="utf-8")
                lock_fd = -1
            finally:
                if lock_fd >= 0:
                    os.close(lock_fd)
            fcntl.flock(
                self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
            )
            self.assert_owned()
        except Exception:
            self._close_lock_resources()
            raise
        self.db_path = self.state_dir / "state.sqlite3"
        self.health_path = self.state_dir / "health.json"
        try:
            health_interval = float(
                os.environ.get("NEF_WATCH_HEALTH_WRITE_INTERVAL", "15")
            )
        except ValueError:
            health_interval = 15.0
        self.health_write_interval = min(60.0, max(1.0, health_interval))
        self._last_health_write_at = 0.0
        self._last_health_signature = None
        try:
            self.conn = sqlite3.connect(self.db_path, timeout=30)
        except Exception:
            self._close_lock_resources()
            raise
        try:
            self.assert_owned()
            self.conn.row_factory = sqlite3.Row
            existing_schema = self._detect_schema()
            self.max_state_bytes = self.max_state_mib * 1024 * 1024
            existing_state_bytes = sum(
                candidate.stat().st_size
                for candidate in (
                    self.db_path,
                    Path(str(self.db_path) + "-journal"),
                    Path(str(self.db_path) + "-wal"),
                    Path(str(self.db_path) + "-shm"),
                )
                if candidate.exists()
            )
            if existing_state_bytes > self.max_state_bytes:
                raise StateCapacityError(
                    "existing durable state exceeds NEF_WATCH_STATE_MAX_MIB"
                )
            page_size = int(self.conn.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(self.conn.execute("PRAGMA page_count").fetchone()[0])
            database_budget = max(
                page_size,
                (self.max_state_bytes - 4 * 1024 * 1024) // 2,
            )
            requested_pages = database_budget // page_size
            if page_count > requested_pages:
                raise StateCapacityError(
                    "existing SQLite database exceeds half of the configured total "
                    "state budget; raise NEF_WATCH_STATE_MAX_MIB or compact it offline"
                )
            current_mode = str(
                self.conn.execute("PRAGMA journal_mode").fetchone()[0]
            ).lower()
            if current_mode == "wal":
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            configured_mode = str(
                self.conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            ).lower()
            if configured_mode != "delete":
                raise sqlite3.DatabaseError(
                    f"cannot select bounded SQLite DELETE journaling: {configured_mode}"
                )
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.execute("PRAGMA busy_timeout=30000")
            effective_pages = int(
                self.conn.execute(
                    f"PRAGMA max_page_count={requested_pages}"
                ).fetchone()[0]
            )
            if effective_pages != requested_pages:
                raise StateCapacityError(
                    "SQLite refused the configured hard database page ceiling"
                )
            self.max_database_bytes = effective_pages * page_size
            self._init_schema(existing_schema)
            tracked_count = int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM tracked_sources"
                ).fetchone()[0]
            )
            if tracked_count > self.max_state_sources:
                raise StateCapacityError(
                    "existing durable state contains more sources than "
                    "NEF_WATCH_MAX_STATE_SOURCES; prune it offline or raise the limit"
                )
            self.assert_owned()
        except Exception:
            self.conn.close()
            self._close_lock_resources()
            raise
        self._closed = False
        self.last_baseline_conflicts = 0

    def assert_owned(self):
        """Revalidate both stable state inode and the public lock sentinel."""
        if self.state_dir_fd < 0 or self.lock_file is None:
            raise UnsafeFileError(errno.EBADF, "durable state ownership lock is closed")
        directory_metadata = os.fstat(self.state_dir_fd)
        if (
            not stat_module.S_ISDIR(directory_metadata.st_mode)
            or (directory_metadata.st_dev, directory_metadata.st_ino)
            != self.state_dir_identity
            or _directory_identity(self.state_dir) != self.state_dir_identity
        ):
            raise UnsafeFileError(errno.ESTALE, "durable state root identity changed")
        descriptor_metadata = os.fstat(self.lock_file.fileno())
        pathname_metadata = os.stat(
            self.lock_name, dir_fd=self.state_dir_fd, follow_symlinks=False
        )
        if (
            not stat_module.S_ISREG(descriptor_metadata.st_mode)
            or not stat_module.S_ISREG(pathname_metadata.st_mode)
            or descriptor_metadata.st_nlink != 1
            or pathname_metadata.st_nlink != 1
            or descriptor_metadata.st_uid != os.geteuid()
            or pathname_metadata.st_uid != os.geteuid()
            or stat_module.S_IMODE(descriptor_metadata.st_mode) != 0o600
            or stat_module.S_IMODE(pathname_metadata.st_mode) != 0o600
            or (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
            != self.lock_identity
            or (pathname_metadata.st_dev, pathname_metadata.st_ino)
            != self.lock_identity
        ):
            raise UnsafeFileError(errno.ESTALE, "durable state lock identity changed")

    def _close_lock_resources(self):
        if self.lock_file is not None:
            try:
                fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self.lock_file.close()
            except OSError:
                pass
            self.lock_file = None
        if self.state_dir_fd >= 0:
            if self.state_dir_locked:
                try:
                    fcntl.flock(self.state_dir_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                os.close(self.state_dir_fd)
            except OSError:
                pass
            self.state_dir_fd = -1
            self.state_dir_locked = False

    def _detect_schema(self):
        metadata_exists = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'"
        ).fetchone()
        if not metadata_exists:
            existing_table = self.conn.execute(
                """SELECT name FROM sqlite_master
                   WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"""
            ).fetchone()
            if existing_table is not None:
                raise sqlite3.DatabaseError(
                    "durable state has tables but no schema metadata marker"
                )
            return 0
        row = self.conn.execute(
            "SELECT value FROM metadata WHERE key='schema'"
        ).fetchone()
        if row is None:
            raise sqlite3.DatabaseError(
                "durable state metadata table has no schema marker"
            )
        try:
            existing_schema = int(row[0])
        except (TypeError, ValueError) as error:
            raise sqlite3.DatabaseError(
                f"invalid durable state schema marker: {row[0]!r}"
            ) from error
        if existing_schema < 0:
            raise sqlite3.DatabaseError(
                f"invalid durable state schema version: {existing_schema}"
            )
        if existing_schema > STATE_SCHEMA:
            raise sqlite3.DatabaseError(
                "durable state uses newer schema "
                f"{existing_schema}; this watcher supports {STATE_SCHEMA}"
            )
        return existing_schema

    def _init_schema(self, existing_schema=0):

        with self.conn:
            schema_statements = """
                CREATE TABLE IF NOT EXISTS sources (
                    source_path TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    config_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    output_metadata TEXT NOT NULL DEFAULT '[]',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS failures (
                    source_path TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    config_fingerprint TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    next_retry_at REAL,
                    permanent INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    error_code TEXT NOT NULL,
                    error_text TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS output_reservations (
                    output_path TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tracked_sources (
                    source_path TEXT PRIMARY KEY,
                    last_activity_at REAL NOT NULL,
                    absent_since REAL
                );
                CREATE TABLE IF NOT EXISTS publication_transactions (
                    transaction_id TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    config_fingerprint TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            for statement in schema_statements.split(";"):
                if statement.strip():
                    self.conn.execute(statement)
            columns = {
                row[1] for row in self.conn.execute("PRAGMA table_info(failures)")
            }
            if "active" not in columns:
                self.conn.execute(
                    "ALTER TABLE failures ADD COLUMN active INTEGER NOT NULL DEFAULT 1"
                )
            self.conn.execute(
                """CREATE INDEX IF NOT EXISTS failures_active_permanent_idx
                   ON failures(permanent, active)
                   WHERE permanent=1 AND active=1"""
            )
            self.conn.execute(
                """CREATE INDEX IF NOT EXISTS tracked_sources_absent_idx
                   ON tracked_sources(absent_since,last_activity_at)
                   WHERE absent_since IS NOT NULL"""
            )
            self.conn.execute(
                """CREATE INDEX IF NOT EXISTS publication_source_idx
                   ON publication_transactions(source_path)"""
            )
            self.conn.execute(
                """INSERT OR IGNORE INTO tracked_sources
                   (source_path,last_activity_at,absent_since)
                   SELECT source_path,MAX(activity),NULL FROM (
                     SELECT source_path,updated_at AS activity FROM sources
                     UNION ALL
                     SELECT source_path,updated_at AS activity FROM failures
                     UNION ALL
                     SELECT source_path,created_at AS activity FROM output_reservations
                   ) GROUP BY source_path"""
            )
            if self.input_root is not None:
                self._migrate_absolute_source_keys(self.input_root)
            self.conn.execute(
                """INSERT INTO metadata(key,value) VALUES('schema',?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (str(STATE_SCHEMA),),
            )

    def _migrate_absolute_source_keys(self, input_root):
        """Rewrite the legacy absolute-key graph before marking schema current."""
        tables = (
            "sources",
            "failures",
            "output_reservations",
            "tracked_sources",
            "publication_transactions",
        )
        legacy = set()
        for table in tables:
            legacy.update(
                row[0]
                for row in self.conn.execute(
                    f"SELECT DISTINCT source_path FROM {table} "
                    "WHERE source_path NOT LIKE ?",
                    (SOURCE_KEY_PREFIX + "%",),
                )
            )
        if not legacy:
            return 0
        mapping = {}
        root = Path(canonical_path(input_root))
        for old in sorted(legacy):
            if not isinstance(old, str) or not os.path.isabs(old):
                raise sqlite3.DatabaseError(
                    f"legacy durable source key is not absolute: {old!r}"
                )
            try:
                mapping[old] = source_state_key(Path(old), root)
            except UnsafeFileError as error:
                raise sqlite3.DatabaseError(
                    "legacy durable source key is outside the configured input "
                    f"root and cannot be migrated safely: {old}"
                ) from error

        for table in ("sources", "failures", "tracked_sources"):
            for old, new in mapping.items():
                conflict = self.conn.execute(
                    f"SELECT 1 FROM {table} WHERE source_path=? AND source_path<>?",
                    (new, old),
                ).fetchone()
                if conflict is not None:
                    raise sqlite3.DatabaseError(
                        f"legacy source-key migration conflicts in {table}: {new}"
                    )

        publication_updates = []
        for row in self.conn.execute(
            "SELECT transaction_id,source_path,plan_json "
            "FROM publication_transactions"
        ):
            old = row["source_path"]
            if old not in mapping:
                continue
            try:
                plan = json.loads(row["plan_json"])
            except (TypeError, ValueError) as error:
                raise sqlite3.DatabaseError(
                    "cannot migrate source key in an invalid publication plan"
                ) from error
            if not isinstance(plan, dict) or plan.get("source_key") != old:
                raise sqlite3.DatabaseError(
                    "publication plan source key disagrees with its durable row"
                )
            plan["source_key"] = mapping[old]
            payload = _json_dumps(plan)
            if len(payload.encode("utf-8")) > MAX_PUBLICATION_PLAN_BYTES:
                raise sqlite3.DatabaseError(
                    "migrated publication plan exceeds its size ceiling"
                )
            publication_updates.append(
                (mapping[old], payload, row["transaction_id"])
            )

        for table in tables:
            if table == "publication_transactions":
                continue
            self.conn.executemany(
                f"UPDATE {table} SET source_path=? WHERE source_path=?",
                ((new, old) for old, new in mapping.items()),
            )
        self.conn.executemany(
            "UPDATE publication_transactions SET source_path=?,plan_json=? "
            "WHERE transaction_id=?",
            publication_updates,
        )
        return len(mapping)

    def _delete_source_state(self, source):
        self.conn.execute("DELETE FROM sources WHERE source_path=?", (source,))
        self.conn.execute("DELETE FROM failures WHERE source_path=?", (source,))
        self.conn.execute(
            "DELETE FROM output_reservations WHERE source_path=?", (source,)
        )
        self.conn.execute(
            "DELETE FROM tracked_sources WHERE source_path=?", (source,)
        )

    def _admit_source(self, source, now):
        row = self.conn.execute(
            "SELECT 1 FROM tracked_sources WHERE source_path=?", (source,)
        ).fetchone()
        if row is not None:
            self.conn.execute(
                """UPDATE tracked_sources SET last_activity_at=?,absent_since=NULL
                   WHERE source_path=?""",
                (now, source),
            )
            return
        count = int(
            self.conn.execute("SELECT COUNT(*) FROM tracked_sources").fetchone()[0]
        )
        if count >= self.max_state_sources:
            victim = self.conn.execute(
                """SELECT tracked.source_path FROM tracked_sources tracked
                   WHERE tracked.absent_since IS NOT NULL
                     AND NOT EXISTS (
                       SELECT 1 FROM publication_transactions publication
                       WHERE publication.source_path=tracked.source_path
                     )
                   ORDER BY tracked.absent_since,tracked.last_activity_at,
                            tracked.source_path LIMIT 1"""
            ).fetchone()
            if victim is None:
                raise StateCapacityError(
                    "durable state source ceiling reached with no absent record "
                    "safe to evict; remove inputs or raise NEF_WATCH_MAX_STATE_SOURCES"
                )
            self._delete_source_state(victim[0])
        self.conn.execute(
            """INSERT INTO tracked_sources
               (source_path,last_activity_at,absent_since) VALUES(?,?,NULL)""",
            (source, now),
        )

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.conn.close()
        finally:
            self._close_lock_resources()

    def get_source(self, source_path):
        self.assert_owned()
        return self.conn.execute(
            "SELECT * FROM sources WHERE source_path=?", (_stored_source_key(source_path),)
        ).fetchone()

    def get_failure(self, source_path):
        self.assert_owned()
        return self.conn.execute(
            "SELECT * FROM failures WHERE source_path=? AND active=1",
            (_stored_source_key(source_path),),
        ).fetchone()

    def clear_failure(self, source_path):
        self.assert_owned()
        with self.conn:
            self.conn.execute(
                "DELETE FROM failures WHERE source_path=?", (_stored_source_key(source_path),)
            )

    def reserve_outputs(self, source_path, outputs):
        self.assert_owned()
        source = _stored_source_key(source_path)
        now = time.time()
        with self.conn:
            self._admit_source(source, now)
            for kind, output in outputs:
                target = reservation_key(output)
                row = self.conn.execute(
                    "SELECT source_path FROM output_reservations WHERE output_path=?",
                    (target,),
                ).fetchone()
                if row and row["source_path"] != source:
                    raise OutputCollisionError(
                        f"output collision: {output} is already reserved for "
                        f"{row['source_path']} (current source: {source})"
                    )
                self.conn.execute(
                    """INSERT OR IGNORE INTO output_reservations
                       (output_path,source_path,kind,created_at) VALUES(?,?,?,?)""",
                    (target, source, kind, now),
                )

    def output_reservation_collision(self, source_path, outputs):
        """Return a stable explanation without mutating a durable reservation."""
        self.assert_owned()
        source = _stored_source_key(source_path)
        for _, output in outputs:
            row = self.conn.execute(
                "SELECT source_path FROM output_reservations WHERE output_path=?",
                (reservation_key(output),),
            ).fetchone()
            if row and row["source_path"] != source:
                return (
                    f"output collision: {output} is already reserved for "
                    f"{row['source_path']} (current source: {source})"
                )
        return None

    def output_reservation_signature(self, outputs):
        """Cheap durable obstruction token for the watch collision cache."""
        self.assert_owned()
        signature = []
        for kind, output in outputs:
            target = reservation_key(output)
            row = self.conn.execute(
                "SELECT source_path,kind FROM output_reservations WHERE output_path=?",
                (target,),
            ).fetchone()
            signature.append(
                (
                    target,
                    kind,
                    row["source_path"] if row is not None else None,
                    row["kind"] if row is not None else None,
                )
            )
        return tuple(signature)

    def record_success(self, source_path, fingerprint, config_fingerprint, metadata):
        self.assert_owned()
        source = _stored_source_key(source_path)
        now = time.time()
        with self.conn:
            self._record_success_sql(
                source, fingerprint, config_fingerprint, metadata, now
            )

    def _record_success_sql(
        self, source, fingerprint, config_fingerprint, metadata, now
    ):
        self._admit_source(source, now)
        self.conn.execute(
            """INSERT INTO sources
               (source_path,fingerprint,config_fingerprint,status,output_metadata,updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(source_path) DO UPDATE SET
                 fingerprint=excluded.fingerprint,
                 config_fingerprint=excluded.config_fingerprint,
                 status='success', output_metadata=excluded.output_metadata,
                 updated_at=excluded.updated_at""",
            (source, fingerprint, config_fingerprint, "success", _json_dumps(metadata), now),
        )
        self.conn.execute("DELETE FROM failures WHERE source_path=?", (source,))
        self.conn.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES('last_success_at',?)",
            (str(now),),
        )

    def record_failure(
        self, source_path, fingerprint, config_fingerprint, detail,
        *, permanent=False, max_retries=3, retry_base=3.0,
    ):
        self.assert_owned()
        source = _stored_source_key(source_path)
        now = time.time()
        previous = self.get_failure(source)
        same = bool(
            previous
            and previous["fingerprint"] == fingerprint
            and previous["config_fingerprint"] == config_fingerprint
        )
        attempts = int(previous["attempts"]) + 1 if same else 1
        dead = bool(permanent or attempts >= max_retries)
        next_retry = None if dead else now + retry_base * (2 ** (attempts - 1))
        code = _bounded_utf8(
            getattr(detail, "code", "conversion"), MAX_FAILURE_CODE_BYTES
        )
        error_text = _bounded_utf8(detail, MAX_FAILURE_TEXT_BYTES)
        if (
            same
            and previous["permanent"]
            and permanent
            and previous["error_code"] == code
            and previous["error_text"] == error_text
        ):
            return int(previous["attempts"]), True, None
        with self.conn:
            self._admit_source(source, now)
            self.conn.execute(
                """INSERT INTO failures
                   (source_path,fingerprint,config_fingerprint,attempts,next_retry_at,
                    permanent,active,error_code,error_text,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_path) DO UPDATE SET
                     fingerprint=excluded.fingerprint,
                     config_fingerprint=excluded.config_fingerprint,
                     attempts=excluded.attempts,
                     next_retry_at=excluded.next_retry_at,
                     permanent=excluded.permanent,
                     active=1,
                     error_code=excluded.error_code,
                     error_text=excluded.error_text,
                     updated_at=excluded.updated_at""",
                (
                    source, fingerprint, config_fingerprint, attempts, next_retry,
                    int(dead), 1, code, error_text, now,
                ),
            )
        return attempts, dead, next_retry

    def begin_publication(self, plan):
        self.assert_owned()
        source = _stored_source_key(plan.source_key)
        payload = plan.to_json()
        now = time.time()
        with self.conn:
            self._admit_source(source, now)
            pending = int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM publication_transactions"
                ).fetchone()[0]
            )
            if pending >= MAX_PENDING_PUBLICATIONS:
                raise StateCapacityError(
                    "too many unresolved durable publication transactions"
                )
            self.conn.execute(
                """INSERT INTO publication_transactions
                   (transaction_id,source_path,fingerprint,config_fingerprint,
                    phase,plan_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    plan.transaction_id,
                    source,
                    plan.fingerprint,
                    plan.config_fingerprint,
                    "prepared",
                    payload,
                    now,
                    now,
                ),
            )

    def record_success_and_commit_publication(self, plan, metadata):
        self.assert_owned()
        source = _stored_source_key(plan.source_key)
        now = time.time()
        with self.conn:
            row = self.conn.execute(
                """SELECT phase FROM publication_transactions
                   WHERE transaction_id=?""",
                (plan.transaction_id,),
            ).fetchone()
            if row is None or row["phase"] != "prepared":
                raise PublicationRecoveryError(
                    "durable publication journal disappeared before commit"
                )
            self._record_success_sql(
                source,
                plan.fingerprint,
                plan.config_fingerprint,
                metadata,
                now,
            )
            self.conn.execute(
                """UPDATE publication_transactions
                   SET phase='committed',updated_at=? WHERE transaction_id=?""",
                (now, plan.transaction_id),
            )

    def delete_publication(self, transaction_id):
        self.assert_owned()
        with self.conn:
            self.conn.execute(
                "DELETE FROM publication_transactions WHERE transaction_id=?",
                (transaction_id,),
            )

    def publication_rows(self):
        self.assert_owned()
        return self.conn.execute(
            """SELECT * FROM publication_transactions
               ORDER BY created_at,transaction_id LIMIT ?""",
            (MAX_PENDING_PUBLICATIONS + 1,),
        ).fetchall()

    def get_publication(self, transaction_id):
        self.assert_owned()
        return self.conn.execute(
            "SELECT * FROM publication_transactions WHERE transaction_id=?",
            (transaction_id,),
        ).fetchone()

    def count_permanent_failures(self):
        self.assert_owned()
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM failures WHERE permanent=1 AND active=1"
            ).fetchone()[0]
        )

    def acknowledge_missing_failures(self, input_root, present_sources):
        """Keep dead letters but remove vanished inputs from active health counts."""
        self.assert_owned()
        root = Path(input_root).resolve(strict=False)
        present = {_stored_source_key(path) for path in present_sources}
        rows = self.conn.execute(
            "SELECT source_path FROM failures WHERE permanent=1 AND active=1"
        ).fetchall()
        acknowledged = []
        for row in rows:
            source = row["source_path"]
            relative_key = source.startswith(SOURCE_KEY_PREFIX)
            if (relative_key or path_is_within(Path(source), root)) and source not in present:
                acknowledged.append(source)
        if acknowledged:
            with self.conn:
                self.conn.executemany(
                    "UPDATE failures SET active=0, updated_at=? WHERE source_path=?",
                    [(time.time(), source) for source in acknowledged],
                )
        return len(acknowledged)

    def observe_complete_scan(self, present_sources, *, now=None):
        """Record only presence transitions, avoiding per-poll write churn."""
        self.assert_owned()
        now = time.time() if now is None else now
        present = sorted({_stored_source_key(path) for path in present_sources})
        with self.conn:
            self.conn.execute(
                "CREATE TEMP TABLE IF NOT EXISTS current_scan_sources "
                "(source_path TEXT PRIMARY KEY)"
            )
            self.conn.execute("DELETE FROM current_scan_sources")
            self.conn.executemany(
                "INSERT INTO current_scan_sources(source_path) VALUES(?)",
                ((source,) for source in present),
            )
            marked = self.conn.execute(
                """UPDATE tracked_sources SET absent_since=?
                   WHERE absent_since IS NULL AND NOT EXISTS (
                     SELECT 1 FROM current_scan_sources current
                     WHERE current.source_path=tracked_sources.source_path
                   )""",
                (now,),
            ).rowcount
            returned = self.conn.execute(
                """UPDATE tracked_sources SET absent_since=NULL
                   WHERE absent_since IS NOT NULL AND EXISTS (
                     SELECT 1 FROM current_scan_sources current
                     WHERE current.source_path=tracked_sources.source_path
                   )"""
            ).rowcount
            self.conn.execute("DELETE FROM current_scan_sources")
        return {"absent": marked, "returned": returned}

    def prune_absent_state(
        self,
        present_sources,
        *,
        older_than_seconds=STATE_RETENTION_SECONDS,
        max_rows=MAX_STATE_PRUNE_ROWS,
        now=None,
    ):
        """Bound durable history without treating a partial scan as absence.

        Callers provide the keys from one complete scan. Active dead letters are
        deliberately retained; acknowledge_missing_failures first makes their
        retention window start when absence was actually observed.
        """
        self.assert_owned()
        now = time.time() if now is None else now
        cutoff = now - max(0.0, float(older_than_seconds))
        max_rows = min(MAX_STATE_PRUNE_ROWS, max(1, int(max_rows)))
        self.observe_complete_scan(present_sources, now=now)
        with self.conn:
            keys = [
                row[0]
                for row in self.conn.execute(
                    """SELECT tracked.source_path FROM tracked_sources tracked
                       WHERE tracked.absent_since < ?
                         AND NOT EXISTS (
                           SELECT 1 FROM publication_transactions publication
                           WHERE publication.source_path=tracked.source_path
                         )
                       ORDER BY tracked.absent_since,tracked.last_activity_at,
                                tracked.source_path LIMIT ?""",
                    (cutoff, max_rows),
                )
            ]
            result = {"sources": 0, "failures": 0, "reservations": 0}
            for source in keys:
                result["sources"] += self.conn.execute(
                    "DELETE FROM sources WHERE source_path=?", (source,)
                ).rowcount
                result["failures"] += self.conn.execute(
                    "DELETE FROM failures WHERE source_path=?", (source,)
                ).rowcount
                result["reservations"] += self.conn.execute(
                    "DELETE FROM output_reservations WHERE source_path=?", (source,)
                ).rowcount
                self.conn.execute(
                    "DELETE FROM tracked_sources WHERE source_path=?", (source,)
                )
        return result

    def reset_failures(self):
        """Explicitly acknowledge all active failures without deleting their records."""
        self.assert_owned()
        with self.conn:
            result = self.conn.execute(
                "UPDATE failures SET active=0, updated_at=? WHERE active=1", (time.time(),)
            )
        return result.rowcount

    def get_meta(self, key, default=None):
        self.assert_owned()
        row = self.conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def record_baseline(
        self,
        baseline_key,
        records,
        reservations=(),
        mapping_failures=(),
        *,
        config_fingerprint="baseline",
    ):
        """Atomically seed a one-time --skip-existing baseline."""
        self.assert_owned()
        now = time.time()
        fingerprints = {
            _stored_source_key(source_path): fingerprint
            for source_path, fingerprint in records
        }
        reservation_rows = [
            (_stored_source_key(source_path), kind, output, reservation_key(output))
            for source_path, kind, output in reservations
        ]
        mapping_failure_rows = {
            _stored_source_key(source_path): (fingerprint, str(detail))
            for source_path, fingerprint, detail in mapping_failures
        }
        owners_by_target = {}
        for source, _, _, target in reservation_rows:
            owners_by_target.setdefault(target, set()).add(source)
        conflicted = {
            source
            for owners in owners_by_target.values()
            if len(owners) > 1
            for source in owners
        }
        for source, _, _, target in reservation_rows:
            existing = self.conn.execute(
                "SELECT source_path FROM output_reservations WHERE output_path=?",
                (target,),
            ).fetchone()
            if existing and existing["source_path"] != source:
                conflicted.add(source)

        with self.conn:
            if self.get_meta(baseline_key) == "complete":
                return False
            for source in sorted(
                set(fingerprints)
                | {row[0] for row in reservation_rows}
                | set(mapping_failure_rows)
            ):
                self._admit_source(source, now)
            for source, kind, _, target in reservation_rows:
                if source in conflicted:
                    continue
                self.conn.execute(
                    """INSERT OR IGNORE INTO output_reservations
                       (output_path,source_path,kind,created_at) VALUES(?,?,?,?)""",
                    (target, source, kind, now),
                )
            for source_path, fingerprint in records:
                source = _stored_source_key(source_path)
                if source in conflicted or source in mapping_failure_rows:
                    continue
                self.conn.execute(
                    """INSERT INTO sources
                       (source_path,fingerprint,config_fingerprint,status,output_metadata,updated_at)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(source_path) DO NOTHING""",
                    (
                        source, fingerprint, "baseline", "baseline", "[]", now,
                    ),
                )
            for source in sorted(conflicted):
                fingerprint = fingerprints.get(source, "baseline-collision")
                self.conn.execute(
                    """INSERT INTO failures
                       (source_path,fingerprint,config_fingerprint,attempts,next_retry_at,
                        permanent,active,error_code,error_text,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(source_path) DO UPDATE SET
                         fingerprint=excluded.fingerprint,
                         config_fingerprint=excluded.config_fingerprint,
                         attempts=1, next_retry_at=NULL, permanent=1, active=1,
                         error_code=excluded.error_code,
                         error_text=excluded.error_text,
                         updated_at=excluded.updated_at""",
                    (
                        source,
                        fingerprint,
                        config_fingerprint,
                        1,
                        None,
                        1,
                        1,
                        "source-output-collision",
                        "multiple source files map to the same requested output; "
                        "rename or remove one source to re-arm conversion",
                        now,
                    ),
                )
            for source in sorted(mapping_failure_rows):
                fingerprint, message = mapping_failure_rows[source]
                self.conn.execute(
                    """INSERT INTO failures
                       (source_path,fingerprint,config_fingerprint,attempts,next_retry_at,
                        permanent,active,error_code,error_text,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(source_path) DO UPDATE SET
                         fingerprint=excluded.fingerprint,
                         config_fingerprint=excluded.config_fingerprint,
                         attempts=1, next_retry_at=NULL, permanent=1, active=1,
                         error_code=excluded.error_code,
                         error_text=excluded.error_text,
                         updated_at=excluded.updated_at""",
                    (
                        source,
                        fingerprint,
                        config_fingerprint,
                        1,
                        None,
                        1,
                        1,
                        "source-output-mapping",
                        message,
                        now,
                    ),
                )
            self.conn.execute(
                "INSERT OR REPLACE INTO metadata(key,value) VALUES(?, 'complete')",
                (baseline_key,),
            )
        self.last_baseline_conflicts = len(
            conflicted | set(mapping_failure_rows)
        )
        return True

    def write_health(
        self, status, *, last_scan_at=None, pending=0, inflight=0,
        permanent_failures=None, force=False,
    ):
        self.assert_owned()
        now = time.time()
        if permanent_failures is None:
            permanent_failures = self.count_permanent_failures()
        last_success = self.get_meta("last_success_at")
        signature = (
            status,
            int(pending),
            int(inflight),
            int(permanent_failures),
            last_success,
        )
        if (
            not force
            and signature == self._last_health_signature
            and now - self._last_health_write_at < self.health_write_interval
        ):
            return False
        payload = {
            "schema": HEALTH_SCHEMA,
            "watcher_pid": os.getpid(),
            "watcher_start_ticks": _linux_process_start_ticks(),
            "status": status,
            "updated_at": now,
            "updated_at_iso": _utc_iso(now),
            "last_scan_at": last_scan_at,
            "last_success_at": float(last_success) if last_success else None,
            "pending": int(pending),
            "inflight": int(inflight),
            "permanent_failures": int(permanent_failures),
        }
        fd, tmp_name = tempfile.mkstemp(
            prefix=".health.", suffix=".tmp", dir=self.state_dir
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, self.health_path)
            try:
                directory_fd = os.open(self.state_dir, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
            self._last_health_write_at = now
            self._last_health_signature = signature
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
        return True


def _env_path(name):
    value = os.environ.get(name)
    return Path(value) if value else None


def _sanitize_log_line(value):
    text = str(value).encode("utf-8", errors="backslashreplace").decode("utf-8")
    safe = []
    for character in text:
        codepoint = ord(character)
        if unicodedata.category(character).startswith("C"):
            if codepoint <= 0xFF:
                safe.append(f"\\x{codepoint:02x}")
            elif codepoint <= 0xFFFF:
                safe.append(f"\\u{codepoint:04x}")
            else:
                safe.append(f"\\U{codepoint:08x}")
        else:
            safe.append(character)
    return "".join(safe)


def log(msg):
    stamp = time.strftime("[%H:%M:%S] ")
    lines = str(msg).splitlines() or [""]
    with LOG_LOCK:
        for line in lines:
            out = stamp + _sanitize_log_line(line)
            print(out, flush=True)
            if LOG_FILE:
                LOG_FILE.write(out + "\n")
                LOG_FILE.flush()


def configure_logging(path):
    global LOG_FILE
    if path:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            LOG_FILE = open(path, "a", encoding="utf-8")
        except OSError as e:
            sys.exit(f"cannot open log file {path}: {e}")


def install_stop_signal_handlers():
    """Turn SIGINT/SIGTERM into a cooperative drain of in-flight work."""
    previous = {}

    def request_stop(_signum, _frame):
        STOP_EVENT.set()

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)
    return previous


def restore_signal_handlers(previous):
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def is_raw_file(path):
    return path.suffix.lower() in RAW_SUFFIXES


def parse_formats(value):
    out = set()
    for part in value.split(","):
        name = part.strip().lower()
        if not name:
            continue
        if name == "both":
            out.update(("tiff", "dng"))
        elif name in FORMAT_ORDER:
            out.add(name)
        else:
            raise argparse.ArgumentTypeError(
                "format must be a comma-separated set of tiff,jpeg,dng (or both)"
            )
    if not out:
        raise argparse.ArgumentTypeError("at least one output format is required")
    return frozenset(out)


def parse_quality(value):
    try:
        quality = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("quality must be an integer")
    if not 1 <= quality <= 100:
        raise argparse.ArgumentTypeError("quality must be between 1 and 100")
    return quality


def parse_exp_comp(value):
    try:
        ev = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("exp-comp must be a number")
    if not -5.0 <= ev <= 5.0:  # also rejects nan/inf before they reach the SDK
        raise argparse.ArgumentTypeError("exp-comp must be between -5 and 5 EV")
    return ev


def parse_jobs(value):
    try:
        jobs = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("jobs must be an integer")
    if not 1 <= jobs <= MAX_JOBS:
        raise argparse.ArgumentTypeError(f"jobs must be between 1 and {MAX_JOBS}")
    return jobs


def parse_interval(value):
    try:
        interval = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("interval must be a number")
    if not math.isfinite(interval) or not 0.1 <= interval <= MAX_INTERVAL_SECONDS:
        raise argparse.ArgumentTypeError(
            f"interval must be between 0.1 and {MAX_INTERVAL_SECONDS:g} seconds"
        )
    return interval


def parse_nonnegative_duration(value):
    try:
        duration = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("duration must be a number")
    if not math.isfinite(duration) or not 0 <= duration <= 86400:
        raise argparse.ArgumentTypeError("duration must be between 0 and 86400 seconds")
    return duration


def parse_timeout(value):
    timeout = parse_nonnegative_duration(value)
    if timeout == 0:
        raise argparse.ArgumentTypeError("timeout must be greater than zero")
    return timeout


def parse_max_pending(value):
    try:
        pending = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("max-pending must be an integer")
    if not 1 <= pending <= MAX_PENDING:
        raise argparse.ArgumentTypeError(
            f"max-pending must be between 1 and {MAX_PENDING}"
        )
    return pending


def parse_max_input_mib(value):
    try:
        maximum = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("max-input-mib must be an integer")
    if not 1 <= maximum <= MAX_INPUT_MIB:
        raise argparse.ArgumentTypeError(
            f"max-input-mib must be between 1 and {MAX_INPUT_MIB}"
        )
    return maximum


def parse_max_scan_entries(value):
    try:
        maximum = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("max-scan-entries must be an integer")
    if not 1 <= maximum <= MAX_SCAN_ENTRIES:
        raise argparse.ArgumentTypeError(
            f"max-scan-entries must be between 1 and {MAX_SCAN_ENTRIES}"
        )
    return maximum


def needs_raster(args):
    return bool(args.formats & RASTER_FORMATS)


def configured_process_env(args):
    return getattr(args, "process_env", getattr(args, "render_env", None))


def configured_parser_env(args, *, temp_dir):
    """Build a minimal parser environment scoped to its writable work dir."""
    source = configured_process_env(args) or os.environ
    environment = {}
    for name in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ"):
        value = source.get(name)
        if value:
            environment[name] = value
    environment.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    environment["HOME"] = "/nonexistent"
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONSAFEPATH"] = "1"
    try:
        parser_temp = Path(temp_dir).resolve(strict=True)
    except OSError as error:
        raise PermanentConversionError(
            f"parser temporary directory is unavailable: {temp_dir}: {error}"
        ) from error
    if not parser_temp.is_dir():
        raise PermanentConversionError(
            f"parser temporary path is not a directory: {parser_temp}"
        )
    environment["TMPDIR"] = os.fspath(parser_temp)
    return environment


def _attest_landlock_launcher(launcher, *, allow_test_override=False):
    launcher = Path(launcher)
    if not allow_test_override and lexical_path(launcher) != lexical_path(
        DEFAULT_LANDLOCK_EXEC
    ):
        raise PermanentConversionError(
            "untrusted parser sandbox must use the packaged Landlock launcher"
        )
    try:
        descriptor, metadata = _open_regular_fd(
            launcher, require_single_link=True
        )
    except OSError as error:
        raise PermanentConversionError(
            f"untrusted parser sandbox is unavailable: {launcher}: {error}"
        ) from error
    else:
        os.close(descriptor)
    expected_uid = os.geteuid() if allow_test_override else 0
    if (
        metadata.st_uid != expected_uid
        or metadata.st_mode & 0o022
        or not metadata.st_mode & 0o111
    ):
        raise PermanentConversionError(
            "Landlock launcher must be single-link, executable, trusted-owned, "
            "and not group/world writable"
        )
    if allow_test_override:
        return launcher
    current = launcher
    while True:
        try:
            ancestor = os.lstat(current)
        except OSError as error:
            raise PermanentConversionError(
                f"cannot attest Landlock launcher ancestor {current}: {error}"
            ) from error
        if stat_module.S_ISLNK(ancestor.st_mode):
            raise PermanentConversionError(
                f"Landlock launcher path contains a symbolic link: {current}"
            )
        if ancestor.st_uid != 0 or ancestor.st_mode & 0o022:
            raise PermanentConversionError(
                f"Landlock launcher path is not root-owned and sealed: {current}"
            )
        if current.parent == current:
            break
        current = current.parent
    return launcher


def sandbox_untrusted_parser(cmd, args, *, read_only=(), read_write=()):
    """Wrap metadata/raw parsers in a fail-closed Linux Landlock domain.

    Exact uploaded inputs are readable and a per-conversion disposable work
    directory is writable.  The public output tree and SQLite state directory
    are deliberately absent from the allowlist.
    """
    command = [os.fspath(value) for value in cmd]
    if not sys.platform.startswith("linux"):
        return command
    launcher = Path(
        getattr(args, "landlock_exec", None) or DEFAULT_LANDLOCK_EXEC
    )
    launcher = _attest_landlock_launcher(
        launcher,
        allow_test_override=bool(
            getattr(args, "_allow_test_landlock_exec", False)
        ),
    )

    runtime_roots = (
        Path("/usr"),
        Path("/bin"),
        Path("/lib"),
        Path("/lib64"),
        Path("/etc"),
        Path("/proc/self"),
        Path("/dev/null"),
        Path("/dev/urandom"),
        Path("/dev/random"),
    )
    wrapped = [sys.executable, "-I", os.fspath(launcher)]
    seen = set()
    for value in (*runtime_roots, *map(Path, read_only)):
        if os.fspath(value) == "/proc/self":
            resolved_text = "/proc/self"
        else:
            try:
                resolved_text = os.fspath(value.resolve(strict=True))
            except OSError:
                continue
        key = ("ro", resolved_text)
        if key in seen:
            continue
        seen.add(key)
        wrapped.extend(("--ro", resolved_text))
    for value in map(Path, read_write):
        try:
            resolved = value.resolve(strict=True)
        except OSError as error:
            raise PermanentConversionError(
                f"parser work path is unavailable: {value}: {error}"
            ) from error
        configured_temp = getattr(args, "temp_dir", None)
        if configured_temp is not None:
            temp_root = Path(configured_temp).resolve(strict=True)
            if resolved != temp_root and not path_is_within(resolved, temp_root):
                raise PermanentConversionError(
                    "parser write access must stay inside configured disposable "
                    f"temp storage: {resolved}"
                )
        for attribute in ("input_root", "output_root", "state_dir"):
            protected = getattr(args, attribute, None)
            if protected is None:
                continue
            protected = Path(protected).resolve(strict=True)
            if (
                resolved == protected
                or path_is_within(resolved, protected)
                or path_is_within(protected, resolved)
            ):
                raise PermanentConversionError(
                    f"parser work path overlaps protected {attribute}: {resolved}"
                )
        key = ("rw", os.fspath(resolved))
        if key in seen:
            continue
        seen.add(key)
        wrapped.extend(("--rw", os.fspath(resolved)))
    wrapped.extend(("--", *command))
    return wrapped


def _raise_parser_error(proc, label):
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [
        "failed"
    ]
    if proc.returncode == 77:
        raise PermanentConversionError(
            f"{label} sandbox could not be enforced: {tail[0]}"
        )
    raise TransientConversionError(f"{label}: {tail[0]}")


def verify_parser_sandbox(args):
    """Fail startup before consuming uploads if the parser boundary is absent."""
    if not sys.platform.startswith("linux"):
        return
    if not (getattr(args, "exiftool", None) or getattr(args, "dng_bin", None)):
        return
    with tempfile.TemporaryDirectory(
        prefix="nef-watch-render-parser-probe-",
        dir=getattr(args, "temp_dir", None),
    ) as probe_root_text:
        probe_root = Path(probe_root_text)
        work = probe_root / "allowed"
        work.mkdir(mode=0o700)
        secret = probe_root / "forbidden-read"
        secret.write_text("secret", encoding="utf-8")
        forbidden_write = probe_root / "forbidden-write"
        allowed_result = work / "result"
        script = """
import pathlib
import sys
import tempfile

secret = pathlib.Path(sys.argv[1])
forbidden_write = pathlib.Path(sys.argv[2])
allowed_result = pathlib.Path(sys.argv[3])
parent_environ = pathlib.Path(sys.argv[4])

denied_read = denied_write = denied_parent = False
try:
    secret.read_bytes()
except PermissionError:
    denied_read = True
try:
    forbidden_write.write_text("unsafe", encoding="utf-8")
except PermissionError:
    denied_write = True
try:
    parent_environ.read_bytes()
except PermissionError:
    denied_parent = True
proc_ok = pathlib.Path("/proc/self/status").read_bytes().startswith(b"Name:")
with tempfile.NamedTemporaryFile(
    prefix="nef-watch-probe-", delete=False
) as temporary:
    temporary.write(b"temporary")
    temp_name = temporary.name
pathlib.Path(temp_name).unlink()
temp_ok = (
    pathlib.Path(temp_name).parent == allowed_result.parent
    and not pathlib.Path(temp_name).exists()
)
allowed_result.write_text("ok", encoding="utf-8")
sys.exit(
    0
    if denied_read and denied_write and denied_parent and proc_ok and temp_ok
    else 9
)
"""
        command = sandbox_untrusted_parser(
            [
                sys.executable,
                "-I",
                "-c",
                script,
                os.fspath(secret),
                os.fspath(forbidden_write),
                os.fspath(allowed_result),
                f"/proc/{os.getpid()}/environ",
            ],
            args,
            read_write=(work,),
        )
        process = run_process(
            command,
            timeout=30,
            label="parser sandbox probe",
            env=configured_parser_env(args, temp_dir=work),
            kill_grace_seconds=getattr(args, "kill_grace_seconds", 5.0),
        )
        if process.returncode != 0 or not allowed_result.is_file():
            tail = (process.stderr or process.stdout or "").strip().splitlines()
            explanation = tail[-1] if tail else f"exit {process.returncode}"
            raise PermanentConversionError(
                "untrusted parser sandbox cannot be enforced: " + explanation
            )


def validate_raw_magic(path):
    try:
        descriptor, _ = _open_regular_fd(path, require_single_link=True)
        with os.fdopen(descriptor, "rb") as f:
            magic = f.read(4)
    except UnsafeFileError as e:
        raise InvalidRawError(f"unsafe input file: {e}") from e
    except OSError as e:
        raise TransientConversionError(f"cannot read input: {e}") from e
    if magic not in (b"II*\0", b"MM\0*"):
        raise InvalidRawError(
            f"{Path(path).name} is not a TIFF-based Nikon NEF/NRW (bad magic {magic!r})"
        )


def _stable_file_identity(st):
    return (
        st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
    )


def _stat_key_from_stat(st):
    return (
        st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
    )


def _stat_identity_fingerprint(st, reason):
    return _json_dumps(
        {
            "schema": 1,
            "reason": reason,
            "device": st.st_dev,
            "inode": st.st_ino,
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "ctime_ns": st.st_ctime_ns,
        }
    )


def _oversized_input_error(metadata, max_bytes, max_mib=None, *, observed=None):
    observed = metadata.st_size if observed is None else observed
    limit_mib = max_mib if max_mib is not None else max_bytes / (1024 * 1024)
    error = InvalidRawError(
        f"input is at least {observed / (1024 * 1024):.1f} MiB; "
        f"limit is {limit_mib:g} MiB (--max-input-mib to override)"
    )
    error.fingerprint = _stat_identity_fingerprint(metadata, "oversized")
    return error


def fingerprint_file(
    path, *, validate_magic=True, max_bytes=None, max_mib=None,
    root=None, root_identity=None,
):
    """Hash a source only when one stable file version can be read end-to-end."""
    if max_bytes is None:
        max_mib = DEFAULT_MAX_INPUT_MIB
        max_bytes = DEFAULT_MAX_INPUT_MIB * 1024 * 1024
    digest = hashlib.sha256()
    first = b""
    try:
        descriptor, before = _open_regular_fd(
            path,
            root=root,
            root_identity=root_identity,
            require_single_link=True,
        )
        with os.fdopen(descriptor, "rb") as f:
            if max_bytes is not None and before.st_size > max_bytes:
                raise _oversized_input_error(before, max_bytes, max_mib)
            total = 0
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise _oversized_input_error(
                        before, max_bytes, max_mib, observed=total
                    )
                if len(first) < 4:
                    first += chunk[: 4 - len(first)]
                digest.update(chunk)
            after = os.fstat(f.fileno())
        current_descriptor, current = _open_regular_fd(
            path,
            root=root,
            root_identity=root_identity,
            require_single_link=True,
        )
        os.close(current_descriptor)
    except InvalidRawError:
        raise
    except UnsafeFileError as e:
        raise InvalidRawError(f"unsafe input file: {e}") from e
    except OSError as e:
        raise TransientConversionError(f"cannot fingerprint input: {e}") from e
    if (
        _stable_file_identity(before) != _stable_file_identity(after)
        or _stable_file_identity(after) != _stable_file_identity(current)
    ):
        raise SourceChangedError(f"{Path(path).name} changed while it was being fingerprinted")
    if before.st_size <= 0 and validate_magic:
        raise SourceChangedError(f"{Path(path).name} is empty and may still be uploading")
    value = {
        "schema": 1,
        "sha256": digest.hexdigest(),
        "size": before.st_size,
    }
    fingerprint = _json_dumps(value)
    if validate_magic and first not in (b"II*\0", b"MM\0*"):
        error = InvalidRawError(
            f"{Path(path).name} is not a TIFF-based Nikon NEF/NRW (bad magic {first!r})"
        )
        error.fingerprint = fingerprint
        raise error
    return fingerprint, _stat_key_from_stat(before)


def lightweight_fingerprint(path):
    try:
        st = Path(path).lstat()
        return _json_dumps(
            {
                "device": st.st_dev,
                "inode": st.st_ino,
                "mode": stat_module.S_IFMT(st.st_mode),
                "size": st.st_size,
                "mtime_ns": st.st_mtime_ns,
                "ctime_ns": st.st_ctime_ns,
            }
        )
    except OSError:
        return _json_dumps({"missing": True})


def create_input_snapshot(source, args, expected_fingerprint=None):
    """Copy one immutable source version into the configured fast temp storage."""
    temp_dir = getattr(args, "temp_dir", None)
    fd, name = tempfile.mkstemp(
        prefix="nef-watch-input-", suffix=Path(source).suffix.lower(), dir=temp_dir
    )
    snapshot_path = Path(name)
    digest = hashlib.sha256()
    first = b""
    try:
        try:
            source_descriptor, before = _open_regular_fd(
                source,
                root=getattr(args, "input_root", None),
                root_identity=getattr(args, "input_root_identity", None),
                require_single_link=True,
            )
            with os.fdopen(source_descriptor, "rb") as src, os.fdopen(fd, "wb") as dst:
                max_bytes = getattr(
                    args,
                    "max_input_bytes",
                    DEFAULT_MAX_INPUT_MIB * 1024 * 1024,
                )
                max_mib = getattr(args, "max_input_mib", DEFAULT_MAX_INPUT_MIB)
                if before.st_size > max_bytes:
                    error = _oversized_input_error(before, max_bytes, max_mib)
                    if expected_fingerprint is not None:
                        error.fingerprint = expected_fingerprint
                    raise error
                total = 0
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        error = _oversized_input_error(
                            before, max_bytes, max_mib, observed=total
                        )
                        if expected_fingerprint is not None:
                            error.fingerprint = expected_fingerprint
                        raise error
                    if len(first) < 4:
                        first += chunk[: 4 - len(first)]
                    digest.update(chunk)
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
                os.fchmod(dst.fileno(), 0o400)
                after = os.fstat(src.fileno())
            current_descriptor, current = _open_regular_fd(
                source,
                root=getattr(args, "input_root", None),
                root_identity=getattr(args, "input_root_identity", None),
                require_single_link=True,
            )
            os.close(current_descriptor)
        except UnsafeFileError as e:
            try:
                os.close(fd)
            except OSError:
                pass
            raise InvalidRawError(f"unsafe input file: {e}") from e
        except OSError as e:
            try:
                os.close(fd)
            except OSError:
                pass
            raise TransientConversionError(f"cannot snapshot input: {e}") from e
        if (
            _stable_file_identity(before) != _stable_file_identity(after)
            or _stable_file_identity(after) != _stable_file_identity(current)
        ):
            raise SourceChangedError(f"{Path(source).name} changed while snapshotting")
        fingerprint = _json_dumps(
            {
                "schema": 1,
                "sha256": digest.hexdigest(),
                "size": before.st_size,
            }
        )
        if first not in (b"II*\0", b"MM\0*"):
            error = InvalidRawError(
                f"{Path(source).name} is not a TIFF-based Nikon NEF/NRW (bad magic {first!r})"
            )
            error.fingerprint = fingerprint
            raise error
        if expected_fingerprint is not None and fingerprint != expected_fingerprint:
            raise SourceChangedError(f"{Path(source).name} changed before snapshotting")
        return InputSnapshot(
            source=Path(source), path=snapshot_path, fingerprint=fingerprint,
            stat_key=_stat_key_from_stat(before),
        )
    except Exception:
        try:
            snapshot_path.unlink()
        except OSError:
            pass
        raise


def remove_snapshot(snapshot):
    try:
        snapshot.path.unlink()
    except OSError:
        pass


def stale_work_age(args):
    return max(
        600.0,
        getattr(args, "render_timeout", 300.0)
        + getattr(args, "kill_grace_seconds", 5.0)
        + 60.0,
        getattr(args, "dng_timeout", 300.0)
        + getattr(args, "kill_grace_seconds", 5.0)
        + 60.0,
    )


@dataclass
class RecoveryProgress:
    recovered: int
    examined: int
    sweep_complete: bool
    limit_exceeded: bool = False
    error_count: int = 0
    ambiguous_count: int = 0


class TempArtifactRecovery:
    """Incrementally reclaim bounded disposable files and private work trees."""

    def __init__(self, temp_dir, *, prior_session_cutoff=None):
        self.temp_dir = Path(temp_dir)
        self.prior_session_cutoff = prior_session_cutoff
        self._entries = None
        self._root_fd = -1
        self._tree = []
        self._sweep_error_count = 0

    def close(self):
        while self._tree:
            frame = self._tree.pop()
            frame["entries"].close()
            os.close(frame["fd"])
        if self._entries is not None:
            self._entries.close()
            self._entries = None
        if self._root_fd >= 0:
            os.close(self._root_fd)
            self._root_fd = -1

    def _open_sweep(self):
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        self._root_fd = os.open(self.temp_dir, flags)
        self._entries = self._scandir_copy(self._root_fd)

    @staticmethod
    def _scandir_copy(descriptor):
        duplicate = os.dup(descriptor)
        try:
            return os.scandir(duplicate)
        except Exception:
            os.close(duplicate)
            raise

    def _start_tree(self, name, expected):
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(name, flags, dir_fd=self._root_fd)
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            os.close(descriptor)
            raise UnsafeFileError(
                errno.ESTALE, "temp work directory changed before recovery"
            )
        try:
            entries = self._scandir_copy(descriptor)
        except Exception:
            os.close(descriptor)
            raise
        self._tree.append(
            {
                "fd": descriptor,
                "entries": entries,
                "parent_fd": self._root_fd,
                "name": name,
            }
        )

    def _recover_tree_entry(self):
        """Process at most one nested name; return (removed, examined, errors)."""
        while self._tree:
            frame = self._tree[-1]
            try:
                entry = next(frame["entries"])
            except StopIteration:
                frame["entries"].close()
                try:
                    os.rmdir(frame["name"], dir_fd=frame["parent_fd"])
                    removed = 1
                except OSError:
                    removed = 0
                os.close(frame["fd"])
                self._tree.pop()
                if removed:
                    return removed, 0, 0
                return 0, 0, 1
            except OSError:
                return 0, 0, 1
            try:
                metadata = entry.stat(follow_symlinks=False)
                if (
                    stat_module.S_ISDIR(metadata.st_mode)
                    and not entry.is_symlink()
                    and len(self._tree) < 64
                ):
                    flags = (
                        os.O_RDONLY
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0)
                    )
                    child = os.open(entry.name, flags, dir_fd=frame["fd"])
                    current = os.fstat(child)
                    if (current.st_dev, current.st_ino) != (
                        metadata.st_dev,
                        metadata.st_ino,
                    ):
                        os.close(child)
                        return 0, 1, 1
                    try:
                        entries = self._scandir_copy(child)
                    except Exception:
                        os.close(child)
                        raise
                    self._tree.append(
                        {
                            "fd": child,
                            "entries": entries,
                            "parent_fd": frame["fd"],
                            "name": entry.name,
                        }
                    )
                    return 0, 1, 0
                if stat_module.S_ISDIR(metadata.st_mode):
                    os.rmdir(entry.name, dir_fd=frame["fd"])
                else:
                    os.unlink(entry.name, dir_fd=frame["fd"])
                return 1, 1, 0
            except OSError:
                return 0, 1, 1
        return 0, 0, 0

    def recover_batch(
        self,
        older_than_seconds,
        *,
        now=None,
        max_entries=DEFAULT_RECOVERY_BATCH_ENTRIES,
    ):
        now = time.time() if now is None else now
        if self._entries is None:
            self._sweep_error_count = 0
            try:
                self._open_sweep()
            except OSError:
                self.close()
                return RecoveryProgress(0, 0, False, error_count=1)
        removed = 0
        examined = 0
        complete = False
        errors = 0
        while examined < max(1, int(max_entries)):
            if self._tree:
                tree_removed, tree_examined, tree_errors = self._recover_tree_entry()
                removed += tree_removed
                examined += tree_examined
                errors += tree_errors
                if tree_errors:
                    self.close()
                    break
                if self._tree or tree_removed or tree_examined:
                    continue
            try:
                entry = next(self._entries)
            except StopIteration:
                self.close()
                complete = True
                break
            except OSError:
                errors += 1
                self.close()
                break
            examined += 1
            if not entry.name.startswith(("nef-watch-input-", "nef-watch-render-")):
                continue
            try:
                metadata = entry.stat(follow_symlinks=False)
                prior_session = bool(
                    self.prior_session_cutoff is not None
                    and metadata.st_mtime <= self.prior_session_cutoff
                )
                stale = prior_session or now - metadata.st_mtime >= older_than_seconds
                if not stale:
                    continue
                if stat_module.S_ISDIR(metadata.st_mode) and not entry.is_symlink():
                    self._start_tree(entry.name, metadata)
                    continue
                if not stat_module.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    errors += 1
                    self.close()
                    break
                os.unlink(entry.name, dir_fd=self._root_fd)
                removed += 1
            except OSError:
                errors += 1
                self.close()
                break
        self._sweep_error_count += errors
        return RecoveryProgress(
            removed,
            examined,
            complete,
            error_count=self._sweep_error_count,
        )


def cleanup_stale_temp_files(
    temp_dir,
    older_than_seconds,
    now=None,
    *,
    max_entries=DEFAULT_RECOVERY_BATCH_ENTRIES,
):
    """Compatibility wrapper for one bounded disposable-storage batch."""
    recovery = TempArtifactRecovery(temp_dir)
    try:
        return recovery.recover_batch(
            older_than_seconds,
            now=now,
            max_entries=max_entries,
        ).recovered
    finally:
        recovery.close()


class OutputArtifactRecovery:
    """Incremental sweep limited to authenticated private artifact trees.

    Recovery always walks the complete output hierarchy.  The ``recursive``
    argument is retained for call-site compatibility, but it cannot safely
    narrow recovery: artifacts from an earlier recursive deployment must
    still be found after the watcher is reconfigured as non-recursive.
    """

    def __init__(
        self,
        out_dir,
        recursive,
        *,
        max_sweep_entries=MAX_RECOVERY_SWEEP_ENTRIES,
        prior_session_cutoff=None,
    ):
        self.root = Path(out_dir)
        self.recursive = bool(recursive)
        self.max_sweep_entries = min(
            MAX_RECOVERY_SWEEP_ENTRIES, max(1, int(max_sweep_entries))
        )
        self.prior_session_cutoff = prior_session_cutoff
        self.root_identity = None
        self._stream = None
        self._sweep_error_count = 0
        self._sweep_ambiguous_count = 0

    def _entries(self):
        pending = [self.root]
        examined = 0

        def count_entry():
            nonlocal examined
            examined += 1
            if examined > self.max_sweep_entries:
                raise ScanLimitError(
                    "output recovery exceeded its bounded sweep ceiling"
                )

        def authenticated_directory(metadata, expected_device):
            return (
                stat_module.S_ISDIR(metadata.st_mode)
                and metadata.st_uid == os.geteuid()
                and stat_module.S_IMODE(metadata.st_mode) == 0o700
                and metadata.st_dev == expected_device
            )

        def artifact_entries(folder, metadata, parent_device):
            if not authenticated_directory(metadata, parent_device):
                yield folder, metadata, "invalid"
                return
            try:
                roles = os.scandir(folder)
            except OSError as error:
                raise IncompleteScanError(
                    error.errno or errno.EIO,
                    f"cannot scan transaction artifact directory {folder}: {error}",
                ) from error
            with roles:
                for role_entry in roles:
                    count_entry()
                    try:
                        role_metadata = role_entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    except OSError as error:
                        raise IncompleteScanError(
                            error.errno or errno.EIO,
                            f"cannot inspect transaction artifact role {role_entry.path}: {error}",
                        ) from error
                    role = role_entry.name
                    role_path = Path(role_entry.path)
                    if (
                        role not in ARTIFACT_ROLES
                        or role_entry.is_symlink()
                        or not authenticated_directory(
                            role_metadata, metadata.st_dev
                        )
                    ):
                        yield role_path, role_metadata, "invalid"
                        continue
                    yield role_path, role_metadata, "skip"
                    try:
                        artifacts = os.scandir(role_path)
                    except OSError as error:
                        raise IncompleteScanError(
                            error.errno or errno.EIO,
                            f"cannot scan transaction artifact role {role_path}: {error}",
                        ) from error
                    with artifacts:
                        for artifact in artifacts:
                            count_entry()
                            try:
                                artifact_metadata = artifact.stat(
                                    follow_symlinks=False
                                )
                            except FileNotFoundError:
                                continue
                            except OSError as error:
                                raise IncompleteScanError(
                                    error.errno or errno.EIO,
                                    f"cannot inspect transaction artifact {artifact.path}: {error}",
                                ) from error
                            valid = (
                                ARTIFACT_UUID_RE.fullmatch(artifact.name)
                                and stat_module.S_ISREG(artifact_metadata.st_mode)
                                and artifact_metadata.st_nlink == 1
                                and artifact_metadata.st_dev == role_metadata.st_dev
                            )
                            yield (
                                Path(artifact.path),
                                artifact_metadata,
                                role if valid else "invalid",
                            )

        while pending:
            folder = pending.pop()
            if folder != self.root and (
                path_has_symlink_component(folder, self.root)
                or not path_is_within(folder, self.root)
            ):
                continue
            try:
                folder_metadata = os.stat(folder, follow_symlinks=False)
                entries = os.scandir(folder)
            except OSError as error:
                raise IncompleteScanError(
                    error.errno or errno.EIO,
                    f"cannot scan output directory {folder}: {error}",
                ) from error
            with entries:
                for entry in entries:
                    count_entry()
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    except OSError as error:
                        raise IncompleteScanError(
                            error.errno or errno.EIO,
                            f"cannot inspect output entry {entry.path}: {error}",
                        ) from error
                    candidate = Path(entry.path)
                    if entry.name == ARTIFACT_DIR_NAME:
                        if (
                            stat_module.S_ISDIR(metadata.st_mode)
                            and not entry.is_symlink()
                        ):
                            yield candidate, metadata, "skip"
                            yield from artifact_entries(
                                candidate, metadata, folder_metadata.st_dev
                            )
                        else:
                            # The reserved name must always be our exact,
                            # authenticated private directory.  A symlink,
                            # file, FIFO, or device would make future durable
                            # publication fail, so recovery must report the
                            # output tree degraded rather than silently skip it.
                            yield candidate, metadata, "invalid"
                    elif stat_module.S_ISDIR(metadata.st_mode):
                        if not entry.is_symlink():
                            pending.append(candidate)
                            yield candidate, metadata, "skip"
                        else:
                            yield candidate, metadata, "skip"
                    else:
                        yield candidate, metadata, "skip"

    def close(self):
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def recover_batch(
        self,
        older_than_seconds,
        *,
        now=None,
        max_entries=DEFAULT_RECOVERY_BATCH_ENTRIES,
    ):
        now = time.time() if now is None else now
        max_entries = max(1, int(max_entries))
        if self._stream is None:
            if not self.root.is_dir() or self.root.is_symlink():
                return RecoveryProgress(0, 0, False, error_count=1)
            try:
                self.root_identity = _directory_identity(self.root)
            except OSError:
                return RecoveryProgress(0, 0, False, error_count=1)
            self._sweep_error_count = 0
            self._sweep_ambiguous_count = 0
            self._stream = self._entries()

        recovered = 0
        examined = 0
        complete = False
        limit_exceeded = False
        errors = 0
        ambiguous = 0
        while examined < max_entries:
            try:
                artifact, metadata, role = next(self._stream)
            except StopIteration:
                self.close()
                complete = True
                break
            except ScanLimitError:
                self.close()
                limit_exceeded = True
                break
            except (IncompleteScanError, OSError):
                self.close()
                errors += 1
                break
            examined += 1
            if role == "skip":
                continue
            if role == "invalid":
                errors += 1
                continue
            if role == ARTIFACT_BACKUP_DIR:
                # A backup cannot exist without a durable transaction. Startup
                # journal recovery runs first, so an unreferenced backup is
                # ambiguous and must be preserved for operator review.
                ambiguous += 1
                continue
            try:
                prior_session = bool(
                    self.prior_session_cutoff is not None
                    and metadata.st_mtime <= self.prior_session_cutoff
                )
                if (
                    not prior_session
                    and now - metadata.st_mtime < older_than_seconds
                ):
                    continue
                identity = (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    None,
                )
                if _unlink_if_identity_matches(
                    artifact, identity, self.root, self.root_identity
                ):
                    recovered += 1
                else:
                    errors += 1
            except (OSError, OutputCollisionError, UnsafeFileError):
                errors += 1
        self._sweep_error_count += errors
        self._sweep_ambiguous_count += ambiguous
        return RecoveryProgress(
            recovered,
            examined,
            complete,
            limit_exceeded=limit_exceeded,
            error_count=self._sweep_error_count,
            ambiguous_count=self._sweep_ambiguous_count,
        )


def cleanup_stale_output_artifacts(
    out_dir,
    recursive,
    older_than_seconds,
    now=None,
    *,
    max_entries=DEFAULT_RECOVERY_BATCH_ENTRIES,
):
    """Compatibility wrapper for one bounded recovery batch."""
    recovery = OutputArtifactRecovery(
        out_dir,
        recursive,
        max_sweep_entries=max_entries,
    )
    try:
        return recovery.recover_batch(
            older_than_seconds,
            now=now,
            max_entries=max_entries,
        ).recovered
    finally:
        recovery.close()


def complete_recovery_sweep(
    recovery,
    older_than_seconds,
    *,
    now=None,
    max_entries=DEFAULT_MAX_SCAN_ENTRIES,
):
    """Drain one recovery sweep to a clean EOF within an explicit ceiling."""
    now = time.time() if now is None else now
    ceiling = max(1, int(max_entries))
    recovered = 0
    examined = 0
    last_errors = 0
    last_ambiguous = 0
    while examined < ceiling:
        progress = recovery.recover_batch(
            older_than_seconds,
            now=now,
            max_entries=min(DEFAULT_RECOVERY_BATCH_ENTRIES, ceiling - examined),
        )
        recovered += progress.recovered
        examined += progress.examined
        last_errors = progress.error_count
        last_ambiguous = progress.ambiguous_count
        if (
            progress.sweep_complete
            or progress.limit_exceeded
            or progress.error_count
            or progress.ambiguous_count
        ):
            return RecoveryProgress(
                recovered,
                examined,
                progress.sweep_complete,
                limit_exceeded=progress.limit_exceeded,
                error_count=last_errors,
                ambiguous_count=last_ambiguous,
            )
        if progress.examined <= 0:
            return RecoveryProgress(
                recovered,
                examined,
                False,
                error_count=max(1, last_errors),
                ambiguous_count=last_ambiguous,
            )
    return RecoveryProgress(
        recovered,
        examined,
        False,
        limit_exceeded=True,
        error_count=last_errors,
        ambiguous_count=last_ambiguous,
    )


def _terminate_process_group(proc, grace_seconds):
    group_id = proc.pid

    def group_exists():
        try:
            os.killpg(group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return False
        return True

    try:
        os.killpg(group_id, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.monotonic() + grace_seconds
    while group_exists() and time.monotonic() < deadline:
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
    if group_exists():
        try:
            os.killpg(group_id, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if proc.poll() is None:
        try:
            proc.wait(timeout=max(0.1, grace_seconds))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


class _BoundedOutputTail:
    def __init__(self, maximum_bytes):
        self.maximum_bytes = maximum_bytes
        self.data = bytearray()
        self.truncated = False

    def append(self, payload):
        if not payload:
            return
        if len(payload) >= self.maximum_bytes:
            self.data[:] = payload[-self.maximum_bytes :]
            self.truncated = True
            return
        overflow = len(self.data) + len(payload) - self.maximum_bytes
        if overflow > 0:
            del self.data[:overflow]
            self.truncated = True
        self.data.extend(payload)

    def text(self):
        marker = b"[... output truncated ...]\n"
        payload = bytes(self.data)
        if self.truncated:
            payload = marker + payload[-(self.maximum_bytes - len(marker)) :]
        return payload.decode("utf-8", errors="replace")


def run_process(cmd, *, timeout, label, env=None, kill_grace_seconds=5.0):
    """Run a tool with bounded output and containment of its whole process tree."""
    original_cmd = list(cmd)
    supervised = sys.platform.startswith("linux")
    if supervised:
        cmd = [
            sys.executable,
            "-I",
            str(Path(__file__).resolve()),
            "--_process-supervisor",
            str(float(kill_grace_seconds)),
            "--",
            *[os.fspath(value) for value in original_cmd],
        ]
    outer_kill_grace = (
        float(kill_grace_seconds) + 1.0
        if supervised else float(kill_grace_seconds)
    )
    if supervised:
        with _SUPERVISOR_REGISTRY_LOCK:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                bufsize=0,
                env=env,
                start_new_session=True,
            )
            _ACTIVE_SUPERVISORS.add(proc.pid)
    else:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
            env=env,
            start_new_session=True,
        )
    output = {
        "stdout": _BoundedOutputTail(MAX_PROCESS_OUTPUT_BYTES),
        "stderr": _BoundedOutputTail(MAX_PROCESS_OUTPUT_BYTES),
    }
    selector = selectors.DefaultSelector()
    streams = {"stdout": proc.stdout, "stderr": proc.stderr}
    for name, stream in streams.items():
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    deadline = time.monotonic() + timeout
    post_exit_deadline = None
    timed_out = False
    group_cleaned = False
    try:
        while selector.get_map() or proc.poll() is None:
            now = time.monotonic()
            returncode = proc.poll()
            if returncode is None and now >= deadline:
                timed_out = True
                _terminate_process_group(proc, outer_kill_grace)
                group_cleaned = True
                returncode = proc.poll()
                post_exit_deadline = time.monotonic() + 1.0
            elif returncode is not None and post_exit_deadline is None:
                # A misbehaving descendant can retain inherited pipe handles.
                # Reap the process group even if the direct child exited cleanly
                # and descendants closed their inherited output handles.
                _terminate_process_group(proc, outer_kill_grace)
                group_cleaned = True
                post_exit_deadline = now + 1.0

            if post_exit_deadline is not None and now >= post_exit_deadline:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                for key in list(selector.get_map().values()):
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                break

            wait_for = 0.1
            if returncode is None:
                wait_for = min(wait_for, max(0.0, deadline - now))
            elif post_exit_deadline is not None:
                wait_for = min(
                    wait_for, max(0.0, post_exit_deadline - now)
                )
            if selector.get_map():
                ready = selector.select(wait_for)
            else:
                time.sleep(wait_for)
                ready = ()
            for key, _ in ready:
                stream = key.fileobj
                while True:
                    try:
                        payload = os.read(stream.fileno(), 64 * 1024)
                    except BlockingIOError:
                        break
                    except OSError:
                        payload = b""
                    if not payload:
                        try:
                            selector.unregister(stream)
                        except KeyError:
                            pass
                        stream.close()
                        break
                    output[key.data].append(payload)
    finally:
        if not group_cleaned:
            _terminate_process_group(proc, outer_kill_grace)
        selector.close()
        for stream in streams.values():
            if not stream.closed:
                stream.close()
        if supervised:
            with _SUPERVISOR_REGISTRY_LOCK:
                _ACTIVE_SUPERVISORS.discard(proc.pid)
    containment_failure = bool(
        supervised and (proc.returncode == 125 or proc.returncode < 0)
    )
    if containment_failure:
        try:
            _drain_unowned_watcher_descendants(kill_grace_seconds)
        except OSError as error:
            raise ProcessContainmentError(
                f"{label} process containment failed and adopted descendants "
                f"could not be drained: {error}"
            ) from error
        if not timed_out:
            raise ProcessContainmentError(
                f"{label} process containment failed; the supervisor exited "
                f"abnormally ({proc.returncode})"
            )
    if timed_out:
        raise ProcessTimeoutError(f"{label} timed out after {timeout:g}s")
    return subprocess.CompletedProcess(
        original_cmd,
        proc.returncode,
        output["stdout"].text(),
        output["stderr"].text(),
    )


def read_nkraw(path):
    descriptor, before = _open_regular_fd(path, require_single_link=True)
    try:
        if before.st_size > MAX_NKRAW_HEADER + MAX_RENDER_BYTES:
            raise ValueError("NKRAW1 file exceeds safety limit")
        f = os.fdopen(descriptor, "rb")
        descriptor = -1
        with f:
            header = f.readline(MAX_NKRAW_HEADER + 1)
            if not header.endswith(b"\n") or len(header) > MAX_NKRAW_HEADER:
                raise ValueError("invalid or oversized NKRAW1 header")
            try:
                parts = header.decode("ascii").split()
            except UnicodeDecodeError as e:
                raise ValueError("NKRAW1 header is not ASCII") from e
            if len(parts) != 6 or parts[0] != "NKRAW1":
                raise ValueError(f"bad raw magic: {parts[:1]}")
            try:
                w, h, ch, depth, orient = (int(x) for x in parts[1:6])
            except ValueError as e:
                raise ValueError("NKRAW1 header contains a non-integer field") from e
            if w <= 0 or h <= 0 or w * h > MAX_RENDER_PIXELS:
                raise ValueError(f"unsafe NKRAW1 dimensions: {w}x{h}")
            if ch != 3 or depth not in (1, 2):
                raise ValueError(f"unsupported NKRAW1 format: channels={ch} depth={depth}")
            expected = w * h * ch * depth
            if expected > MAX_RENDER_BYTES:
                raise ValueError(f"NKRAW1 payload exceeds safety limit: {expected} bytes")
            if before.st_size != len(header) + expected:
                raise ValueError(
                    f"raw payload {before.st_size - len(header)} != {expected}"
                )
            data = f.read(expected + 1)
            after = os.fstat(f.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(data) != expected:
        raise ValueError(f"raw payload {len(data)} != {expected}")
    if _stable_file_identity(before) != _stable_file_identity(after):
        raise ValueError("NKRAW1 file changed while reading")
    dt = "<u2" if depth == 2 else "u1"
    arr = np.frombuffer(data, dtype=dt).reshape(h, w, ch)
    # GetImageData returns display-oriented pixels (validated in spike 001), so the
    # array is already correctly oriented — no rotation from `orient` needed.
    return arr, dict(w=w, h=h, ch=ch, depth=depth, orient=orient)


def unique_partial(out_path, *, output_root=None, root_identity=None):
    """Allocate a UUID-only stage name in the final parent's private namespace."""
    out_path = Path(out_path)
    if output_root is None:
        output_root = out_path.parent
    output_root = Path(lexical_path(output_root))
    if root_identity is None:
        root_identity = _directory_identity(output_root)
    return _artifact_path_for_final(
        out_path,
        ARTIFACT_STAGE_DIR,
        output_root,
        root_identity,
    )


def encode_tiff(arr, out_path, icc_bytes):
    """8-bit -> Pillow (validated format); 16-bit -> tifffile. Both LZW + ICC."""
    if arr.dtype == np.uint8:
        Image.fromarray(arr).save(
            out_path, format="TIFF", compression="tiff_lzw", icc_profile=icc_bytes
        )
    else:  # uint16
        import tifffile
        extratags = []
        if icc_bytes:
            extratags = [(34675, 7, len(icc_bytes), icc_bytes, True)]  # ICCProfile
        tifffile.imwrite(
            out_path, arr, photometric="rgb", compression="lzw", extratags=extratags
        )


def jpeg_pixels(arr):
    if arr.dtype == np.uint8:
        return arr
    return ((arr.astype(np.uint32) * 255 + 32767) // 65535).astype(np.uint8)


def encode_jpeg(arr, out_path, icc_bytes, quality):
    Image.fromarray(jpeg_pixels(arr)).save(
        out_path, format="JPEG", quality=quality, icc_profile=icc_bytes
    )


def copy_exif(nef, tmp_out, args):
    if not args.exiftool:
        return
    command = [
        str(args.exiftool),
        "-q",
        "-overwrite_original",
        "-tagsFromFile",
        str(nef),
        "-EXIF:all",
        "-makernotes",
        "-Orientation#=1",
        "-ExifImageWidth=",
        "-ExifImageHeight=",
        str(tmp_out),
    ]
    command = sandbox_untrusted_parser(
        command,
        args,
        read_only=(nef,),
        read_write=(Path(tmp_out).parent,),
    )
    proc = run_process(
        command,
        timeout=getattr(args, "exif_timeout", 60.0),
        label="exiftool",
        env=configured_parser_env(args, temp_dir=Path(tmp_out).parent),
        kill_grace_seconds=getattr(args, "kill_grace_seconds", 5.0),
    )
    if proc.returncode != 0:
        _raise_parser_error(proc, "exiftool")


def render_raster(nef, todo, args, icc):
    """SDK develop -> staged TIFF/JPEG. Both share one SDK develop."""
    work_context = tempfile.TemporaryDirectory(
        prefix="nef-watch-render-",
        dir=getattr(args, "temp_dir", None),
    )
    work_root = Path(work_context.name)
    with tempfile.NamedTemporaryFile(
        prefix="nef-watch-render-", suffix=".raw", delete=False,
        # The hardened Wine wrapper accepts only direct children of the
        # configured disposable root for both its immutable input snapshot and
        # pre-created raw destination. Encoded/Exif work remains in work_root.
        dir=getattr(args, "temp_dir", None),
    ) as tf:
        raw = tf.name
    staged = []
    work_files = []
    try:
        develop_bits = args.bits if "tiff" in args.formats else 8
        proc = run_process(
            [
                str(args.render_bin),
                str(nef),
                raw,
                str(args.profile),
                str(develop_bits),
                str(args.exp_comp),
            ],
            timeout=getattr(args, "render_timeout", 300.0),
            label="renderer",
            env=configured_process_env(args),
            kill_grace_seconds=getattr(args, "kill_grace_seconds", 5.0),
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
            raise TransientConversionError(f"render exit {proc.returncode}: {tail[0]}")
        arr, _ = read_nkraw(raw)
        for kind, out_path in todo:
            work_fd, work_name = tempfile.mkstemp(
                prefix="nef-watch-render-",
                suffix=out_path.suffix,
                dir=work_root,
            )
            os.close(work_fd)
            work_path = Path(work_name)
            work_files.append(work_path)
            if kind == "tiff":
                encode_tiff(arr, work_path, icc)
            else:
                encode_jpeg(arr, work_path, icc, args.quality)
            # If ExifTool is available, metadata is part of the requested
            # archival output contract. A failed copy is retryable and the
            # staged raster is discarded instead of knowingly publishing an
            # incomplete file.
            copy_exif(nef, work_path, args)
            staged_path = _copy_to_staged_output(
                work_path, out_path, args, kind=kind
            )
            staged.append((kind, staged_path, out_path))
        return staged
    except Exception:
        _cleanup_staged(staged, args=args)
        raise
    finally:
        for work_path in work_files:
            try:
                work_path.unlink()
            except OSError:
                pass
        try:
            os.unlink(raw)
        except OSError:
            pass
        work_context.cleanup()


def render_dng(nef, out_path, args):
    """Raw transcode NEF -> a staged DNG via dnglab or Adobe DNG Converter.
    A DNG preserves the raw sensor data — it does NOT bake in the Nikon look."""
    try:
        with tempfile.TemporaryDirectory(
            prefix="nef-watch-render-dng-",
            dir=getattr(args, "temp_dir", None),
        ) as td:
            work_dir = Path(td)
            work_path = work_dir / out_path.name
            if args.dng_engine == "dnglab":
                cmd = [str(args.dng_bin), "convert", "-c", "lossless",
                       "--embed-raw", "true" if args.dng_embed_original else "false",
                       str(nef), str(work_path)]
                cmd = sandbox_untrusted_parser(
                    cmd,
                    args,
                    read_only=(nef,),
                    read_write=(work_dir,),
                )
                proc = run_process(
                    cmd,
                    timeout=getattr(args, "dng_timeout", 300.0),
                    label="dnglab",
                    env=configured_parser_env(args, temp_dir=work_dir),
                    kill_grace_seconds=getattr(args, "kill_grace_seconds", 5.0),
                )
                if proc.returncode != 0 or not work_path.exists():
                    if proc.returncode != 0:
                        _raise_parser_error(proc, "dnglab")
                    raise TransientConversionError(
                        "dnglab exited without producing its requested DNG"
                    )
            else:  # adobe — drives the app's CLI; not verified on this machine
                cmd = [str(args.dng_bin), "-c"]
                if args.dng_embed_original:
                    cmd += ["-e"]
                cmd += ["-d", td, "-o", work_path.name, str(nef)]
                cmd = sandbox_untrusted_parser(
                    cmd,
                    args,
                    read_only=(nef,),
                    read_write=(work_dir,),
                )
                proc = run_process(
                    cmd,
                    timeout=getattr(args, "dng_timeout", 300.0),
                    label="Adobe DNG Converter",
                    env=configured_parser_env(args, temp_dir=work_dir),
                    kill_grace_seconds=getattr(args, "kill_grace_seconds", 5.0),
                )
                if proc.returncode != 0 or not work_path.exists():
                    if proc.returncode != 0:
                        _raise_parser_error(proc, "Adobe DNG Converter")
                    raise TransientConversionError(
                        "Adobe DNG Converter exited without producing its requested DNG"
                    )
            staged_path = _copy_to_staged_output(
                work_path, out_path, args, kind="dng"
            )
        return ("dng", staged_path, out_path)
    except Exception:
        raise


def _inspect_output_file(
    path,
    kind,
    *,
    include_hash=False,
    output_root=None,
    root_identity=None,
    maximum_bytes=None,
):
    descriptor = -1
    try:
        if output_root is None:
            descriptor, before = _open_regular_fd(path, require_single_link=True)
        else:
            descriptor, before = _open_output_regular_fd(
                path, output_root, root_identity, require_single_link=True
            )
        if before.st_size <= 4:
            raise UnsafeFileError(errno.EINVAL, f"output is empty: {path}")
        if maximum_bytes is None:
            maximum_bytes = (
                MAX_RASTER_OUTPUT_BYTES
                if kind in RASTER_FORMATS
                else MAX_DNG_OUTPUT_BYTES
            )
        if before.st_size > maximum_bytes:
            raise UnsafeFileError(
                errno.EFBIG, f"{kind} output is oversized: {path}"
            )
        magic = os.pread(descriptor, 4, 0)
        if kind == "jpeg" and magic[:3] != b"\xff\xd8\xff":
            raise ValueError("invalid JPEG magic")
        if kind != "jpeg" and magic not in (b"II*\0", b"MM\0*"):
            raise ValueError("invalid TIFF/DNG magic")

        if kind in ("tiff", "jpeg"):
            with os.fdopen(os.dup(descriptor), "rb") as decoder:
                with Image.open(decoder) as image:
                    if image.format != ("JPEG" if kind == "jpeg" else "TIFF"):
                        raise ValueError("unexpected raster format")
                    if (
                        image.width <= 0
                        or image.height <= 0
                        or image.width * image.height > MAX_RENDER_PIXELS
                    ):
                        raise ValueError(
                            f"unsafe raster dimensions: {image.width}x{image.height}"
                        )
                    if image.mode != "RGB":
                        raise ValueError(f"unexpected raster mode: {image.mode}")
                    image.load()  # fail closed on truncated strips/scan data
        else:
            import tifffile
            with os.fdopen(os.dup(descriptor), "rb") as decoder:
                # File objects created from descriptors have an integer `.name`;
                # tifffile expects a string when deriving a display name.
                with tifffile.TiffFile(decoder, name=Path(path).name) as dng:
                    if not dng.pages:
                        raise ValueError("DNG has no image pages")
                    dng_pages = [page for page in dng.pages if 50706 in page.tags]
                    if not dng_pages:
                        raise ValueError("TIFF is missing the required DNGVersion tag")
                    for page in dng_pages:
                        version = page.tags[50706].value
                        if isinstance(version, (bytes, bytearray)):
                            version_bytes = bytes(version)
                        elif isinstance(version, (tuple, list)) and len(version) == 4:
                            if any(
                                not isinstance(value, int) or not 0 <= value <= 255
                                for value in version
                            ):
                                raise ValueError("DNGVersion has an invalid value")
                            version_bytes = bytes(version)
                        else:
                            raise ValueError("DNGVersion has an invalid value")
                        if len(version_bytes) != 4 or version_bytes[0] == 0:
                            raise ValueError("DNGVersion must be four valid bytes")
                        shape = tuple(int(dimension) for dimension in page.shape)
                        if (
                            not shape
                            or any(dimension <= 0 for dimension in shape)
                            or math.prod(shape) > MAX_RENDER_PIXELS * 4
                        ):
                            raise ValueError("DNG has invalid or unsafe dimensions")
                        offsets = tuple(int(value) for value in page.dataoffsets)
                        bytecounts = tuple(int(value) for value in page.databytecounts)
                        if not offsets or len(offsets) != len(bytecounts):
                            raise ValueError("DNG has no bounded image payload")
                        for offset, bytecount in zip(offsets, bytecounts):
                            if (
                                offset < 0
                                or bytecount <= 0
                                or offset > before.st_size
                                or bytecount > before.st_size - offset
                            ):
                                raise ValueError("DNG image payload escapes the file")

        digest = None
        if include_hash:
            digest_state = hashlib.sha256()
            os.lseek(descriptor, 0, os.SEEK_SET)
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest_state.update(chunk)
            digest = digest_state.hexdigest()
        after = os.fstat(descriptor)
        if _stable_file_identity(before) != _stable_file_identity(after):
            raise UnsafeFileError(errno.ESTALE, f"output changed while validating: {path}")
        return {
            "size": before.st_size,
            "mtime_ns": before.st_mtime_ns,
            "ctime_ns": before.st_ctime_ns,
            "device": before.st_dev,
            "inode": before.st_ino,
            "sha256": digest,
        }
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def validate_output_file(
    path,
    kind,
    *,
    output_root=None,
    root_identity=None,
    maximum_bytes=None,
):
    try:
        _inspect_output_file(
            path,
            kind,
            output_root=output_root,
            root_identity=root_identity,
            maximum_bytes=maximum_bytes,
        )
    except Exception:
        # Decoder/plugin implementations expose several exception types. An
        # externally modified artifact must fail validation, never crash the
        # long-running watcher because one backend used RuntimeError/KeyError.
        return False
    return True


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inspected_signature_entry(path, kind, inspected):
    return (
        lexical_path(path), kind, inspected["device"], inspected["inode"],
        inspected["size"], inspected["mtime_ns"], inspected["ctime_ns"],
    )


def capture_output_metadata(
    outputs,
    *,
    output_root=None,
    root_identity=None,
    return_signature=False,
    maximum_bytes_by_kind=None,
):
    metadata = []
    signature = []
    for kind, path in outputs:
        try:
            inspected = _inspect_output_file(
                path,
                kind,
                include_hash=True,
                output_root=output_root,
                root_identity=root_identity,
                maximum_bytes=(
                    maximum_bytes_by_kind.get(kind)
                    if maximum_bytes_by_kind is not None
                    else None
                ),
            )
        except Exception:
            raise TransientConversionError(f"invalid or incomplete {kind} output: {path}")
        metadata.append(
            {
                "kind": kind,
                "path": lexical_path(path),
                "size": inspected["size"],
                "mtime_ns": inspected["mtime_ns"],
                "sha256": inspected["sha256"],
            }
        )
        signature.append(_inspected_signature_entry(path, kind, inspected))
    if return_signature:
        return metadata, tuple(signature)
    return metadata


_OUTPUT_METADATA_KEYS = frozenset({"kind", "path", "size", "mtime_ns", "sha256"})


def _parse_output_metadata(metadata_json):
    """Parse the bounded, exact ownership record stored after validation."""
    if not isinstance(metadata_json, str) or len(metadata_json) > MAX_OUTPUT_METADATA_BYTES:
        raise ValueError("durable output metadata is invalid or oversized")
    if len(metadata_json.encode("utf-8")) > MAX_OUTPUT_METADATA_BYTES:
        raise ValueError("durable output metadata is oversized")
    stored = json.loads(metadata_json)
    if not isinstance(stored, list) or len(stored) > len(FORMAT_ORDER):
        raise ValueError("durable output metadata is not a bounded list")
    by_path = {}
    seen_kinds = set()
    for entry in stored:
        if not isinstance(entry, dict) or set(entry) != _OUTPUT_METADATA_KEYS:
            raise ValueError("durable output metadata has an invalid entry")
        kind = entry["kind"]
        path_text = entry["path"]
        size = entry["size"]
        mtime_ns = entry["mtime_ns"]
        digest = entry["sha256"]
        if (
            kind not in FORMAT_ORDER
            or kind in seen_kinds
            or not isinstance(path_text, str)
            or not path_text
            or not _path_has_strict_utf8_name(path_text)
            or lexical_path(path_text) != path_text
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 4
            or isinstance(mtime_ns, bool)
            or not isinstance(mtime_ns, int)
            or mtime_ns < 0
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or path_text in by_path
        ):
            raise ValueError("durable output metadata has an invalid entry")
        seen_kinds.add(kind)
        by_path[path_text] = entry
    return stored, by_path


def _prove_durable_output(
    path,
    kind,
    entry,
    *,
    output_root,
    root_identity,
    maximum_bytes,
):
    """Prove exact known bytes before any public file can reach a decoder."""
    if entry["kind"] != kind or entry["path"] != lexical_path(path):
        raise UnsafeFileError(errno.EINVAL, "durable output identity disagrees")
    descriptor = -1
    try:
        descriptor, before = _open_output_regular_fd(
            path, output_root, root_identity, require_single_link=True
        )
        # These constant-time comparisons reject the usual foreign replacement
        # before reading any file payload at all.
        if (
            before.st_size != entry["size"]
            or before.st_mtime_ns != entry["mtime_ns"]
            or before.st_size > maximum_bytes
        ):
            raise UnsafeFileError(
                errno.ESTALE, f"output differs from durable state: {path}"
            )
        digest_state = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest_state.update(chunk)
        after = os.fstat(descriptor)
        if _stable_file_identity(before) != _stable_file_identity(after):
            raise UnsafeFileError(
                errno.ESTALE, f"output changed while proving ownership: {path}"
            )
        digest = digest_state.hexdigest()
        if digest != entry["sha256"]:
            raise UnsafeFileError(
                errno.ESTALE, f"output hash differs from durable state: {path}"
            )
        return {
            "size": before.st_size,
            "mtime_ns": before.st_mtime_ns,
            "ctime_ns": before.st_ctime_ns,
            "device": before.st_dev,
            "inode": before.st_ino,
            "sha256": digest,
        }
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def outputs_match_metadata(
    nef, args, out_dir, metadata_json, *, return_signature=False,
):
    try:
        stored, by_path = _parse_output_metadata(metadata_json)
    except (TypeError, ValueError):
        return (False, None) if return_signature else False
    expected_outputs_list = expected_outputs(nef, args, out_dir)
    expected = {lexical_path(path): kind for kind, path in expected_outputs_list}
    if len(stored) != len(expected_outputs_list) or set(expected) != set(by_path):
        return (False, None) if return_signature else False
    output_root, root_identity = _output_root_parameters(args=args, out_dir=out_dir)
    signature = []
    for path_text, kind in expected.items():
        entry = by_path[path_text]
        path = Path(path_text)
        try:
            inspected = _prove_durable_output(
                path,
                kind,
                entry,
                output_root=output_root,
                root_identity=root_identity,
                maximum_bytes=output_size_limit(kind, args),
            )
        except Exception:
            return (False, None) if return_signature else False
        signature.append(_inspected_signature_entry(path, kind, inspected))
    result = tuple(signature)
    return (True, result) if return_signature else True


def _cleanup_staged(
    staged, *, args=None, output_root=None, root_identity=None,
):
    for _, tmp_path, final_path in staged:
        try:
            if output_root is None and args is not None:
                output_root, root_identity = _output_root_parameters(
                    args=args, path=tmp_path
                )
            if output_root is None:
                output_root = Path(final_path).parent
                root_identity = _directory_identity(output_root)
            if not _artifact_path_matches(
                tmp_path, Path(final_path).parent, ARTIFACT_STAGE_DIR
            ):
                continue
            identity = _capture_output_identity(
                tmp_path,
                output_root,
                root_identity,
                require_single_link=False,
            )
            _unlink_if_identity_matches(
                tmp_path, identity, output_root, root_identity
            )
        except OSError:
            pass


def _current_output_signature(path, kind, output_root, root_identity):
    descriptor, metadata = _open_output_regular_fd(
        path, output_root, root_identity, require_single_link=True
    )
    os.close(descriptor)
    return (
        lexical_path(path),
        kind,
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _owned_output_signature(path, kind, inspected):
    """Ownership proof retained across a rename (ctime itself may change)."""
    return _inspected_signature_entry(path, kind, inspected) + (
        inspected["sha256"],
    )


def _capture_output_identity(
    path, output_root, root_identity, *, include_hash=False, require_single_link=True,
):
    descriptor, before = _open_output_regular_fd(
        path,
        output_root,
        root_identity,
        require_single_link=require_single_link,
    )
    try:
        digest = None
        if include_hash:
            state = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                state.update(chunk)
            digest = state.hexdigest()
        after = os.fstat(descriptor)
        if _stable_file_identity(before) != _stable_file_identity(after):
            raise UnsafeFileError(
                errno.ESTALE, f"output changed while proving identity: {path}"
            )
        return (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            digest,
        )
    finally:
        os.close(descriptor)


def _owned_proof_matches(identity, expected):
    if identity[:4] != (expected[2], expected[3], expected[4], expected[5]):
        return False
    return len(expected) < 8 or identity[4] == expected[7]


def _unlink_if_identity_matches(path, identity, output_root, root_identity):
    """Quarantine by rename, then refuse to unlink an unexpected inode."""
    try:
        current = _capture_output_identity(
            path,
            output_root,
            root_identity,
            include_hash=identity[4] is not None,
            require_single_link=False,
        )
    except OSError:
        return False
    if current != tuple(identity):
        return False
    path = Path(path)
    if (
        path.parent.name in ARTIFACT_ROLES
        and path.parent.parent.name == ARTIFACT_DIR_NAME
    ):
        artifact_owner = path.parent.parent.parent
    else:
        artifact_owner = path.parent
    quarantine = _artifact_path_for_parent(
        artifact_owner,
        ARTIFACT_TRASH_DIR,
        output_root,
        root_identity,
    )
    moved = False
    try:
        _replace_output(path, quarantine, output_root, root_identity)
    except OSError as error:
        moved = bool(getattr(error, "output_mutation_completed", False))
        if not moved:
            return False
    else:
        moved = True
    if not moved:
        return False
    try:
        quarantined = _capture_output_identity(
            quarantine,
            output_root,
            root_identity,
            include_hash=identity[4] is not None,
            require_single_link=False,
        )
    except OSError:
        return False
    if quarantined != tuple(identity):
        try:
            _restore_backup_if_unchanged(
                quarantine,
                path,
                quarantined,
                output_root,
                root_identity,
            )
        except (OSError, OutputCollisionError):
            pass
        return False
    return _unlink_output(
        quarantine, output_root, root_identity, missing_ok=True
    )


def _restore_backup_if_unchanged(
    backup, final_path, identity, output_root, root_identity,
):
    if _output_exists(final_path, output_root, root_identity):
        return False
    try:
        current = _capture_output_identity(
            backup,
            output_root,
            root_identity,
            include_hash=identity[4] is not None,
        )
    except OSError:
        return False
    if current != identity:
        return False
    _publish_output_no_replace(backup, final_path, output_root, root_identity)
    return True


def _publish_output_no_replace(source, target, output_root, root_identity):
    """Atomically publish a new pathname, failing if anything already owns it."""
    source_parent, source_name = _open_output_parent(
        source, output_root, root_identity
    )
    target_parent = -1
    linked = False
    try:
        target_parent, target_name = _open_output_parent(
            target, output_root, root_identity, create=True
        )
        try:
            os.link(
                source_name,
                target_name,
                src_dir_fd=source_parent,
                dst_dir_fd=target_parent,
                follow_symlinks=False,
            )
        except FileExistsError as error:
            raise OutputCollisionError(
                f"output appeared before atomic publication: {target}"
            ) from error
        linked = True
        os.fsync(target_parent)
        os.unlink(source_name, dir_fd=source_parent)
        os.fsync(source_parent)
    except OSError as error:
        if linked:
            error.output_mutation_completed = True
        raise
    finally:
        if target_parent >= 0:
            os.close(target_parent)
        os.close(source_parent)


def _path_matches_identity(path, identity, output_root, root_identity):
    try:
        current = _capture_output_identity(
            path,
            output_root,
            root_identity,
            include_hash=identity[4] is not None,
            require_single_link=False,
        )
    except OSError:
        return False
    return current == tuple(identity)


def prepare_publication(
    staged,
    *,
    source_key,
    fingerprint,
    config_fingerprint,
    output_root=None,
    root_identity=None,
    allow_overwrite=False,
    owned_outputs=(),
    ownership_lock=None,
):
    """Build and validate a durable plan before any public pathname changes."""
    staged = tuple(staged)
    if not staged:
        raise TransientConversionError("renderer produced no staged outputs")
    if output_root is None:
        output_root = Path(staged[0][2]).parent
        root_identity = _directory_identity(output_root)
    output_root = Path(lexical_path(output_root))
    if ownership_lock is not None:
        ownership_lock.assert_owned()
    owned_by_path = {entry[0]: tuple(entry) for entry in owned_outputs}
    outputs = []
    final_paths = set()
    for kind, tmp_path, final_path in staged:
        tmp_path = Path(tmp_path)
        final_path = Path(final_path)
        _reject_reserved_output_path(final_path, output_root)
        if not _artifact_path_matches(
            tmp_path, final_path.parent, ARTIFACT_STAGE_DIR
        ):
            raise OutputCollisionError(
                "staged output is outside its final parent's authenticated "
                f"transaction namespace: {tmp_path}"
            )
        final_key = reservation_key(final_path)
        if final_key in final_paths:
            raise OutputCollisionError(
                f"duplicate final path in publication plan: {final_path}"
            )
        final_paths.add(final_key)
        _fsync_output_file(tmp_path, output_root, root_identity)
        new_identity = _capture_output_identity(
            tmp_path, output_root, root_identity, include_hash=True
        )
        backup = None
        old_identity = None
        if _output_exists(final_path, output_root, root_identity):
            expected = owned_by_path.get(lexical_path(final_path))
            if not allow_overwrite:
                if expected is None or expected[1] != kind:
                    raise OutputCollisionError(
                        f"refusing to replace unowned output: {final_path}"
                    )
                current = _current_output_signature(
                    final_path, kind, output_root, root_identity
                )
                if current != expected[:7]:
                    raise OutputCollisionError(
                        f"output changed after ownership validation: {final_path}"
                    )
            old_identity = _capture_output_identity(
                final_path, output_root, root_identity, include_hash=True
            )
            if not allow_overwrite and not _owned_proof_matches(
                old_identity, expected
            ):
                raise OutputCollisionError(
                    f"output content changed after ownership validation: {final_path}"
                )
            backup = _artifact_path_for_final(
                final_path,
                ARTIFACT_BACKUP_DIR,
                output_root,
                root_identity,
            )
        outputs.append(
            {
                "kind": kind,
                "staged_path": lexical_path(tmp_path),
                "final_path": lexical_path(final_path),
                "new_identity": list(new_identity),
                "backup_path": lexical_path(backup) if backup else None,
                "old_identity": list(old_identity) if old_identity else None,
            }
        )
    return PublicationPlan(
        uuid.uuid4().hex,
        _stored_source_key(source_key),
        str(fingerprint),
        str(config_fingerprint),
        output_root,
        tuple(outputs),
    )


def rollback_publication(plan, output_root, root_identity):
    """Idempotently restore the pre-plan namespace without touching foreign data."""
    complete = True
    for entry in reversed(plan.outputs):
        final = Path(entry["final_path"])
        staged = Path(entry["staged_path"])
        new_identity = tuple(entry["new_identity"])
        backup = Path(entry["backup_path"]) if entry["backup_path"] else None
        old_identity = (
            tuple(entry["old_identity"]) if entry["old_identity"] else None
        )
        moved_identity = (
            tuple(entry["_moved_identity"])
            if entry.get("_moved_identity")
            else None
        )
        restored_foreign = bool(
            moved_identity is not None and moved_identity != old_identity
        )

        if _output_exists(final, output_root, root_identity):
            if _path_matches_identity(
                final, new_identity, output_root, root_identity
            ):
                if not _unlink_if_identity_matches(
                    final, new_identity, output_root, root_identity
                ):
                    complete = False
            elif restored_foreign and _path_matches_identity(
                final, moved_identity, output_root, root_identity
            ):
                pass
            elif old_identity is None or not _path_matches_identity(
                final, old_identity, output_root, root_identity
            ):
                complete = False

        if backup is not None and _output_exists(
            backup, output_root, root_identity
        ):
            backup_identity = moved_identity or old_identity
            if not _path_matches_identity(
                backup, backup_identity, output_root, root_identity
            ):
                complete = False
            elif _output_exists(final, output_root, root_identity):
                complete = False
            else:
                try:
                    _publish_output_no_replace(
                        backup, final, output_root, root_identity
                    )
                except (OSError, OutputCollisionError):
                    complete = False
        elif old_identity is not None:
            restored = restored_foreign and _path_matches_identity(
                final, moved_identity, output_root, root_identity
            )
            if not restored and not _path_matches_identity(
                final, old_identity, output_root, root_identity
            ):
                complete = False

        if _output_exists(staged, output_root, root_identity):
            if _path_matches_identity(
                staged, new_identity, output_root, root_identity
            ):
                if not _unlink_if_identity_matches(
                    staged, new_identity, output_root, root_identity
                ):
                    complete = False
            else:
                complete = False
    return complete


def execute_publication(plan, output_root, root_identity, *, ownership_lock=None):
    """Execute a previously journaled plan, retaining backups until DB commit."""
    try:
        if ownership_lock is not None:
            ownership_lock.assert_owned()
        for entry in plan.outputs:
            backup_text = entry["backup_path"]
            if backup_text is None:
                continue
            final = Path(entry["final_path"])
            backup = Path(backup_text)
            _replace_output(final, backup, output_root, root_identity)
            moved_identity = _capture_output_identity(
                backup,
                output_root,
                root_identity,
                include_hash=True,
                require_single_link=False,
            )
            entry["_moved_identity"] = list(moved_identity)
            if moved_identity != tuple(entry["old_identity"]):
                restored = _restore_backup_if_unchanged(
                    backup,
                    final,
                    moved_identity,
                    output_root,
                    root_identity,
                )
                if not restored:
                    raise OutputCollisionError(
                        "output changed during atomic ownership handoff and the "
                        f"displaced inode could not be restored: {final}"
                    )
                raise OutputCollisionError(
                    f"output changed during atomic ownership handoff: {final}"
                )
        if ownership_lock is not None:
            ownership_lock.assert_owned()
        for entry in plan.outputs:
            staged = Path(entry["staged_path"])
            final = Path(entry["final_path"])
            if not _path_matches_identity(
                staged, tuple(entry["new_identity"]), output_root, root_identity
            ):
                raise OutputCollisionError(
                    f"staged output changed before publication: {staged}"
                )
            _publish_output_no_replace(
                staged, final, output_root, root_identity
            )
    except (OSError, OutputCollisionError) as error:
        rollback_publication(plan, output_root, root_identity)
        if isinstance(error, OutputCollisionError):
            raise
        raise TransientConversionError(
            f"could not publish output transaction: {error}"
        ) from error


def finalize_publication(plan, output_root, root_identity):
    """Remove exact backups only after durable success is visible in SQLite."""
    for entry in plan.outputs:
        if not _path_matches_identity(
            Path(entry["final_path"]),
            tuple(entry["new_identity"]),
            output_root,
            root_identity,
        ):
            return False
    complete = True
    for entry in plan.outputs:
        backup_text = entry["backup_path"]
        if backup_text is not None:
            backup = Path(backup_text)
            if _output_exists(backup, output_root, root_identity):
                if not _unlink_if_identity_matches(
                    backup,
                    tuple(entry["old_identity"]),
                    output_root,
                    root_identity,
                ):
                    complete = False
        staged = Path(entry["staged_path"])
        if _output_exists(staged, output_root, root_identity):
            if not _unlink_if_identity_matches(
                staged,
                tuple(entry["new_identity"]),
                output_root,
                root_identity,
            ):
                complete = False
    return complete


def _validated_publication_plan(row, output_root):
    plan = PublicationPlan.from_json(row["plan_json"])
    root = Path(lexical_path(output_root))
    if (
        plan.transaction_id != row["transaction_id"]
        or plan.source_key != row["source_path"]
        or plan.fingerprint != row["fingerprint"]
        or plan.config_fingerprint != row["config_fingerprint"]
        or not re.fullmatch(r"[0-9a-f]{32}", plan.transaction_id)
        or os.fspath(plan.output_root) != lexical_path(plan.output_root)
        or lexical_path(plan.output_root) != lexical_path(root)
    ):
        raise PublicationRecoveryError(
            "publication journal columns do not match its signed plan fields"
        )
    seen = set()
    seen_kinds = set()
    for entry in plan.outputs:
        kind = entry["kind"]
        final = Path(entry["final_path"])
        staged = Path(entry["staged_path"])
        backup = Path(entry["backup_path"]) if entry["backup_path"] else None
        try:
            _reject_reserved_output_path(final, root)
        except (OutputCollisionError, UnsafeFileError) as error:
            raise PublicationRecoveryError(
                "publication journal maps a final into the reserved artifact namespace"
            ) from error
        if (
            entry["final_path"] != lexical_path(final)
            or entry["staged_path"] != lexical_path(staged)
            or final.suffix.lower() != EXTS[kind]
            or not _artifact_path_matches(
                staged, final.parent, ARTIFACT_STAGE_DIR
            )
            or kind in seen_kinds
        ):
            raise PublicationRecoveryError(
                "publication journal contains a non-canonical output path"
            )
        seen_kinds.add(kind)
        for candidate in (final, staged, backup):
            if candidate is None:
                continue
            try:
                candidate.relative_to(root)
            except ValueError as error:
                raise PublicationRecoveryError(
                    "publication journal path escapes the configured output root"
                ) from error
            key = reservation_key(candidate)
            if key in seen:
                raise PublicationRecoveryError(
                    "publication journal aliases two transaction paths"
                )
            seen.add(key)
        if backup is not None and (
            entry["backup_path"] != lexical_path(backup)
            or not _artifact_path_matches(
                backup, final.parent, ARTIFACT_BACKUP_DIR
            )
            or entry["old_identity"] is None
        ):
            raise PublicationRecoveryError(
                "publication journal contains an invalid rollback path"
            )
        if backup is None and entry["old_identity"] is not None:
            raise PublicationRecoveryError(
                "publication journal has old identity without a rollback path"
            )
    return plan


def _committed_publication_matches_state(state, plan, output_root, root_identity):
    source = state.get_source(plan.source_key)
    if (
        source is None
        or source["status"] != "success"
        or source["fingerprint"] != plan.fingerprint
        or source["config_fingerprint"] != plan.config_fingerprint
    ):
        return False
    try:
        metadata, by_path = _parse_output_metadata(source["output_metadata"])
    except (TypeError, ValueError):
        return False
    if len(metadata) != len(plan.outputs):
        return False
    for entry in plan.outputs:
        identity = tuple(entry["new_identity"])
        durable = by_path.get(entry["final_path"])
        if (
            durable is None
            or durable.get("kind") != entry["kind"]
            or durable.get("size") != identity[2]
            or durable.get("mtime_ns") != identity[3]
            or durable.get("sha256") != identity[4]
            or not _path_matches_identity(
                Path(entry["final_path"]),
                identity,
                output_root,
                root_identity,
            )
        ):
            return False
    return True


def recover_publication_transactions(state, output_root):
    """Reconcile journaled namespace mutations before generic artifact cleanup."""
    rows = state.publication_rows()
    if len(rows) > MAX_PENDING_PUBLICATIONS:
        raise PublicationRecoveryError(
            "durable state exceeds the publication recovery transaction ceiling"
        )
    output_root = Path(lexical_path(output_root))
    root_identity = _directory_identity(output_root)
    recovered = 0
    for row in rows:
        plan = _validated_publication_plan(row, output_root)
        if row["phase"] == "prepared":
            if not rollback_publication(plan, output_root, root_identity):
                raise PublicationRecoveryError(
                    f"prepared publication {plan.transaction_id} is ambiguous; "
                    "foreign files and rollback copies were preserved"
                )
        elif row["phase"] == "committed":
            if not _committed_publication_matches_state(
                state, plan, output_root, root_identity
            ):
                raise PublicationRecoveryError(
                    f"committed publication {plan.transaction_id} no longer "
                    "matches durable success state; all artifacts were preserved"
                )
            if not finalize_publication(
                plan, output_root, root_identity
            ):
                raise PublicationRecoveryError(
                    f"committed publication {plan.transaction_id} could not be "
                    "cleaned without touching an ambiguous file"
                )
        else:
            raise PublicationRecoveryError(
                f"publication {plan.transaction_id} has invalid phase {row['phase']!r}"
            )
        state.delete_publication(plan.transaction_id)
        recovered += 1
    return recovered


def commit_staged_outputs(
    staged,
    *,
    output_root=None,
    root_identity=None,
    allow_overwrite=False,
    owned_outputs=(),
    ownership_lock=None,
):
    """Compatibility path for callers that do not own a durable StateStore."""
    if output_root is None and staged:
        output_root = Path(staged[0][2]).parent
        root_identity = _directory_identity(output_root)
    plan = prepare_publication(
        staged,
        source_key="direct-publication",
        fingerprint="direct-publication",
        config_fingerprint="direct-publication",
        output_root=output_root,
        root_identity=root_identity,
        allow_overwrite=allow_overwrite,
        owned_outputs=owned_outputs,
        ownership_lock=ownership_lock,
    )
    execute_publication(
        plan,
        output_root,
        root_identity,
        ownership_lock=ownership_lock,
    )
    finalize_publication(plan, output_root, root_identity)
    return plan


def render_config_fingerprint(args, icc, out_dir):
    renderer = None
    if needs_raster(args):
        renderer_path = Path(args.render_bin)
        renderer = {
            "path": canonical_path(renderer_path),
            "sha256": file_sha256(renderer_path),
        }
    dng = None
    if "dng" in args.formats:
        dng_path = Path(args.dng_bin)
        dng = {
            "path": canonical_path(dng_path),
            "sha256": file_sha256(dng_path),
        }
    runtime_identity = os.environ.get("NEF_WATCH_RUNTIME_IDENTITY")
    runtime_dir = os.environ.get("NIKON_RUNTIME_DIR")
    if runtime_identity is None and runtime_dir:
        resolved_runtime = Path(runtime_dir).resolve(strict=False)
        adapter = resolved_runtime / "nef_render.exe"
        runtime_identity = {
            "path": str(resolved_runtime),
            "adapter_sha256": file_sha256(adapter) if adapter.is_file() else None,
        }
    exiftool = None
    if getattr(args, "exiftool", None):
        exif_path = Path(args.exiftool)
        exiftool = {
            "path": canonical_path(exif_path),
            "sha256": file_sha256(exif_path),
        }
    encoders = {
        "pillow": getattr(Image, "__version__", None),
        "numpy": np.__version__,
    }
    if args.bits == 16 or "dng" in args.formats:
        try:
            import tifffile
            encoders["tifffile"] = getattr(tifffile, "__version__", None)
        except ImportError:
            encoders["tifffile"] = None
    payload = {
        "schema": CONFIG_SCHEMA,
        "formats": sorted(args.formats),
        "bits": args.bits,
        "quality": args.quality,
        "exp_comp": args.exp_comp,
        "deterministic": bool(args.deterministic),
        "dng_engine": args.dng_engine,
        "dng_embed_original": bool(args.dng_embed_original),
        "profile_sha256": hashlib.sha256(icc).hexdigest() if icc else None,
        "renderer": renderer,
        "runtime_identity": runtime_identity,
        "exiftool": exiftool,
        "encoders": encoders,
        "dng": dng,
        "recursive": bool(args.recursive),
        "input_root": canonical_path(args.input_root),
        "output_root": canonical_path(out_dir),
    }
    return hashlib.sha256(_json_dumps(payload).encode()).hexdigest()


def expected_outputs(nef, args, out_dir):
    parent = out_dir
    if args.recursive and not args.input_is_file:
        try:
            rel = nef.relative_to(args.input_root)
            parent = out_dir / rel.parent
        except ValueError:
            parent = out_dir
    outputs = [
        (kind, parent / (nef.stem + EXTS[kind]))
        for kind in FORMAT_ORDER
        if kind in args.formats
    ]
    for _, output in outputs:
        _reject_reserved_output_path(output, out_dir)
    return outputs


def fingerprint_source(path, args, *, validate_magic=True, enforce_size=True):
    max_bytes = None
    max_mib = None
    if enforce_size:
        max_bytes = getattr(args, "max_input_bytes", None)
        max_mib = getattr(args, "max_input_mib", None)
    return fingerprint_file(
        path,
        validate_magic=validate_magic,
        max_bytes=max_bytes,
        max_mib=max_mib,
        root=getattr(args, "input_root", None),
        root_identity=getattr(args, "input_root_identity", None),
    )


def fingerprint_source_cached(cache, canonical, stat_identity, path, args):
    cached = cache.get(canonical)
    if cached and cached[0] == stat_identity:
        return cached[1]
    try:
        fingerprint, fingerprint_identity = fingerprint_source(path, args)
    except ConversionError as error:
        failure_fingerprint = getattr(error, "fingerprint", None)
        if failure_fingerprint is not None:
            cache[canonical] = (stat_identity, failure_fingerprint)
        raise
    if fingerprint_identity != stat_identity:
        raise SourceChangedError(f"{Path(path).name} changed after its quiet period")
    cache[canonical] = (stat_identity, fingerprint)
    return fingerprint


def convert_one(
    nef, out_dir, args, icc, force=False, *, source_path=None,
    expected_fingerprint=None, owned_outputs=(), source_key=None,
    defer_publication=False,
):
    """Produce the requested output(s) for one NEF. Returns (status, detail) with
    status in {ok,skip,error}. Safe to run concurrently."""
    source_path = Path(source_path) if source_path is not None else Path(nef)
    if getattr(args, "output_root", None) is None:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
    output_root, output_root_identity = _output_root_parameters(
        args=args, out_dir=out_dir
    )
    wanted = expected_outputs(source_path, args, out_dir)
    owned_by_path = {entry[0]: tuple(entry) for entry in owned_outputs}
    todo = []
    for kind, path in wanted:
        if force or args.overwrite:
            todo.append((kind, path))
            continue
        if not _output_exists(path, output_root, output_root_identity):
            todo.append((kind, path))
            continue
        expected = owned_by_path.get(lexical_path(path))
        if expected is None or expected[1] != kind:
            return "error", FailureDetail(
                f"output collision: unowned {kind} appeared before worker start: {path}",
                permanent=True,
                code="output-collision",
            )
        try:
            current = _current_output_signature(
                path, kind, output_root, output_root_identity
            )
            identity = _capture_output_identity(
                path,
                output_root,
                output_root_identity,
                include_hash=True,
            )
        except OSError as error:
            return "error", FailureDetail(
                f"output collision: cannot prove worker-time ownership: {error}",
                permanent=True,
                code="output-collision",
            )
        if current != expected[:7] or not _owned_proof_matches(identity, expected):
            return "error", FailureDetail(
                f"output collision: owned {kind} changed before worker start: {path}",
                permanent=True,
                code="output-collision",
            )
    if not todo:
        return "skip", StagedConversionDetail(
            "verified worker-time ownership", owned_outputs=owned_outputs
        )
    t0 = time.time()
    made = []
    staged = []
    try:
        initial_fingerprint, _ = fingerprint_file(
            nef,
            max_bytes=getattr(args, "max_input_bytes", None),
            max_mib=getattr(args, "max_input_mib", None),
        )
        if expected_fingerprint is not None and initial_fingerprint != expected_fingerprint:
            raise SourceChangedError(
                f"{source_path.name} snapshot changed before conversion"
            )
        raster_todo = [(kind, p) for kind, p in todo if kind in RASTER_FORMATS]
        if raster_todo:
            staged.extend(render_raster(nef, raster_todo, args, icc) or [])
            made.extend(p.suffix for _, p in raster_todo)
        for kind, outp in todo:
            if kind == "dng":
                staged_dng = render_dng(nef, outp, args)
                if isinstance(staged_dng, tuple) and len(staged_dng) == 3:
                    staged.append(staged_dng)
                made.append(outp.suffix)
        for kind, tmp_path, _ in staged:
            if not validate_output_file(
                tmp_path,
                kind,
                output_root=output_root,
                root_identity=output_root_identity,
                maximum_bytes=output_size_limit(kind, args),
            ):
                raise TransientConversionError(
                    f"renderer produced an invalid or incomplete {kind}"
                )
        if expected_fingerprint is not None:
            try:
                snapshot_fingerprint, _ = fingerprint_file(
                    nef,
                    max_bytes=getattr(args, "max_input_bytes", None),
                    max_mib=getattr(args, "max_input_mib", None),
                )
            except ConversionError as error:
                raise SourceChangedError(
                    f"{source_path.name} snapshot became unsafe during conversion"
                ) from error
            if snapshot_fingerprint != expected_fingerprint:
                raise SourceChangedError(
                    f"{source_path.name} snapshot changed during conversion; "
                    "staged outputs discarded"
                )
            try:
                current_fingerprint, _ = fingerprint_source(source_path, args)
            except ConversionError as error:
                raise SourceChangedError(
                    f"{source_path.name} changed during conversion"
                ) from error
            if current_fingerprint != expected_fingerprint:
                raise SourceChangedError(
                    f"{source_path.name} changed during conversion; staged outputs discarded"
                )
        if len(staged) != len(todo):
            raise TransientConversionError(
                f"renderer staged {len(staged)} of {len(todo)} requested outputs"
            )
        if defer_publication:
            return "staged", StagedConversionDetail(
                f"{'+'.join(made)}  ({time.time()-t0:.1f}s)",
                staged=staged,
                owned_outputs=owned_outputs,
            )
        commit_staged_outputs(
            staged,
            output_root=output_root,
            root_identity=output_root_identity,
            allow_overwrite=bool(args.overwrite),
            owned_outputs=owned_outputs,
            ownership_lock=getattr(args, "output_lock", None),
        )
    except PermanentConversionError as e:
        _cleanup_staged(
            staged, output_root=output_root, root_identity=output_root_identity
        )
        return "error", FailureDetail(str(e), permanent=True, code=e.code)
    except (ConversionError, RuntimeError, ValueError, OSError) as e:
        _cleanup_staged(
            staged, output_root=output_root, root_identity=output_root_identity
        )
        return "error", FailureDetail(
            str(e), permanent=getattr(e, "permanent", False),
            code=getattr(e, "code", "conversion"),
        )
    return "ok", f"{'+'.join(made)}  ({time.time()-t0:.1f}s)"


def _warn_unsafe_raw(path, reason):
    try:
        key = ("raw-bytes", os.fsencode(path))
    except (TypeError, ValueError):
        key = ("display", repr(path))
    if key in UNSAFE_PATH_WARNED:
        UNSAFE_PATH_WARNED.move_to_end(key)
        return
    log(
        "warning: rejecting unsafe raw input "
        f"({reason}): {_safe_path_display(path)}"
    )
    UNSAFE_PATH_WARNED[key] = None
    while len(UNSAFE_PATH_WARNED) > MAX_UNSAFE_WARNING_CACHE:
        UNSAFE_PATH_WARNED.popitem(last=False)


def find_nefs(
    path,
    recursive,
    root_identity=None,
    max_entries=DEFAULT_MAX_SCAN_ENTRIES,
):
    max_entries = parse_max_scan_entries(str(max_entries))
    if path.is_file():
        if not _path_has_strict_utf8_name(path):
            _warn_unsafe_raw(path, "filename is not valid UTF-8")
            return []
        if path.is_symlink():
            raise InvalidRawError(f"symbolic-link input is not allowed: {path}")
        try:
            metadata = path.lstat()
        except OSError:
            return []
        if (
            is_raw_file(path)
            and stat_module.S_ISREG(metadata.st_mode)
            and metadata.st_nlink == 1
        ):
            return [path]
        if is_raw_file(path) and metadata.st_nlink != 1:
            _warn_unsafe_raw(path, "multiple hard links")
        return []
    folder = path
    current_root_identity = _directory_identity(folder)
    if root_identity is not None and current_root_identity != tuple(root_identity):
        raise OSError(errno.ESTALE, f"watched input root identity changed: {folder}")
    root = folder.resolve(strict=True)
    found = []
    directories = [folder]
    scanned = 0
    while directories:
        current = directories.pop()
        if current != folder and (
            path_has_symlink_component(current, folder)
            or not path_is_within(current, root)
        ):
            continue
        try:
            entries = os.scandir(current)
        except OSError as error:
            raise IncompleteScanError(
                error.errno or errno.EIO,
                f"input scan incomplete at {current}: {error}",
            ) from error
        with entries:
            for entry in entries:
                scanned += 1
                if scanned > max_entries:
                    raise ScanLimitError(
                        f"scan stopped after {max_entries} filesystem entries; "
                        "raise --max-scan-entries only after checking the input tree"
                    )
                if STOP_EVENT.is_set():
                    return sorted(found)
                candidate = Path(entry.path)
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError as error:
                    raise IncompleteScanError(
                        error.errno or errno.EIO,
                        f"input scan incomplete at {candidate}: {error}",
                    ) from error
                if stat_module.S_ISDIR(metadata.st_mode):
                    if recursive and not entry.is_symlink():
                        directories.append(candidate)
                    continue
                if not is_raw_file(candidate):
                    continue
                if not _path_has_strict_utf8_name(candidate):
                    _warn_unsafe_raw(candidate, "filename is not valid UTF-8")
                    continue
                if (
                    entry.is_symlink()
                    or path_has_symlink_component(candidate, folder)
                    or not path_is_within(candidate, root)
                ):
                    _warn_unsafe_raw(candidate, "symlink or root escape")
                    continue
                if not stat_module.S_ISREG(metadata.st_mode):
                    _warn_unsafe_raw(candidate, "not a regular file")
                    continue
                if metadata.st_nlink != 1:
                    _warn_unsafe_raw(candidate, "multiple hard links")
                    continue
                found.append(candidate)
        if not recursive:
            break
    return sorted(found)


def _submit(
    ex, nef, args, out_dir, icc, force=False, *, source_path=None,
    expected_fingerprint=None, owned_outputs=(), source_key=None,
):
    return ex.submit(
        convert_one, nef, out_dir, args, icc, force,
        source_path=source_path,
        expected_fingerprint=expected_fingerprint,
        owned_outputs=owned_outputs,
        source_key=source_key,
        defer_publication=True,
    )


def cancel_queued_jobs(jobs, active_sources=None):
    """Cancel executor work that has not started and discard only its snapshot."""
    cancelled = 0
    for future, job in list(jobs.items()):
        if not future.cancel():
            continue
        jobs.pop(future, None)
        if active_sources is not None:
            active_sources.discard(job.canonical_source)
        remove_snapshot(job.snapshot)
        cancelled += 1
    return cancelled


def stat_key(path):
    st = path.lstat()
    if not stat_module.S_ISREG(st.st_mode):
        raise UnsafeFileError(errno.EINVAL, f"not a regular source file: {path}")
    if st.st_nlink != 1:
        raise UnsafeFileError(errno.EMLINK, f"source has multiple hard links: {path}")
    return _stat_key_from_stat(st)


def outputs_complete(nef, args, out_dir):
    output_root, root_identity = _output_root_parameters(args=args, out_dir=out_dir)
    return all(
        validate_output_file(
            path, kind, output_root=output_root, root_identity=root_identity
        )
        for kind, path in expected_outputs(nef, args, out_dir)
    )


def processed_source_state(processed_key, current_key, complete):
    """Classify an already-seen source without conflating missing outputs with edits."""
    if processed_key is None:
        return "new"
    if processed_key != current_key:
        return "source-changed"
    return "complete" if complete else "output-missing"


def output_stat_signature(nef, args, out_dir):
    output_root, root_identity = _output_root_parameters(args=args, out_dir=out_dir)
    signature = []
    for kind, path in expected_outputs(nef, args, out_dir):
        try:
            descriptor, st = _open_output_regular_fd(
                path, output_root, root_identity, require_single_link=True
            )
            os.close(descriptor)
            signature.append(
                (
                    lexical_path(path), kind, st.st_dev, st.st_ino,
                    st.st_size, st.st_mtime_ns, st.st_ctime_ns,
                )
            )
        except OSError:
            signature.append((lexical_path(path), kind, None, None, None, None, None))
    return tuple(signature)


def _source_output_mapping_failure(error):
    return FailureDetail(
        "source path maps into nef-watch's reserved output namespace; rename "
        f"or move this source: {error}",
        permanent=True,
        code="source-output-mapping",
    )


def classify_output_source_mappings(nefs, args, out_dir):
    """Return duplicate and individually unsafe mappings for a complete scan."""
    owners = {}
    failures = {}
    for nef in nefs:
        source_key = source_state_key(nef, args.input_root)
        try:
            outputs = expected_outputs(nef, args, out_dir)
        except OutputCollisionError as error:
            failures[source_key] = _source_output_mapping_failure(error)
            continue
        for _, output in outputs:
            owners.setdefault(reservation_key(output), set()).add(source_key)
    conflicts = {
        source
        for sources in owners.values()
        if len(sources) > 1
        for source in sources
    }
    return conflicts, failures


def duplicate_output_source_keys(nefs, args, out_dir):
    """Return sources whose requested paths collide within this complete scan."""
    conflicts, _ = classify_output_source_mappings(nefs, args, out_dir)
    return conflicts


def collision_obstruction_signature(state, nef, args, out_dir):
    wanted = expected_outputs(nef, args, out_dir)
    return (
        output_stat_signature(nef, args, out_dir),
        state.output_reservation_signature(wanted),
    )


def _durably_owned_existing_outputs(
    row, wanted, args, output_root, root_identity
):
    """Return exact validating signatures, or a collision explanation."""
    try:
        _, by_path = _parse_output_metadata(row["output_metadata"])
    except (TypeError, ValueError):
        return (), "durable output metadata is unreadable"

    owned = []
    for kind, path in wanted:
        if not _output_exists(path, output_root, root_identity):
            continue
        path_text = lexical_path(path)
        entry = by_path.get(path_text)
        if entry is None or entry.get("kind") != kind:
            return (), f"pre-existing {kind} is not owned by durable state: {path}"
        try:
            inspected = _prove_durable_output(
                path,
                kind,
                entry,
                output_root=output_root,
                root_identity=root_identity,
                maximum_bytes=output_size_limit(kind, args),
            )
        except Exception as error:
            return (), f"pre-existing {kind} cannot be proven owned: {path}: {error}"
        owned.append(_owned_output_signature(path, kind, inspected))
    return tuple(owned), None


def source_action(
    state, nef, fingerprint, config_fingerprint, args, out_dir,
    *, force_overwrite=False, source_key=None, source_conflict=None,
):
    """Return (action, force, detail): convert/skip/wait/quarantine."""
    database_source = source_key or nef
    failure = state.get_failure(database_source)
    if source_conflict is True:
        collision = FailureDetail(
            "multiple source files map to the same requested output; rename "
            "or remove one source to re-arm conversion",
            permanent=True,
            code="source-output-collision",
        )
        collision.persist_failure = not bool(
            failure
            and failure["fingerprint"] == fingerprint
            and failure["config_fingerprint"] == config_fingerprint
            and failure["permanent"]
            and failure["error_code"] == "source-output-collision"
            and failure["error_text"] == str(collision)
        )
        return "quarantine", False, collision
    active_collision_failure = None
    if failure:
        same_failure = (
            failure["fingerprint"] == fingerprint
            and failure["config_fingerprint"] == config_fingerprint
        )
        if not same_failure:
            state.clear_failure(database_source)
        elif failure["permanent"]:
            if failure["error_code"] == "source-output-collision":
                if source_conflict is False:
                    state.clear_failure(database_source)
                else:
                    return "quarantine", False, failure["error_text"]
            elif failure["error_code"] == "output-collision":
                # Re-evaluate the obstruction read-only, but retain the row
                # unless its condition changes. This avoids a DELETE+INSERT and
                # fsync cycle for every unchanged watched-directory scan.
                active_collision_failure = failure
            else:
                return "quarantine", False, failure["error_text"]
        elif failure["next_retry_at"] and time.time() < failure["next_retry_at"]:
            return "wait", False, failure["error_text"]

    row = state.get_source(database_source)
    if row and row["status"] == "baseline" and row["fingerprint"] == fingerprint:
        return "skip", False, "one-time baseline"
    if (
        row
        and row["status"] == "success"
        and row["fingerprint"] == fingerprint
        and row["config_fingerprint"] == config_fingerprint
        and not force_overwrite
    ):
        matched, signature = outputs_match_metadata(
            nef,
            args,
            out_dir,
            row["output_metadata"],
            return_signature=True,
        )
        if matched:
            return "skip", False, SkipDetail(
                "verified durable state", output_signature=signature
            )

    wanted = expected_outputs(nef, args, out_dir)
    output_root, root_identity = _output_root_parameters(args=args, out_dir=out_dir)
    if force_overwrite:
        if active_collision_failure is not None:
            state.clear_failure(database_source)
        return "convert", True, ActionDetail("explicit overwrite")

    reservation_collision = state.output_reservation_collision(
        database_source, wanted
    )
    if reservation_collision:
        collision_detail = FailureDetail(
            reservation_collision,
            permanent=True,
            code="output-collision",
        )
        collision_detail.persist_failure = not bool(
            active_collision_failure
            and active_collision_failure["error_text"] == str(collision_detail)
        )
        return "quarantine", False, collision_detail

    occupied = [
        (kind, path)
        for kind, path in wanted
        if _output_exists(path, output_root, root_identity)
    ]
    owned = ()
    if occupied:
        if not row or row["status"] != "success":
            collision = FailureDetail(
                "output collision: a requested output already exists but is not "
                "owned by durable success state; use --overwrite only after review",
                permanent=True,
                code="output-collision",
            )
            collision.persist_failure = not bool(
                active_collision_failure
                and active_collision_failure["error_text"] == str(collision)
            )
            return (
                "quarantine",
                False,
                collision,
            )
        owned, collision = _durably_owned_existing_outputs(
            row, wanted, args, output_root, root_identity
        )
        if collision:
            collision_detail = FailureDetail(
                f"output collision: {collision}",
                permanent=True,
                code="output-collision",
            )
            collision_detail.persist_failure = not bool(
                active_collision_failure
                and active_collision_failure["error_text"] == str(collision_detail)
            )
            return "quarantine", False, collision_detail

    same_version = bool(
        row
        and row["status"] == "success"
        and row["fingerprint"] == fingerprint
        and row["config_fingerprint"] == config_fingerprint
    )
    force = bool(owned and not same_version)
    if active_collision_failure is not None:
        state.clear_failure(database_source)
    return "convert", force, ActionDetail("durable repair", owned_outputs=owned)


def _captured_outputs_match_proofs(
    outputs, metadata, signature, *, plan=None, owned_outputs=()
):
    metadata_by_path = {entry["path"]: entry for entry in metadata}
    signature_by_path = {entry[0]: entry for entry in signature}
    proofs = {}
    if plan is not None:
        for entry in plan.outputs:
            proofs[entry["final_path"]] = (
                entry["kind"], tuple(entry["new_identity"]), "new"
            )
    for owned in owned_outputs:
        proofs.setdefault(owned[0], (owned[1], tuple(owned), "owned"))
    for kind, path in outputs:
        path_text = lexical_path(path)
        proof = proofs.get(path_text)
        current_signature = signature_by_path.get(path_text)
        current_metadata = metadata_by_path.get(path_text)
        if proof is None or current_signature is None or current_metadata is None:
            return False
        expected_kind, identity, proof_kind = proof
        if expected_kind != kind or current_metadata.get("kind") != kind:
            return False
        if proof_kind == "new":
            if (
                current_signature[2:6] != identity[:4]
                or current_metadata.get("sha256") != identity[4]
            ):
                return False
        elif (
            current_signature != identity[:7]
            or current_metadata.get("sha256") != identity[7]
        ):
            return False
    return True


def _publication_recovery_detail(message):
    _request_fatal_stop()
    detail = FailureDetail(
        message,
        permanent=True,
        code="publication-recovery",
    )
    detail.attempts = 1
    detail.dead = True
    detail.next_retry_at = None
    return detail


def _rollback_and_clear_publication(state, plan, output_root, root_identity):
    """Return true only when both namespace rollback and journal delete commit."""
    try:
        rolled_back = rollback_publication(
            plan, output_root, root_identity
        )
    except (OSError, ConversionError):
        rolled_back = False
    if not rolled_back:
        return False
    try:
        state.delete_publication(plan.transaction_id)
    except sqlite3.Error:
        return False
    return True


def _record_conversion_result(state, job, status, detail, args, out_dir):
    """Publish and persist one result through the durable transaction protocol."""
    plan = None
    output_root, root_identity = _output_root_parameters(
        args=args, out_dir=out_dir
    )
    if status == "staged":
        staged = tuple(getattr(detail, "staged", ()))
        try:
            current_fingerprint, _ = fingerprint_source(job.source, args)
            if current_fingerprint != job.fingerprint:
                raise SourceChangedError(
                    f"{job.source.name} changed before durable publication"
                )
            plan = prepare_publication(
                staged,
                source_key=job.source_key or job.canonical_source,
                fingerprint=job.fingerprint,
                config_fingerprint=job.config_fingerprint,
                output_root=output_root,
                root_identity=root_identity,
                allow_overwrite=bool(args.overwrite),
                owned_outputs=job.owned_outputs,
                ownership_lock=getattr(args, "output_lock", None),
            )
        except (ConversionError, OSError) as error:
            _cleanup_staged(
                staged, output_root=output_root, root_identity=root_identity
            )
            status = "error"
            detail = FailureDetail(
                str(error),
                permanent=getattr(error, "permanent", False),
                code=getattr(error, "code", "publication"),
            )
        else:
            try:
                state.begin_publication(plan)
            except (StateCapacityError, sqlite3.Error) as error:
                # A failed SQLite commit has an uncertain outcome until the row
                # can be read back.  Never discard a staged file referenced by
                # a possibly durable prepared transaction.
                try:
                    journal_row = state.get_publication(plan.transaction_id)
                except sqlite3.Error:
                    journal_row = "unknown"
                if journal_row == "unknown" or journal_row is not None:
                    return "error", _publication_recovery_detail(
                        "durable publication prepare had an uncertain SQLite "
                        f"outcome; restart recovery is required: {error}"
                    )
                _cleanup_staged(
                    staged,
                    output_root=output_root,
                    root_identity=root_identity,
                )
                status = "error"
                detail = FailureDetail(
                    str(error), permanent=True, code="state-capacity"
                )
            else:
                try:
                    execute_publication(
                        plan,
                        output_root,
                        root_identity,
                        ownership_lock=getattr(args, "output_lock", None),
                    )
                except (ConversionError, OSError) as error:
                    if not _rollback_and_clear_publication(
                        state, plan, output_root, root_identity
                    ):
                        return "error", _publication_recovery_detail(
                            "publication failed and exact rollback could not be "
                            f"proven; artifacts were preserved: {error}"
                        )
                    status = "error"
                    detail = FailureDetail(
                        str(error),
                        permanent=getattr(error, "permanent", False),
                        code=getattr(error, "code", "publication"),
                    )
                else:
                    status = "ok"

    if status in ("ok", "skip"):
        try:
            wanted = expected_outputs(job.source, args, out_dir)
            metadata, signature = capture_output_metadata(
                wanted,
                output_root=output_root,
                root_identity=root_identity,
                return_signature=True,
                maximum_bytes_by_kind={
                    kind: output_size_limit(kind, args)
                    for kind in args.formats
                },
            )
            if not _captured_outputs_match_proofs(
                wanted,
                metadata,
                signature,
                plan=plan,
                owned_outputs=job.owned_outputs,
            ):
                raise OutputCollisionError(
                    "an output changed between scheduling, publication, and durable commit"
                )
            if plan is None:
                state.record_success(
                    job.source_key or job.canonical_source,
                    job.fingerprint,
                    job.config_fingerprint,
                    metadata,
                )
        except (
            ConversionError,
            OSError,
            StateCapacityError,
            sqlite3.Error,
        ) as error:
            if plan is not None:
                if not _rollback_and_clear_publication(
                    state, plan, output_root, root_identity
                ):
                    return "error", _publication_recovery_detail(
                        "output validation failed after publication and exact "
                        f"rollback could not be proven: {error}"
                    )
            status = "error"
            detail = FailureDetail(
                str(error),
                permanent=getattr(error, "permanent", False),
                code=getattr(error, "code", "output-validation"),
            )
        else:
            if plan is not None:
                try:
                    state.record_success_and_commit_publication(plan, metadata)
                except (
                    StateCapacityError,
                    PublicationRecoveryError,
                    sqlite3.Error,
                ) as error:
                    try:
                        journal_row = state.get_publication(plan.transaction_id)
                    except sqlite3.Error:
                        journal_row = "unknown"
                    if journal_row == "unknown":
                        return "error", _publication_recovery_detail(
                            "durable publication commit had an uncertain SQLite "
                            f"outcome; restart recovery is required: {error}"
                        )
                    if journal_row is not None and journal_row["phase"] == "committed":
                        return "error", _publication_recovery_detail(
                            "durable publication committed but SQLite reported an "
                            f"error; restart recovery is required: {error}"
                        )
                    if not _rollback_and_clear_publication(
                        state, plan, output_root, root_identity
                    ):
                        return "error", _publication_recovery_detail(
                            "durable publication commit failed and exact rollback "
                            f"could not be proven: {error}"
                        )
                    status = "error"
                    detail = FailureDetail(
                        str(error), permanent=True, code="state-capacity"
                    )
                else:
                    if not finalize_publication(
                        plan, output_root, root_identity
                    ):
                        return "error", _publication_recovery_detail(
                            "durable publication committed but exact cleanup could "
                            "not be proven; restart recovery is required"
                        )
                    try:
                        state.delete_publication(plan.transaction_id)
                    except sqlite3.Error as error:
                        return "error", _publication_recovery_detail(
                            "publication cleanup completed but its committed journal "
                            f"row remains; restart recovery is required: {error}"
                        )
            if status == "error":
                pass
            else:
                return status, SkipDetail(
                    str(detail), output_signature=signature
                )
    if not isinstance(detail, FailureDetail):
        detail = FailureDetail(
            str(detail), permanent=getattr(detail, "permanent", False),
            code=getattr(detail, "code", "conversion"),
        )
    try:
        attempts, dead, next_retry = state.record_failure(
            job.source_key or job.canonical_source,
            job.fingerprint,
            job.config_fingerprint,
            detail,
            permanent=detail.permanent,
            max_retries=args.max_retries,
            retry_base=args.interval,
        )
    except (UnsafeFileError, StateCapacityError, sqlite3.Error) as state_error:
        _request_fatal_stop()
        detail = FailureDetail(
            f"durable state capacity failure: {state_error}",
            permanent=True,
            code="state-capacity",
        )
        detail.attempts = 1
        detail.dead = True
        detail.next_retry_at = None
        return "error", detail
    detail.attempts = attempts
    detail.dead = dead
    detail.next_retry_at = next_retry
    if detail.code == "containment":
        _request_fatal_stop()
    return "error", detail


def _record_preparation_failure(
    state, source, fingerprint, config, error, args, *, source_key=None,
):
    detail = FailureDetail(
        str(error), permanent=getattr(error, "permanent", False),
        code=getattr(error, "code", "preparation"),
    )
    try:
        return state.record_failure(
            source_key or source, fingerprint, config, detail,
            permanent=detail.permanent,
            max_retries=args.max_retries,
            retry_base=args.interval,
        )
    except (UnsafeFileError, StateCapacityError, sqlite3.Error) as state_error:
        _request_fatal_stop()
        log(f"ERROR: durable state cannot accept another result: {state_error}")
        return 1, True, None


def _future_containment_priority(future):
    """Order an already-complete containment failure before publishable work."""
    if future.cancelled() or not future.done():
        return 1
    try:
        result = future.result()
    except ProcessContainmentError:
        return 0
    except Exception:
        return 1
    if (
        isinstance(result, tuple)
        and len(result) == 2
        and getattr(result[1], "code", None) == "containment"
    ):
        return 0
    return 1


def run_once(args, out_dir, icc, state=None, config_fingerprint=None):
    own_state = state is None
    state = state or StateStore(
        getattr(args, "state_dir", out_dir / ".nef-watch-state"),
        input_root=getattr(args, "input_root", None),
    )
    config_fingerprint = config_fingerprint or render_config_fingerprint(args, icc, out_dir)
    try:
        nefs = find_nefs(
            args.input,
            args.recursive,
            getattr(args, "input_root_identity", None),
            getattr(args, "max_scan_entries", DEFAULT_MAX_SCAN_ENTRIES),
        )
    except ScanLimitError as error:
        log(f"ERROR: {error}")
        state.write_health("degraded", pending=0, inflight=0, force=True)
        if own_state:
            state.close()
        return 1
    except OSError as error:
        log(f"ERROR: input scan incomplete: {error}")
        state.write_health("degraded", pending=0, inflight=0, force=True)
        if own_state:
            state.close()
        return 1
    if STOP_EVENT.is_set():
        state.write_health("stopping", pending=0, inflight=0, force=True)
        if own_state:
            state.close()
        return 130
    try:
        present = {
            source_state_key(path, args.input_root) for path in nefs
        }
        source_conflicts, source_mapping_failures = (
            classify_output_source_mappings(nefs, args, out_dir)
        )
        state.observe_complete_scan(present)
        state.acknowledge_missing_failures(args.input, present)
        state.prune_absent_state(present)
    except (UnsafeFileError, StateCapacityError, sqlite3.Error) as error:
        log(f"ERROR: cannot record the complete input scan: {error}")
        state.write_health("degraded", pending=0, inflight=0, force=True)
        if own_state:
            state.close()
        return 1
    if not nefs:
        log(f"no NEF/NRW files found in {args.input}")
        if own_state:
            state.close()
        return 0
    log(
        f"converting {len(nefs)} NEF/NRW file(s) -> {out_dir}  "
        f"({args.jobs} parallel, {args.max_pending} pending max)"
    )
    total = len(nefs)
    ok = skip = err = done = 0
    next_index = 0
    futs = {}
    ex = cf.ThreadPoolExecutor(max_workers=args.jobs)
    state.write_health("starting", pending=0, inflight=0)

    def finish_future(fut):
        nonlocal ok, skip, err, done
        job = futs.pop(fut)
        try:
            status, detail = fut.result()
        except Exception as e:
            status, detail = "error", FailureDetail(str(e))
        finally:
            remove_snapshot(job.snapshot)
        status, detail = _record_conversion_result(
            state, job, status, detail, args, out_dir
        )
        done += 1
        if status == "ok":
            ok += 1
            log(f"  [{done}/{total}] {job.source.name} -> {detail}")
        elif status == "skip":
            skip += 1
            log(f"  [{done}/{total}] {job.source.name} -> skip (verified)")
        else:
            err += 1
            suffix = " — quarantined" if getattr(detail, "dead", False) else ""
            log(f"  [{done}/{total}] {job.source.name} -> ERROR: {detail}{suffix}")

    def discard_future(fut):
        job = futs.pop(fut)
        try:
            if not fut.cancelled():
                fut.result()
        except Exception:
            pass
        finally:
            remove_snapshot(job.snapshot)

    interrupted = False
    try:
        while (next_index < total or futs) and not STOP_EVENT.is_set():
            while (
                next_index < total
                and len(futs) < args.max_pending
                and not STOP_EVENT.is_set()
            ):
                nef = nefs[next_index]
                next_index += 1
                state_key = source_state_key(nef, args.input_root)
                fingerprint = lightweight_fingerprint(nef)
                try:
                    fingerprint, _ = fingerprint_source(nef, args)
                    if STOP_EVENT.is_set():
                        break
                    mapping_failure = source_mapping_failures.get(state_key)
                    if mapping_failure is not None:
                        attempts, dead, _ = _record_preparation_failure(
                            state,
                            nef,
                            fingerprint,
                            config_fingerprint,
                            mapping_failure,
                            args,
                            source_key=state_key,
                        )
                        done += 1
                        err += 1
                        suffix = (
                            " — quarantined" if dead else f" — attempt {attempts}"
                        )
                        log(
                            f"  [{done}/{total}] {nef.name} -> ERROR: "
                            f"{mapping_failure}{suffix}"
                        )
                        continue
                    action, force, detail = source_action(
                        state, nef, fingerprint, config_fingerprint, args, out_dir,
                        force_overwrite=args.overwrite, source_key=state_key,
                        source_conflict=state_key in source_conflicts,
                    )
                    if STOP_EVENT.is_set():
                        break
                    if action == "skip":
                        done += 1
                        skip += 1
                        log(f"  [{done}/{total}] {nef.name} -> skip ({detail})")
                        continue
                    if action in ("wait", "quarantine"):
                        if getattr(detail, "persist_failure", False):
                            _record_preparation_failure(
                                state,
                                nef,
                                fingerprint,
                                config_fingerprint,
                                detail,
                                args,
                                source_key=state_key,
                            )
                        done += 1
                        err += 1
                        log(f"  [{done}/{total}] {nef.name} -> ERROR: {detail} ({action})")
                        continue
                    state.reserve_outputs(
                        state_key, expected_outputs(nef, args, out_dir)
                    )
                    if STOP_EVENT.is_set():
                        break
                    snapshot = create_input_snapshot(nef, args, fingerprint)
                    if STOP_EVENT.is_set():
                        remove_snapshot(snapshot)
                        break
                except (UnsafeFileError, StateCapacityError, sqlite3.Error) as e:
                    _request_fatal_stop()
                    done += 1
                    err += 1
                    log(f"  [{done}/{total}] {nef.name} -> ERROR: durable state: {e}")
                    break
                except ConversionError as e:
                    fingerprint = getattr(e, "fingerprint", fingerprint)
                    attempts, dead, _ = _record_preparation_failure(
                        state,
                        nef,
                        fingerprint,
                        config_fingerprint,
                        e,
                        args,
                        source_key=state_key,
                    )
                    done += 1
                    err += 1
                    suffix = " — quarantined" if dead else f" — attempt {attempts}"
                    log(f"  [{done}/{total}] {nef.name} -> ERROR: {e}{suffix}")
                    continue
                owned_outputs = getattr(detail, "owned_outputs", ())
                job = PendingJob(
                    source=nef,
                    canonical_source=state_key,
                    snapshot=snapshot,
                    fingerprint=fingerprint,
                    config_fingerprint=config_fingerprint,
                    force=force,
                    source_key=state_key,
                    owned_outputs=owned_outputs,
                )
                fut = _submit(
                    ex, snapshot.path, args, out_dir, icc, force,
                    source_path=nef,
                    expected_fingerprint=fingerprint,
                    owned_outputs=owned_outputs,
                    source_key=state_key,
                )
                futs[fut] = job
            if futs:
                completed, _ = cf.wait(
                    futs, timeout=0.25, return_when=cf.FIRST_COMPLETED
                )
                for fut in sorted(completed, key=_future_containment_priority):
                    if FATAL_STOP_EVENT.is_set():
                        break
                    finish_future(fut)
                    if FATAL_STOP_EVENT.is_set():
                        break
            state.write_health(
                "degraded" if state.count_permanent_failures() else "healthy",
                last_scan_at=time.time(),
                pending=max(0, len(futs) - args.jobs),
                inflight=min(len(futs), args.jobs),
            )
        if STOP_EVENT.is_set():
            interrupted = True
            log("stop requested — finishing in-flight file(s); queued files cancelled")
            for fut in futs:
                fut.cancel()
    except KeyboardInterrupt:
        interrupted = True
        log("interrupted — finishing in-flight file(s); queued files cancelled")
        for fut in futs:
            fut.cancel()
    finally:
        try:
            ex.shutdown(wait=True, cancel_futures=True)
        except KeyboardInterrupt:
            log("second interrupt — exiting immediately (outputs stay staged/atomic)")
            os._exit(130)
        for fut in list(futs):
            if fut.done() and not fut.cancelled():
                if FATAL_STOP_EVENT.is_set():
                    discard_future(fut)
                else:
                    finish_future(fut)
            elif fut.cancelled():
                remove_snapshot(futs.pop(fut).snapshot)
        state.write_health(
            "stopping", last_scan_at=time.time(), pending=0, inflight=0
        )
        if own_state:
            state.close()
    if interrupted:
        not_started = total - done
        log(
            f"partial: {ok} converted, {skip} skipped, {err} errors, "
            f"{not_started} not-started"
        )
        return 130
    log(f"done: {ok} converted, {skip} skipped, {err} errors")
    return 1 if err else 0


def _baseline_key(args):
    payload = {
        "input": canonical_path(args.input),
        "recursive": bool(args.recursive),
        "mode": "skip-existing-v1",
    }
    return "baseline:" + hashlib.sha256(_json_dumps(payload).encode()).hexdigest()


def initialize_skip_existing(
    args, state, out_dir, *, config_fingerprint="baseline",
):
    key = _baseline_key(args)
    if state.get_meta(key) == "complete":
        return False
    legacy_marker = Path(out_dir) / LEGACY_BASELINE_MARKER
    if legacy_marker.exists() or legacy_marker.is_symlink():
        return import_legacy_baseline(
            args,
            state,
            out_dir,
            key,
            legacy_marker,
            config_fingerprint=config_fingerprint,
        )
    nefs = find_nefs(
        args.input,
        args.recursive,
        getattr(args, "input_root_identity", None),
        getattr(args, "max_scan_entries", DEFAULT_MAX_SCAN_ENTRIES),
    )
    if STOP_EVENT.is_set():
        log("stop requested — one-time baseline was not committed")
        return False
    records = []
    reservations = []
    mapping_failures = []
    permanent_failures = state.count_permanent_failures()
    for nef in nefs:
        if STOP_EVENT.is_set():
            log("stop requested — one-time baseline was not committed")
            return False
        state_key = source_state_key(nef, args.input_root)
        try:
            fingerprint, _ = fingerprint_source(nef, args, validate_magic=False)
        except ConversionError:
            # A baseline is explicitly a promise not to process the versions
            # present at first startup. If one is mid-write, retain its observed
            # stat token so the completed content necessarily re-arms later.
            fingerprint = lightweight_fingerprint(nef)
        if STOP_EVENT.is_set():
            log("stop requested — one-time baseline was not committed")
            return False
        try:
            outputs = expected_outputs(nef, args, out_dir)
        except OutputCollisionError as error:
            mapping_failures.append(
                (state_key, fingerprint, _source_output_mapping_failure(error))
            )
        else:
            reservations.extend(
                (state_key, kind, output) for kind, output in outputs
            )
            records.append((state_key, fingerprint))
        state.write_health(
            "starting",
            pending=0,
            inflight=0,
            permanent_failures=permanent_failures,
        )
    if STOP_EVENT.is_set():
        log("stop requested — one-time baseline was not committed")
        return False
    created = state.record_baseline(
        key,
        records,
        reservations,
        mapping_failures,
        config_fingerprint=config_fingerprint,
    )
    if created:
        skipped = len(nefs) - state.last_baseline_conflicts
        log(
            f"one-time baseline saved: {skipped} existing NEF/NRW file(s) "
            "will be skipped unless their content changes; "
            f"{state.last_baseline_conflicts} conflicting file(s) quarantined"
        )
    return created


def import_legacy_baseline(
    args,
    state,
    out_dir,
    baseline_key,
    marker,
    *,
    config_fingerprint="baseline",
):
    """Import the exact legacy path set without swallowing post-marker arrivals."""
    marker = Path(marker)
    try:
        output_root, output_identity = _output_root_parameters(
            args=args, out_dir=out_dir
        )
        marker_bytes = _read_bounded_regular_file(
            marker,
            MAX_LEGACY_MARKER_BYTES,
            require_single_link=True,
            root=output_root,
            root_identity=output_identity,
        )
        marker_text = marker_bytes.decode("utf-8")
    except (OSError, UnicodeError) as e:
        raise PermanentConversionError(
            f"cannot safely read legacy skip-existing marker {marker}: {e}"
        ) from e
    if STOP_EVENT.is_set():
        log("stop requested — legacy baseline was not committed")
        return False
    lines = marker_text.splitlines()

    # Keep the configured lexical spelling. On macOS `/var` and `/private/var`
    # are aliases; resolving only the root while discovery retains `/var` would
    # make legitimate relative keys appear to escape.
    root = Path(lexical_path(args.input))
    configured_legacy_root = getattr(args, "legacy_input_root", None)
    legacy_root = (
        Path(lexical_path(configured_legacy_root))
        if configured_legacy_root is not None else None
    )
    requested = set()
    for line_number, line in enumerate(lines, 1):
        if STOP_EVENT.is_set():
            log("stop requested — legacy baseline was not committed")
            return False
        if not line or "\x00" in line or len(line.encode("utf-8")) > 4096:
            raise PermanentConversionError(
                f"invalid path at {marker}:{line_number}"
            )
        candidate = Path(line)
        if not candidate.is_absolute() or not is_raw_file(candidate):
            raise PermanentConversionError(
                f"non-absolute or non-raw path at {marker}:{line_number}: {line!r}"
            )
        if legacy_root is not None:
            normalized_candidate = Path(os.path.normpath(line))
            try:
                relative = normalized_candidate.relative_to(legacy_root)
            except ValueError as e:
                raise PermanentConversionError(
                    f"legacy marker path escapes declared legacy input root at "
                    f"line {line_number}: {line}"
                ) from e
            candidate = root / relative
        elif not path_is_within(candidate, root):
            raise PermanentConversionError(
                f"legacy marker path escapes input root at line {line_number}: {line}"
            )
        if not path_is_within(candidate, root):
            raise PermanentConversionError(
                f"remapped legacy path escapes current input root at line "
                f"{line_number}: {line}"
            )
        try:
            relative_candidate = Path(lexical_path(candidate)).relative_to(root)
        except ValueError:
            # Legacy markers may contain `/private/var` while the configured
            # macOS input spelling is `/var` (or vice versa). Containment above
            # was checked on resolved paths; remap that verified relative part
            # back into the current lexical namespace before creating the key.
            relative_candidate = candidate.resolve(strict=False).relative_to(
                root.resolve(strict=True)
            )
        requested.add(source_state_key(root / relative_candidate, root))

    current = {
        source_state_key(path, root): path
        for path in find_nefs(
            args.input,
            args.recursive,
            getattr(args, "input_root_identity", None),
            getattr(args, "max_scan_entries", DEFAULT_MAX_SCAN_ENTRIES),
        )
    }
    records = []
    reservations = []
    mapping_failures = []
    permanent_failures = state.count_permanent_failures()
    for state_key in sorted(requested):
        if STOP_EVENT.is_set():
            log("stop requested — legacy baseline was not committed")
            return False
        nef = current.get(state_key)
        if nef is None:
            continue
        # Legacy paths are an explicit skip set, so magic is irrelevant, but
        # content identity must be robust and race-free before it is imported.
        try:
            fingerprint, _ = fingerprint_source(nef, args, validate_magic=False)
        except InvalidRawError:
            fingerprint = lightweight_fingerprint(nef)
        if STOP_EVENT.is_set():
            log("stop requested — legacy baseline was not committed")
            return False
        try:
            outputs = expected_outputs(nef, args, out_dir)
        except OutputCollisionError as error:
            mapping_failures.append(
                (state_key, fingerprint, _source_output_mapping_failure(error))
            )
        else:
            records.append((state_key, fingerprint))
            reservations.extend(
                (state_key, kind, output) for kind, output in outputs
            )
        state.write_health(
            "starting",
            pending=0,
            inflight=0,
            permanent_failures=permanent_failures,
        )

    if STOP_EVENT.is_set():
        log("stop requested — legacy baseline was not committed")
        return False
    created = state.record_baseline(
        baseline_key,
        records,
        reservations,
        mapping_failures,
        config_fingerprint=config_fingerprint,
    )
    if not created:
        return False
    digest = hashlib.sha256(marker_bytes).hexdigest()[:12]
    imported_marker = None
    archive_error = None
    for attempt in range(MAX_LEGACY_ARCHIVE_ATTEMPTS):
        if attempt == 0:
            candidate = marker.with_name(marker.name + ".imported")
        elif attempt == 1:
            candidate = marker.with_name(marker.name + f".imported-{digest}")
        else:
            candidate = marker.with_name(
                marker.name + f".imported-{digest}-{attempt - 1:02d}"
            )
        try:
            _publish_output_no_replace(
                marker, candidate, output_root, output_identity
            )
        except OutputCollisionError:
            continue
        except OSError as error:
            # A failed no-replace publication never overwrites the candidate.
            # If the link was already durable, both names contain the same
            # marker bytes; otherwise the original remains the sole copy.
            archive_error = error
            if getattr(error, "output_mutation_completed", False):
                imported_marker = candidate
            break
        else:
            imported_marker = candidate
            break
    if imported_marker is None:
        if archive_error is not None:
            log(
                "warning: legacy marker imported but could not be archived; "
                f"the original was preserved: {archive_error}"
            )
        else:
            log(
                "warning: legacy marker imported but every bounded archive name "
                "is occupied; the original marker was preserved"
            )
        archive_name = marker.name
    else:
        archive_name = imported_marker.name
        if archive_error is not None:
            log(
                "warning: legacy marker archive completed but a directory sync "
                f"failed; at least one marker name was preserved: {archive_error}"
            )
    log(
        f"legacy one-time baseline imported: {len(records)} current file(s) "
        f"from {len(requested)} recorded path(s); "
        f"{state.last_baseline_conflicts} conflicting file(s) quarantined; "
        f"marker preserved as {archive_name}"
    )
    return True


def run_watch(args, out_dir, icc, state=None, config_fingerprint=None):
    own_state = state is None
    state = state or StateStore(
        getattr(args, "state_dir", out_dir / ".nef-watch-state"),
        input_root=getattr(args, "input_root", None),
    )
    config_fingerprint = config_fingerprint or render_config_fingerprint(args, icc, out_dir)
    log(
        f"watching {args.input}  ->  {out_dir}   "
        f"(every {args.interval}s, settle {args.settle_seconds}s, "
        f"{args.jobs} parallel, {args.max_pending} pending max, Ctrl-C to stop)"
    )
    state.write_health("starting", pending=0, inflight=0)
    if args.skip_existing:
        try:
            initialize_skip_existing(
                args,
                state,
                out_dir,
                config_fingerprint=config_fingerprint,
            )
        except (
            ScanLimitError,
            ConversionError,
            StateCapacityError,
            sqlite3.Error,
            OSError,
        ) as error:
            log(f"ERROR: {error}")
            state.write_health("degraded", pending=0, inflight=0, force=True)
            if own_state:
                state.close()
            return 1

    # O(1) active-source membership and bounded futures prevent a large camera
    # dump from creating an unbounded executor queue.
    stable = {}              # canonical source -> (stat key, monotonic since)
    fingerprint_cache = {}   # canonical source -> (stat key, robust fingerprint)
    verified_cache = {}      # canonical source -> (stat key, output stat signature)
    collision_cache = {}     # source -> unchanged obstruction proof
    active_sources = set()
    handled_this_run = set()
    reported_quarantine = set()
    inflight = {}            # future -> PendingJob (bounded by max_pending)
    n = 0
    unavailable = False
    scan_limit_reported = False
    last_scan_at = None
    output_recovery = getattr(args, "output_recovery", None)
    owns_output_recovery = output_recovery is None
    temp_recovery = getattr(args, "temp_recovery", None)
    owns_temp_recovery = temp_recovery is None
    if temp_recovery is None:
        temp_recovery = TempArtifactRecovery(
            args.temp_dir, prior_session_cutoff=time.time()
        )
    if output_recovery is None:
        output_recovery = OutputArtifactRecovery(
            out_dir,
            args.recursive,
            max_sweep_entries=getattr(
                args, "max_scan_entries", DEFAULT_MAX_SCAN_ENTRIES
            ),
            prior_session_cutoff=time.time(),
        )
    last_recovery_at = 0.0
    last_state_prune_at = 0.0
    recovery_limit_reported = False
    recovery_issue_reported = False
    recovery_degraded = bool(
        getattr(args, "output_recovery_degraded", False)
    )
    work_age = stale_work_age(args)
    ex = cf.ThreadPoolExecutor(max_workers=args.jobs)

    def collect_finished():
        nonlocal n
        completed = [candidate for candidate in inflight if candidate.done()]
        for fut in sorted(completed, key=_future_containment_priority):
            if FATAL_STOP_EVENT.is_set():
                break
            job = inflight.pop(fut)
            active_sources.discard(job.canonical_source)
            if fut.cancelled():
                remove_snapshot(job.snapshot)
                continue
            try:
                status, detail = fut.result()
            except Exception as e:
                status, detail = "error", FailureDetail(str(e))
            finally:
                remove_snapshot(job.snapshot)
            status, detail = _record_conversion_result(
                state, job, status, detail, args, out_dir
            )
            n += 1
            if status in ("ok", "skip"):
                handled_this_run.add(job.canonical_source)
                try:
                    current_key = stat_key(job.source)
                except OSError:
                    current_key = None
                validated_signature = getattr(detail, "output_signature", None)
                if current_key == job.snapshot.stat_key and validated_signature is not None:
                    verified_cache[job.canonical_source] = (
                        current_key, validated_signature
                    )
                reported_quarantine.discard(job.canonical_source)
                log(f"  [{n}] {job.source.name} -> {detail}")
            else:
                verified_cache.pop(job.canonical_source, None)
                suffix = ""
                if getattr(detail, "dead", False):
                    suffix = " — quarantined; change the source to re-arm"
                    reported_quarantine.add(job.canonical_source)
                elif getattr(detail, "attempts", None):
                    suffix = f" — attempt {detail.attempts}/{args.max_retries}"
                log(f"  [{n}] {job.source.name} -> ERROR: {detail}{suffix}")
            if FATAL_STOP_EVENT.is_set():
                break

    def discard_finished():
        for fut in [candidate for candidate in inflight if candidate.done()]:
            job = inflight.pop(fut)
            active_sources.discard(job.canonical_source)
            try:
                if not fut.cancelled():
                    fut.result()
            except Exception:
                pass
            finally:
                remove_snapshot(job.snapshot)

    try:
        while not STOP_EVENT.is_set():
            collect_finished()
            wall_now = time.time()
            monotonic_now = time.monotonic()
            if monotonic_now - last_recovery_at >= RECOVERY_INTERVAL_SECONDS:
                recovery_ceiling = getattr(
                    args, "max_scan_entries", DEFAULT_MAX_SCAN_ENTRIES
                )
                temp_progress = complete_recovery_sweep(
                    temp_recovery,
                    work_age,
                    now=wall_now,
                    max_entries=recovery_ceiling,
                )
                progress = complete_recovery_sweep(
                    output_recovery,
                    work_age,
                    now=wall_now,
                    max_entries=recovery_ceiling,
                )
                last_recovery_at = monotonic_now
                if temp_progress.recovered or progress.recovered:
                    log(
                        "periodic recovery: removed "
                        f"{temp_progress.recovered} stale temp file(s), "
                        f"resolved {progress.recovered} output artifact(s)"
                    )
                if (
                    temp_progress.limit_exceeded or progress.limit_exceeded
                ) and not recovery_limit_reported:
                    log(
                        "warning: artifact recovery reached its bounded sweep ceiling; "
                        "raise --max-scan-entries after reviewing temp/output storage"
                    )
                    recovery_limit_reported = True
                elif (
                    temp_progress.sweep_complete
                    and progress.sweep_complete
                    and not temp_progress.limit_exceeded
                    and not progress.limit_exceeded
                ):
                    recovery_limit_reported = False
                recovery_issue = bool(
                    not temp_progress.sweep_complete
                    or not progress.sweep_complete
                    or temp_progress.limit_exceeded
                    or progress.limit_exceeded
                    or temp_progress.error_count
                    or progress.error_count
                    or temp_progress.ambiguous_count
                    or progress.ambiguous_count
                )
                if recovery_issue:
                    recovery_degraded = True
                    if not recovery_issue_reported:
                        log(
                            "ERROR: temp/output artifact recovery is incomplete, "
                            "failed, or ambiguous; health remains degraded until a "
                            "full clean sweep succeeds"
                        )
                        recovery_issue_reported = True
                    state.write_health(
                        "degraded",
                        last_scan_at=last_scan_at,
                        pending=max(0, len(inflight) - args.jobs),
                        inflight=min(len(inflight), args.jobs),
                        force=True,
                    )
                    _request_fatal_stop()
                    break
                elif temp_progress.sweep_complete and progress.sweep_complete:
                    recovery_degraded = False
                    recovery_issue_reported = False
            if not args.input.exists() or not args.input.is_dir():
                if not unavailable:
                    log(f"warning: watched folder unavailable: {args.input} — waiting")
                    unavailable = True
                permanent = state.count_permanent_failures()
                state.write_health(
                    "degraded", last_scan_at=last_scan_at,
                    pending=max(0, len(inflight) - args.jobs),
                    inflight=min(len(inflight), args.jobs),
                    permanent_failures=permanent,
                )
                STOP_EVENT.wait(args.interval)
                continue
            if unavailable:
                log(f"watched folder available again: {args.input}")
                unavailable = False
            try:
                nefs = find_nefs(
                    args.input, args.recursive,
                    getattr(args, "input_root_identity", None),
                    getattr(args, "max_scan_entries", DEFAULT_MAX_SCAN_ENTRIES),
                )
            except ScanLimitError as error:
                if not scan_limit_reported:
                    log(f"ERROR: {error}")
                    scan_limit_reported = True
                state.write_health(
                    "degraded",
                    last_scan_at=last_scan_at,
                    pending=max(0, len(inflight) - args.jobs),
                    inflight=min(len(inflight), args.jobs),
                    force=True,
                )
                STOP_EVENT.wait(args.interval)
                continue
            except OSError:
                if not unavailable:
                    log(f"warning: watched folder unavailable: {args.input} — waiting")
                    unavailable = True
                state.write_health(
                    "degraded", last_scan_at=last_scan_at,
                    pending=max(0, len(inflight) - args.jobs),
                    inflight=min(len(inflight), args.jobs),
                )
                STOP_EVENT.wait(args.interval)
                continue

            if STOP_EVENT.is_set():
                # find_nefs can stop cooperatively between directory entries.
                # Its partial result must never drive absence acknowledgement,
                # cache pruning, or a healthy scan timestamp.
                break

            if scan_limit_reported:
                log("input scan is within the configured entry ceiling again")
                scan_limit_reported = False

            last_scan_at = wall_now
            try:
                present = {
                    source_state_key(path, args.input_root) for path in nefs
                }
                source_conflicts, source_mapping_failures = (
                    classify_output_source_mappings(nefs, args, out_dir)
                )
                state.observe_complete_scan(present, now=wall_now)
            except (UnsafeFileError, StateCapacityError, sqlite3.Error) as error:
                log(f"ERROR: cannot record a complete input scan safely: {error}")
                state.write_health(
                    "degraded",
                    last_scan_at=last_scan_at,
                    pending=max(0, len(inflight) - args.jobs),
                    inflight=min(len(inflight), args.jobs),
                    force=True,
                )
                _request_fatal_stop()
                break
            for nef in nefs:
                if STOP_EVENT.is_set():
                    break
                last_scan_at = time.time()
                state_key = source_state_key(nef, args.input_root)
                if state_key in active_sources:
                    continue
                try:
                    key = stat_key(nef)
                except OSError:
                    continue

                mapping_failure = source_mapping_failures.get(state_key)
                if mapping_failure is None:
                    cached_verified = verified_cache.get(state_key)
                    if cached_verified and cached_verified == (
                        key, output_stat_signature(nef, args, out_dir)
                    ):
                        continue

                stable_entry = stable.get(state_key)
                if not stable_entry or stable_entry[0] != key:
                    stable[state_key] = (key, monotonic_now)
                    stable_entry = stable[state_key]
                    fingerprint_cache.pop(state_key, None)
                    verified_cache.pop(state_key, None)
                    collision_cache.pop(state_key, None)
                    reported_quarantine.discard(state_key)
                    if args.settle_seconds > 0:
                        continue
                if monotonic_now - stable_entry[1] < args.settle_seconds:
                    continue
                if len(inflight) >= args.max_pending:
                    break

                try:
                    fingerprint = fingerprint_source_cached(
                        fingerprint_cache, state_key, key, nef, args
                    )
                    if STOP_EVENT.is_set():
                        break
                except SourceChangedError:
                    stable[state_key] = (key, monotonic_now)
                    fingerprint_cache.pop(state_key, None)
                    continue
                except (UnsafeFileError, StateCapacityError, sqlite3.Error) as e:
                    _request_fatal_stop()
                    log(f"  {nef.name} -> ERROR: durable state: {e}")
                    break
                except ConversionError as e:
                    failure_fp = getattr(e, "fingerprint", lightweight_fingerprint(nef))
                    previous = state.get_failure(state_key)
                    if (
                        previous
                        and previous["fingerprint"] == failure_fp
                        and previous["config_fingerprint"] == config_fingerprint
                    ):
                        if previous["permanent"]:
                            if state_key not in reported_quarantine:
                                log(f"  {nef.name} -> quarantined: {previous['error_text']}")
                                reported_quarantine.add(state_key)
                            continue
                        if (
                            previous["next_retry_at"]
                            and wall_now < previous["next_retry_at"]
                        ):
                            continue
                    attempts, dead, _ = _record_preparation_failure(
                        state,
                        nef,
                        failure_fp,
                        config_fingerprint,
                        e,
                        args,
                        source_key=state_key,
                    )
                    log(
                        f"  {nef.name} -> ERROR: {e} "
                        f"({'quarantined' if dead else f'attempt {attempts}/{args.max_retries}'})"
                    )
                    if dead:
                        reported_quarantine.add(state_key)
                    continue

                if mapping_failure is not None:
                    previous = state.get_failure(state_key)
                    same_failure = bool(
                        previous
                        and previous["fingerprint"] == fingerprint
                        and previous["config_fingerprint"]
                        == config_fingerprint
                        and previous["permanent"]
                        and previous["error_code"] == mapping_failure.code
                        and previous["error_text"] == str(mapping_failure)
                    )
                    if not same_failure:
                        _record_preparation_failure(
                            state,
                            nef,
                            fingerprint,
                            config_fingerprint,
                            mapping_failure,
                            args,
                            source_key=state_key,
                        )
                    collision_cache.pop(state_key, None)
                    verified_cache.pop(state_key, None)
                    if state_key not in reported_quarantine:
                        log(f"  {nef.name} -> quarantined: {mapping_failure}")
                        reported_quarantine.add(state_key)
                    continue

                force_overwrite = args.overwrite and state_key not in handled_this_run
                active_failure = state.get_failure(state_key)
                if (
                    active_failure is not None
                    and active_failure["permanent"]
                    and active_failure["error_code"] == "output-collision"
                    and active_failure["fingerprint"] == fingerprint
                    and active_failure["config_fingerprint"] == config_fingerprint
                ):
                    obstruction = collision_obstruction_signature(
                        state, nef, args, out_dir
                    )
                    collision_token = (
                        fingerprint,
                        config_fingerprint,
                        obstruction,
                        active_failure["error_text"],
                    )
                    if collision_cache.get(state_key) == collision_token:
                        continue
                action, force, detail = source_action(
                    state, nef, fingerprint, config_fingerprint, args, out_dir,
                    force_overwrite=force_overwrite,
                    source_key=state_key,
                    source_conflict=state_key in source_conflicts,
                )
                if STOP_EVENT.is_set():
                    break
                if action == "skip":
                    collision_cache.pop(state_key, None)
                    validated_signature = getattr(detail, "output_signature", None)
                    if validated_signature is not None:
                        verified_cache[state_key] = (key, validated_signature)
                    continue
                if action == "wait":
                    collision_cache.pop(state_key, None)
                    continue
                if action == "quarantine":
                    if getattr(detail, "persist_failure", False):
                        _record_preparation_failure(
                            state,
                            nef,
                            fingerprint,
                            config_fingerprint,
                            detail,
                            args,
                            source_key=state_key,
                        )
                    if getattr(detail, "code", None) == "output-collision":
                        collision_cache[state_key] = (
                            fingerprint,
                            config_fingerprint,
                            collision_obstruction_signature(
                                state, nef, args, out_dir
                            ),
                            str(detail),
                        )
                    else:
                        collision_cache.pop(state_key, None)
                    if state_key not in reported_quarantine:
                        log(f"  {nef.name} -> quarantined: {detail}")
                        reported_quarantine.add(state_key)
                    continue

                collision_cache.pop(state_key, None)
                try:
                    state.reserve_outputs(
                        state_key, expected_outputs(nef, args, out_dir)
                    )
                    if STOP_EVENT.is_set():
                        break
                    snapshot = create_input_snapshot(nef, args, fingerprint)
                    if STOP_EVENT.is_set():
                        remove_snapshot(snapshot)
                        break
                except (UnsafeFileError, StateCapacityError, sqlite3.Error) as e:
                    _request_fatal_stop()
                    log(f"  {nef.name} -> ERROR: durable state: {e}")
                    break
                except ConversionError as e:
                    attempts, dead, _ = _record_preparation_failure(
                        state,
                        nef,
                        fingerprint,
                        config_fingerprint,
                        e,
                        args,
                        source_key=state_key,
                    )
                    log(
                        f"  {nef.name} -> ERROR: {e} "
                        f"({'quarantined' if dead else f'attempt {attempts}/{args.max_retries}'})"
                    )
                    if dead:
                        reported_quarantine.add(state_key)
                    continue

                owned_outputs = getattr(detail, "owned_outputs", ())
                job = PendingJob(
                    source=nef,
                    canonical_source=state_key,
                    snapshot=snapshot,
                    fingerprint=fingerprint,
                    config_fingerprint=config_fingerprint,
                    force=force,
                    source_key=state_key,
                    owned_outputs=owned_outputs,
                )
                future = _submit(
                    ex, snapshot.path, args, out_dir, icc, force,
                    source_path=nef,
                    expected_fingerprint=fingerprint,
                    owned_outputs=owned_outputs,
                    source_key=state_key,
                )
                inflight[future] = job
                active_sources.add(state_key)

            try:
                state.acknowledge_missing_failures(args.input, present)
                if monotonic_now - last_state_prune_at >= STATE_PRUNE_INTERVAL_SECONDS:
                    pruned = state.prune_absent_state(present, now=wall_now)
                    last_state_prune_at = monotonic_now
                    if any(pruned.values()):
                        log(
                            "state retention: pruned "
                            f"{pruned['sources']} source, {pruned['failures']} failure, "
                            f"and {pruned['reservations']} reservation row(s)"
                        )
            except (UnsafeFileError, StateCapacityError, sqlite3.Error) as error:
                log(f"ERROR: durable state maintenance failed: {error}")
                _request_fatal_stop()
                break
            for mapping in (
                stable, fingerprint_cache, verified_cache, collision_cache
            ):
                for path_text in list(mapping):
                    if path_text not in present and path_text not in active_sources:
                        del mapping[path_text]
            handled_this_run &= present | active_sources
            reported_quarantine &= present
            permanent = state.count_permanent_failures()
            state.write_health(
                "degraded" if permanent or recovery_degraded else "healthy",
                last_scan_at=last_scan_at,
                pending=max(0, len(inflight) - args.jobs),
                inflight=min(len(inflight), args.jobs),
                permanent_failures=permanent,
            )
            STOP_EVENT.wait(args.interval)
    except KeyboardInterrupt:
        log("stopping, finishing in-flight conversions…")
    finally:
        cancelled = cancel_queued_jobs(inflight, active_sources)
        if cancelled:
            log(f"stop requested — cancelled {cancelled} queued conversion(s)")
        try:
            ex.shutdown(wait=True, cancel_futures=True)
        except KeyboardInterrupt:
            log("second interrupt — exiting immediately (outputs stay staged/atomic)")
            os._exit(130)
        cancel_queued_jobs(inflight, active_sources)
        if FATAL_STOP_EVENT.is_set():
            discard_finished()
        else:
            collect_finished()
        state.write_health(
            "stopping", last_scan_at=last_scan_at, pending=0, inflight=0
        )
        if owns_output_recovery:
            output_recovery.close()
        if owns_temp_recovery:
            temp_recovery.close()
        if own_state:
            state.close()
    log(f"stopped. {len(handled_this_run)} file(s) handled this session.")
    return 1 if FATAL_STOP_EVENT.is_set() else 0


def resolve_dng_engine(args):
    """Return the path to the chosen DNG transcoder, or exit with install hint."""
    if args.dng_engine == "dnglab":
        p = shutil.which("dnglab")
        if not p:
            sys.exit("dnglab not found — install: brew install dnglab   (or --dng-engine adobe)")
        return p
    cand = Path("/Applications/Adobe DNG Converter.app/Contents/MacOS/Adobe DNG Converter")
    if not cand.exists():
        sys.exit("Adobe DNG Converter not found — install: "
                 "brew install --cask adobe-dng-converter   (or --dng-engine dnglab)")
    return str(cand)


def main():
    try:
        harden_watcher_process()
    except OSError as error:
        sys.exit(f"cannot establish Linux watcher containment: {error}")
    ap = argparse.ArgumentParser(
        prog="nef-watch",
        description="Watch a folder for Nikon NEFs and convert them to TIFF (Nikon look, via "
                    "the Image SDK) and/or DNG (raw transcode, no baked look).",
    )
    ap.add_argument("input", type=Path, help="folder to watch/scan, or a single .NEF/.NRW file")
    ap.add_argument("--out", "-o", type=Path, required=True, help="output folder")
    ap.add_argument("--format", dest="formats", type=parse_formats, default=parse_formats("tiff"),
                    metavar="FORMATS", help="comma-separated output formats: tiff,jpeg,dng; both=tiff,dng")
    ap.add_argument("--quality", type=parse_quality, default=90, help="JPEG quality 1-100 (default 90)")
    ap.add_argument("--once", action="store_true", help="convert existing NEFs once, then exit (default: keep watching)")
    ap.add_argument(
        "--jobs", "-j", type=parse_jobs, default=4,
        help=f"parallel workers, 1-{MAX_JOBS} (default 4)",
    )
    ap.add_argument(
        "--max-pending", type=parse_max_pending,
        help="bound active + queued conversions (default: 2 x jobs)",
    )
    ap.add_argument("--bits", type=int, choices=(8, 16), default=8, help="TIFF bit depth (default 8)")
    ap.add_argument("--exp-comp", type=parse_exp_comp, default=0.0,
                    help="exposure compensation in EV for TIFF/JPEG, -5..5 (default 0.0)")
    ap.add_argument("--deterministic", action="store_true",
                    help="byte-reproducible TIFF/JPEG: pin the SDK's rand()-seeded dither "
                         "(native macOS renderer only; same look, stable bytes)")
    ap.add_argument("--dng-engine", choices=("dnglab", "adobe"), default="dnglab",
                    help="DNG backend (default dnglab; adobe needs the app installed)")
    ap.add_argument("--dng-embed-original", action="store_true",
                    help="embed the original NEF inside the DNG (much larger files)")
    ap.add_argument("--recursive", "-r", action="store_true", help="scan subfolders too")
    ap.add_argument("--overwrite", action="store_true", help="re-convert even if the output already exists")
    ap.add_argument(
        "--skip-existing", action="store_true",
        help="once per state database, baseline files present at first watch startup",
    )
    ap.add_argument(
        "--legacy-input-root", type=Path,
        default=_env_path("NEF_WATCH_LEGACY_INPUT_ROOT"),
        help="old absolute input root used by a legacy skip marker "
             "(env NEF_WATCH_LEGACY_INPUT_ROOT)",
    )
    ap.add_argument(
        "--reset-failures", action="store_true",
        help="acknowledge durable dead letters and retry their inputs",
    )
    ap.add_argument(
        "--interval", type=parse_interval, default=3.0,
        help="watch poll interval in seconds, 0.1-3600 (default 3)",
    )
    ap.add_argument(
        "--settle-seconds", "--quiet-seconds", dest="settle_seconds",
        type=parse_nonnegative_duration,
        default=os.environ.get("NEF_WATCH_SETTLE_SECONDS", "3"),
        help="required unchanged size/mtime quiet period before snapshotting (default 3)",
    )
    ap.add_argument(
        "--render-timeout", type=parse_timeout,
        default=os.environ.get("NEF_WATCH_RENDER_TIMEOUT", "300"),
        help="renderer timeout in seconds (default 300)",
    )
    ap.add_argument(
        "--exif-timeout", type=parse_timeout,
        default=os.environ.get("NEF_WATCH_EXIF_TIMEOUT", "60"),
        help="ExifTool timeout in seconds (default 60)",
    )
    ap.add_argument(
        "--dng-timeout", type=parse_timeout,
        default=os.environ.get("NEF_WATCH_DNG_TIMEOUT", "300"),
        help="DNG converter timeout in seconds (default 300)",
    )
    ap.add_argument(
        "--kill-grace-seconds", type=parse_timeout,
        default=os.environ.get("NEF_WATCH_KILL_GRACE_SECONDS", "5"),
        help="TERM grace period before KILL for timed-out tools (default 5)",
    )
    ap.add_argument(
        "--max-retries", type=int, default=3,
        help="transient attempts before durable quarantine (default 3)",
    )
    ap.add_argument(
        "--max-input-mib",
        type=parse_max_input_mib,
        default=DEFAULT_MAX_INPUT_MIB,
        help="maximum NEF/NRW size for every output format (default 512 MiB)",
    )
    ap.add_argument(
        "--max-scan-entries",
        type=parse_max_scan_entries,
        default=os.environ.get(
            "NEF_WATCH_MAX_SCAN_ENTRIES", str(DEFAULT_MAX_SCAN_ENTRIES)
        ),
        help="maximum filesystem entries examined per scan (default 100000)",
    )
    ap.add_argument("--profile", type=Path, default=DEFAULT_PROFILE, help="output ICC profile (default: Nikon sRGB)")
    ap.add_argument("--log-file", type=Path, help="also append timestamped log lines to this file")
    ap.add_argument("--render-bin", type=Path, default=DEFAULT_RENDER_BIN, help="path to the nef_render helper")
    ap.add_argument(
        "--state-dir", type=Path, default=_env_path("NEF_WATCH_STATE_DIR"),
        help="durable SQLite/health state directory (env NEF_WATCH_STATE_DIR)",
    )
    ap.add_argument(
        "--temp-dir", type=Path,
        default=_env_path("NEF_WATCH_TEMP_DIR") or _env_path("TMPDIR"),
        help="fast storage for renderer buffers and immutable input snapshots",
    )
    args = ap.parse_args()

    if args.log_file:
        args.log_file = args.log_file.expanduser()
    configure_logging(args.log_file)

    args.input = args.input.expanduser()
    if not _path_has_strict_utf8_name(args.input):
        sys.exit(
            "input path is not valid UTF-8: "
            f"{_safe_path_display(args.input)}"
        )
    args.out = args.out.expanduser().resolve(strict=False)
    args.render_bin = args.render_bin.expanduser()
    args.profile = args.profile.expanduser()
    args.state_dir = (
        args.state_dir.expanduser() if args.state_dir else args.out / ".nef-watch-state"
    )
    args.temp_dir = (
        args.temp_dir.expanduser() if args.temp_dir else Path(tempfile.gettempdir())
    )
    if args.legacy_input_root:
        args.legacy_input_root = args.legacy_input_root.expanduser()
    if args.input.is_symlink():
        sys.exit(f"symbolic-link input is not allowed: {args.input}")
    args.input_is_file = args.input.is_file()
    if args.input_is_file:
        if not is_raw_file(args.input):
            sys.exit(f"input file is not a NEF/NRW: {args.input}")
        args.once = True
        args.recursive = False
        args.input_root = args.input.parent
    elif args.input.is_dir():
        args.input_root = args.input
    else:
        sys.exit(f"input folder not found: {args.input}")
    args.input = args.input.resolve(strict=True)
    args.input_root = args.input_root.resolve(strict=True)
    try:
        args.input_root_identity = _directory_identity(args.input_root)
    except OSError as e:
        sys.exit(f"cannot safely open input root {args.input_root}: {e}")
    if args.max_pending is None:
        args.max_pending = min(MAX_PENDING, args.jobs * 2)
    if args.max_pending < args.jobs:
        sys.exit("--max-pending must be at least --jobs")
    if not 1 <= args.max_retries <= 100:
        sys.exit("--max-retries must be between 1 and 100")
    # argparse does not apply `type` to defaults, including environment-derived
    # values, so validate them explicitly too.
    try:
        args.interval = parse_interval(str(args.interval))
        args.settle_seconds = parse_nonnegative_duration(str(args.settle_seconds))
        args.render_timeout = parse_timeout(str(args.render_timeout))
        args.exif_timeout = parse_timeout(str(args.exif_timeout))
        args.dng_timeout = parse_timeout(str(args.dng_timeout))
        args.kill_grace_seconds = parse_timeout(str(args.kill_grace_seconds))
    except argparse.ArgumentTypeError as e:
        sys.exit(str(e))
    try:
        args.max_input_mib = parse_max_input_mib(str(args.max_input_mib))
        args.max_scan_entries = parse_max_scan_entries(
            str(args.max_scan_entries)
        )
    except argparse.ArgumentTypeError as e:
        sys.exit(str(e))
    args.max_input_bytes = args.max_input_mib * 1024 * 1024
    if args.skip_existing and args.once:
        sys.exit("--skip-existing is a one-time watch baseline and cannot be used with --once")
    if args.skip_existing and args.overwrite:
        sys.exit("--skip-existing and --overwrite are mutually exclusive")
    if args.legacy_input_root and not args.skip_existing:
        sys.exit("--legacy-input-root requires --skip-existing")
    try:
        args.out.mkdir(parents=True, exist_ok=True)
        args.state_dir.mkdir(parents=True, exist_ok=True)
        args.temp_dir.mkdir(parents=True, exist_ok=True)
        args.out = args.out.resolve(strict=True)
        args.output_root = args.out
        args.output_root_identity = _directory_identity(args.output_root)
        args.state_dir = args.state_dir.resolve(strict=True)
        args.temp_dir = args.temp_dir.resolve(strict=True)
        validate_storage_layout(
            args.input_root, args.out, args.state_dir, args.temp_dir
        )
        with tempfile.NamedTemporaryFile(dir=args.temp_dir):
            pass
    except (OSError, StorageLayoutError) as e:
        sys.exit(f"storage configuration is unsafe or not writable: {e}")
    # Validate only what the chosen format needs.
    icc = b""
    args.render_env = None
    if needs_raster(args):
        if not args.render_bin.exists():
            sys.exit(f"render helper not found: {args.render_bin}\n  build it: bash {HERE/'build.sh'}")
        if not args.profile.exists():
            sys.exit(f"ICC profile not found: {args.profile}\n  re-run: bash {HERE/'build.sh'}  (or set --profile)")
        icc = args.profile.read_bytes()
        args.exiftool = shutil.which("exiftool")
        if not args.exiftool:
            log("warning: outputs will carry no EXIF — brew install exiftool")
        if args.deterministic:
            if sys.platform != "darwin":
                sys.exit("--deterministic is only supported by the native macOS renderer; "
                         "the unmodified Windows SDK under Wine has no supported interposer")
            lib = args.render_bin.with_name("rand_freeze.dylib")
            if not lib.exists():
                sys.exit(f"--deterministic needs {lib}\n  re-run: bash {HERE/'build.sh'}")
            args.render_env = {**os.environ, "DYLD_INSERT_LIBRARIES": str(lib)}
    else:
        args.exiftool = None
        if args.deterministic:
            log("note: --deterministic only affects TIFF/JPEG; DNG is already reproducible")
    args.dng_bin = resolve_dng_engine(args) if "dng" in args.formats else None
    args.process_env = {
        **(args.render_env or os.environ),
        "TMPDIR": str(args.temp_dir),
        "NEF_WATCH_TEMP_DIR": str(args.temp_dir),
    }
    try:
        verify_parser_sandbox(args)
    except (ConversionError, OSError) as error:
        sys.exit(f"external metadata/DNG parser isolation is unavailable: {error}")

    try:
        output_lock = OutputRootLock(args.out, args.state_dir)
    except (OSError, BlockingIOError) as e:
        sys.exit(f"cannot acquire exclusive output ownership for {args.out}: {e}")
    args.output_lock = output_lock
    try:
        try:
            state = StateStore(args.state_dir, input_root=args.input_root)
        except (OSError, sqlite3.Error, StateCapacityError) as e:
            sys.exit(f"cannot open durable state in {args.state_dir}: {e}")
        output_recovery = None
        temp_recovery = None
        try:
            try:
                journal_recovered = recover_publication_transactions(
                    state, args.out
                )
            except (OSError, sqlite3.Error, PublicationRecoveryError) as error:
                state.write_health(
                    "degraded", pending=0, inflight=0, force=True
                )
                sys.exit(
                    "cannot safely recover a durable output publication; "
                    f"outputs and rollback copies were preserved: {error}"
                )
            if journal_recovered:
                log(
                    "startup recovery: reconciled "
                    f"{journal_recovered} durable publication transaction(s)"
                )
            work_age = stale_work_age(args)
            startup_cutoff = time.time()
            temp_recovery = TempArtifactRecovery(
                args.temp_dir, prior_session_cutoff=startup_cutoff
            )
            temp_progress = complete_recovery_sweep(
                temp_recovery,
                0,
                now=startup_cutoff,
                max_entries=args.max_scan_entries,
            )
            output_recovery = OutputArtifactRecovery(
                args.out,
                args.recursive,
                max_sweep_entries=args.max_scan_entries,
                prior_session_cutoff=startup_cutoff,
            )
            recovery_progress = complete_recovery_sweep(
                output_recovery,
                work_age,
                now=startup_cutoff,
                max_entries=args.max_scan_entries,
            )
            args.output_recovery = output_recovery
            args.temp_recovery = temp_recovery
            args.output_recovery_degraded = bool(
                not temp_progress.sweep_complete
                or not recovery_progress.sweep_complete
                or temp_progress.limit_exceeded
                or recovery_progress.limit_exceeded
                or temp_progress.error_count
                or recovery_progress.error_count
                or temp_progress.ambiguous_count
                or recovery_progress.ambiguous_count
            )
            reaped = temp_progress.recovered
            recovered = recovery_progress.recovered
            if reaped or recovered:
                log(
                    f"startup recovery: removed {reaped} stale temp file(s), "
                    f"resolved {recovered} staged/rollback output artifact(s)"
                )
            if args.output_recovery_degraded:
                state.write_health(
                    "degraded", pending=0, inflight=0, force=True
                )
                sys.exit(
                    "temp/output artifact recovery is incomplete, failed, or "
                    "ambiguous; preserved files require review before conversion "
                    "can continue"
                )
            if args.reset_failures:
                acknowledged = state.reset_failures()
                log(f"acknowledged {acknowledged} durable failure record(s)")
            config_fingerprint = render_config_fingerprint(args, icc, args.out)
            STOP_EVENT.clear()
            FATAL_STOP_EVENT.clear()
            previous_handlers = install_stop_signal_handlers()
            try:
                if args.once:
                    return run_once(
                        args, out_dir=args.out, icc=icc, state=state,
                        config_fingerprint=config_fingerprint,
                    )
                return run_watch(
                    args, out_dir=args.out, icc=icc, state=state,
                    config_fingerprint=config_fingerprint,
                )
            finally:
                restore_signal_handlers(previous_handlers)
        finally:
            if output_recovery is not None:
                output_recovery.close()
            if temp_recovery is not None:
                temp_recovery.close()
            state.close()
    finally:
        output_lock.close()


if __name__ == "__main__":
    sys.exit(main())
