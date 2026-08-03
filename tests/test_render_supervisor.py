from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR = ROOT / "docker" / "render_supervisor.py"


@unittest.skipUnless(sys.platform.startswith("linux"), "subreaping requires Linux")
class RenderSupervisorTests(unittest.TestCase):
    def test_preserves_command_exit_status(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SUPERVISOR), "--", "/bin/sh", "-c", "exit 23"],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 23, result.stderr)

    def test_kills_and_reaps_a_detached_setsid_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready = root / "ready"
            escaped = root / "escaped"
            descendant = (
                "import os,pathlib,time;"
                "os.setsid();"
                f"pathlib.Path({str(ready)!r}).write_text('ready');"
                "time.sleep(0.8);"
                f"pathlib.Path({str(escaped)!r}).write_text('escaped')"
            )
            command = (
                "import pathlib,subprocess,sys,time;"
                f"subprocess.Popen([sys.executable,'-c',{descendant!r}]);"
                f"p=pathlib.Path({str(ready)!r});"
                "deadline=time.monotonic()+2;"
                "\nwhile not p.exists() and time.monotonic()<deadline: time.sleep(.01);"
                "\nraise SystemExit(0 if p.exists() else 9)"
            )

            result = subprocess.run(
                [sys.executable, str(SUPERVISOR), "--", sys.executable, "-c", command],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(ready.exists())
            time.sleep(1.0)
            self.assertFalse(escaped.exists(), "detached renderer descendant survived")


if __name__ == "__main__":
    unittest.main()
