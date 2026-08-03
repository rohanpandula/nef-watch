import argparse
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "docker" / "validate_storage.py"
SPEC = importlib.util.spec_from_file_location("validate_storage", MODULE_PATH)
validate_storage = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = validate_storage
SPEC.loader.exec_module(validate_storage)


class HostStorageIsolationTests(unittest.TestCase):
    def test_distinct_sibling_directories_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {}
            for name in ("input", "output", "temporary", "sdk"):
                path = root / name
                path.mkdir()
                paths[name] = path
            resolved = validate_storage.validate_host_paths(
                paths, allow_generic_host_paths=True
            )
            self.assertEqual(set(resolved), set(paths))

    def test_nested_and_symlinked_aliases_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            nested = source / "scratch"
            source.mkdir()
            nested.mkdir()
            with self.assertRaisesRegex(
                validate_storage.StorageIsolationError, "overlap"
            ):
                validate_storage.validate_host_paths(
                    {"input": source, "temporary": nested},
                    allow_generic_host_paths=True,
                )

            alias = root / "alias"
            alias.symlink_to(source, target_is_directory=True)
            with self.assertRaisesRegex(
                validate_storage.StorageIsolationError, "overlap"
            ):
                validate_storage.validate_host_paths(
                    {"input": source, "output": alias},
                    allow_generic_host_paths=True,
                )

    def test_unraid_user_and_direct_views_of_one_share_fail(self):
        original = validate_storage._resolve_existing
        try:
            validate_storage._resolve_existing = lambda _label, value, **_kwargs: value
            with self.assertRaisesRegex(
                validate_storage.StorageIsolationError, "mix /mnt/user"
            ):
                validate_storage.validate_host_paths(
                    {
                        "input": Path("/mnt/user/Photos/NEF"),
                        "output": Path("/mnt/cache/Photos/TIFF"),
                    },
                    allow_generic_host_paths=True,
                )
        finally:
            validate_storage._resolve_existing = original

    def test_production_host_paths_reject_symbolic_link_components(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "source"
            output = root / "output"
            alias = root / "temp-alias"
            source.mkdir()
            output.mkdir()
            alias.symlink_to(source, target_is_directory=True)
            with self.assertRaisesRegex(
                validate_storage.StorageIsolationError, "symbolic-link component"
            ):
                validate_storage.validate_host_paths(
                    {"input": output, "temporary": alias}
                )

    def test_unraid_temp_and_report_must_share_the_mounted_direct_pool(self):
        temporary = Path("/mnt/cache/nef-watch-scratch/production")
        scratch = Path("/mnt/cache/nef-watch-scratch")
        report = scratch / ".trust/acceptance-reports/accepted.json"

        def fake_directory(_label, value, **_kwargs):
            return value

        def fake_stat(path, *args, **kwargs):
            del args, kwargs
            return SimpleNamespace(st_dev=7 if str(path).startswith(str(scratch)) else 8)

        with mock.patch.object(
            validate_storage, "_resolve_existing", side_effect=fake_directory
        ), mock.patch.object(
            validate_storage, "_resolve_existing_file", return_value=report
        ), mock.patch.object(Path, "stat", new=fake_stat):
            result = validate_storage.validate_unraid_temp_binding(
                temporary,
                trusted_scratch_root=scratch,
                acceptance_report=report,
                mountpoint_check=lambda path: path == scratch,
            )
            self.assertEqual(result, (temporary, scratch, report))

            with self.assertRaisesRegex(
                validate_storage.StorageIsolationError, "direct Unraid pool path"
            ):
                validate_storage.validate_unraid_temp_binding(
                    Path("/mnt/user/Scratch/production"),
                    trusted_scratch_root=Path("/mnt/user/Scratch"),
                    acceptance_report=Path("/mnt/user/Scratch/.trust/report.json"),
                    mountpoint_check=lambda _path: True,
                )


class TempFilesystemCapacityTests(unittest.TestCase):
    @staticmethod
    def filesystem(*, total_bytes, available_bytes, fragment_size=4096):
        if total_bytes % fragment_size or available_bytes % fragment_size:
            raise AssertionError("test capacity must be block aligned")
        return SimpleNamespace(
            f_frsize=fragment_size,
            f_blocks=total_bytes // fragment_size,
            f_bavail=available_bytes // fragment_size,
        )

    def test_injected_statvfs_accepts_filesystem_at_configured_ceiling(self):
        maximum = validate_storage.MIN_TEMP_FILESYSTEM_BYTES
        seen = []

        def probe(path):
            seen.append(path)
            return self.filesystem(
                total_bytes=maximum, available_bytes=maximum // 2
            )

        result = validate_storage.validate_temp_filesystem_capacity(
            Path("/bounded-temp"), maximum, statvfs_fn=probe
        )
        self.assertEqual(seen, [Path("/bounded-temp")])
        self.assertEqual(result.total_bytes, maximum)
        self.assertEqual(result.available_bytes, maximum // 2)

    def test_filesystem_larger_than_ceiling_fails_closed(self):
        maximum = validate_storage.MIN_TEMP_FILESYSTEM_BYTES
        filesystem = self.filesystem(
            total_bytes=maximum + 4096, available_bytes=maximum // 2
        )
        with self.assertRaisesRegex(
            validate_storage.StorageIsolationError,
            "dedicated bounded filesystem or tmpfs",
        ):
            validate_storage.validate_temp_filesystem_capacity(
                Path("/unbounded-temp"),
                maximum,
                statvfs_fn=lambda _path: filesystem,
            )

    def test_optional_minimum_free_policy_uses_available_not_root_free_blocks(self):
        maximum = validate_storage.MIN_TEMP_FILESYSTEM_BYTES
        filesystem = self.filesystem(
            total_bytes=maximum, available_bytes=maximum // 2
        )
        with self.assertRaisesRegex(
            validate_storage.StorageIsolationError, "at least"
        ):
            validate_storage.validate_temp_filesystem_capacity(
                Path("/bounded-temp"),
                maximum,
                minimum_free_bytes=maximum,
                statvfs_fn=lambda _path: filesystem,
            )

    def test_minimum_free_must_not_exceed_capacity_ceiling(self):
        maximum = validate_storage.MIN_TEMP_FILESYSTEM_BYTES
        with self.assertRaisesRegex(
            validate_storage.StorageIsolationError, "no larger"
        ):
            validate_storage.validate_temp_filesystem_capacity(
                Path("/bounded-temp"),
                maximum,
                minimum_free_bytes=2 * maximum,
                statvfs_fn=lambda _path: self.filesystem(
                    total_bytes=maximum, available_bytes=maximum
                ),
            )

    def test_invalid_statvfs_values_fail_closed(self):
        invalid = SimpleNamespace(f_frsize=0, f_blocks=1, f_bavail=0)
        with self.assertRaisesRegex(
            validate_storage.StorageIsolationError, "invalid statvfs"
        ):
            validate_storage.validate_temp_filesystem_capacity(
                Path("/temp"),
                validate_storage.MIN_TEMP_FILESYSTEM_BYTES,
                statvfs_fn=lambda _path: invalid,
            )

    def test_capacity_argument_is_integer_and_bounded_to_one_through_64_gib(self):
        self.assertEqual(
            validate_storage.parse_temp_filesystem_bytes(
                str(validate_storage.MIN_TEMP_FILESYSTEM_BYTES)
            ),
            validate_storage.MIN_TEMP_FILESYSTEM_BYTES,
        )
        self.assertEqual(
            validate_storage.parse_temp_filesystem_bytes(
                str(validate_storage.MAX_TEMP_FILESYSTEM_BYTES)
            ),
            validate_storage.MAX_TEMP_FILESYSTEM_BYTES,
        )
        for value in (
            "0",
            "-1",
            str(validate_storage.MIN_TEMP_FILESYSTEM_BYTES - 1),
            str(validate_storage.MAX_TEMP_FILESYSTEM_BYTES + 1),
            "1GiB",
        ):
            with self.subTest(value=value), self.assertRaises(
                argparse.ArgumentTypeError
            ):
                validate_storage.parse_temp_filesystem_bytes(value)

        self.assertEqual(
            validate_storage.parse_temp_minimum_free_bytes(
                str(validate_storage.MIN_TEMP_FILESYSTEM_BYTES)
            ),
            validate_storage.MIN_TEMP_FILESYSTEM_BYTES,
        )
        for value in (
            "0",
            str(validate_storage.MIN_TEMP_FILESYSTEM_BYTES - 1),
            str(validate_storage.MAX_TEMP_FILESYSTEM_BYTES + 1),
            "one-gib",
        ):
            with self.subTest(minimum=value), self.assertRaises(
                argparse.ArgumentTypeError
            ):
                validate_storage.parse_temp_minimum_free_bytes(value)

    def test_environment_default_is_validated_by_argument_parser(self):
        capacity = str(2 * validate_storage.MIN_TEMP_FILESYSTEM_BYTES)
        minimum_free = str(validate_storage.MIN_TEMP_FILESYSTEM_BYTES)
        with mock.patch.dict(
            os.environ,
            {
                "NEF_WATCH_TEMP_CAPACITY_BYTES": capacity,
                "NEF_WATCH_TEMP_MIN_FREE_BYTES": minimum_free,
            },
        ):
            args = validate_storage.build_parser().parse_args([])
        self.assertEqual(args.max_temp_filesystem_bytes, int(capacity))
        self.assertEqual(
            args.min_temp_filesystem_free_bytes, int(minimum_free)
        )

    def test_minimum_free_requires_maximum_capacity(self):
        minimum = str(validate_storage.MIN_TEMP_FILESYSTEM_BYTES)
        with mock.patch.object(
            validate_storage, "validate_host_paths", return_value={
                "temporary": Path("/resolved-temp")
            }
        ), contextlib.redirect_stderr(io.StringIO()):
            result = validate_storage.main(
                ["--allow-generic-host-paths", "--min-temp-filesystem-free-bytes", minimum]
            )
        self.assertEqual(result, validate_storage.EXIT_CONFIG)

    def test_main_enforces_minimum_available_space(self):
        minimum = validate_storage.MIN_TEMP_FILESYSTEM_BYTES
        maximum = 2 * minimum
        filesystem = self.filesystem(
            total_bytes=maximum, available_bytes=minimum // 2
        )
        with mock.patch.object(
            validate_storage, "validate_host_paths", return_value={
                "temporary": Path("/resolved-temp")
            }
        ), mock.patch.object(
            validate_storage.os, "statvfs", return_value=filesystem
        ), contextlib.redirect_stderr(io.StringIO()):
            result = validate_storage.main(
                [
                    "--max-temp-filesystem-bytes",
                    str(maximum),
                    "--min-temp-filesystem-free-bytes",
                    str(minimum),
                    "--allow-generic-host-paths",
                ]
            )
        self.assertEqual(result, validate_storage.EXIT_CONFIG)

    def test_main_applies_capacity_check_to_resolved_temporary_path(self):
        maximum = validate_storage.MIN_TEMP_FILESYSTEM_BYTES
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = []
            for name in ("input", "output", "temporary", "sdk"):
                path = root / name
                path.mkdir()
                paths.extend((f"--{name if name != 'temporary' else 'temp'}", str(path)))
            filesystem = self.filesystem(
                total_bytes=maximum + 4096,
                available_bytes=maximum // 2,
            )
            with mock.patch.object(
                validate_storage.os, "statvfs", return_value=filesystem
            ), contextlib.redirect_stderr(io.StringIO()):
                result = validate_storage.main(
                    paths
                    + [
                        "--allow-generic-host-paths",
                        "--max-temp-filesystem-bytes",
                        str(maximum),
                    ]
                )
        self.assertEqual(result, validate_storage.EXIT_CONFIG)


class ContainerMountIsolationTests(unittest.TestCase):
    def test_runtime_mode_rechecks_live_mounts_and_capacity_without_host_sdk(self):
        maximum = validate_storage.MIN_TEMP_FILESYSTEM_BYTES
        minimum = validate_storage.MIN_TEMP_FILESYSTEM_BYTES
        with mock.patch.object(
            validate_storage,
            "validate_container_mounts",
            return_value={"temporary": Path("/work/nef-watch")},
        ) as mount_check, mock.patch.object(
            validate_storage, "validate_temp_filesystem_capacity"
        ) as capacity_check, contextlib.redirect_stdout(io.StringIO()):
            result = validate_storage.main(
                [
                    "--container",
                    "--runtime",
                    "--input",
                    "/input",
                    "--output",
                    "/output",
                    "--temp",
                    "/work/nef-watch",
                    "--state",
                    "/var/lib/nef-watch/app-state",
                    "--acceptance-report",
                    "/run/nef-watch-acceptance.json",
                    "--max-temp-filesystem-bytes",
                    str(maximum),
                    "--min-temp-filesystem-free-bytes",
                    str(minimum),
                ]
            )
        self.assertEqual(result, 0)
        checked_paths = mount_check.call_args.args[0]
        self.assertEqual(
            checked_paths,
            {
                "input": Path("/input"),
                "output": Path("/output"),
                "temporary": Path("/work/nef-watch"),
                "state": Path("/var/lib/nef-watch/app-state"),
            },
        )
        capacity_check.assert_called_once_with(
            Path("/work/nef-watch"), maximum, minimum_free_bytes=minimum
        )
        self.assertEqual(
            mount_check.call_args.kwargs["acceptance_report"],
            Path("/run/nef-watch-acceptance.json"),
        )

    def test_nested_backing_bind_roots_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input"
            output_path = root / "output"
            input_path.mkdir()
            output_path.mkdir()
            mountinfo = root / "mountinfo"
            mountinfo.write_text(
                "10 1 0:43 /host/photos /INPUT rw - fuse.sshfs host rw\n"
                "11 1 0:43 /host/photos/output /OUTPUT rw - fuse.sshfs host rw\n",
                encoding="utf-8",
            )
            # Patch the namespace paths after real-path validation so this test
            # can exercise synthetic mountinfo without requiring mount privileges.
            original = validate_storage.validate_host_paths
            try:
                validate_storage.validate_host_paths = lambda _paths, **_kwargs: {
                    "input": Path("/INPUT"),
                    "output": Path("/OUTPUT"),
                }
                with self.assertRaisesRegex(
                    validate_storage.StorageIsolationError, "alias on the host"
                ):
                    validate_storage.validate_container_mounts(
                        {"input": input_path, "output": output_path},
                        mountinfo_path=mountinfo,
                    )
            finally:
                validate_storage.validate_host_paths = original

    def test_runtime_rejects_report_on_different_backing_filesystem(self):
        with tempfile.TemporaryDirectory() as directory:
            mountinfo = Path(directory) / "mountinfo"
            mountinfo.write_text(
                "10 1 0:43 /photos /INPUT ro - xfs /dev/sda1 ro\n"
                "11 1 0:44 /scratch/production /WORK rw - zfs fast/scratch rw\n"
                "12 1 0:45 /scratch/.trust/report.json /REPORT ro - tmpfs tmpfs ro\n",
                encoding="utf-8",
            )
            with mock.patch.object(
                validate_storage,
                "validate_host_paths",
                return_value={
                    "input": Path("/INPUT"),
                    "temporary": Path("/WORK"),
                },
            ), mock.patch.object(
                validate_storage,
                "_resolve_existing_file",
                return_value=Path("/REPORT"),
            ):
                with self.assertRaisesRegex(
                    validate_storage.StorageIsolationError,
                    "acceptance report.*backing filesystem",
                ):
                    validate_storage.validate_container_mounts(
                        {
                            "input": Path("/unused-input"),
                            "temporary": Path("/unused-temp"),
                        },
                        mountinfo_path=mountinfo,
                        acceptance_report=Path("/unused-report"),
                    )

    def test_mountinfo_octal_escapes_are_decoded(self):
        mounts = validate_storage.parse_mountinfo(
            ["10 1 0:43 /host/Photo\\040Work /work rw - ext4 /dev/sda rw\n"]
        )
        self.assertEqual(str(mounts[0].root), "/host/Photo Work")

    def test_exact_mountpoint_uses_mountinfo_even_for_same_device_bind(self):
        with tempfile.TemporaryDirectory() as directory:
            mountinfo = Path(directory) / "mountinfo"
            mountinfo.write_text(
                "10 1 0:43 /pool/scratch /mnt/cache/scratch rw - xfs /dev/nvme0n1 rw\n",
                encoding="utf-8",
            )
            self.assertTrue(
                validate_storage._is_exact_mountpoint(
                    Path("/mnt/cache/scratch"), mountinfo
                )
            )
            self.assertFalse(
                validate_storage._is_exact_mountpoint(Path("/mnt/cache"), mountinfo)
            )


if __name__ == "__main__":
    unittest.main()
