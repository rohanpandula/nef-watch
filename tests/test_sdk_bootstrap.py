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
        )

    def test_first_start_compiles_and_stages_only_runtime_files(self) -> None:
        runtime = bootstrap_sdk.bootstrap(self.config())

        self.assertEqual(runtime, (self.state / "nikon-runtime" / "current").resolve())
        self.assertEqual((runtime / "nef_render.exe").read_bytes(), b"MZfake-adapter")
        self.assertEqual((runtime / "NkImgSDK.dll").read_bytes(), b"sdk-dll")
        self.assertEqual((runtime / "Profiles" / "NKsRGB.icm").read_bytes(), b"nikon-srgb")
        self.assertFalse((runtime / "Include").exists())
        self.assertEqual(len(self.compiler_log.read_text(encoding="utf-8").splitlines()), 1)

    def test_ready_fingerprinted_runtime_restarts_without_sdk_mount(self) -> None:
        config = self.config()
        first = bootstrap_sdk.bootstrap(config)
        for path in sorted(self.sdk.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        self.sdk.rmdir()

        second = bootstrap_sdk.bootstrap(config)

        self.assertEqual(second, first)
        self.assertEqual(len(self.compiler_log.read_text(encoding="utf-8").splitlines()), 1)

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
        self.assertEqual(len(self.compiler_log.read_text(encoding="utf-8").splitlines()), 1)

    def test_modified_sdk_is_rejected_before_compilation(self) -> None:
        (self.sdk / "Bin/x64/Release/NkImgSDK.dll").write_bytes(b"modified")

        with self.assertRaisesRegex(bootstrap_sdk.BootstrapError, "hash mismatch"):
            bootstrap_sdk.bootstrap(self.config())

        self.assertFalse(self.compiler_log.exists())
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
        self.assertIn("/snapshot/Include", command)

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
        self.assertTrue(first.is_dir())
        self.assertTrue(second.is_dir())
        self.assertEqual(len(self.compiler_log.read_text(encoding="utf-8").splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
