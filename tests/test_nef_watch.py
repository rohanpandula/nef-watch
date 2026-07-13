import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tool"))
import nef_watch  # noqa: E402


class NkRawReaderTests(unittest.TestCase):
    def write_raw(self, payload):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        self.addCleanup(Path(tmp.name).unlink, missing_ok=True)
        tmp.write(payload)
        tmp.close()
        return Path(tmp.name)

    def test_reads_small_valid_payload(self):
        path = self.write_raw(b"NKRAW1 2 1 3 1 1\n\x01\x02\x03\x04\x05\x06")
        pixels, info = nef_watch.read_nkraw(path)
        self.assertEqual(pixels.shape, (1, 2, 3))
        self.assertEqual(pixels.tolist(), [[[1, 2, 3], [4, 5, 6]]])
        self.assertEqual(info["orient"], 1)

    def test_rejects_oversized_dimensions_before_reading_payload(self):
        path = self.write_raw(b"NKRAW1 100000001 1 3 1 1\n")
        with self.assertRaisesRegex(ValueError, "unsafe NKRAW1 dimensions"):
            nef_watch.read_nkraw(path)

    def test_rejects_extra_payload_bytes(self):
        path = self.write_raw(b"NKRAW1 1 1 3 1 1\n\x01\x02\x03\x04")
        with self.assertRaisesRegex(ValueError, "raw payload 4 != 3"):
            nef_watch.read_nkraw(path)

    def test_rejects_unbounded_header(self):
        path = self.write_raw(b"X" * (nef_watch.MAX_NKRAW_HEADER + 1))
        with self.assertRaisesRegex(ValueError, "invalid or oversized"):
            nef_watch.read_nkraw(path)


class ConversionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.nef = self.root / "sample.NEF"
        self.nef.write_bytes(b"II*\0valid-enough-for-this-unit-test")
        self.output = self.root / "output"
        self.output.mkdir()
        self.args = SimpleNamespace(
            formats=frozenset({"tiff"}),
            recursive=False,
            input_is_file=True,
            input_root=self.root,
            overwrite=False,
            max_input_bytes=512 * 1024 * 1024,
            max_input_mib=512,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_force_rerenders_when_output_already_exists(self):
        (self.output / "sample.tif").write_bytes(b"old")
        with mock.patch.object(nef_watch, "render_raster") as render:
            status, _ = nef_watch.convert_one(
                self.nef, self.output, self.args, b"icc", force=True
            )
        self.assertEqual(status, "ok")
        render.assert_called_once()

    def test_missing_format_does_not_overwrite_existing_output(self):
        self.args.formats = frozenset({"tiff", "jpeg"})
        existing = self.output / "sample.tif"
        existing.write_bytes(b"manually-edited")
        with mock.patch.object(nef_watch, "render_raster") as render:
            status, _ = nef_watch.convert_one(
                self.nef, self.output, self.args, b"icc"
            )
        self.assertEqual(status, "ok")
        self.assertEqual(existing.read_bytes(), b"manually-edited")
        self.assertEqual(
            render.call_args.args[1], [("jpeg", self.output / "sample.jpg")]
        )

    def test_oversized_input_is_rejected_before_renderer(self):
        self.args.max_input_bytes = 4
        self.args.max_input_mib = 0
        with mock.patch.object(nef_watch, "render_raster") as render:
            status, detail = nef_watch.convert_one(
                self.nef, self.output, self.args, b"icc"
            )
        self.assertEqual(status, "error")
        self.assertIn("limit", detail)
        render.assert_not_called()

    def test_sdk_input_limit_does_not_reject_dng_only_conversion(self):
        self.args.formats = frozenset({"dng"})
        self.args.max_input_bytes = 4
        self.args.max_input_mib = 0
        with mock.patch.object(nef_watch, "render_dng") as render:
            status, _ = nef_watch.convert_one(
                self.nef, self.output, self.args, b""
            )
        self.assertEqual(status, "ok")
        render.assert_called_once()


class WatchStateTests(unittest.TestCase):
    def test_missing_output_is_not_classified_as_source_change(self):
        key = (123, 456)
        self.assertEqual(
            nef_watch.processed_source_state(key, key, complete=False),
            "output-missing",
        )
        self.assertEqual(
            nef_watch.processed_source_state(key, (124, 456), complete=True),
            "source-changed",
        )


if __name__ == "__main__":
    unittest.main()
