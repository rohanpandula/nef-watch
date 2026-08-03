from __future__ import annotations

import importlib.util
import errno
import json
import os
import platform
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "docker" / "landlock_exec.py"
SPEC = importlib.util.spec_from_file_location("nef_watch_landlock", LAUNCHER)
assert SPEC is not None and SPEC.loader is not None
landlock = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = landlock
SPEC.loader.exec_module(landlock)


class LandlockLauncherTests(unittest.TestCase):
    def test_seccomp_filter_covers_metadata_mutation_and_io_uring(self) -> None:
        expected = {
            "chmod": 90,
            "fchmod": 91,
            "chown": 92,
            "fchown": 93,
            "lchown": 94,
            "utime": 132,
            "setxattr": 188,
            "lsetxattr": 189,
            "fsetxattr": 190,
            "removexattr": 197,
            "lremovexattr": 198,
            "fremovexattr": 199,
            "utimes": 235,
            "fchownat": 260,
            "futimesat": 261,
            "fchmodat": 268,
            "utimensat": 280,
            "io_uring_setup": 425,
            "io_uring_enter": 426,
            "io_uring_register": 427,
            "fchmodat2": 452,
        }

        self.assertEqual(dict(landlock.DENIED_SYSCALLS), expected)
        filters = landlock.build_metadata_seccomp_filter()
        self.assertEqual(filters[0].code, landlock.BPF_LD_W_ABS)
        self.assertEqual(filters[0].k, landlock.SECCOMP_DATA_ARCH_OFFSET)
        self.assertEqual(filters[1].code, landlock.BPF_JMP_JEQ_K)
        self.assertEqual(filters[1].k, landlock.AUDIT_ARCH_X86_64)
        self.assertEqual(filters[2].k, landlock.SECCOMP_RET_KILL_PROCESS)
        self.assertEqual(filters[3].k, landlock.SECCOMP_DATA_NR_OFFSET)
        self.assertEqual(filters[4].code, landlock.BPF_JMP_JSET_K)
        self.assertEqual(filters[4].k, landlock.X32_SYSCALL_BIT)
        self.assertEqual(filters[5].k, landlock.SECCOMP_RET_ERRNO | errno.EPERM)
        compared = {
            instruction.k
            for instruction in filters
            if instruction.code == landlock.BPF_JMP_JEQ_K
        }
        self.assertEqual(
            compared - {landlock.AUDIT_ARCH_X86_64}, set(expected.values())
        )
        self.assertEqual(filters[-1].k, landlock.SECCOMP_RET_ALLOW)

    def test_seccomp_installation_fails_closed_on_wrong_platform_or_prctl_error(
        self,
    ) -> None:
        with mock.patch.object(landlock.platform, "system", return_value="Darwin"):
            with self.assertRaisesRegex(landlock.SeccompError, "linux/amd64"):
                landlock.install_metadata_seccomp(object())
        with mock.patch.object(landlock.platform, "system", return_value="Linux"), \
             mock.patch.object(landlock.platform, "machine", return_value="aarch64"):
            with self.assertRaisesRegex(landlock.SeccompError, "linux/amd64"):
                landlock.install_metadata_seccomp(object())

        library = mock.Mock()
        library.prctl.return_value = -1
        with mock.patch.object(landlock.platform, "system", return_value="Linux"), \
             mock.patch.object(landlock.platform, "machine", return_value="x86_64"), \
             mock.patch.object(landlock.ctypes, "get_errno", return_value=errno.EPERM):
            with self.assertRaisesRegex(landlock.SeccompError, "seccomp"):
                landlock.install_metadata_seccomp(library)

    def test_filesystem_restriction_stacks_seccomp_before_returning(self) -> None:
        library = mock.Mock()
        library.prctl.return_value = 0
        with mock.patch.object(landlock.platform, "system", return_value="Linux"), \
             mock.patch.object(landlock.platform, "machine", return_value="x86_64"), \
             mock.patch.object(landlock, "_libc", return_value=library), \
             mock.patch.object(landlock, "landlock_abi", return_value=6), \
             mock.patch.object(landlock, "_syscall", side_effect=(71, 0)), \
             mock.patch.object(landlock.os, "close"), \
             mock.patch.object(landlock, "install_metadata_seccomp") as install:
            self.assertEqual(landlock.restrict_filesystem([], []), 6)

        install.assert_called_once_with(library)

    def test_ruleset_requests_signal_scope(self) -> None:
        attribute = landlock.RulesetAttr(
            handled_access_fs=0,
            handled_access_net=0,
            scoped=landlock.LANDLOCK_SCOPE_SIGNAL,
        )
        self.assertEqual(attribute.scoped, landlock.LANDLOCK_SCOPE_SIGNAL)
        self.assertEqual(landlock.MINIMUM_LANDLOCK_ABI, 6)

    def test_rights_are_bounded_by_reported_kernel_abi(self) -> None:
        self.assertFalse(landlock.handled_rights(1) & landlock.ACCESS_REFER)
        self.assertTrue(landlock.handled_rights(2) & landlock.ACCESS_REFER)
        self.assertFalse(landlock.handled_rights(2) & landlock.ACCESS_TRUNCATE)
        self.assertTrue(landlock.handled_rights(3) & landlock.ACCESS_TRUNCATE)
        self.assertFalse(landlock.handled_rights(4) & landlock.ACCESS_IOCTL_DEV)
        self.assertTrue(landlock.handled_rights(5) & landlock.ACCESS_IOCTL_DEV)

    def test_file_allowlist_masks_out_directory_only_rights(self) -> None:
        handled = landlock.handled_rights(6)
        cases = {
            stat.S_IFREG: (
                landlock.ACCESS_READ_FILE
                | landlock.ACCESS_WRITE_FILE
                | landlock.ACCESS_TRUNCATE
            ),
            stat.S_IFCHR: (
                landlock.ACCESS_READ_FILE
                | landlock.ACCESS_WRITE_FILE
                | landlock.ACCESS_IOCTL_DEV
            ),
            stat.S_IFBLK: (
                landlock.ACCESS_READ_FILE
                | landlock.ACCESS_WRITE_FILE
                | landlock.ACCESS_IOCTL_DEV
            ),
        }
        for file_type, expected in cases.items():
            with self.subTest(file_type=file_type):
                self.assertEqual(
                    landlock.allowed_access_for_mode(
                        file_type | 0o600, writable=True, handled=handled
                    ),
                    expected & handled,
                )
                self.assertFalse(expected & landlock.ACCESS_READ_DIR)
                self.assertFalse(expected & landlock.ACCESS_MAKE_DIR)
                self.assertFalse(expected & landlock.ACCESS_REMOVE_FILE)

            with self.subTest(file_type=file_type), mock.patch.object(
                landlock.os, "open", return_value=41
            ), mock.patch.object(
                landlock.os,
                "fstat",
                return_value=os.stat_result((file_type | 0o600,) + (0,) * 9),
            ), mock.patch.object(
                landlock.os, "close"
            ), mock.patch.object(
                landlock, "_syscall", return_value=0
            ) as syscall:
                landlock.add_path_rule(
                    object(),
                    17,
                    Path("/allowlisted-file"),
                    writable=True,
                    handled=handled,
                )
                attribute = syscall.call_args.args[4]._obj
                self.assertEqual(attribute.allowed_access, expected & handled)

    def test_file_allowlist_uses_least_privilege_read_masks(self) -> None:
        handled = landlock.handled_rights(6)
        self.assertEqual(
            landlock.allowed_access_for_mode(
                stat.S_IFREG | 0o400, writable=False, handled=handled
            ),
            landlock.ACCESS_EXECUTE | landlock.ACCESS_READ_FILE,
        )
        for device_type in (stat.S_IFCHR, stat.S_IFBLK):
            with self.subTest(device_type=device_type):
                self.assertEqual(
                    landlock.allowed_access_for_mode(
                        device_type | 0o400, writable=False, handled=handled
                    ),
                    landlock.ACCESS_READ_FILE,
                )
        with self.assertRaisesRegex(landlock.LandlockError, "directory, regular file"):
            landlock.allowed_access_for_mode(
                stat.S_IFIFO | 0o600, writable=True, handled=handled
            )

    @unittest.skipUnless(sys.platform.startswith("linux"), "Landlock requires Linux")
    def test_actual_ruleset_accepts_a_read_write_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            allowed = Path(directory) / "allowed"
            allowed.write_text("before", encoding="ascii")
            arguments = [sys.executable, str(LAUNCHER)]
            for path in ("/usr", "/etc", "/lib", "/lib64"):
                if Path(path).exists():
                    arguments.extend(("--ro", path))
            arguments.extend(
                (
                    "--rw",
                    str(allowed),
                    "--",
                    "/bin/sh",
                    "-c",
                    f"printf after > {allowed}",
                )
            )

            result = subprocess.run(arguments, capture_output=True, text=True)
            if result.returncode == 77:
                self.skipTest(result.stderr.strip())

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(allowed.read_text(encoding="ascii"), "after")

    @unittest.skipUnless(
        sys.platform.startswith("linux") and platform.machine() in ("x86_64", "amd64"),
        "seccomp probe requires linux/amd64",
    )
    def test_actual_seccomp_filter_denies_every_number_and_allows_file_io(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = r"""
import ctypes
import errno
import importlib.util
import pathlib
import sys

launcher, work_text = sys.argv[1:]
spec = importlib.util.spec_from_file_location("seccomp_probe", launcher)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
library = module._libc()
if library.prctl(module.PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
    raise SystemExit("cannot set no_new_privs")
module.install_metadata_seccomp(library)

for name, number in module.DENIED_SYSCALLS:
    ctypes.set_errno(0)
    result = library.syscall(number, 0, 0, 0, 0, 0, 0)
    error = ctypes.get_errno()
    if result != -1 or error != errno.EPERM:
        raise SystemExit(f"{name} was not denied: result={result}, errno={error}")

ctypes.set_errno(0)
result = library.syscall(module.X32_SYSCALL_BIT, 0, 0, 0, 0, 0, 0)
error = ctypes.get_errno()
if result != -1 or error != errno.EPERM:
    raise SystemExit(f"x32 syscall number was not denied: result={result}, errno={error}")

work = pathlib.Path(work_text)
ordinary = work / "ordinary"
ordinary.write_bytes(b"before")
with ordinary.open("r+b") as stream:
    stream.seek(0)
    stream.write(b"after!")
if ordinary.read_bytes() != b"after!":
    raise SystemExit("ordinary file read/write failed")
ordinary.unlink()
if ordinary.exists():
    raise SystemExit("ordinary file removal failed")
"""
            result = subprocess.run(
                [sys.executable, "-I", "-c", script, str(LAUNCHER), directory],
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Landlock requires Linux")
    def test_actual_ruleset_denies_metadata_inside_and_outside_rw_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = root / "allowed"
            forbidden = root / "forbidden"
            allowed.mkdir()
            forbidden.mkdir()
            allowed_file = allowed / "inside"
            forbidden_file = forbidden / "outside"
            allowed_file.write_bytes(b"inside")
            forbidden_file.write_bytes(b"outside")
            script = r"""
import errno
import os
from pathlib import Path
import sys

allowed_root = Path(sys.argv[1])
ordinary = allowed_root / "ordinary"
ordinary.write_bytes(b"before")
with ordinary.open("r+b") as stream:
    stream.seek(0)
    stream.write(b"after!")
if ordinary.read_bytes() != b"after!":
    raise SystemExit("ordinary Landlock file read/write failed")
ordinary.unlink()
if ordinary.exists():
    raise SystemExit("ordinary Landlock file removal failed")

for path in sys.argv[2:]:
    operations = (
        ("chmod", lambda: os.chmod(path, 0o600)),
        ("chown", lambda: os.chown(path, os.getuid(), os.getgid())),
        ("utime", lambda: os.utime(path, None)),
        ("setxattr", lambda: os.setxattr(path, b"user.nef_watch_probe", b"x")),
        ("removexattr", lambda: os.removexattr(path, b"user.nef_watch_probe")),
    )
    for label, operation in operations:
        try:
            operation()
        except OSError as error:
            if error.errno == errno.EPERM:
                continue
            raise SystemExit(f"{label} returned unexpected errno {error.errno}: {path}")
        raise SystemExit(f"{label} unexpectedly succeeded: {path}")
"""
            arguments = [sys.executable, str(LAUNCHER)]
            for path in ("/usr", "/etc", "/lib", "/lib64"):
                if Path(path).exists():
                    arguments.extend(("--ro", path))
            arguments.extend(
                (
                    "--rw",
                    str(allowed),
                    "--",
                    sys.executable,
                    "-I",
                    "-c",
                    script,
                    str(allowed),
                    str(allowed_file),
                    str(forbidden_file),
                )
            )

            result = subprocess.run(arguments, capture_output=True, text=True)
            if result.returncode == 77:
                self.skipTest(result.stderr.strip())

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(allowed_file.read_bytes(), b"inside")
            self.assertEqual(forbidden_file.read_bytes(), b"outside")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Landlock requires Linux")
    def test_actual_ruleset_accepts_the_null_character_device(self) -> None:
        if not Path("/dev/null").exists():
            self.skipTest("/dev/null is unavailable")
        arguments = [sys.executable, str(LAUNCHER)]
        for path in ("/usr", "/etc", "/lib", "/lib64"):
            if Path(path).exists():
                arguments.extend(("--ro", path))
        arguments.extend(
            ("--rw", "/dev/null", "--", "/bin/sh", "-c", "printf ok >/dev/null")
        )

        result = subprocess.run(arguments, capture_output=True, text=True)
        if result.returncode == 77:
            self.skipTest(result.stderr.strip())

        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Landlock requires Linux")
    def test_actual_ruleset_denies_a_non_allowlisted_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = root / "allowed"
            forbidden = root / "forbidden"
            allowed.mkdir()
            forbidden.mkdir()
            command = (
                f"printf allowed > {allowed / 'created'}; "
                f"printf forbidden > {forbidden / 'created'}"
            )
            arguments = [sys.executable, str(LAUNCHER)]
            for path in ("/usr", "/etc", "/lib", "/lib64"):
                if Path(path).exists():
                    arguments.extend(("--ro", path))
            arguments.extend(("--rw", str(allowed), "--", "/bin/sh", "-c", command))

            result = subprocess.run(arguments, capture_output=True, text=True)
            if result.returncode == 77:
                self.skipTest(result.stderr.strip())

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(
                (allowed / "created").read_text(encoding="utf-8"), "allowed"
            )
            self.assertFalse((forbidden / "created").exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Landlock requires Linux")
    def test_proc_self_rule_does_not_enable_root_magic_link_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            forbidden = Path(directory) / "outside-policy"
            forbidden.write_text("secret", encoding="ascii")
            through_proc_root = Path("/proc/self/root") / forbidden.relative_to("/")
            arguments = [sys.executable, str(LAUNCHER)]
            for path in ("/usr", "/etc", "/lib", "/lib64", "/proc/self"):
                if Path(path).exists():
                    arguments.extend(("--ro", path))
            arguments.extend(
                (
                    "--",
                    "/bin/sh",
                    "-c",
                    f"! cat {through_proc_root}",
                )
            )

            result = subprocess.run(arguments, capture_output=True, text=True)
            if result.returncode == 77:
                self.skipTest(result.stderr.strip())

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("secret", result.stdout)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Landlock requires Linux")
    def test_actual_ruleset_denies_signalling_unconfined_parent(self) -> None:
        # The sacrificial shell is the Landlocked child's parent.  If signal
        # scoping is absent, only that subprocess dies; the test runner is safe.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = root / "allowed"
            allowed.mkdir()
            arguments = [sys.executable, str(LAUNCHER)]
            for path in ("/usr", "/etc", "/lib", "/lib64"):
                if Path(path).exists():
                    arguments.extend(("--ro", path))
            arguments.extend(
                (
                    "--rw",
                    str(allowed),
                    "--",
                    "/bin/sh",
                    "-c",
                    '! kill -TERM "$PPID"',
                )
            )
            env = os.environ.copy()
            env["LANDLOCK_TEST_COMMAND"] = json.dumps(arguments)
            script = """
trap 'exit 99' TERM
python3 - <<'PY'
import os
import json
import subprocess
import sys

command = json.loads(os.environ['LANDLOCK_TEST_COMMAND'])
result = subprocess.run(command, capture_output=True, text=True)
if result.returncode == 77:
    print('SKIP:' + result.stderr.strip())
    raise SystemExit(77)
raise SystemExit(result.returncode)
PY
status=$?
test "$status" -eq 0 || exit "$status"
printf survived
"""
            result = subprocess.run(
                ["/bin/sh", "-c", script],
                capture_output=True,
                text=True,
                env=env,
            )
            if result.returncode == 77:
                self.skipTest(result.stdout.removeprefix("SKIP:").strip())

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "survived")


if __name__ == "__main__":
    unittest.main()
