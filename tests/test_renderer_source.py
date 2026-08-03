from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tool" / "nef_render_win.cpp"
STUB_INCLUDE = ROOT / "tests" / "renderer_stub"


class WindowsRendererSourceTests(unittest.TestCase):
    def test_mingw_strict_syntax(self) -> None:
        compiler = shutil.which("x86_64-w64-mingw32-g++")
        if compiler is None:
            self.skipTest("MinGW cross-compiler is not installed")
        subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-I",
                str(STUB_INCLUDE),
                "-fsyntax-only",
                str(SOURCE),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_loader_and_swap_contracts_are_fail_closed(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("SetDefaultDllDirectories", source)
        self.assertIn("LoadLibraryExW(executablePath", source)
        self.assertNotIn('LoadLibraryExA("NkImgSDK.dll"', source)
        self.assertIn("NEF_WATCH_WINE_TEMP_DIR is required", source)
        self.assertIn("NEF_WATCH_WINE_SWAP_PATH is required", source)
        self.assertNotIn("GetTempFileNameA", source)
        self.assertNotIn("nef-watch.swap", source)
        self.assertIn("DeleteFileA(g_swapPath)", source)
        self.assertIn("parseBits", source)
        self.assertIn("std::isfinite", source)
        self.assertIn("sourceBytes64 > configuredBufferLimit", source)
        self.assertIn('"NEF_WATCH_RENDER_MEMORY_MIB"', source)
        self.assertIn('"NEF_WATCH_SDK_MEMORY_MIB"', source)
        self.assertIn("*totalMiB - kRendererOverheadMiB - *sdkMiB", source)
        self.assertIn("CreateFileA(", source)
        self.assertIn("CREATE_NEW", source)
        self.assertIn("FILE_FLAG_OPEN_REPARSE_POINT", source)
        self.assertIn("information.nNumberOfLinks != 1", source)
        self.assertIn('safeRendererPath(nefPath, "T:\\\\input\\\\"', source)
        self.assertIn('safeRendererPath(rawPath, "T:\\\\output\\\\"', source)
        self.assertIn('safeRendererPath(profilePath, "R:\\\\Profiles\\\\"', source)


if __name__ == "__main__":
    unittest.main()
