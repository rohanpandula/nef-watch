#!/usr/bin/env python3
"""Compute the canonical public-image source fingerprint without running it.

Normal builds hash a checked-out source tree.  The private acceptance harness uses
``--git-commit`` so it can verify a candidate directly from Git blobs without
checking out, importing, or executing any candidate-controlled file.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath


FIXED_FILES = {
    ".github/workflows/verify_sdk_free_image.py",
    ".dockerignore",
    "requirements.txt",
    "pyproject.toml",
    "tool/nef_watch.py",
    "tool/nef_render_win.cpp",
    "tool/nef_render_wine.sh",
    "tool/nef_wine_sandbox.sh",
    "tool/stage_runtime_acceptance.py",
    "tool/validate_tiffs.py",
    "tool/verify_runtime_acceptance.py",
    "validation/baseline-v1.json",
}
GIT_OBJECT_RE = re.compile(r"^[0-9a-f]{40,64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_SOURCE_FILE_BYTES = 16 * 1024 * 1024
MAX_SOURCE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_GIT_TREE_BYTES = 8 * 1024 * 1024
MAX_GIT_TREE_ENTRIES = 4096


class FingerprintError(RuntimeError):
    """The source tree cannot be fingerprinted safely and deterministically."""


def _included_docker_file(path: str) -> bool:
    relative = PurePosixPath(path)
    if not relative.parts or relative.parts[0] != "docker":
        return False
    if "__pycache__" in relative.parts:
        return False
    if relative.name in {".env", ".DS_Store"}:
        return False
    if relative.suffix in {".pyc", ".pyo"}:
        return False
    return True


def _safe_manifest_path(path: str) -> str:
    if not path or "\x00" in path or "\n" in path or "\r" in path or "\\" in path:
        raise FingerprintError(f"unsafe source path: {path!r}")
    relative = PurePosixPath(path)
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise FingerprintError(f"unsafe source path: {path!r}")
    return relative.as_posix()


def _hash_stream(stream, *, size: int, path: str) -> str:
    if size < 0 or size > MAX_SOURCE_FILE_BYTES:
        raise FingerprintError(
            f"source file exceeds {MAX_SOURCE_FILE_BYTES} bytes: {path}"
        )
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        block = stream.read(min(1024 * 1024, remaining))
        if not block:
            raise FingerprintError(f"source file ended early: {path}")
        digest.update(block)
        remaining -= len(block)
    if stream.read(1):
        raise FingerprintError(f"source file grew while hashing: {path}")
    return digest.hexdigest()


def _manifest_fingerprint(entries: list[tuple[str, str]]) -> str:
    if not entries:
        raise FingerprintError("source manifest is empty")
    manifest = "".join(f"{digest}  {path}\n" for path, digest in sorted(entries))
    return hashlib.sha256(manifest.encode("utf-8")).hexdigest()


def _tree_paths(root: Path) -> list[str]:
    docker_root = root / "docker"
    if not docker_root.is_dir() or docker_root.is_symlink():
        raise FingerprintError(f"missing or unsafe source directory: {docker_root}")
    paths = set(FIXED_FILES)
    for folder_text, directories, files in os.walk(docker_root, followlinks=False):
        folder = Path(folder_text)
        safe_directories = []
        for name in directories:
            candidate = folder / name
            if candidate.is_symlink():
                raise FingerprintError(f"symbolic links are forbidden in docker/: {candidate}")
            if name != "__pycache__":
                safe_directories.append(name)
        directories[:] = safe_directories
        for name in files:
            candidate = folder / name
            relative = candidate.relative_to(root).as_posix()
            if candidate.is_symlink():
                raise FingerprintError(f"symbolic links are forbidden in docker/: {candidate}")
            if _included_docker_file(relative):
                paths.add(relative)
    return sorted(_safe_manifest_path(path) for path in paths)


def fingerprint_tree(root: Path) -> str:
    root = root.expanduser().resolve(strict=True)
    entries: list[tuple[str, str]] = []
    total = 0
    for relative in _tree_paths(root):
        candidate = root.joinpath(*PurePosixPath(relative).parts)
        try:
            metadata = candidate.lstat()
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise FingerprintError(f"missing or escaping fingerprint input: {relative}") from exc
        if not stat.S_ISREG(metadata.st_mode) or candidate.is_symlink():
            raise FingerprintError(f"fingerprint input is not a regular file: {relative}")
        if metadata.st_size > MAX_SOURCE_FILE_BYTES:
            raise FingerprintError(
                f"source file exceeds {MAX_SOURCE_FILE_BYTES} bytes: {relative}"
            )
        total += metadata.st_size
        if total > MAX_SOURCE_TOTAL_BYTES:
            raise FingerprintError("source fingerprint inputs exceed the aggregate size limit")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(candidate, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_size != metadata.st_size:
                raise FingerprintError(f"source file changed before hashing: {relative}")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                digest = _hash_stream(stream, size=opened.st_size, path=relative)
            after = os.fstat(descriptor)
            if (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise FingerprintError(f"source file changed while hashing: {relative}")
        finally:
            os.close(descriptor)
        entries.append((relative, digest))
    return _manifest_fingerprint(entries)


def _git(repo: Path, *arguments: str, capture: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            detail = exc.stderr.decode("utf-8", "replace").strip()
        raise FingerprintError(f"Git source inspection failed: {detail or exc}") from exc


def _git_blob_hash(repo: Path, object_id: str, size: int, path: str) -> str:
    if not GIT_OBJECT_RE.fullmatch(object_id):
        raise FingerprintError(f"malformed Git object ID for {path}")
    process = subprocess.Popen(
        ["git", "-C", str(repo), "cat-file", "blob", object_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    try:
        digest = _hash_stream(process.stdout, size=size, path=path)
    except Exception:
        process.kill()
        process.wait()
        process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        raise
    process.stdout.close()
    stderr = process.stderr.read() if process.stderr is not None else b""
    if process.stderr is not None:
        process.stderr.close()
    return_code = process.wait()
    if return_code != 0:
        raise FingerprintError(
            f"cannot read Git blob for {path}: {stderr.decode('utf-8', 'replace').strip()}"
        )
    return digest


def _bounded_git_tree(repo: Path, commit: str) -> bytes:
    process = subprocess.Popen(
        [
            "git",
            "-C",
            str(repo),
            "ls-tree",
            "-r",
            "-z",
            "-l",
            "--full-tree",
            commit,
            "--",
            "docker",
            *sorted(FIXED_FILES),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    listing = process.stdout.read(MAX_GIT_TREE_BYTES + 1)
    if len(listing) > MAX_GIT_TREE_BYTES:
        process.kill()
        process.wait()
        process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        raise FingerprintError("Git source tree listing exceeds the safety limit")
    process.stdout.close()
    stderr = process.stderr.read(64 * 1024 + 1) if process.stderr is not None else b""
    if process.stderr is not None:
        process.stderr.close()
    return_code = process.wait()
    if return_code != 0:
        raise FingerprintError(
            "Git source tree inspection failed: "
            + stderr[: 64 * 1024].decode("utf-8", "replace").strip()
        )
    return listing


def fingerprint_git_commit(repo: Path, commit: str) -> str:
    if not GIT_COMMIT_RE.fullmatch(commit):
        raise FingerprintError("--git-commit must be a full 40-character lowercase SHA-1")
    repo = repo.expanduser().resolve(strict=True)
    _git(repo, "cat-file", "-e", f"{commit}^{{commit}}", capture=False)
    listing = _bounded_git_tree(repo, commit)
    selected: dict[str, tuple[str, int]] = {}
    records = listing.split(b"\x00")
    if len(records) - 1 > MAX_GIT_TREE_ENTRIES:
        raise FingerprintError("Git source tree contains too many fingerprint entries")
    for record in records:
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode, object_type, object_id, raw_size = header.decode("ascii").split()
            path = _safe_manifest_path(raw_path.decode("utf-8"))
            size = int(raw_size)
        except (UnicodeError, ValueError) as exc:
            raise FingerprintError("malformed or non-UTF-8 Git tree entry") from exc
        relevant = path in FIXED_FILES or _included_docker_file(path)
        if not relevant:
            continue
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise FingerprintError(f"source input is not a regular Git blob: {path}")
        selected[path] = (object_id, size)

    missing = sorted(FIXED_FILES - set(selected))
    if missing:
        raise FingerprintError(f"missing fingerprint input(s): {', '.join(missing)}")
    entries: list[tuple[str, str]] = []
    total = 0
    for path, (object_id, size) in sorted(selected.items()):
        if size < 0 or size > MAX_SOURCE_FILE_BYTES:
            raise FingerprintError(
                f"source file exceeds {MAX_SOURCE_FILE_BYTES} bytes: {path}"
            )
        total += size
        if total > MAX_SOURCE_TOTAL_BYTES:
            raise FingerprintError("source fingerprint inputs exceed the aggregate size limit")
        entries.append((path, _git_blob_hash(repo, object_id, size, path)))
    return _manifest_fingerprint(entries)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="checked-out source root (default: repository containing this script)",
    )
    parser.add_argument("--git-commit", help="hash a commit's Git blobs without checkout")
    parser.add_argument("--repo", type=Path, help="Git repository used with --git-commit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.git_commit:
            if args.repo is None:
                raise FingerprintError("--repo is required with --git-commit")
            result = fingerprint_git_commit(args.repo, args.git_commit)
        else:
            if args.repo is not None:
                raise FingerprintError("--repo requires --git-commit")
            result = fingerprint_tree(args.root)
    except (OSError, FingerprintError) as exc:
        print(f"source fingerprint failed: {exc}", file=sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
