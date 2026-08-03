#!/usr/bin/env python3
"""Validate nef-watch's atomic JSON heartbeat for the container healthcheck."""

from __future__ import annotations

import json
import math
import os
import stat
import sys
import time
from pathlib import Path


MAX_HEARTBEAT_BYTES = 64 * 1024
MIN_HEALTH_MAX_AGE_SECONDS = 60.0
MAX_HEALTH_MAX_AGE_SECONDS = 900.0
MIN_WATCH_INTERVAL_SECONDS = 0.1
MAX_WATCH_INTERVAL_SECONDS = 840.0
HEALTH_INTERVAL_MARGIN_SECONDS = 60.0


def fail(message: str) -> None:
    print(f"nef-watch heartbeat invalid: {message}", file=sys.stderr)
    raise SystemExit(1)


def numeric_field(payload: dict[str, object], name: str) -> float:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(f"{name} must be an epoch timestamp")
    result = float(value)
    if not math.isfinite(result):
        fail(f"{name} must be finite")
    return result


def counter_field(payload: dict[str, object], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        fail(f"{name} must be a non-negative integer")
    return value


def positive_integer_field(payload: dict[str, object], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        fail(f"{name} must be a positive integer")
    return value


def process_start_ticks(watcher_pid: int, proc_root: Path) -> int:
    """Read Linux proc field 22 without following a replaced final component."""
    stat_path = proc_root / str(watcher_pid) / "stat"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(stat_path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                fail(f"watcher process stat is not a regular procfs file: {stat_path}")
            chunks: list[bytes] = []
            remaining = 64 * 1024 + 1
            while remaining:
                block = os.read(descriptor, min(remaining, 16 * 1024))
                if not block:
                    break
                chunks.append(block)
                remaining -= len(block)
            payload_bytes = b"".join(chunks)
        finally:
            os.close(descriptor)
    except OSError as exc:
        fail(f"cannot read watcher process stat {stat_path}: {exc}")
    if len(payload_bytes) > 64 * 1024:
        fail("watcher process stat is unexpectedly large")
    try:
        stat_text = payload_bytes.decode("utf-8").strip()
    except UnicodeError as exc:
        fail(f"watcher process stat is not UTF-8: {exc}")
    prefix = f"{watcher_pid} ("
    closing = stat_text.rfind(") ")
    if not stat_text.startswith(prefix) or closing < len(prefix):
        fail("watcher process stat has a malformed pid/comm prefix")
    # Tokens after the final parenthesized comm start at proc field 3. Field 22
    # is therefore index 19, even when comm itself contains spaces or ')'.
    fields = stat_text[closing + 2 :].split()
    if len(fields) <= 19 or len(fields[0]) != 1:
        fail("watcher process stat is missing field 22")
    try:
        actual_start = int(fields[19], 10)
    except ValueError:
        fail("watcher process start time is malformed")
    if actual_start <= 0:
        fail("watcher process start time is invalid")
    return actual_start


def live_watcher_identity(payload: dict[str, object], proc_root: Path) -> None:
    """Bind the heartbeat to one live Linux PID instance without cmdline access."""
    watcher_pid = positive_integer_field(payload, "watcher_pid")
    expected_start = positive_integer_field(payload, "watcher_start_ticks")
    try:
        os.kill(watcher_pid, 0)
    except (OSError, OverflowError) as exc:
        fail(f"watcher process {watcher_pid} is not running: {exc}")

    actual_start = process_start_ticks(watcher_pid, proc_root)
    if actual_start != expected_start:
        fail(
            "watcher process start time does not match the heartbeat "
            f"({actual_start} != {expected_start})"
        )
    try:
        os.kill(watcher_pid, 0)
    except (OSError, OverflowError) as exc:
        fail(f"watcher process {watcher_pid} exited during the health probe: {exc}")
    final_start = process_start_ticks(watcher_pid, proc_root)
    if final_start != expected_start:
        fail("watcher process identity changed during the health probe")


def read_heartbeat(path: Path) -> dict[str, object]:
    """Read the watcher-owned atomic heartbeat through one bounded file descriptor."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_size > MAX_HEARTBEAT_BYTES
            ):
                fail(
                    "heartbeat must be an owner-written, single-link, mode-0600 "
                    "bounded regular file"
                )
            chunks: list[bytes] = []
            remaining = MAX_HEARTBEAT_BYTES + 1
            while remaining:
                block = os.read(descriptor, min(remaining, 16 * 1024))
                if not block:
                    break
                chunks.append(block)
                remaining -= len(block)
            raw = b"".join(chunks)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except FileNotFoundError:
        fail(f"heartbeat file is missing: {path}")
    except OSError as exc:
        fail(f"cannot read heartbeat file {path}: {exc}")
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if (
        len(raw) != before.st_size
        or len(raw) > MAX_HEARTBEAT_BYTES
        or before_identity != after_identity
    ):
        fail("heartbeat file changed while it was read")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot parse heartbeat file {path}: {exc}")
    if not isinstance(value, dict):
        fail("heartbeat root must be an object")
    return value


def main() -> int:
    if len(sys.argv) not in (3, 4):
        fail(
            "usage: check_health_state.py HEALTH_FILE MAX_AGE_SECONDS "
            "[PROC_ROOT]"
        )

    path = Path(sys.argv[1])
    proc_root = Path(sys.argv[3]) if len(sys.argv) == 4 else Path("/proc")
    try:
        max_age = float(sys.argv[2])
    except ValueError:
        fail("maximum heartbeat age must be numeric")
    if (
        not math.isfinite(max_age)
        or not MIN_HEALTH_MAX_AGE_SECONDS
        <= max_age
        <= MAX_HEALTH_MAX_AGE_SECONDS
    ):
        fail(
            "maximum heartbeat age must be between "
            f"{MIN_HEALTH_MAX_AGE_SECONDS:g} and "
            f"{MAX_HEALTH_MAX_AGE_SECONDS:g} seconds"
        )
    try:
        watch_interval = float(os.environ.get("NEF_WATCH_INTERVAL_SECONDS", "3"))
    except ValueError:
        fail("watch interval must be numeric")
    if (
        not math.isfinite(watch_interval)
        or not MIN_WATCH_INTERVAL_SECONDS
        <= watch_interval
        <= MAX_WATCH_INTERVAL_SECONDS
    ):
        fail(
            "watch interval must be between "
            f"{MIN_WATCH_INTERVAL_SECONDS:g} and "
            f"{MAX_WATCH_INTERVAL_SECONDS:g} seconds"
        )
    if max_age < watch_interval + HEALTH_INTERVAL_MARGIN_SECONDS:
        fail(
            "maximum heartbeat age must exceed the watch interval by at least "
            f"{HEALTH_INTERVAL_MARGIN_SECONDS:g} seconds"
        )

    payload = read_heartbeat(path)
    schema = payload.get("schema")
    if isinstance(schema, bool) or schema != 2:
        fail("unsupported heartbeat schema")
    live_watcher_identity(payload, proc_root)

    now = time.time()
    updated_at = numeric_field(payload, "updated_at")
    age = now - updated_at
    if age < -300:
        fail("updated_at is more than five minutes in the future")
    if age > max_age:
        fail(f"heartbeat is stale ({age:.1f}s old; limit {max_age:.1f}s)")

    status = payload.get("status")
    if status not in {"starting", "healthy", "degraded", "stopping"}:
        fail(f"unknown watcher status: {status!r}")
    if status not in {"starting", "healthy"}:
        fail(f"watcher status is {status}")

    for name in ("pending", "inflight", "permanent_failures"):
        counter_field(payload, name)
    if payload["permanent_failures"]:
        fail(f"{payload['permanent_failures']} conversion(s) need attention")

    if payload.get("last_success_at") is not None:
        numeric_field(payload, "last_success_at")

    if status == "healthy":
        last_scan_at = numeric_field(payload, "last_scan_at")
        scan_age = now - last_scan_at
        if scan_age < -300:
            fail("last_scan_at is more than five minutes in the future")
        if scan_age > max_age:
            fail(f"last scan is stale ({scan_age:.1f}s old; limit {max_age:.1f}s)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
