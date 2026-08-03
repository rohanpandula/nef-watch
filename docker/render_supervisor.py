#!/usr/bin/env python3
"""Run one renderer as a Linux child subreaper and leave no descendants behind."""

from __future__ import annotations

import ctypes
import errno
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


PR_SET_CHILD_SUBREAPER = 36
TERM_GRACE_SECONDS = 5.0
KILL_DRAIN_SECONDS = 5.0
POLL_SECONDS = 0.02
PROC_CHILDREN = Path("/proc/self/task")


class SupervisorError(RuntimeError):
    """The supervisor could not prove that the renderer was drained."""


def enable_subreaper() -> None:
    if not sys.platform.startswith("linux"):
        raise SupervisorError("the render supervisor requires Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise SupervisorError(f"cannot become a child subreaper: {os.strerror(error)}")


def direct_children() -> set[int]:
    children: set[int] = set()
    try:
        tasks = PROC_CHILDREN.iterdir()
    except OSError as exc:
        raise SupervisorError(f"cannot enumerate supervisor threads: {exc}") from exc
    for task in tasks:
        try:
            values = (task / "children").read_text(encoding="ascii").split()
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError) as exc:
            raise SupervisorError(f"cannot enumerate render descendants: {exc}") from exc
        for value in values:
            try:
                child = int(value)
            except ValueError as exc:
                raise SupervisorError("kernel returned a malformed child PID") from exc
            if child > 1:
                children.add(child)
    return children


def descendant_tree(root: int) -> set[int]:
    """Best-effort snapshot used only before the command root has exited."""

    found: set[int] = set()
    pending = [root]
    while pending:
        parent = pending.pop()
        if parent in found or parent <= 1:
            continue
        found.add(parent)
        children_file = Path(f"/proc/{parent}/task/{parent}/children")
        try:
            values = children_file.read_text(encoding="ascii").split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, UnicodeError):
            # The authoritative drain happens after orphaned processes are
            # reparented to this subreaper. A racing /proc snapshot may vanish.
            continue
        for value in values:
            try:
                pending.append(int(value))
            except ValueError:
                continue
    return found


def signal_pids(pids: set[int], signum: int) -> None:
    for pid in sorted(pids):
        descriptor = -1
        try:
            descriptor = os.pidfd_open(pid)
            signal.pidfd_send_signal(descriptor, signum)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            raise SupervisorError(f"cannot signal render descendant {pid}") from exc
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                continue
            raise SupervisorError(f"cannot safely signal render descendant {pid}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def reap_exited_children() -> bool:
    """Reap every available child and report whether any child still exists."""

    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return False
        except InterruptedError:
            continue
        if pid == 0:
            return True


def drain_adopted_children() -> None:
    deadline = time.monotonic() + TERM_GRACE_SECONDS
    while True:
        children = direct_children()
        signal_pids(children, signal.SIGTERM)
        if not reap_exited_children():
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(POLL_SECONDS)

    deadline = time.monotonic() + KILL_DRAIN_SECONDS
    while True:
        children = direct_children()
        signal_pids(children, signal.SIGKILL)
        if not reap_exited_children():
            return
        if time.monotonic() >= deadline:
            raise SupervisorError("render descendants survived SIGKILL drain")
        time.sleep(POLL_SECONDS)


def normalized_status(returncode: int) -> int:
    if returncode < 0:
        return 128 + (-returncode)
    return min(returncode, 255)


def supervise(command: list[str]) -> int:
    if not command:
        raise SupervisorError("render command is required")
    enable_subreaper()

    requested_signal = 0

    def remember_signal(signum: int, _frame: object) -> None:
        nonlocal requested_signal
        if requested_signal == 0:
            requested_signal = signum

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, remember_signal)

    try:
        process = subprocess.Popen(command, start_new_session=True, close_fds=True)
    except FileNotFoundError:
        print(f"render command is unavailable: {command[0]}", file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"cannot start render command: {exc}", file=sys.stderr)
        return 126

    while process.poll() is None and requested_signal == 0:
        time.sleep(POLL_SECONDS)

    if requested_signal:
        # The new session catches ordinary children. The /proc walk additionally
        # catches descendants that called setsid() before the command root exits.
        try:
            os.killpg(process.pid, requested_signal)
        except ProcessLookupError:
            pass
        signal_pids(descendant_tree(process.pid), requested_signal)
        deadline = time.monotonic() + TERM_GRACE_SECONDS
        while process.poll() is None and time.monotonic() < deadline:
            signal_pids(descendant_tree(process.pid), requested_signal)
            time.sleep(POLL_SECONDS)
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            signal_pids(descendant_tree(process.pid), signal.SIGKILL)
        try:
            process.wait(timeout=KILL_DRAIN_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise SupervisorError("render command root survived SIGKILL") from exc
        status = 128 + requested_signal
    else:
        status = normalized_status(process.returncode)

    drain_adopted_children()
    return status


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == "--":
        arguments = arguments[1:]
    try:
        return supervise(arguments)
    except SupervisorError as exc:
        print(f"render supervisor failed: {exc}", file=sys.stderr)
        return 74


if __name__ == "__main__":
    raise SystemExit(main())
