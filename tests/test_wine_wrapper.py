from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "tool" / "nef_render_wine.sh"
SCHEMA = "wine8-deb12-vc14-cc0ff0eb1dc3-landlock6-v3"


class WineWrapperStaticTests(unittest.TestCase):
    def test_render_file_limit_never_exceeds_container_hard_limit(self) -> None:
        wrapper = WRAPPER.read_text(encoding="utf-8")
        self.assertIn("file_size_mib > 2048", wrapper)
        self.assertNotIn("file_size_mib > 4096", wrapper)

    def test_landlock_does_not_allow_the_whole_device_tree(self) -> None:
        wrapper = WRAPPER.read_text(encoding="utf-8")
        self.assertNotIn("for write_path in /dev ", wrapper)
        self.assertNotIn("/dev/shm", wrapper)
        for device in ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom"):
            self.assertIn(device, wrapper)

    def test_landlock_uses_the_bounded_private_tmp_namespace(self) -> None:
        wrapper = WRAPPER.read_text(encoding="utf-8")
        helper = (ROOT / "tool" / "nef_wine_sandbox.sh").read_text(encoding="utf-8")
        landlock_setup = wrapper.split("landlock_command=()", 1)[1].split(
            "set +e", 1
        )[0]
        wine_read_loop = next(
            line
            for line in landlock_setup.splitlines()
            if line.strip().startswith("for read_path in /usr")
        )
        xvfb_read_loop = next(
            line
            for line in helper.splitlines()
            if line.strip().startswith("for read_path in /usr")
        )
        self.assertNotIn("/tmp", landlock_setup)
        self.assertIn(" /proc ", wine_read_loop)
        self.assertNotIn("/proc/self", wine_read_loop)
        self.assertNotIn(" /sys", landlock_setup)
        self.assertIn("/tmp", helper)
        self.assertIn("xvfb_domain", helper)
        self.assertIn("/proc/self", xvfb_read_loop)
        self.assertNotIn(" /proc ", helper)
        self.assertNotIn(" /sys", helper)
        self.assertIn("clean_private_tmp_namespace", wrapper)

    def test_subreaper_wraps_the_landlock_domain(self) -> None:
        wrapper = WRAPPER.read_text(encoding="utf-8")
        invocation = wrapper.split('if [[ "$SMOKE_MODE" -eq 1 ]]', 1)[1]
        self.assertLess(
            invocation.index('"$RENDER_SUPERVISOR" --'),
            invocation.index('"${landlock_command[@]}"'),
        )

    def test_landlock_can_execute_the_sealed_sandbox_helper(self) -> None:
        wrapper = WRAPPER.read_text(encoding="utf-8")
        invocation = wrapper.split('if [[ "$SMOKE_MODE" -eq 1 ]]', 1)[1]
        self.assertLess(
            invocation.index('"$WINE_SANDBOX_HELPER"'),
            invocation.index('"${landlock_command[@]}"'),
        )


@unittest.skipUnless(sys.platform.startswith("linux"), "Wine wrapper targets Linux")
class WineWrapperTests(unittest.TestCase):
    def make_fixture(
        self, root: Path, wine_body: str = "printf 'raw' > \"$NEF_WATCH_JOB_ROOT/output/render.raw\"\n"
    ) -> tuple[dict[str, str], Path, Path, Path, Path, Path]:
        work = root / "work"
        work.mkdir()
        x_socket_dir = root / "x11"
        x_socket_dir.mkdir()
        source = work / "nef-watch-input-sample.nef"
        source.write_bytes(b"II*\0stable-nef")
        raw = work / "nef-watch-render-sample.raw"
        raw.touch()

        runtime = root / "runtime"
        (runtime / "Profiles").mkdir(parents=True)
        renderer = runtime / "nef_render.exe"
        renderer.write_bytes(b"MZ")
        renderer.chmod(0o555)
        profile = runtime / "Profiles" / "NKsRGB.icm"
        profile.write_bytes(b"profile")
        profile.chmod(0o444)

        template = root / "template"
        (template / "drive_c" / "windows" / "system32").mkdir(parents=True)
        installed_profiles = (
            template
            / "drive_c"
            / "Program Files"
            / "Common Files"
            / "Nikon"
            / "Profiles"
        )
        installed_profiles.mkdir(parents=True)
        (installed_profiles / profile.name).write_bytes(profile.read_bytes())
        (template / "dosdevices").mkdir()
        (template / "dosdevices" / "c:").symlink_to("../drive_c")
        (template / ".nef-watch-template-ready").write_text(
            f"{SCHEMA}\n", encoding="ascii"
        )

        tools = root / "tools"
        tools.mkdir()
        fake_wineserver = tools / "wineserver"
        fake_wineserver.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        fake_wineserver.chmod(0o755)
        fake_wine = root / "wine"
        fake_wine.write_text(f"#!/usr/bin/env bash\n{wine_body}", encoding="utf-8")
        fake_wine.chmod(0o755)
        fake_sandbox = tools / "wine-sandbox"
        fake_sandbox.write_text("#!/usr/bin/env bash\nexec \"$@\"\n", encoding="utf-8")
        fake_sandbox.chmod(0o755)
        fake_supervisor = tools / "render-supervisor"
        fake_supervisor.write_text(
            "import os, sys\n"
            "arguments = sys.argv[1:]\n"
            "if arguments and arguments[0] == '--': arguments.pop(0)\n"
            "os.execvp(arguments[0], arguments)\n",
            encoding="utf-8",
        )
        fake_supervisor.chmod(0o755)

        env = {
            **os.environ,
            "PATH": f"{tools}:{os.environ.get('PATH', '')}",
            "NIKON_RUNTIME_DIR": str(runtime),
            "NEF_WATCH_WINE_TEMPLATE": str(template),
            "NEF_WATCH_WINE_SCHEMA": SCHEMA,
            "NEF_WATCH_TEMP_DIR": str(work),
            "NEF_WATCH_X_SOCKET_DIR": str(x_socket_dir),
            "NEF_WATCH_TEST_ALLOW_UNSEALED_TEMPLATE": "1",
            "NEF_WATCH_REQUIRE_LANDLOCK": "0",
            "NEF_WATCH_WINE_SANDBOX_HELPER": str(fake_sandbox),
            "NEF_WATCH_RENDER_SUPERVISOR": str(fake_supervisor),
            "NEF_WATCH_RENDER_MEMORY_MIB": "1536",
            "NEF_WATCH_SDK_MEMORY_MIB": "512",
            "NEF_WATCH_RENDER_JOBS": "1",
            "WINE_BIN": str(fake_wine),
        }
        return env, work, source, raw, profile, template

    def run_render(
        self,
        env: dict[str, str],
        source: Path,
        raw: Path,
        profile: Path,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(WRAPPER), str(source), str(raw), str(profile)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

    def test_uses_disposable_prefix_with_only_c_t_r_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / "capture"
            body = r'''
{
  printf 'prefix=%s\n' "$WINEPREFIX"
  printf 'work=%s\n' "$NEF_WATCH_TEMP_DIR"
  printf 'wintemp=%s\n' "$NEF_WATCH_WINE_TEMP_DIR"
  printf 'swap=%s\n' "$NEF_WATCH_WINE_SWAP_PATH"
  printf 'fsize=%s\n' "$(ulimit -f)"
  printf 'cwd=%s\n' "$PWD"
  for map in "$WINEPREFIX"/dosdevices/*; do printf 'map=%s\n' "${map##*/}"; done
  printf 'arg=%s\n' "$@"
} > "$CAPTURE"
printf 'rendered' > "$NEF_WATCH_JOB_ROOT/output/render.raw"
'''
            env, work, source, raw, profile, _ = self.make_fixture(root, body)
            env["CAPTURE"] = str(capture)

            result = self.run_render(env, source, raw, profile)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(raw.read_bytes(), b"rendered")
            captured = capture.read_text(encoding="utf-8")
            self.assertIn("map=c:\n", captured)
            self.assertIn("map=r:\n", captured)
            self.assertIn("map=t:\n", captured)
            self.assertNotIn("map=z:\n", captured.lower())
            self.assertIn("arg=R:\\nef_render.exe\n", captured)
            self.assertIn("arg=T:\\input\\source.nef\n", captured)
            self.assertIn("arg=T:\\output\\render.raw\n", captured)
            self.assertIn("arg=R:\\Profiles\\NKsRGB.icm\n", captured)
            self.assertIn("wintemp=T:\\swap\n", captured)
            self.assertIn("swap=T:\\swap\\nkr-nef-watch.", captured)
            self.assertIn("fsize=2097152\n", captured)
            self.assertIn(f"cwd={root / 'runtime'}\n", captured)
            self.assertEqual(list(work.glob("nef-watch-job.*")), [])

    def test_removes_renderer_artifacts_from_private_x_socket_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            body = r'''
touch "$NEF_WATCH_X_SOCKET_DIR/X-poison-for-next-render"
mkdir -m 000 "$NEF_WATCH_X_SOCKET_DIR/blocked"
ln -s "$X_SOCKET_SENTINEL" "$NEF_WATCH_X_SOCKET_DIR/outside-link"
printf 'raw' > "$NEF_WATCH_JOB_ROOT/output/render.raw"
'''
            env, _, source, raw, profile, _ = self.make_fixture(root, body)
            x_socket_dir = Path(env["NEF_WATCH_X_SOCKET_DIR"])
            sentinel = root / "outside-sentinel"
            sentinel.write_text("unchanged", encoding="ascii")
            sentinel.chmod(0o400)
            before_mode = sentinel.stat().st_mode
            env["X_SOCKET_SENTINEL"] = str(sentinel)

            result = self.run_render(env, source, raw, profile)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(x_socket_dir.iterdir()), [])
            self.assertEqual(sentinel.read_text(encoding="ascii"), "unchanged")
            self.assertEqual(sentinel.stat().st_mode, before_mode)

    def test_rejects_unbounded_render_file_limit_before_wine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            started = root / "started"
            env, _, source, raw, profile, _ = self.make_fixture(
                root, f"touch {started}\n"
            )
            env["NEF_WATCH_RENDER_FILE_SIZE_MIB"] = "2049"

            result = self.run_render(env, source, raw, profile)

            self.assertEqual(result.returncode, 64, result.stderr)
            self.assertIn("RENDER_FILE_SIZE_MIB", result.stderr)
            self.assertFalse(started.exists())

    def test_total_memory_budget_is_clamped_once_and_sdk_is_a_subset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / "capture"
            body = r'''
printf 'total=%s\nsdk=%s\n' "$NEF_WATCH_RENDER_MEMORY_MIB" "$NEF_WATCH_SDK_MEMORY_MIB" > "$CAPTURE"
printf 'raw' > "$NEF_WATCH_JOB_ROOT/output/render.raw"
'''
            env, _, source, raw, profile, _ = self.make_fixture(root, body)
            cgroup = root / "cgroup"
            cgroup.mkdir()
            (cgroup / "memory.max").write_text(str(1408 * 1024**2), encoding="ascii")
            (cgroup / "memory.current").write_text(str(256 * 1024**2), encoding="ascii")
            env.update(
                {
                    "CAPTURE": str(capture),
                    "NEF_WATCH_CGROUP_ROOT": str(cgroup),
                    "NEF_WATCH_RENDER_MEMORY_MIB": "1024",
                    "NEF_WATCH_SDK_MEMORY_MIB": "512",
                    "NEF_WATCH_RENDER_JOBS": "1",
                }
            )

            result = self.run_render(env, source, raw, profile)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(capture.read_text(encoding="utf-8"), "total=768\nsdk=256\n")

    def test_rejects_more_than_one_render_job_before_wine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            started = root / "started"
            env, _, source, raw, profile, _ = self.make_fixture(
                root, f"touch {started}\n"
            )
            env["NEF_WATCH_RENDER_JOBS"] = "2"

            result = self.run_render(env, source, raw, profile)

            self.assertEqual(result.returncode, 64, result.stderr)
            self.assertIn("exactly 1", result.stderr)
            self.assertFalse(started.exists())

    def test_detects_input_mutation_and_does_not_publish_raw(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            body = r'''
printf 'changed' > "$SOURCE_PATH"
printf 'raw' > "$NEF_WATCH_JOB_ROOT/output/render.raw"
'''
            env, _, source, raw, profile, _ = self.make_fixture(root, body)
            env["SOURCE_PATH"] = str(source)

            result = self.run_render(env, source, raw, profile)

            self.assertEqual(result.returncode, 75, result.stderr)
            self.assertIn("input snapshot changed during", result.stderr)
            self.assertFalse(raw.exists())

    def test_refuses_recreated_raw_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            body = r'''
printf 'attacker' > "$RAW_PATH"
printf 'raw' > "$NEF_WATCH_JOB_ROOT/output/render.raw"
'''
            env, _, source, raw, profile, _ = self.make_fixture(root, body)
            env["RAW_PATH"] = str(raw)

            result = self.run_render(env, source, raw, profile)

            self.assertEqual(result.returncode, 75, result.stderr)
            self.assertIn("destination was recreated", result.stderr)
            self.assertEqual(raw.read_bytes(), b"attacker")

    def test_rejects_forbidden_template_mapping_before_wine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            started = root / "started"
            body = f"touch {started}\n"
            env, _, source, raw, profile, template = self.make_fixture(root, body)
            (template / "dosdevices" / "z:").symlink_to("/")

            result = self.run_render(env, source, raw, profile)

            self.assertEqual(result.returncode, 78, result.stderr)
            self.assertIn("mapping other than C", result.stderr)
            self.assertFalse(started.exists())

    def test_rejects_missing_private_nikon_profile_installation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            started = root / "started"
            env, _, source, raw, profile, template = self.make_fixture(
                root, f"touch {started}\n"
            )
            installed = (
                template
                / "drive_c"
                / "Program Files"
                / "Common Files"
                / "Nikon"
                / "Profiles"
                / profile.name
            )
            installed.unlink()

            result = self.run_render(env, source, raw, profile)

            self.assertEqual(result.returncode, 78, result.stderr)
            self.assertIn("profile", result.stderr.casefold())
            self.assertFalse(started.exists())

    def test_rejects_symbolic_link_input_before_wine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            started = root / "started"
            env, work, source, raw, profile, _ = self.make_fixture(
                root, f"touch {started}\n"
            )
            target = work / "target.nef"
            target.write_bytes(source.read_bytes())
            source.unlink()
            source.symlink_to(target)

            result = self.run_render(env, source, raw, profile)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("symbolic link", result.stderr)
            self.assertFalse(started.exists())

    def test_landlock_is_required_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env, _, source, raw, profile, _ = self.make_fixture(root)
            env.pop("NEF_WATCH_REQUIRE_LANDLOCK")
            env.pop("NEF_WATCH_X_SOCKET_DIR")
            default_x_socket = Path("/tmp/.X11-unix")
            created_default_x_socket = not default_x_socket.exists()
            default_x_socket.mkdir(mode=0o700, exist_ok=True)
            if created_default_x_socket:
                self.addCleanup(default_x_socket.rmdir)
            env["NEF_WATCH_LANDLOCK_EXEC"] = str(root / "missing-landlock")

            result = self.run_render(env, source, raw, profile)

            self.assertEqual(result.returncode, 77, result.stderr)
            self.assertIn("required sealed Landlock launcher", result.stderr)

    def test_stale_disposable_job_is_pruned_without_touching_other_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env, work, source, raw, profile, _ = self.make_fixture(root)
            stale = work / "nef-watch-job.ABCDEFGH"
            stale.mkdir()
            (stale / "owned").write_text("old", encoding="utf-8")
            unrelated = work / "keep-me"
            unrelated.mkdir()
            old = time.time() - 20 * 60
            os.utime(stale, (old, old))

            result = self.run_render(env, source, raw, profile)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(stale.exists())
            self.assertTrue(unrelated.exists())


if __name__ == "__main__":
    unittest.main()
