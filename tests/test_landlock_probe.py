import errno
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "docker" / "landlock_probe.py"
SPEC = importlib.util.spec_from_file_location("landlock_probe", MODULE_PATH)
landlock_probe = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = landlock_probe
SPEC.loader.exec_module(landlock_probe)


class LandlockProbeTests(unittest.TestCase):
    def test_confined_probe_requires_eperm_for_every_metadata_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            allowed = root / "allowed"
            allowed.mkdir()
            (allowed / "metadata-target").write_bytes(b"inside")
            outside = root / "outside"
            outside.write_bytes(b"outside")

            denied = PermissionError(errno.EPERM, "denied")
            with mock.patch.object(
                landlock_probe, "_expect_access_denied"
            ) as access_denied, \
                mock.patch.object(os, "chmod", side_effect=denied) as chmod, \
                mock.patch.object(os, "chown", side_effect=denied) as chown, \
                mock.patch.object(os, "utime", side_effect=denied) as utime, \
                mock.patch.object(
                    os, "setxattr", side_effect=denied, create=True
                ) as setxattr, \
                mock.patch.object(
                    os, "removexattr", side_effect=denied, create=True
                ) as removexattr:
                self.assertEqual(landlock_probe.confined_probe(allowed, outside), 0)

            self.assertEqual(access_denied.call_count, 2)
            self.assertIn("ordinary read outside", access_denied.call_args_list[0].args[0])
            self.assertIn(
                "ordinary create/write outside",
                access_denied.call_args_list[1].args[0],
            )
            for operation in (chmod, chown, utime, setxattr, removexattr):
                self.assertEqual(operation.call_count, 2)
            self.assertFalse((allowed / "ordinary-io").exists())

    def test_metadata_probe_rejects_success_or_the_wrong_errno(self):
        with self.assertRaisesRegex(landlock_probe.ProbeError, "unexpectedly succeeded"):
            landlock_probe._expect_eperm("chmod", lambda: None)
        with self.assertRaisesRegex(landlock_probe.ProbeError, "expected EPERM"):
            landlock_probe._expect_eperm(
                "setxattr",
                lambda: (_ for _ in ()).throw(OSError(errno.EACCES, "denied")),
            )

    def test_filesystem_probe_rejects_success_and_accepts_access_denial(self):
        with self.assertRaisesRegex(landlock_probe.ProbeError, "unexpectedly succeeded"):
            landlock_probe._expect_access_denied("read", lambda: None)
        for error_number in (errno.EACCES, errno.EPERM):
            with self.subTest(errno=error_number):
                self.assertIsNone(
                    landlock_probe._expect_access_denied(
                        "read",
                        lambda error_number=error_number: (
                            _ for _ in ()
                        ).throw(OSError(error_number, "denied")),
                    )
                )

    @unittest.skipUnless(
        sys.platform.startswith("linux") and os.uname().machine in ("x86_64", "amd64"),
        "seccomp-only regression requires linux/amd64",
    )
    def test_seccomp_without_landlock_cannot_pass_confined_probe(self):
        launcher = ROOT / "docker" / "landlock_exec.py"
        script = r"""
import importlib.util
import os
import sys

launcher, probe, allowed, outside = sys.argv[1:]
spec = importlib.util.spec_from_file_location("seccomp_only", launcher)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
library = module._libc()
if library.prctl(module.PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
    raise SystemExit("cannot set no_new_privs")
module.install_metadata_seccomp(library)
os.execv(
    sys.executable,
    [sys.executable, "-I", probe, "--confined", allowed, outside],
)
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = root / "allowed"
            allowed.mkdir()
            (allowed / "metadata-target").write_bytes(b"inside")
            outside = root / "outside"
            outside.write_bytes(b"outside")
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    script,
                    os.fspath(launcher),
                    os.fspath(MODULE_PATH),
                    os.fspath(allowed),
                    os.fspath(outside),
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )

        self.assertEqual(result.returncode, landlock_probe.EXIT_CONFIG)
        self.assertIn("ordinary read outside", result.stderr)

    def test_parent_probe_uses_only_the_sealed_launcher_and_disposable_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            launcher = Path("/usr/local/libexec/nef-watch-landlock-exec.py")
            completed = subprocess.CompletedProcess([], 0, "", "")
            with mock.patch.object(
                landlock_probe, "_trusted_launcher", return_value=launcher
            ), mock.patch.object(
                landlock_probe, "_trusted_image_file", return_value=MODULE_PATH
            ), mock.patch.object(
                landlock_probe.subprocess, "run", return_value=completed
            ) as run:
                self.assertEqual(landlock_probe.run_probe(root), 0)

            command = run.call_args.args[0]
            self.assertEqual(command[0:4], [sys.executable, "-I", os.fspath(launcher), "--ro"])
            self.assertIn("--rw", command)
            self.assertIn("--confined", command)
            self.assertEqual(list(root.iterdir()), [])

    def test_parent_probe_fails_when_disposable_tree_cannot_be_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            completed = subprocess.CompletedProcess([], 0, "", "")
            with mock.patch.object(
                landlock_probe,
                "_trusted_launcher",
                return_value=Path("/usr/local/libexec/nef-watch-landlock-exec.py"),
            ), mock.patch.object(
                landlock_probe, "_trusted_image_file", return_value=MODULE_PATH
            ), mock.patch.object(
                landlock_probe.subprocess, "run", return_value=completed
            ), mock.patch.object(
                landlock_probe.shutil,
                "rmtree",
                side_effect=PermissionError(errno.EPERM, "denied"),
            ):
                with self.assertRaisesRegex(
                    landlock_probe.ProbeError, "cleanup failed"
                ):
                    landlock_probe.run_probe(root)


if __name__ == "__main__":
    unittest.main()
