import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "tool" / "nef_render_wine.sh"


class WineWrapperTests(unittest.TestCase):
    def test_removes_only_old_empty_nikon_temp_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            runtime.mkdir()
            (runtime / "nef_render.exe").touch()

            fake_wine = root / "wine"
            fake_wine.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            fake_wine.chmod(0o755)

            wine_prefix = root / "prefix"
            temp_dir = wine_prefix / "drive_c" / "users" / "nef-watch" / "Temp"
            temp_dir.mkdir(parents=True)
            old_empty = temp_dir / "nkn-old.tmp"
            fresh_empty = temp_dir / "nkn-fresh.tmp"
            old_nonempty = temp_dir / "nkn-data.tmp"
            old_empty.touch()
            fresh_empty.touch()
            old_nonempty.write_bytes(b"active")
            old_time = time.time() - 20 * 60
            os.utime(old_empty, (old_time, old_time))
            os.utime(old_nonempty, (old_time, old_time))

            nef = root / "sample.NEF"
            raw = root / "sample.raw"
            profile = root / "profile.icm"
            nef.touch()
            profile.touch()
            env = {
                **os.environ,
                "NIKON_RUNTIME_DIR": str(runtime),
                "WINEPREFIX": str(wine_prefix),
                "WINE_BIN": str(fake_wine),
            }
            subprocess.run(
                [str(WRAPPER), str(nef), str(raw), str(profile)],
                check=True,
                env=env,
            )

            self.assertFalse(old_empty.exists())
            self.assertTrue(fresh_empty.exists())
            self.assertTrue(old_nonempty.exists())


if __name__ == "__main__":
    unittest.main()
