from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "docker" / "bootstrap_sdk.py"
SPEC = importlib.util.spec_from_file_location("bootstrap_sdk", MODULE_PATH)
bootstrap_sdk = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = bootstrap_sdk
SPEC.loader.exec_module(bootstrap_sdk)


class SdkBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.sdk = self.root / "sdk"
        self.state = self.root / "state"
        self.adapter = self.root / "nef_render_win.cpp"
        self.adapter.write_text("int main() { return 0; }\n", encoding="utf-8")
        self.compiler_log = self.root / "compiler.log"
        self.compiler = self.root / "fake-mingw"
        self.compiler.write_text(
            """#!/bin/sh
if [ "${1:-}" = "--version" ]; then
  echo 'fake-mingw 1.0'
  exit 0
fi
sleep "${FAKE_COMPILER_DELAY:-0}"
printf '%s\n' "$*" >> "$FAKE_COMPILER_LOG"
out=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = '-o' ]; then
    shift
    out=$1
    break
  fi
  shift
done
[ -n "$out" ] || exit 2
printf 'MZfake-adapter' > "$out"
""",
            encoding="utf-8",
        )
        self.compiler.chmod(0o755)
        self.old_compiler_log = os.environ.get("FAKE_COMPILER_LOG")
        os.environ["FAKE_COMPILER_LOG"] = str(self.compiler_log)

        self.files = {
            "Include/Nkfl_Interface.h": b"fake-header",
            "Bin/x64/Release/NkImgSDK.dll": b"sdk-dll",
            "Bin/x64/Release/Elm.dll": b"elm-dll",
            "Bin/x64/Release/Elm.nlf": b"elm-nlf",
            "Bin/x64/Release/RCSigProc.dll": b"rc-sig",
            "Bin/x64/Release/tbb.dll": b"tbb",
            "Bin/x64/Release/tbbmalloc.dll": b"tbbmalloc",
            "Bin/x64/Release/prm.bin": b"parameters",
            "Profiles/NKsRGB.icm": b"nikon-srgb",
        }
        manifest_lines = []
        for relative, content in self.files.items():
            path = self.sdk / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            manifest_lines.append(f"{hashlib.sha256(content).hexdigest()}  {relative}")
        self.manifest = self.root / "nikon-sdk.sha256"
        self.manifest.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
        self.manifest_hash = hashlib.sha256(self.manifest.read_bytes()).hexdigest()

    def tearDown(self) -> None:
        if self.old_compiler_log is None:
            os.environ.pop("FAKE_COMPILER_LOG", None)
        else:
            os.environ["FAKE_COMPILER_LOG"] = self.old_compiler_log
        self.tempdir.cleanup()

    def config(self):
        return bootstrap_sdk.BootstrapConfig(
            sdk_root=self.sdk,
            state_root=self.state,
            manifest=self.manifest,
            expected_manifest_sha256=self.manifest_hash,
            adapter_source=self.adapter,
            compiler=self.compiler,
            require_read_only_mount=False,
            require_immutable_tools=False,
        )

    def test_first_start_compiles_and_stages_only_runtime_files(self) -> None:
        runtime = bootstrap_sdk.bootstrap(self.config())

        self.assertEqual(runtime, (self.state / "nikon-runtime" / "current").resolve())
        self.assertEqual((runtime / "nef_render.exe").read_bytes(), b"MZfake-adapter")
        self.assertEqual((runtime / "NkImgSDK.dll").read_bytes(), b"sdk-dll")
        self.assertEqual((runtime / "Profiles" / "NKsRGB.icm").read_bytes(), b"nikon-srgb")
        self.assertFalse((runtime / "Include").exists())
        self.assertEqual(
            (runtime / ".attestation" / "Include" / "Nkfl_Interface.h").read_bytes(),
            b"fake-header",
        )
        self.assertEqual(len(self.compiler_log.read_text(encoding="utf-8").splitlines()), 1)

    def test_ready_fingerprinted_runtime_restarts_without_sdk_mount(self) -> None:
        config = self.config()
        first = bootstrap_sdk.bootstrap(config)
        for path in sorted(self.sdk.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        self.sdk.rmdir()

        second = bootstrap_sdk.bootstrap(config)

        self.assertEqual(second, first)
        # Rootless state cannot hold an unforgeable marker. Recompiling a
        # deterministic candidate from the hash-checked private header and
        # immutable image source is the trust anchor on every restart.
        self.assertEqual(len(self.compiler_log.read_text(encoding="utf-8").splitlines()), 2)

    def test_runtime_service_resolves_and_validates_exact_active_payload(self) -> None:
        config = self.config()
        runtime = bootstrap_sdk.bootstrap(config)

        resolved = bootstrap_sdk.resolve_active_runtime(
            config, owner=os.geteuid()
        )

        self.assertEqual(resolved, runtime)

    def test_runtime_service_rejects_redirected_current_link(self) -> None:
        config = self.config()
        bootstrap_sdk.bootstrap(config)
        runtime_root = self.state / "nikon-runtime"
        attacker = runtime_root / "sdk-attacker-bootstrap-attacker"
        attacker.mkdir()
        current = runtime_root / "current"
        current.unlink()
        current.symlink_to(attacker.name)

        with self.assertRaisesRegex(
            bootstrap_sdk.BootstrapError, "exact sdk-\\* payload"
        ):
            bootstrap_sdk.resolve_active_runtime(config, owner=os.geteuid())

    def test_runtime_service_rejects_unattested_extra_payload(self) -> None:
        config = self.config()
        runtime = bootstrap_sdk.bootstrap(config)
        (runtime / "hidden.dll").write_bytes(b"unattested")

        with self.assertRaisesRegex(
            bootstrap_sdk.BootstrapError, "unexpected or missing entries"
        ):
            bootstrap_sdk.resolve_active_runtime(config, owner=os.geteuid())

    def test_read_only_image_tool_may_be_a_rootfs_package_hardlink(self) -> None:
        hardlink = self.root / "fake-mingw-hardlink"
        os.link(self.compiler, hardlink)

        with mock.patch.object(
            bootstrap_sdk, "mount_is_read_only", return_value=True
        ), mock.patch.object(bootstrap_sdk.os, "access", return_value=True):
            resolved = bootstrap_sdk.ensure_immutable_tool(
                self.compiler, "MinGW compiler", owner=os.geteuid()
            )

        self.assertEqual(resolved, self.compiler.resolve())

    def test_non_object_ready_marker_rebuilds_cleanly(self) -> None:
        config = self.config()
        first = bootstrap_sdk.bootstrap(config)
        marker = first / ".nef-watch-sdk-ready.json"
        marker.write_text("[]\n", encoding="utf-8")

        second = bootstrap_sdk.bootstrap(config)

        self.assertEqual(second, first)
        self.assertIsInstance(json.loads(marker.read_text(encoding="utf-8")), dict)
        self.assertEqual(len(self.compiler_log.read_text(encoding="utf-8").splitlines()), 2)

    def test_first_start_rejects_writable_sdk_location(self) -> None:
        config = replace(self.config(), require_read_only_mount=True)

        with self.assertRaisesRegex(bootstrap_sdk.BootstrapError, "read-only mount"):
            bootstrap_sdk.bootstrap(config)

        self.assertFalse(self.compiler_log.exists())

    def test_writable_renderer_source_cannot_be_a_trust_anchor(self) -> None:
        config = replace(self.config(), require_immutable_tools=True)

        with self.assertRaisesRegex(
            bootstrap_sdk.BootstrapError, "renderer source is writable"
        ):
            bootstrap_sdk.bootstrap(config)

        self.assertFalse(self.compiler_log.exists())

    def test_parallel_first_start_bootstraps_once(self) -> None:
        old_delay = os.environ.get("FAKE_COMPILER_DELAY")
        os.environ["FAKE_COMPILER_DELAY"] = "0.2"
        self.addCleanup(
            lambda: os.environ.pop("FAKE_COMPILER_DELAY", None)
            if old_delay is None
            else os.environ.__setitem__("FAKE_COMPILER_DELAY", old_delay)
        )
        config = self.config()

        with ThreadPoolExecutor(max_workers=2) as executor:
            runtimes = list(executor.map(lambda _: bootstrap_sdk.bootstrap(config), range(2)))

        self.assertEqual(runtimes[0], runtimes[1])
        # One installation compile plus one source-derived attestation compile
        # for the caller that acquires the lock second.
        self.assertEqual(len(self.compiler_log.read_text(encoding="utf-8").splitlines()), 2)

    def test_modified_sdk_is_rejected_before_compilation(self) -> None:
        (self.sdk / "Bin/x64/Release/NkImgSDK.dll").write_bytes(b"modified")

        with self.assertRaisesRegex(bootstrap_sdk.BootstrapError, "hash mismatch"):
            bootstrap_sdk.bootstrap(self.config())

        self.assertFalse(self.compiler_log.exists())
        self.assertFalse((self.state / "nikon-runtime" / "current").exists())

    def test_compiler_timeout_fails_closed_without_activating_runtime(self) -> None:
        old_delay = os.environ.get("FAKE_COMPILER_DELAY")
        os.environ["FAKE_COMPILER_DELAY"] = "0.3"
        self.addCleanup(
            lambda: os.environ.pop("FAKE_COMPILER_DELAY", None)
            if old_delay is None
            else os.environ.__setitem__("FAKE_COMPILER_DELAY", old_delay)
        )

        with mock.patch.object(bootstrap_sdk, "COMPILE_TIMEOUT_SECONDS", 0.05):
            with self.assertRaisesRegex(
                bootstrap_sdk.BootstrapError, "compilation timed out"
            ):
                bootstrap_sdk.bootstrap(self.config())

        self.assertFalse((self.state / "nikon-runtime" / "current").exists())

    def test_host_replacement_after_source_hash_is_rejected(self) -> None:
        target = self.sdk / "Bin/x64/Release/NkImgSDK.dll"
        original_file_sha256 = bootstrap_sdk.file_sha256
        replaced = False

        def replace_after_hash(path: Path) -> str:
            nonlocal replaced
            digest = original_file_sha256(path)
            if Path(path) == target and not replaced:
                target.write_bytes(b"host-replaced-after-verification")
                replaced = True
            return digest

        with mock.patch.object(
            bootstrap_sdk, "file_sha256", side_effect=replace_after_hash
        ):
            with self.assertRaisesRegex(
                bootstrap_sdk.BootstrapError, "snapshot hash mismatch"
            ):
                bootstrap_sdk.bootstrap(self.config())

        self.assertTrue(replaced)
        self.assertFalse(self.compiler_log.exists())
        self.assertFalse((self.state / "nikon-runtime" / "current").exists())

    def test_compiler_uses_verified_private_header_snapshot(self) -> None:
        bootstrap_sdk.bootstrap(self.config())

        command = self.compiler_log.read_text(encoding="utf-8")
        self.assertNotIn(f"-I{self.sdk / 'Include'}", command)
        self.assertIn("/build/inputs/Include", command)
        self.assertIn("-Wl,--no-insert-timestamp", command)

    def test_runtime_is_staged_from_verified_snapshot(self) -> None:
        source = self.sdk / "Bin/x64/Release/NkImgSDK.dll"
        original_file_sha256 = bootstrap_sdk.file_sha256
        replaced = False

        def replace_source_after_snapshot_hash(path: Path) -> str:
            nonlocal replaced
            digest = original_file_sha256(path)
            candidate = Path(path)
            if (
                candidate.name == "NkImgSDK.dll"
                and "snapshot" in candidate.parts
                and not replaced
            ):
                source.write_bytes(b"host-replaced-after-snapshot")
                replaced = True
            return digest

        with mock.patch.object(
            bootstrap_sdk,
            "file_sha256",
            side_effect=replace_source_after_snapshot_hash,
        ):
            runtime = bootstrap_sdk.bootstrap(self.config())

        self.assertTrue(replaced)
        self.assertEqual((runtime / "NkImgSDK.dll").read_bytes(), b"sdk-dll")

    def test_adapter_change_creates_a_new_fingerprinted_runtime(self) -> None:
        config = self.config()
        first = bootstrap_sdk.bootstrap(config)
        self.adapter.write_text("int main() { return 1; }\n", encoding="utf-8")

        second = bootstrap_sdk.bootstrap(config)

        self.assertNotEqual(first, second)
        self.assertFalse(first.exists())
        self.assertTrue(second.is_dir())
        self.assertEqual(len(self.compiler_log.read_text(encoding="utf-8").splitlines()), 2)

    def test_pruning_rejects_an_unsafe_inactive_runtime_without_following_it(self) -> None:
        runtime_root = self.root / "runtime-prune"
        active = runtime_root / "sdk-active"
        active.mkdir(parents=True)
        victim = self.root / "victim-runtime"
        victim.mkdir()
        (victim / "keep").write_text("keep", encoding="utf-8")
        (runtime_root / "sdk-unsafe").symlink_to(victim, target_is_directory=True)

        with self.assertRaisesRegex(
            bootstrap_sdk.BootstrapError, "unsafe inactive Nikon runtime"
        ):
            bootstrap_sdk.prune_inactive_runtimes(runtime_root, active)

        self.assertEqual((victim / "keep").read_text(encoding="utf-8"), "keep")

    def test_restart_prunes_owned_interrupted_staging_trees(self) -> None:
        runtime_root = self.state / "nikon-runtime"
        runtime_root.mkdir(parents=True, mode=0o750)
        for name in (".attest-stale", ".bootstrap-stale"):
            stale = runtime_root / name
            stale.mkdir(mode=0o700)
            (stale / "private-sdk-copy").write_bytes(b"stale")

        bootstrap_sdk.bootstrap(self.config())

        self.assertFalse((runtime_root / ".attest-stale").exists())
        self.assertFalse((runtime_root / ".bootstrap-stale").exists())

    def test_restart_rejects_symlinked_interrupted_staging_tree(self) -> None:
        runtime_root = self.state / "nikon-runtime"
        runtime_root.mkdir(parents=True, mode=0o750)
        victim = self.root / "staging-victim"
        victim.mkdir()
        (victim / "keep").write_text("keep", encoding="utf-8")
        (runtime_root / ".bootstrap-hostile").symlink_to(
            victim, target_is_directory=True
        )

        with self.assertRaisesRegex(
            bootstrap_sdk.BootstrapError, "unsafe interrupted Nikon staging"
        ):
            bootstrap_sdk.bootstrap(self.config())

        self.assertEqual((victim / "keep").read_text(encoding="utf-8"), "keep")

    def test_tampered_adapter_and_matching_marker_are_rebuilt_without_sdk(self) -> None:
        config = self.config()
        runtime = bootstrap_sdk.bootstrap(config)
        adapter = runtime / "nef_render.exe"
        marker_path = runtime / ".nef-watch-sdk-ready.json"
        adapter.chmod(0o600)
        adapter.write_bytes(b"MZattacker-controlled")
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["adapter_executable_sha256"] = hashlib.sha256(
            adapter.read_bytes()
        ).hexdigest()
        marker_path.write_text(json.dumps(marker) + "\n", encoding="utf-8")
        for path in sorted(self.sdk.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        self.sdk.rmdir()

        repaired = bootstrap_sdk.bootstrap(config)

        self.assertEqual(repaired, runtime)
        self.assertEqual(adapter.read_bytes(), b"MZfake-adapter")
        repaired_marker = json.loads(marker_path.read_text(encoding="utf-8"))
        self.assertEqual(
            repaired_marker["adapter_executable_sha256"],
            hashlib.sha256(b"MZfake-adapter").hexdigest(),
        )
        self.assertEqual(len(self.compiler_log.read_text(encoding="utf-8").splitlines()), 2)

    def test_tampered_private_header_cannot_attest_without_sdk(self) -> None:
        config = self.config()
        runtime = bootstrap_sdk.bootstrap(config)
        header = runtime / ".attestation" / "Include" / "Nkfl_Interface.h"
        header.chmod(0o600)
        header.write_bytes(b"attacker-header")
        for path in sorted(self.sdk.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        self.sdk.rmdir()

        with self.assertRaisesRegex(
            bootstrap_sdk.BootstrapError, "required on first start"
        ):
            bootstrap_sdk.bootstrap(config)

    def test_sdk_file_symlink_is_rejected(self) -> None:
        target = self.sdk / "Bin/x64/Release/NkImgSDK.dll"
        real_file = self.root / "same-content.dll"
        real_file.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(real_file)

        with self.assertRaisesRegex(bootstrap_sdk.BootstrapError, "not a real regular"):
            bootstrap_sdk.bootstrap(self.config())

        self.assertFalse(self.compiler_log.exists())

    def test_symlink_bootstrap_lock_is_rejected_without_touching_target(self) -> None:
        self.state.mkdir(mode=0o750)
        runtime_root = self.state / "nikon-runtime"
        runtime_root.mkdir(mode=0o750)
        victim = self.root / "victim"
        victim.write_bytes(b"do-not-touch")
        (runtime_root / ".bootstrap.lock").symlink_to(victim)

        with self.assertRaisesRegex(bootstrap_sdk.BootstrapError, "bootstrap lock"):
            bootstrap_sdk.bootstrap(self.config())

        self.assertEqual(victim.read_bytes(), b"do-not-touch")


if __name__ == "__main__":
    unittest.main()
