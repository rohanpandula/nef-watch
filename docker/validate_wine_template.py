#!/usr/bin/env python3
"""Validate sealed Wine-template permissions without following its symlinks."""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import NoReturn


EXIT_CONFIG = 78
MAX_TEMPLATE_ENTRIES = 100_000
USER_LINK_TAILS = {
    ("Desktop",),
    ("Documents",),
    ("Downloads",),
    ("Music",),
    ("Pictures",),
    ("Videos",),
    ("AppData", "Roaming", "Microsoft", "Windows", "Templates"),
}


class TemplateValidationError(RuntimeError):
    """The reusable Wine prefix is writable or exposes an unsafe link."""


def _fail(message: str) -> NoReturn:
    raise TemplateValidationError(message)


def _expected_link_target(relative: PurePosixPath, app_home: Path) -> str:
    if relative == PurePosixPath("dosdevices/c:"):
        return "../drive_c"
    parts = relative.parts
    if (
        len(parts) >= 4
        and parts[0:2] == ("drive_c", "users")
        and parts[2] == "nef-watch"
        and parts[3:] in USER_LINK_TAILS
    ):
        return str(app_home)
    _fail(f"Wine template contains an unexpected symbolic link: {relative}")


def validate_template_tree(
    template: Path, app_home: Path, *, expected_template_owner: int = 0
) -> None:
    """Reject writable non-links and allow only exact Wine-managed links."""
    try:
        requested_template = template.absolute()
        requested_home = app_home.absolute()
        if (
            requested_template != requested_template.resolve(strict=True)
            or requested_home != requested_home.resolve(strict=True)
        ):
            _fail("Wine template and application home must be canonical directories")
        template_metadata = requested_template.lstat()
        home_metadata = requested_home.lstat()
    except OSError as exc:
        raise TemplateValidationError(f"cannot inspect Wine template roots: {exc}") from exc
    if (
        not stat.S_ISDIR(template_metadata.st_mode)
        or stat.S_ISLNK(template_metadata.st_mode)
        or template_metadata.st_uid != expected_template_owner
        or template_metadata.st_mode & 0o022
        or not stat.S_ISDIR(home_metadata.st_mode)
        or stat.S_ISLNK(home_metadata.st_mode)
    ):
        _fail(
            "Wine template must be an owner-sealed real directory and "
            "application home must be a real directory"
        )
    if os.access(requested_template, os.W_OK):
        _fail("Wine template root is writable by the watcher")

    template_device = template_metadata.st_dev
    stack = [requested_template]
    entries_seen = 0
    c_drive_links = 0
    while stack:
        directory = stack.pop()
        try:
            entries = os.scandir(directory)
        except OSError as exc:
            raise TemplateValidationError(
                f"cannot enumerate Wine template directory {directory}: {exc}"
            ) from exc
        with entries:
            for entry in entries:
                entries_seen += 1
                if entries_seen > MAX_TEMPLATE_ENTRIES:
                    _fail("Wine template exceeds the bounded entry count")
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise TemplateValidationError(
                        f"cannot inspect Wine template entry {entry.path}: {exc}"
                    ) from exc
                candidate = Path(entry.path)
                relative = PurePosixPath(candidate.relative_to(requested_template))
                if metadata.st_uid != expected_template_owner:
                    _fail(
                        f"Wine template entry has an unexpected owner: {relative}"
                    )
                if stat.S_ISLNK(metadata.st_mode):
                    try:
                        target = os.readlink(candidate)
                    except OSError as exc:
                        raise TemplateValidationError(
                            f"cannot read Wine template link {relative}: {exc}"
                        ) from exc
                    expected = _expected_link_target(relative, requested_home)
                    if target != expected:
                        _fail(
                            f"Wine template link {relative} targets {target!r}, "
                            f"expected {expected!r}"
                        )
                    if relative == PurePosixPath("dosdevices/c:"):
                        c_drive_links += 1
                    continue
                if metadata.st_dev != template_device:
                    _fail(f"Wine template entry crosses a filesystem boundary: {relative}")
                if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
                    _fail(f"Wine template contains a special file: {relative}")
                if metadata.st_mode & 0o022 or os.access(candidate, os.W_OK):
                    _fail(f"Wine template entry is writable by the watcher: {relative}")
                if stat.S_ISDIR(metadata.st_mode):
                    stack.append(candidate)
    if c_drive_links != 1:
        _fail("Wine template must contain exactly one dosdevices/c: link")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="validate sealed Wine-template permissions and symbolic links"
    )
    parser.add_argument("template", type=Path)
    parser.add_argument("app_home", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validate_template_tree(args.template, args.app_home)
    except TemplateValidationError as exc:
        print(f"Wine template validation failed: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    print("Wine template validation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
