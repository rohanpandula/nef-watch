from __future__ import annotations

import shutil
import subprocess
import os
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
FINGERPRINT_SCRIPT = REPO / "docker" / "source-fingerprint.sh"


class SourceFingerprintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "docker").mkdir()
        (self.root / "tool").mkdir()
        for relative in (
            ".dockerignore",
            "requirements.txt",
            "pyproject.toml",
            "tool/nef_watch.py",
            "tool/nef_render_win.cpp",
            "tool/nef_render_wine.sh",
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"contents of {relative}\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def fingerprint(self) -> str:
        result = subprocess.run(
            ["bash", str(FINGERPRINT_SCRIPT), str(self.root)],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def test_fingerprint_tracks_adapter_but_ignores_local_env(self) -> None:
        shutil.copy2(FINGERPRINT_SCRIPT, self.root / "docker" / FINGERPRINT_SCRIPT.name)
        first = self.fingerprint()
        (self.root / "docker" / ".env").write_text("PRIVATE=path\n", encoding="utf-8")
        self.assertEqual(self.fingerprint(), first)
        cache = self.root / "docker" / "__pycache__"
        cache.mkdir()
        (cache / "bootstrap.cpython-312.pyc").write_bytes(b"local bytecode")
        self.assertEqual(self.fingerprint(), first)

        (self.root / "tool" / "nef_render_win.cpp").write_text(
            "changed adapter\n", encoding="utf-8"
        )
        self.assertNotEqual(self.fingerprint(), first)
        self.assertRegex(first, r"^[0-9a-f]{64}$")


class PublicImagePackagingTests(unittest.TestCase):
    def test_image_build_is_sdk_free_and_compose_injects_sdk_read_only(self) -> None:
        dockerfile = (REPO / "docker" / "Dockerfile").read_text(encoding="utf-8")
        compose = (REPO / "docker" / "compose.yaml").read_text(encoding="utf-8")
        build_script = (REPO / "docker" / "build-image.sh").read_text(encoding="utf-8")

        self.assertNotIn("--build-context", dockerfile)
        self.assertNotIn("NkImgSDK.dll", dockerfile)
        self.assertNotIn("additional_contexts", compose)
        self.assertNotIn("    build:", compose)
        self.assertIn(":/nikon-sdk:ro", compose)
        self.assertIn("ghcr.io/rohanpandula/nef-watch:linux-amd64", compose)
        self.assertIn("user: 99:100", compose)
        self.assertIn("nef-watch-state:/var/lib/nef-watch:rw", compose)
        self.assertNotIn("wine-prefix", compose)
        self.assertIn("g++-mingw-w64-x86-64", dockerfile)
        self.assertIn("bootstrap_sdk.py", dockerfile)
        self.assertNotIn('VOLUME ["/var/lib/nef-watch"]', dockerfile)
        self.assertIn("source-fingerprint.sh", build_script)
        self.assertNotIn("SDK_DIR", build_script)

        example_env = (REPO / "docker" / ".env.example").read_text(encoding="utf-8")
        self.assertNotIn("PUID=", example_env)
        self.assertNotIn("PGID=", example_env)
        self.assertNotIn("SOURCE_FINGERPRINT=", example_env)

    def test_help_bypasses_private_sdk_and_wine_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invocation = root / "python-invocation"
            fake_python = root / "python3"
            fake_python.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$HELP_INVOCATION\"\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{root}:/usr/bin:/bin",
                "HELP_INVOCATION": str(invocation),
                "NIKON_SDK_DIR": str(root / "does-not-exist"),
            }

            subprocess.run(
                [str(REPO / "docker" / "entrypoint.sh"), "--help"],
                check=True,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(
                invocation.read_text(encoding="utf-8").strip(),
                "/app/tool/nef_watch.py --help",
            )

    def test_bootstrap_cli_has_concise_failure_contract(self) -> None:
        result = subprocess.run(
            [sys.executable, str(REPO / "docker" / "bootstrap_sdk.py")],
            env={**os.environ, "NIKON_SDK_DIR": "/definitely-not-mounted"},
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 70)
        self.assertEqual(result.stdout, "")
        self.assertIn("Nikon SDK bootstrap failed:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
