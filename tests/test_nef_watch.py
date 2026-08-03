import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tool"))
import nef_watch  # noqa: E402


def watcher_args(root, *, formats=frozenset({"tiff"})):
    return SimpleNamespace(
        formats=formats,
        recursive=False,
        input_is_file=False,
        input_root=root,
        input=root,
        overwrite=False,
        max_input_bytes=512 * 1024 * 1024,
        max_input_mib=512,
        max_scan_entries=nef_watch.DEFAULT_MAX_SCAN_ENTRIES,
        bits=8,
        quality=90,
        exp_comp=0.0,
        deterministic=False,
        dng_engine="dnglab",
        dng_embed_original=False,
        dng_bin=None,
        render_bin=root / "renderer",
        render_env=None,
        profile=root / "profile.icc",
        exiftool=None,
        render_timeout=2.0,
        exif_timeout=2.0,
        dng_timeout=2.0,
        kill_grace_seconds=0.05,
        temp_dir=root / "temp",
        interval=0.1,
        max_retries=3,
        jobs=1,
        max_pending=2,
        settle_seconds=0,
        skip_existing=False,
        legacy_input_root=None,
        input_root_identity=nef_watch._directory_identity(root),
    )


def write_valid_tiff(path, color=(1, 2, 3)):
    nef_watch.Image.new("RGB", (2, 2), color).save(path, format="TIFF")


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

    def test_rejects_non_rgb_renderer_payload(self):
        path = self.write_raw(b"NKRAW1 1 1 4 1 1\n\x01\x02\x03\x04")
        with self.assertRaisesRegex(ValueError, "channels=4"):
            nef_watch.read_nkraw(path)

    def test_rejects_symlink_fifo_hardlink_and_mid_read_change(self):
        path = self.write_raw(b"NKRAW1 1 1 3 1 1\n\x01\x02\x03")
        root = path.parent
        symlink = root / f"{path.name}.link"
        symlink.symlink_to(path)
        self.addCleanup(symlink.unlink, missing_ok=True)
        with self.assertRaises(nef_watch.UnsafeFileError):
            nef_watch.read_nkraw(symlink)

        hardlink = root / f"{path.name}.hard"
        os.link(path, hardlink)
        self.addCleanup(hardlink.unlink, missing_ok=True)
        with self.assertRaises(nef_watch.UnsafeFileError):
            nef_watch.read_nkraw(hardlink)
        hardlink.unlink()

        fifo = root / f"{path.name}.fifo"
        os.mkfifo(fifo)
        self.addCleanup(fifo.unlink, missing_ok=True)
        started = time.monotonic()
        with self.assertRaises(nef_watch.UnsafeFileError):
            nef_watch.read_nkraw(fifo)
        self.assertLess(time.monotonic() - started, 1.0)

        real_fstat = os.fstat
        calls = 0

        def change_before_final_identity(descriptor):
            nonlocal calls
            calls += 1
            if calls == 2:
                current = path.stat().st_mtime_ns
                os.utime(path, ns=(current + 1_000_000, current + 1_000_000))
            return real_fstat(descriptor)

        with mock.patch.object(
            nef_watch.os, "fstat", side_effect=change_before_final_identity
        ):
            with self.assertRaisesRegex(ValueError, "changed while reading"):
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
        self.args.overwrite = True
        def fake_render(_nef, todo, _args, _icc):
            staged = []
            for kind, final in todo:
                partial = nef_watch.unique_partial(final)
                write_valid_tiff(partial)
                staged.append((kind, partial, final))
            return staged

        with mock.patch.object(
            nef_watch, "render_raster", side_effect=fake_render
        ) as render:
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
            status, detail = nef_watch.convert_one(
                self.nef, self.output, self.args, b"icc"
            )
        self.assertEqual(status, "error")
        self.assertEqual(detail.code, "output-collision")
        self.assertEqual(existing.read_bytes(), b"manually-edited")
        render.assert_not_called()

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

    def test_input_limit_also_rejects_dng_only_conversion(self):
        self.args.formats = frozenset({"dng"})
        self.args.max_input_bytes = 4
        self.args.max_input_mib = 0
        with mock.patch.object(nef_watch, "render_dng") as render:
            status, _ = nef_watch.convert_one(
                self.nef, self.output, self.args, b""
            )
        self.assertEqual(status, "error")
        self.assertIn("limit", _)
        render.assert_not_called()


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


class FingerprintAndSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.source = self.root / "photo.NEF"
        self.source.write_bytes(b"II*\0abcdefgh")
        self.args = watcher_args(self.root)
        self.args.temp_dir.mkdir()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_content_hash_detects_change_with_same_size_and_mtime(self):
        original_mtime = self.source.stat().st_mtime_ns
        first, _ = nef_watch.fingerprint_file(self.source)
        self.source.write_bytes(b"II*\0abcdWXYZ")
        os.utime(self.source, ns=(original_mtime, original_mtime))
        second, _ = nef_watch.fingerprint_file(self.source)
        self.assertNotEqual(first, second)

    def test_durable_fingerprint_ignores_metadata_only_touch(self):
        first, _ = nef_watch.fingerprint_file(self.source)
        new_mtime = self.source.stat().st_mtime_ns + 1_000_000_000
        os.utime(self.source, ns=(new_mtime, new_mtime))
        second, _ = nef_watch.fingerprint_file(self.source)
        self.assertEqual(first, second)
        self.assertEqual(set(json.loads(first)), {"schema", "sha256", "size"})

    def test_bad_magic_is_permanent_and_renderer_is_not_called(self):
        self.source.write_bytes(b"JPEG-not-a-nef")
        with mock.patch.object(nef_watch, "render_raster") as renderer:
            status, detail = nef_watch.convert_one(
                self.source, self.root / "out", self.args, b"icc"
            )
        self.assertEqual(status, "error")
        self.assertTrue(detail.permanent)
        self.assertEqual(detail.code, "invalid-input")
        renderer.assert_not_called()

    def test_bad_magic_error_carries_content_hash_not_only_metadata(self):
        self.source.write_bytes(b"bad!same-size")
        original_mtime = self.source.stat().st_mtime_ns
        with self.assertRaises(nef_watch.InvalidRawError) as first:
            nef_watch.fingerprint_file(self.source)
        self.source.write_bytes(b"evilsame-size")
        os.utime(self.source, ns=(original_mtime, original_mtime))
        with self.assertRaises(nef_watch.InvalidRawError) as second:
            nef_watch.fingerprint_file(self.source)
        self.assertNotEqual(first.exception.fingerprint, second.exception.fingerprint)

    def test_oversized_source_is_rejected_before_hash_loop(self):
        self.source.write_bytes(b"II*\0" + b"x" * 64)
        real_digest = nef_watch.hashlib.sha256()
        digest = mock.Mock()
        digest.update.side_effect = real_digest.update
        digest.hexdigest.side_effect = real_digest.hexdigest
        with mock.patch.object(nef_watch.hashlib, "sha256", return_value=digest):
            with self.assertRaises(nef_watch.InvalidRawError) as raised:
                nef_watch.fingerprint_file(
                    self.source, max_bytes=8, max_mib=8 / (1024 * 1024)
                )
        self.assertEqual(json.loads(raised.exception.fingerprint)["reason"], "oversized")
        digest.update.assert_not_called()

    def test_streaming_limit_catches_growth_past_a_stale_size_precheck(self):
        self.source.write_bytes(b"II*\0" + b"x" * 64)
        real_open = nef_watch._open_regular_fd

        def stale_size_open(*args, **kwargs):
            descriptor, metadata = real_open(*args, **kwargs)
            stale = SimpleNamespace(
                st_dev=metadata.st_dev,
                st_ino=metadata.st_ino,
                st_mode=metadata.st_mode,
                st_nlink=metadata.st_nlink,
                st_size=4,
                st_mtime_ns=metadata.st_mtime_ns,
                st_ctime_ns=metadata.st_ctime_ns,
            )
            return descriptor, stale

        with mock.patch.object(
            nef_watch, "_open_regular_fd", side_effect=stale_size_open
        ):
            with self.assertRaisesRegex(nef_watch.InvalidRawError, "limit"):
                nef_watch.fingerprint_file(
                    self.source, max_bytes=8, max_mib=8 / (1024 * 1024)
                )

        self.args.max_input_bytes = 8
        self.args.max_input_mib = 8 / (1024 * 1024)
        with mock.patch.object(
            nef_watch, "_open_regular_fd", side_effect=stale_size_open
        ):
            with self.assertRaisesRegex(nef_watch.InvalidRawError, "limit"):
                nef_watch.create_input_snapshot(self.source, self.args)
        self.assertEqual(list(self.args.temp_dir.glob("nef-watch-input-*")), [])

    def test_invalid_content_fingerprint_is_cached_for_unchanged_stat(self):
        error = nef_watch.InvalidRawError("bad magic")
        error.fingerprint = '{"sha256":"cached"}'
        cache = {}
        key = nef_watch.stat_key(self.source)
        canonical = nef_watch.canonical_path(self.source)
        with mock.patch.object(
            nef_watch, "fingerprint_source", side_effect=error
        ) as fingerprint:
            with self.assertRaises(nef_watch.InvalidRawError):
                nef_watch.fingerprint_source_cached(
                    cache, canonical, key, self.source, self.args
                )
            self.assertEqual(
                nef_watch.fingerprint_source_cached(
                    cache, canonical, key, self.source, self.args
                ),
                error.fingerprint,
            )
        fingerprint.assert_called_once()

    def test_secure_fingerprint_rejects_symlink_and_fifo_without_blocking(self):
        link = self.root / "link.NEF"
        link.symlink_to(self.source)
        with self.assertRaises(nef_watch.InvalidRawError):
            nef_watch.fingerprint_file(link)

        fifo = self.root / "pipe.NEF"
        os.mkfifo(fifo)
        started = time.monotonic()
        with self.assertRaises(nef_watch.InvalidRawError):
            nef_watch.fingerprint_file(fifo)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_source_hardlinks_are_rejected_by_fingerprint_snapshot_and_stat(self):
        second_name = self.root / "second-name.NEF"
        os.link(self.source, second_name)
        with self.assertRaisesRegex(nef_watch.InvalidRawError, "hard link"):
            nef_watch.fingerprint_file(self.source)
        with self.assertRaisesRegex(nef_watch.InvalidRawError, "hard link"):
            nef_watch.create_input_snapshot(self.source, self.args)
        with self.assertRaisesRegex(nef_watch.UnsafeFileError, "hard link"):
            nef_watch.stat_key(self.source)
        self.assertEqual(
            nef_watch.find_nefs(
                self.root,
                recursive=True,
                root_identity=nef_watch._directory_identity(self.root),
            ),
            [],
        )

    def test_snapshot_is_immutable_copy_on_configured_temp_storage(self):
        fingerprint, _ = nef_watch.fingerprint_file(self.source)
        snapshot = nef_watch.create_input_snapshot(self.source, self.args, fingerprint)
        self.addCleanup(nef_watch.remove_snapshot, snapshot)
        self.assertEqual(snapshot.path.parent, self.args.temp_dir)
        self.assertEqual(snapshot.path.read_bytes(), self.source.read_bytes())
        self.source.write_bytes(b"II*\0changed-data")
        self.assertNotEqual(snapshot.path.read_bytes(), self.source.read_bytes())

    def test_source_change_discards_staged_output_before_publish(self):
        out_dir = self.root / "out"
        out_dir.mkdir()
        fingerprint, _ = nef_watch.fingerprint_file(self.source)
        snapshot = nef_watch.create_input_snapshot(self.source, self.args, fingerprint)
        self.addCleanup(nef_watch.remove_snapshot, snapshot)

        def fake_render(_nef, todo, _args, _icc):
            staged = []
            for kind, final in todo:
                final.parent.mkdir(parents=True, exist_ok=True)
                partial = nef_watch.unique_partial(final)
                write_valid_tiff(partial)
                staged.append((kind, partial, final))
            return staged

        self.source.write_bytes(b"II*\0new-version!")
        with mock.patch.object(nef_watch, "render_raster", side_effect=fake_render):
            status, detail = nef_watch.convert_one(
                snapshot.path,
                out_dir,
                self.args,
                b"icc",
                source_path=self.source,
                expected_fingerprint=fingerprint,
            )
        self.assertEqual(status, "error")
        self.assertEqual(detail.code, "source-changed")
        self.assertFalse((out_dir / "photo.tif").exists())
        self.assertEqual(list(out_dir.glob("*.partial*")), [])

    def test_snapshot_mutation_during_render_is_detected_before_publish(self):
        out_dir = self.root / "snapshot-out"
        out_dir.mkdir()
        fingerprint, _ = nef_watch.fingerprint_file(self.source)
        snapshot = nef_watch.create_input_snapshot(self.source, self.args, fingerprint)
        self.addCleanup(nef_watch.remove_snapshot, snapshot)

        def mutate_snapshot(nef, todo, _args, _icc):
            Path(nef).chmod(0o600)
            Path(nef).write_bytes(b"II*\0renderer-mutated-snapshot")
            staged = []
            for kind, final in todo:
                partial = nef_watch.unique_partial(final)
                write_valid_tiff(partial)
                staged.append((kind, partial, final))
            return staged

        with mock.patch.object(
            nef_watch, "render_raster", side_effect=mutate_snapshot
        ):
            status, detail = nef_watch.convert_one(
                snapshot.path,
                out_dir,
                self.args,
                b"icc",
                source_path=self.source,
                expected_fingerprint=fingerprint,
            )
        self.assertEqual(status, "error")
        self.assertEqual(detail.code, "source-changed")
        self.assertIn("snapshot changed", detail)
        self.assertFalse((out_dir / "photo.tif").exists())

    def test_renderer_raw_buffer_uses_configured_temp_storage(self):
        out = self.root / "out.tif"
        captured = {}

        def fake_process(cmd, **_kwargs):
            raw = Path(cmd[2])
            captured["raw"] = raw
            raw.write_bytes(b"NKRAW1 1 1 3 1 1\n\x01\x02\x03")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        def fake_encode(_arr, path, _icc):
            write_valid_tiff(path)

        with mock.patch.object(nef_watch, "run_process", side_effect=fake_process), \
             mock.patch.object(nef_watch, "encode_tiff", side_effect=fake_encode):
            staged = nef_watch.render_raster(
                self.source, [("tiff", out)], self.args, b"icc"
            )
        self.addCleanup(nef_watch._cleanup_staged, staged)
        self.assertEqual(captured["raw"].parent, self.args.temp_dir)
        self.assertTrue(captured["raw"].name.startswith("nef-watch-render-"))
        self.assertFalse(captured["raw"].exists())

    def test_watcher_renderer_paths_satisfy_strict_wine_wrapper_policy(self):
        snapshot = self.args.temp_dir / "nef-watch-input-policy.NEF"
        snapshot.write_bytes(self.source.read_bytes())
        out = self.root / "policy.tif"
        checked = {}

        def strict_wrapper_policy(cmd, **_kwargs):
            source = Path(cmd[1]).resolve()
            raw = Path(cmd[2]).resolve()
            temp_root = self.args.temp_dir.resolve()
            checked["source"] = source
            checked["raw"] = raw
            if source.parent != temp_root or raw.parent != temp_root:
                return subprocess.CompletedProcess(
                    cmd,
                    66,
                    "",
                    "input snapshot and raw output must be direct temp children",
                )
            raw.write_bytes(b"NKRAW1 1 1 3 1 1\n\x01\x02\x03")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(
            nef_watch, "run_process", side_effect=strict_wrapper_policy
        ), mock.patch.object(
            nef_watch, "encode_tiff", side_effect=lambda _a, p, _i: write_valid_tiff(p)
        ):
            staged = nef_watch.render_raster(
                snapshot, [("tiff", out)], self.args, b"icc"
            )
        self.addCleanup(nef_watch._cleanup_staged, staged)
        self.assertEqual(checked["source"].parent, self.args.temp_dir.resolve())
        self.assertEqual(checked["raw"].parent, self.args.temp_dir.resolve())

    def test_exif_failure_discards_staged_raster(self):
        out = self.root / "out.tif"

        def fake_process(cmd, **_kwargs):
            Path(cmd[2]).write_bytes(b"NKRAW1 1 1 3 1 1\n\x01\x02\x03")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        def fake_encode(_arr, path, _icc):
            write_valid_tiff(path)

        self.args.exiftool = Path("/fake/exiftool")
        with mock.patch.object(nef_watch, "run_process", side_effect=fake_process), \
             mock.patch.object(nef_watch, "encode_tiff", side_effect=fake_encode), \
             mock.patch.object(nef_watch, "copy_exif", side_effect=RuntimeError("exif failed")):
            with self.assertRaisesRegex(RuntimeError, "exif failed"):
                nef_watch.render_raster(
                    self.source, [("tiff", out)], self.args, b"icc"
                )
        self.assertEqual(list(self.root.glob("*.partial*")), [])


class ProcessTimeoutTests(unittest.TestCase):
    def test_stdout_and_stderr_are_concurrently_drained_into_fixed_tails(self):
        script = (
            "import os; "
            "os.write(1, b'a' * 200000 + b'OUT-END\\n'); "
            "os.write(2, b'b' * 200000 + b'ERR-END\\n')"
        )
        proc = nef_watch.run_process(
            [sys.executable, "-c", script],
            timeout=30,
            label="noisy helper",
        )
        self.assertEqual(proc.returncode, 0)
        self.assertLessEqual(
            len(proc.stdout.encode("utf-8")), nef_watch.MAX_PROCESS_OUTPUT_BYTES
        )
        self.assertLessEqual(
            len(proc.stderr.encode("utf-8")), nef_watch.MAX_PROCESS_OUTPUT_BYTES
        )
        self.assertTrue(proc.stdout.endswith("OUT-END\n"))
        self.assertTrue(proc.stderr.endswith("ERR-END\n"))
        self.assertIn("output truncated", proc.stdout)
        self.assertIn("output truncated", proc.stderr)

    def test_timeout_terminates_process_group_promptly(self):
        started = time.monotonic()
        with self.assertRaisesRegex(nef_watch.ProcessTimeoutError, "timed out"):
            nef_watch.run_process(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                timeout=0.05,
                label="test helper",
                kill_grace_seconds=0.05,
            )
        self.assertLess(time.monotonic() - started, 2.0)

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX process groups")
    def test_successful_helper_exit_still_reaps_ignored_group_descendant(self):
        with tempfile.TemporaryDirectory() as td:
            child_pid_file = Path(td) / "child.pid"
            script = (
                "import os,signal,time; "
                "pid=os.fork(); "
                "(os.close(1),os.close(2),"
                f"open({str(child_pid_file)!r},'w').write(str(os.getpid())),"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN),time.sleep(30)) "
                "if pid==0 else None"
            )
            nef_watch.run_process(
                [sys.executable, "-c", script],
                timeout=30,
                label="forking helper",
                kill_grace_seconds=0.05,
            )
            deadline = time.monotonic() + 1.0
            while not child_pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(child_pid_file.exists())
            child_pid = int(child_pid_file.read_text(encoding="utf-8"))

            def child_is_gone():
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    return True
                return False

            self.addCleanup(
                lambda: os.kill(child_pid, signal.SIGKILL)
                if not child_is_gone()
                else None
            )
            deadline = time.monotonic() + 2.0
            while not child_is_gone() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(child_is_gone(), "helper descendant survived group cleanup")


class DurableStateTests(unittest.TestCase):
    def test_unlinked_lock_sentinel_cannot_admit_a_second_live_store(self):
        sentinel = self.state_dir / "watcher.lock"
        sentinel.unlink()
        sentinel.write_bytes(b"replacement")
        sentinel.chmod(0o600)

        with self.assertRaises(nef_watch.UnsafeFileError):
            self.store.assert_owned()
        with self.assertRaises(BlockingIOError):
            nef_watch.StateStore(self.state_dir)

    def test_absolute_source_keys_migrate_transactionally_with_input_root(self):
        legacy_source = str(self.source.resolve())
        fingerprint, _ = nef_watch.fingerprint_file(self.source)
        self.store.record_success(legacy_source, fingerprint, "cfg", [])
        self.store.record_failure(
            legacy_source,
            fingerprint,
            "cfg",
            nef_watch.FailureDetail("retry"),
        )
        self.store.reserve_outputs(
            legacy_source, [("tiff", self.output)]
        )
        staged = nef_watch.unique_partial(self.output)
        staged.write_bytes(b"II*\0new")
        plan = nef_watch.prepare_publication(
            [("tiff", staged, self.output)],
            source_key=legacy_source,
            fingerprint=fingerprint,
            config_fingerprint="cfg",
            output_root=self.out_dir,
            root_identity=nef_watch._directory_identity(self.out_dir),
            allow_overwrite=True,
        )
        self.store.begin_publication(plan)
        self.store.close()

        self.store = nef_watch.StateStore(
            self.state_dir, input_root=self.root
        )
        relative = nef_watch.source_state_key(self.source, self.root)
        self.assertIsNotNone(self.store.get_source(relative))
        self.assertIsNotNone(self.store.get_failure(relative))
        reservation = self.store.conn.execute(
            "SELECT source_path FROM output_reservations"
        ).fetchone()
        self.assertEqual(reservation["source_path"], relative)
        row = self.store.publication_rows()[0]
        self.assertEqual(row["source_path"], relative)
        self.assertEqual(
            json.loads(row["plan_json"])["source_key"], relative
        )
        for table in (
            "sources",
            "failures",
            "output_reservations",
            "tracked_sources",
            "publication_transactions",
        ):
            self.assertEqual(
                self.store.conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE source_path=?",
                    (legacy_source,),
                ).fetchone()[0],
                0,
            )

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.state_dir = self.root / "state"
        self.store = nef_watch.StateStore(self.state_dir)
        self.source = self.root / "photo.NEF"
        self.source.write_bytes(b"II*\0source-data")
        self.out_dir = self.root / "out"
        self.out_dir.mkdir()
        self.output = self.out_dir / "photo.tif"
        write_valid_tiff(self.output)
        self.args = watcher_args(self.root)

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def test_success_provenance_survives_restart_and_validates_output(self):
        fingerprint, _ = nef_watch.fingerprint_file(self.source)
        metadata = nef_watch.capture_output_metadata([("tiff", self.output)])
        self.store.record_success(self.source, fingerprint, "config-a", metadata)
        self.store.close()
        self.store = nef_watch.StateStore(self.state_dir)
        row = self.store.get_source(self.source)
        self.assertEqual(row["fingerprint"], fingerprint)
        self.assertEqual(row["config_fingerprint"], "config-a")
        self.assertTrue(
            nef_watch.outputs_match_metadata(
                self.source, self.args, self.out_dir, row["output_metadata"]
            )
        )

    def test_future_state_schema_is_rejected_without_downgrading_marker(self):
        self.store.close()
        future_dir = self.root / "future-state"
        future_dir.mkdir()
        database = future_dir / "state.sqlite3"
        conn = nef_watch.sqlite3.connect(database)
        with conn:
            conn.execute(
                "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO metadata(key,value) VALUES('schema','999')"
            )
        conn.close()

        with self.assertRaisesRegex(nef_watch.sqlite3.DatabaseError, "newer schema"):
            nef_watch.StateStore(future_dir)

        conn = nef_watch.sqlite3.connect(database)
        try:
            marker = conn.execute(
                "SELECT value FROM metadata WHERE key='schema'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(marker, "999")
        self.store = nef_watch.StateStore(self.state_dir)

    def test_known_legacy_schema_is_migrated_and_marked_only_after_success(self):
        self.store.close()
        legacy_dir = self.root / "legacy-state"
        legacy_dir.mkdir()
        database = legacy_dir / "state.sqlite3"
        conn = nef_watch.sqlite3.connect(database)
        with conn:
            conn.execute(
                "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute("INSERT INTO metadata(key,value) VALUES('schema','0')")
            conn.execute(
                """CREATE TABLE failures (
                    source_path TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    config_fingerprint TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    next_retry_at REAL,
                    permanent INTEGER NOT NULL,
                    error_code TEXT NOT NULL,
                    error_text TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )
        conn.close()

        migrated = nef_watch.StateStore(legacy_dir)
        try:
            columns = {
                row[1]
                for row in migrated.conn.execute("PRAGMA table_info(failures)")
            }
            self.assertIn("active", columns)
            self.assertEqual(migrated.get_meta("schema"), str(nef_watch.STATE_SCHEMA))
        finally:
            migrated.close()
        self.store = nef_watch.StateStore(self.state_dir)

    def test_output_corruption_invalidates_durable_success(self):
        fingerprint, _ = nef_watch.fingerprint_file(self.source)
        metadata = nef_watch.capture_output_metadata([("tiff", self.output)])
        self.store.record_success(self.source, fingerprint, "config-a", metadata)
        row = self.store.get_source(self.source)
        write_valid_tiff(self.output, color=(9, 8, 7))
        self.assertFalse(
            nef_watch.outputs_match_metadata(
                self.source, self.args, self.out_dir, row["output_metadata"]
            )
        )

    def test_collision_reservation_is_durable_and_case_extension_safe(self):
        other = self.root / "PHOTO.NRW"
        other.write_bytes(b"II*\0other")
        outputs = nef_watch.expected_outputs(self.source, self.args, self.out_dir)
        self.store.reserve_outputs(self.source, outputs)
        with self.assertRaisesRegex(nef_watch.OutputCollisionError, "collision"):
            self.store.reserve_outputs(
                other, nef_watch.expected_outputs(other, self.args, self.out_dir)
            )

    def test_unchanged_output_collision_does_not_delete_or_rewrite_failure_row(self):
        fingerprint, _ = nef_watch.fingerprint_file(self.source)
        source_key = nef_watch.source_state_key(self.source, self.root)
        detail = nef_watch.FailureDetail(
            "output collision: a requested output already exists but is not "
            "owned by durable success state; use --overwrite only after review",
            permanent=True,
            code="output-collision",
        )
        self.store.record_failure(
            source_key, fingerprint, "cfg", detail, permanent=True
        )
        before = self.store.conn.total_changes
        before_row = dict(self.store.get_failure(source_key))

        action, _, repeated = nef_watch.source_action(
            self.store,
            self.source,
            fingerprint,
            "cfg",
            self.args,
            self.out_dir,
            source_key=source_key,
        )

        self.assertEqual(action, "quarantine")
        self.assertFalse(getattr(repeated, "persist_failure", True))
        self.assertEqual(self.store.conn.total_changes, before)
        self.assertEqual(dict(self.store.get_failure(source_key)), before_row)

    def test_removed_dead_letter_is_acknowledged_but_retained(self):
        detail = nef_watch.FailureDetail(
            "bad input", permanent=True, code="invalid-input"
        )
        self.store.record_failure(
            self.source, "fp", "cfg", detail, permanent=True
        )
        self.source.unlink()
        self.assertEqual(
            self.store.acknowledge_missing_failures(self.root, set()), 1
        )
        self.assertEqual(self.store.count_permanent_failures(), 0)
        retained = self.store.conn.execute(
            "SELECT active,error_text FROM failures WHERE source_path=?",
            (nef_watch.canonical_path(self.source),),
        ).fetchone()
        self.assertEqual(retained["active"], 0)
        self.assertEqual(retained["error_text"], "bad input")

    def test_absent_old_state_is_pruned_in_bounded_batches(self):
        present = nef_watch.source_state_key(self.source, self.root)
        missing = [f"{nef_watch.SOURCE_KEY_PREFIX}missing-{index}.NEF" for index in range(3)]
        now = time.time()
        with self.store.conn:
            self.store.conn.execute(
                """INSERT INTO sources
                   (source_path,fingerprint,config_fingerprint,status,output_metadata,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (present, "fp", "cfg", "success", "[]", 1),
            )
            for index, source_key in enumerate(missing):
                self.store.conn.execute(
                    """INSERT INTO sources
                       (source_path,fingerprint,config_fingerprint,status,output_metadata,updated_at)
                       VALUES(?,?,?,?,?,?)""",
                    (source_key, "fp", "cfg", "success", "[]", 1),
                )
                self.store.conn.execute(
                    """INSERT INTO failures
                       (source_path,fingerprint,config_fingerprint,attempts,next_retry_at,
                        permanent,active,error_code,error_text,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        source_key,
                        "fp",
                        "cfg",
                        1,
                        None,
                        1,
                        0,
                        "invalid-input",
                        "old",
                        1,
                    ),
                )
                self.store.conn.execute(
                    """INSERT INTO output_reservations
                       (output_path,source_path,kind,created_at) VALUES(?,?,?,?)""",
                    (f"/out/missing-{index}.tif", source_key, "tiff", 1),
                )
                self.store.conn.execute(
                    """INSERT INTO tracked_sources
                       (source_path,last_activity_at,absent_since)
                       VALUES(?,?,NULL)""",
                    (source_key, 1),
                )
            self.store.conn.execute(
                """INSERT INTO tracked_sources
                   (source_path,last_activity_at,absent_since)
                   VALUES(?,?,NULL)""",
                (present, 1),
            )

        first = self.store.prune_absent_state(
            {present}, older_than_seconds=60, max_rows=1, now=now
        )
        self.assertEqual(first, {"sources": 0, "failures": 0, "reservations": 0})
        self.assertIsNotNone(self.store.get_source(present))

        for _ in range(4):
            self.store.prune_absent_state(
                {present}, older_than_seconds=60, max_rows=1, now=now + 61
            )
        self.assertEqual(
            self.store.conn.execute(
                "SELECT COUNT(*) FROM sources WHERE source_path LIKE ?",
                (f"{nef_watch.SOURCE_KEY_PREFIX}missing-%",),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.store.conn.execute("SELECT COUNT(*) FROM failures").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.store.conn.execute(
                "SELECT COUNT(*) FROM output_reservations"
            ).fetchone()[0],
            0,
        )

    def test_transient_failures_retry_then_enter_durable_dead_letter(self):
        detail = nef_watch.FailureDetail("renderer unavailable", code="renderer")
        first = self.store.record_failure(
            self.source, "fp", "cfg", detail, max_retries=3, retry_base=0.1
        )
        second = self.store.record_failure(
            self.source, "fp", "cfg", detail, max_retries=3, retry_base=0.1
        )
        third = self.store.record_failure(
            self.source, "fp", "cfg", detail, max_retries=3, retry_base=0.1
        )
        self.assertEqual((first[0], second[0], third[0]), (1, 2, 3))
        self.assertFalse(first[1])
        self.assertTrue(third[1])
        self.assertEqual(self.store.count_permanent_failures(), 1)
        reset = self.store.record_failure(
            self.source, "new-fp", "cfg", detail, max_retries=3, retry_base=0.1
        )
        self.assertEqual(reset[0], 1)
        self.assertFalse(reset[1])

    def test_deterministic_failure_is_quarantined_immediately(self):
        detail = nef_watch.FailureDetail(
            "bad input", permanent=True, code="invalid-input"
        )
        attempts, dead, retry = self.store.record_failure(
            self.source, "fp", "cfg", detail, permanent=True
        )
        self.assertEqual(attempts, 1)
        self.assertTrue(dead)
        self.assertIsNone(retry)

    def test_permanent_failure_health_count_uses_partial_index(self):
        indexes = {
            row[1]
            for row in self.store.conn.execute("PRAGMA index_list(failures)")
        }
        self.assertIn("failures_active_permanent_idx", indexes)
        plan = " ".join(
            str(column)
            for row in self.store.conn.execute(
                "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM failures "
                "WHERE permanent=1 AND active=1"
            )
            for column in row
        )
        self.assertIn("failures_active_permanent_idx", plan)

    def test_relative_state_key_survives_mount_root_and_source_replacement(self):
        other_root = self.root / "other-mount"
        other_root.mkdir()
        other = other_root / self.source.name
        other.write_bytes(self.source.read_bytes())
        first_key = nef_watch.source_state_key(self.source, self.root)
        second_key = nef_watch.source_state_key(other, other_root)
        self.assertEqual(first_key, second_key)

        fingerprint, stat_identity = nef_watch.fingerprint_file(self.source)
        snapshot = nef_watch.InputSnapshot(
            source=self.source,
            path=self.source,
            fingerprint=fingerprint,
            stat_key=stat_identity,
        )
        job = nef_watch.PendingJob(
            source=self.source,
            canonical_source=first_key,
            snapshot=snapshot,
            fingerprint=fingerprint,
            config_fingerprint="cfg",
            force=False,
            source_key=first_key,
        )
        outside = self.root.parent / f"outside-{self.root.name}.NEF"
        outside.write_bytes(b"II*\0outside")
        self.addCleanup(outside.unlink, missing_ok=True)
        self.source.unlink()
        self.source.symlink_to(outside)
        detail = nef_watch.FailureDetail("renderer failed", code="renderer")
        nef_watch._record_conversion_result(
            self.store, job, "error", detail, self.args, self.out_dir
        )
        self.assertIsNotNone(self.store.get_failure(first_key))
        self.assertIsNone(self.store.get_failure(outside))

    def test_one_time_baseline_does_not_swallow_files_added_later(self):
        fp, _ = nef_watch.fingerprint_file(self.source)
        self.assertTrue(self.store.record_baseline("baseline:test", [(self.source, fp)]))
        later = self.root / "later.NEF"
        later.write_bytes(b"II*\0later")
        later_fp, _ = nef_watch.fingerprint_file(later)
        self.assertFalse(
            self.store.record_baseline("baseline:test", [(later, later_fp)])
        )
        self.assertEqual(self.store.get_source(self.source)["status"], "baseline")
        self.assertIsNone(self.store.get_source(later))

    def test_baseline_source_change_never_claims_or_replaces_existing_output(self):
        fp, _ = nef_watch.fingerprint_file(self.source)
        self.store.record_baseline("baseline:test", [(self.source, fp)])
        action = nef_watch.source_action(
            self.store, self.source, fp, "cfg", self.args, self.out_dir
        )
        self.assertEqual(action[0], "skip")
        changed = json.loads(fp)
        changed["sha256"] = "0" * 64
        action = nef_watch.source_action(
            self.store,
            self.source,
            json.dumps(changed, sort_keys=True, separators=(",", ":")),
            "cfg",
            self.args,
            self.out_dir,
        )
        self.assertEqual(action[:2], ("quarantine", False))
        self.assertIn("collision", action[2])

    def test_integrated_unowned_collision_never_reaches_renderer(self):
        before = self.output.read_bytes()
        self.args.input = self.root
        with mock.patch.object(nef_watch, "render_raster") as renderer:
            result = nef_watch.run_once(
                self.args,
                self.out_dir,
                b"icc",
                state=self.store,
                config_fingerprint="cfg",
            )
        self.assertEqual(result, 1)
        renderer.assert_not_called()
        self.assertEqual(self.output.read_bytes(), before)
        failure = self.store.get_failure(
            nef_watch.source_state_key(self.source, self.root)
        )
        self.assertIsNotNone(failure)
        self.assertEqual(failure["error_code"], "output-collision")
        self.assertTrue(failure["permanent"])
        self.output.unlink()
        fingerprint, _ = nef_watch.fingerprint_file(self.source)
        action, force, _ = nef_watch.source_action(
            self.store,
            self.source,
            fingerprint,
            "cfg",
            self.args,
            self.out_dir,
            source_key=nef_watch.source_state_key(self.source, self.root),
        )
        self.assertEqual((action, force), ("convert", False))
        self.assertIsNone(
            self.store.get_failure(
                nef_watch.source_state_key(self.source, self.root)
            )
        )

    def test_durable_owned_output_can_be_replaced_after_source_change(self):
        old_fingerprint, _ = nef_watch.fingerprint_file(self.source)
        old_metadata = nef_watch.capture_output_metadata(
            [("tiff", self.output)]
        )
        self.store.record_success(
            self.source, old_fingerprint, "cfg", old_metadata
        )
        self.source.write_bytes(b"II*\0changed-source-data")
        new_fingerprint, _ = nef_watch.fingerprint_file(self.source)
        action, force, detail = nef_watch.source_action(
            self.store,
            self.source,
            new_fingerprint,
            "cfg",
            self.args,
            self.out_dir,
        )
        self.assertEqual((action, force), ("convert", True))
        self.assertEqual(len(detail.owned_outputs), 1)

        def fake_render(_source, todo, _args, _icc):
            staged = []
            for kind, final in todo:
                partial = nef_watch.unique_partial(final)
                write_valid_tiff(partial, color=(11, 12, 13))
                staged.append((kind, partial, final))
            return staged

        with mock.patch.object(
            nef_watch, "render_raster", side_effect=fake_render
        ):
            status, conversion_detail = nef_watch.convert_one(
                self.source,
                self.out_dir,
                self.args,
                b"icc",
                force=force,
                source_path=self.source,
                expected_fingerprint=new_fingerprint,
                owned_outputs=detail.owned_outputs,
            )
        self.assertEqual(status, "ok", conversion_detail)
        with nef_watch.Image.open(self.output) as image:
            self.assertEqual(image.getpixel((0, 0)), (11, 12, 13))

    def test_health_json_is_atomic_and_has_monitor_contract(self):
        self.store.write_health(
            "degraded", last_scan_at=123.5, pending=2, inflight=1,
            permanent_failures=4,
        )
        payload = json.loads(self.store.health_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], nef_watch.HEALTH_SCHEMA)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["last_scan_at"], 123.5)
        self.assertEqual(payload["pending"], 2)
        self.assertEqual(payload["inflight"], 1)
        self.assertEqual(payload["permanent_failures"], 4)
        self.assertEqual(payload["watcher_pid"], os.getpid())
        if sys.platform.startswith("linux"):
            self.assertIsInstance(payload["watcher_start_ticks"], int)
            self.assertGreater(payload["watcher_start_ticks"], 0)
        self.assertIsInstance(payload["updated_at"], float)
        self.assertEqual(list(self.state_dir.glob(".health.*.tmp")), [])

    def test_idle_health_writes_are_rate_limited_but_counter_changes_are_immediate(self):
        self.store.health_write_interval = 60
        self.assertTrue(
            self.store.write_health("healthy", last_scan_at=1, pending=0, inflight=0)
        )
        first = self.store.health_path.stat().st_mtime_ns
        self.assertFalse(
            self.store.write_health("healthy", last_scan_at=2, pending=0, inflight=0)
        )
        self.assertEqual(self.store.health_path.stat().st_mtime_ns, first)
        self.assertTrue(
            self.store.write_health("healthy", last_scan_at=3, pending=1, inflight=0)
        )
        payload = json.loads(self.store.health_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["last_scan_at"], 3)
        self.assertEqual(payload["pending"], 1)

    def test_state_directory_has_a_single_process_lifetime_owner(self):
        with self.assertRaises(BlockingIOError):
            nef_watch.StateStore(self.state_dir)

    def test_skip_existing_baselines_empty_and_invalid_inputs_without_aborting(self):
        invalid = self.root / "invalid.NEF"
        empty = self.root / "empty.NEF"
        invalid.write_bytes(b"not-a-nef")
        empty.write_bytes(b"")
        self.args.input = self.root
        self.args.skip_existing = True
        self.assertTrue(
            nef_watch.initialize_skip_existing(self.args, self.store, self.out_dir)
        )
        invalid_key = nef_watch.source_state_key(invalid, self.args.input_root)
        empty_key = nef_watch.source_state_key(empty, self.args.input_root)
        self.assertEqual(self.store.get_source(invalid_key)["status"], "baseline")
        self.assertEqual(self.store.get_source(empty_key)["status"], "baseline")
        empty.write_bytes(b"II*\0completed")
        completed_fp, _ = nef_watch.fingerprint_file(empty)
        action, force, _ = nef_watch.source_action(
            self.store,
            empty,
            completed_fp,
            "cfg",
            self.args,
            self.out_dir,
            source_key=empty_key,
        )
        self.assertEqual((action, force), ("convert", False))

    def test_skip_existing_quarantines_duplicate_stems_and_commits_the_rest(self):
        conflict_nef = self.root / "same.NEF"
        conflict_nrw = self.root / "same.NRW"
        unique = self.root / "unique.NEF"
        for path in (conflict_nef, conflict_nrw, unique):
            path.write_bytes(b"II*\0" + path.name.encode("ascii"))
        self.args.input = self.root
        self.args.input_root = self.root
        self.args.skip_existing = True

        created = nef_watch.initialize_skip_existing(
            self.args,
            self.store,
            self.out_dir,
            config_fingerprint="cfg",
        )

        self.assertTrue(created)
        self.assertEqual(
            self.store.get_meta(nef_watch._baseline_key(self.args)), "complete"
        )
        unique_key = nef_watch.source_state_key(unique, self.root)
        self.assertEqual(self.store.get_source(unique_key)["status"], "baseline")
        for conflict in (conflict_nef, conflict_nrw):
            source_key = nef_watch.source_state_key(conflict, self.root)
            self.assertIsNone(self.store.get_source(source_key))
            failure = self.store.get_failure(source_key)
            self.assertIsNotNone(failure)
            self.assertTrue(failure["permanent"])
            self.assertEqual(failure["error_code"], "source-output-collision")
            action, _, _ = nef_watch.source_action(
                self.store,
                conflict,
                failure["fingerprint"],
                "cfg",
                self.args,
                self.out_dir,
                source_key=source_key,
            )
            self.assertEqual(action, "quarantine")
        reserved = self.store.conn.execute(
            "SELECT output_path,source_path FROM output_reservations ORDER BY source_path"
        ).fetchall()
        self.assertIn(unique_key, [row["source_path"] for row in reserved])
        self.assertNotIn(
            nef_watch.reservation_key(self.out_dir / "same.tif"),
            [row["output_path"] for row in reserved],
        )

    def test_skip_existing_quarantines_reserved_recursive_mapping(self):
        reserved_dir = self.root / nef_watch.ARTIFACT_DIR_NAME
        reserved_dir.mkdir()
        reserved = reserved_dir / "uploaded.NEF"
        reserved.write_bytes(b"II*\0reserved")
        self.args.input = self.root
        self.args.input_root = self.root
        self.args.recursive = True

        created = nef_watch.initialize_skip_existing(
            self.args,
            self.store,
            self.out_dir,
            config_fingerprint="cfg",
        )

        self.assertTrue(created)
        self.assertEqual(
            self.store.get_meta(nef_watch._baseline_key(self.args)), "complete"
        )
        safe_key = nef_watch.source_state_key(self.source, self.root)
        self.assertEqual(self.store.get_source(safe_key)["status"], "baseline")
        reserved_key = nef_watch.source_state_key(reserved, self.root)
        self.assertIsNone(self.store.get_source(reserved_key))
        failure = self.store.get_failure(reserved_key)
        self.assertIsNotNone(failure)
        self.assertTrue(failure["permanent"])
        self.assertEqual(failure["error_code"], "source-output-mapping")

    def test_legacy_baseline_quarantines_reserved_recursive_mapping(self):
        reserved_dir = self.root / nef_watch.ARTIFACT_DIR_NAME
        reserved_dir.mkdir()
        reserved = reserved_dir / "uploaded.NEF"
        reserved.write_bytes(b"II*\0reserved")
        marker = self.out_dir / nef_watch.LEGACY_BASELINE_MARKER
        marker.write_text(
            f"{self.source.resolve()}\n{reserved.resolve()}\n",
            encoding="utf-8",
        )
        self.args.input = self.root
        self.args.input_root = self.root
        self.args.recursive = True

        self.assertTrue(
            nef_watch.initialize_skip_existing(
                self.args,
                self.store,
                self.out_dir,
                config_fingerprint="cfg",
            )
        )

        safe_key = nef_watch.source_state_key(self.source, self.root)
        self.assertEqual(self.store.get_source(safe_key)["status"], "baseline")
        reserved_key = nef_watch.source_state_key(reserved, self.root)
        failure = self.store.get_failure(reserved_key)
        self.assertIsNotNone(failure)
        self.assertEqual(failure["error_code"], "source-output-mapping")
        self.assertFalse(marker.exists())

    def test_interrupted_baseline_never_sets_completion_marker(self):
        self.args.input = self.root
        baseline_key = nef_watch._baseline_key(self.args)

        def interrupt_after_hash(*_args, **_kwargs):
            nef_watch.STOP_EVENT.set()
            return "fingerprint", (1, 2, 3, 4, 5)

        nef_watch.STOP_EVENT.clear()
        try:
            with mock.patch.object(
                nef_watch, "fingerprint_source", side_effect=interrupt_after_hash
            ):
                self.assertFalse(
                    nef_watch.initialize_skip_existing(
                        self.args, self.store, self.out_dir
                    )
                )
            self.assertIsNone(self.store.get_meta(baseline_key))
            rows = self.store.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
            self.assertEqual(rows, 0)
        finally:
            nef_watch.STOP_EVENT.clear()
            nef_watch.FATAL_STOP_EVENT.clear()

    def test_legacy_marker_imports_only_recorded_paths_and_preserves_marker(self):
        arrival = self.root / "arrived-after-marker.NEF"
        arrival.write_bytes(b"II*\0arrival")
        marker = self.out_dir / nef_watch.LEGACY_BASELINE_MARKER
        marker.write_text(
            f"{self.source.resolve()}\n{self.source.resolve()}\n",
            encoding="utf-8",
        )
        self.args.input = self.root
        self.assertTrue(
            nef_watch.initialize_skip_existing(self.args, self.store, self.out_dir)
        )
        self.assertEqual(
            self.store.get_source(
                nef_watch.source_state_key(self.source, self.args.input_root)
            )["status"],
            "baseline",
        )
        self.assertIsNone(self.store.get_source(arrival))
        self.assertFalse(marker.exists())
        self.assertTrue(
            (self.out_dir / f"{nef_watch.LEGACY_BASELINE_MARKER}.imported").is_file()
        )

    def test_legacy_marker_archive_never_replaces_occupied_names(self):
        marker = self.out_dir / nef_watch.LEGACY_BASELINE_MARKER
        marker_bytes = f"{self.source.resolve()}\n".encode("utf-8")
        marker.write_bytes(marker_bytes)
        digest = nef_watch.hashlib.sha256(marker_bytes).hexdigest()[:12]
        first = marker.with_name(marker.name + ".imported")
        second = marker.with_name(marker.name + f".imported-{digest}")
        first.write_bytes(b"foreign-first")
        second.write_bytes(b"foreign-second")
        self.args.input = self.root

        self.assertTrue(
            nef_watch.initialize_skip_existing(self.args, self.store, self.out_dir)
        )

        archive = marker.with_name(marker.name + f".imported-{digest}-01")
        self.assertEqual(first.read_bytes(), b"foreign-first")
        self.assertEqual(second.read_bytes(), b"foreign-second")
        self.assertEqual(archive.read_bytes(), marker_bytes)
        self.assertFalse(marker.exists())

    def test_legacy_marker_is_retained_when_archive_names_stay_occupied(self):
        marker = self.out_dir / nef_watch.LEGACY_BASELINE_MARKER
        marker_bytes = f"{self.source.resolve()}\n".encode("utf-8")
        marker.write_bytes(marker_bytes)
        self.args.input = self.root

        with mock.patch.object(
            nef_watch,
            "_publish_output_no_replace",
            side_effect=nef_watch.OutputCollisionError("occupied"),
        ) as publish:
            self.assertTrue(
                nef_watch.initialize_skip_existing(
                    self.args, self.store, self.out_dir
                )
            )

        self.assertEqual(
            publish.call_count, nef_watch.MAX_LEGACY_ARCHIVE_ATTEMPTS
        )
        self.assertEqual(marker.read_bytes(), marker_bytes)

    def test_empty_legacy_marker_is_a_valid_zero_entry_baseline(self):
        marker = self.out_dir / nef_watch.LEGACY_BASELINE_MARKER
        marker.write_bytes(b"")
        self.args.input = self.root
        self.assertTrue(
            nef_watch.initialize_skip_existing(self.args, self.store, self.out_dir)
        )
        self.assertEqual(
            self.store.get_meta(nef_watch._baseline_key(self.args)), "complete"
        )
        self.assertIsNone(self.store.get_source(self.source))
        self.assertTrue(
            (self.out_dir / f"{nef_watch.LEGACY_BASELINE_MARKER}.imported").is_file()
        )

    def test_legacy_root_explicitly_remaps_old_absolute_namespace(self):
        nested = self.root / "camera" / "photo.NEF"
        nested.parent.mkdir()
        nested.write_bytes(b"II*\0nested")
        marker = self.out_dir / nef_watch.LEGACY_BASELINE_MARKER
        marker.write_text("/old/upload/camera/photo.NEF\n", encoding="utf-8")
        self.args.input = self.root
        self.args.recursive = True
        self.args.legacy_input_root = Path("/old/upload")
        self.assertTrue(
            nef_watch.initialize_skip_existing(self.args, self.store, self.out_dir)
        )
        self.assertEqual(
            self.store.get_source(
                nef_watch.source_state_key(nested, self.args.input_root)
            )["status"],
            "baseline",
        )

    def test_legacy_marker_symlink_is_rejected_without_following(self):
        target = self.root / "marker-target"
        target.write_text(f"{self.source.resolve()}\n", encoding="utf-8")
        marker = self.out_dir / nef_watch.LEGACY_BASELINE_MARKER
        marker.symlink_to(target)
        self.args.input = self.root
        with self.assertRaisesRegex(
            nef_watch.PermanentConversionError, "cannot safely read"
        ):
            nef_watch.initialize_skip_existing(self.args, self.store, self.out_dir)
        self.assertIsNone(self.store.get_meta(nef_watch._baseline_key(self.args)))
        self.assertTrue(marker.is_symlink())

    def test_invalid_legacy_marker_fails_without_completing_baseline(self):
        marker = self.out_dir / nef_watch.LEGACY_BASELINE_MARKER
        marker.write_text("/outside/input/escape.NEF\n", encoding="utf-8")
        self.args.input = self.root
        with self.assertRaisesRegex(
            nef_watch.PermanentConversionError, "escapes input root"
        ):
            nef_watch.initialize_skip_existing(self.args, self.store, self.out_dir)
        self.assertIsNone(self.store.get_meta(nef_watch._baseline_key(self.args)))
        self.assertIsNone(self.store.get_source(self.source))
        self.assertTrue(marker.is_file())


class OutputTransactionTests(unittest.TestCase):
    def test_durable_size_mismatch_is_rejected_before_image_decode(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "photo.NEF"
            source.write_bytes(b"II*\0source")
            output = root / "photo.tif"
            write_valid_tiff(output)
            args = watcher_args(root)
            metadata = nef_watch.capture_output_metadata([("tiff", output)])
            output.write_bytes(b"II*\0foreign replacement with another size")

            with mock.patch.object(nef_watch.Image, "open") as decoder:
                self.assertFalse(
                    nef_watch.outputs_match_metadata(
                        source, args, root, json.dumps(metadata)
                    )
                )
            decoder.assert_not_called()

    def test_durable_hash_mismatch_is_rejected_before_image_decode(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "photo.NEF"
            source.write_bytes(b"II*\0source")
            output = root / "photo.tif"
            write_valid_tiff(output)
            args = watcher_args(root)
            metadata = nef_watch.capture_output_metadata([("tiff", output)])
            size = output.stat().st_size
            output.write_bytes(b"II*\0" + b"x" * (size - 4))
            os.utime(
                output,
                ns=(metadata[0]["mtime_ns"], metadata[0]["mtime_ns"]),
            )

            with mock.patch.object(nef_watch.Image, "open") as decoder:
                self.assertFalse(
                    nef_watch.outputs_match_metadata(
                        source, args, root, json.dumps(metadata)
                    )
                )
            decoder.assert_not_called()

    def test_changed_source_owned_output_is_proven_before_decode(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "photo.NEF"
            source.write_bytes(b"II*\0source")
            output = root / "photo.tif"
            write_valid_tiff(output)
            args = watcher_args(root)
            store = nef_watch.StateStore(root / "state")
            self.addCleanup(store.close)
            key = nef_watch.source_state_key(source, root)
            metadata = nef_watch.capture_output_metadata([("tiff", output)])
            store.record_success(key, "old-source", "cfg", metadata)
            output.write_bytes(b"II*\0foreign replacement with another size")

            with mock.patch.object(nef_watch.Image, "open") as decoder:
                action, _, detail = nef_watch.source_action(
                    store,
                    source,
                    "changed-source",
                    "cfg",
                    args,
                    root,
                    source_key=key,
                )
            self.assertEqual(action, "quarantine")
            self.assertIn("cannot be proven owned", str(detail))
            decoder.assert_not_called()

    def test_preexisting_artifact_directory_must_be_private_and_owned(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact_root = root / nef_watch.ARTIFACT_DIR_NAME
            artifact_root.mkdir(mode=0o755)
            sentinel = artifact_root / "user-data"
            sentinel.write_bytes(b"preserve")

            with self.assertRaisesRegex(
                nef_watch.UnsafeFileError, "private and authenticated"
            ):
                nef_watch.unique_partial(root / "photo.tif")

            self.assertEqual(sentinel.read_bytes(), b"preserve")

    def test_transaction_artifacts_use_private_uuid_only_namespace(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            final = root / "nested" / "photo.tif"
            final.parent.mkdir()

            staged = nef_watch.unique_partial(
                final,
                output_root=root,
                root_identity=nef_watch._directory_identity(root),
            )

            self.assertEqual(
                staged.parent,
                final.parent
                / nef_watch.ARTIFACT_DIR_NAME
                / nef_watch.ARTIFACT_STAGE_DIR,
            )
            self.assertRegex(staged.name, r"^[0-9a-f]{32}$")
            self.assertEqual(
                staged.parent.parent.stat().st_mode & 0o777,
                0o700,
            )

    def test_recursive_mapping_rejects_reserved_artifact_component(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source_dir = root / nef_watch.ARTIFACT_DIR_NAME
            source_dir.mkdir()
            source = source_dir / "photo.NEF"
            source.write_bytes(b"II*\0source")
            output = root / "output"
            output.mkdir()
            args = watcher_args(root)
            args.recursive = True

            with self.assertRaisesRegex(
                nef_watch.OutputCollisionError, "reserved"
            ):
                nef_watch.expected_outputs(source, args, output)

    def test_default_publication_refuses_unowned_preexisting_output(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            final = root / "photo.tif"
            staged = nef_watch.unique_partial(final)
            staged.write_bytes(b"II*\0new")
            final.write_bytes(b"II*\0manual")
            with self.assertRaisesRegex(
                nef_watch.OutputCollisionError, "unowned"
            ):
                nef_watch.commit_staged_outputs(
                    [("tiff", staged, final)],
                    output_root=root,
                    root_identity=nef_watch._directory_identity(root),
                )
            self.assertEqual(final.read_bytes(), b"II*\0manual")
            self.assertEqual(staged.read_bytes(), b"II*\0new")

    def test_dng_validation_uses_descriptor_name_and_checks_tags_and_payload(self):
        import tifffile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            valid = root / "valid.dng"
            tifffile.imwrite(
                valid,
                nef_watch.np.zeros((2, 2), dtype=nef_watch.np.uint16),
                extratags=[(50706, "B", 4, (1, 4, 0, 0), False)],
            )
            self.assertTrue(nef_watch.validate_output_file(valid, "dng"))

            plain_tiff = root / "plain-tiff.dng"
            tifffile.imwrite(
                plain_tiff,
                nef_watch.np.zeros((2, 2), dtype=nef_watch.np.uint16),
            )
            self.assertFalse(nef_watch.validate_output_file(plain_tiff, "dng"))

            truncated = root / "truncated.dng"
            truncated.write_bytes(valid.read_bytes()[:-1])
            self.assertFalse(nef_watch.validate_output_file(truncated, "dng"))

    def test_truncated_image_with_valid_magic_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "truncated.tif"
            path.write_bytes(b"II*\0" + b"\0" * 32)
            self.assertFalse(nef_watch.validate_output_file(path, "tiff"))

    def test_decoder_specific_runtime_error_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "external.tif"
            path.write_bytes(b"II*\0" + b"\0" * 64)
            with mock.patch.object(
                nef_watch.Image, "open", side_effect=RuntimeError("decoder plugin")
            ):
                self.assertFalse(nef_watch.validate_output_file(path, "tiff"))

    def test_output_validation_rejects_symlink_and_dimensions_before_decode(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "target.tif"
            write_valid_tiff(target)
            link = root / "linked.tif"
            link.symlink_to(target)
            self.assertFalse(nef_watch.validate_output_file(link, "tiff"))

            fake_image = mock.MagicMock()
            fake_image.format = "TIFF"
            fake_image.mode = "RGB"
            fake_image.width = nef_watch.MAX_RENDER_PIXELS + 1
            fake_image.height = 1
            image_context = mock.MagicMock()
            image_context.__enter__.return_value = fake_image
            with mock.patch.object(
                nef_watch.Image, "open", return_value=image_context
            ):
                self.assertFalse(nef_watch.validate_output_file(target, "tiff"))
            fake_image.load.assert_not_called()

    def test_output_stat_cache_detects_replacement_with_restored_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "photo.NEF"
            source.write_bytes(b"II*\0source")
            output = root / "photo.tif"
            write_valid_tiff(output, color=(1, 2, 3))
            args = watcher_args(root)
            before = nef_watch.output_stat_signature(source, args, root)
            old_mtime = output.stat().st_mtime_ns

            replacement = root / "replacement.tif"
            write_valid_tiff(replacement, color=(9, 8, 7))
            self.assertEqual(replacement.stat().st_size, output.stat().st_size)
            os.replace(replacement, output)
            os.utime(output, ns=(old_mtime, old_mtime))
            after = nef_watch.output_stat_signature(source, args, root)
            self.assertNotEqual(before, after)

    def test_verified_cache_signature_comes_from_validating_descriptor(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "photo.NEF"
            source.write_bytes(b"II*\0source")
            output = root / "photo.tif"
            write_valid_tiff(output, color=(1, 2, 3))
            args = watcher_args(root)
            state = nef_watch.StateStore(root / "state")
            self.addCleanup(state.close)
            fingerprint, _ = nef_watch.fingerprint_file(source)
            metadata = nef_watch.capture_output_metadata([("tiff", output)])
            source_key = nef_watch.source_state_key(source, root)
            state.record_success(source_key, fingerprint, "cfg", metadata)

            old_mtime = output.stat().st_mtime_ns
            real_prove = nef_watch._prove_durable_output
            replaced = False

            def replace_after_validation(*prove_args, **prove_kwargs):
                nonlocal replaced
                inspected = real_prove(*prove_args, **prove_kwargs)
                if not replaced:
                    replacement = root / "replacement.tif"
                    write_valid_tiff(replacement, color=(9, 8, 7))
                    os.replace(replacement, output)
                    os.utime(output, ns=(old_mtime, old_mtime))
                    replaced = True
                return inspected

            with mock.patch.object(
                nef_watch,
                "_prove_durable_output",
                side_effect=replace_after_validation,
            ):
                action, _, detail = nef_watch.source_action(
                    state,
                    source,
                    fingerprint,
                    "cfg",
                    args,
                    root,
                    source_key=source_key,
                )
            self.assertEqual(action, "skip")
            self.assertNotEqual(
                detail.output_signature,
                nef_watch.output_stat_signature(source, args, root),
            )

    def test_publish_fsyncs_file_before_rename_and_directory_after_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            final = root / "photo.tif"
            staged = nef_watch.unique_partial(final)
            staged.write_bytes(b"II*\0new")
            final.write_bytes(b"II*\0old")
            events = []
            real_fsync = os.fsync
            real_replace = os.replace
            real_unlink = os.unlink

            def record_fsync(descriptor):
                mode = os.fstat(descriptor).st_mode
                events.append(
                    "fsync-directory"
                    if nef_watch.stat_module.S_ISDIR(mode)
                    else "fsync-file"
                )
                return real_fsync(descriptor)

            def record_replace(*args, **kwargs):
                events.append("replace")
                return real_replace(*args, **kwargs)

            def record_unlink(*args, **kwargs):
                events.append("unlink")
                return real_unlink(*args, **kwargs)

            with mock.patch.object(
                nef_watch.os, "fsync", side_effect=record_fsync
            ), mock.patch.object(
                nef_watch.os, "replace", side_effect=record_replace
            ), mock.patch.object(
                nef_watch.os, "unlink", side_effect=record_unlink
            ):
                nef_watch.commit_staged_outputs(
                    [("tiff", staged, final)],
                    output_root=root,
                    root_identity=nef_watch._directory_identity(root),
                    allow_overwrite=True,
                )

            self.assertLess(events.index("fsync-file"), events.index("replace"))
            self.assertIn("fsync-directory", events[events.index("replace") + 1 :])
            last_unlink = len(events) - 1 - events[::-1].index("unlink")
            self.assertIn("fsync-directory", events[last_unlink + 1 :])

    def test_directory_fsync_failure_after_rename_rolls_back_old_output(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            final = root / "photo.tif"
            staged = nef_watch.unique_partial(final)
            staged.write_bytes(b"II*\0new")
            final.write_bytes(b"II*\0old")
            identity = nef_watch._directory_identity(root)
            nef_watch._artifact_path_for_final(
                final,
                nef_watch.ARTIFACT_BACKUP_DIR,
                root,
                identity,
            )
            real_fsync = os.fsync
            calls = 0

            def fail_first_directory_sync(descriptor):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError(nef_watch.errno.EIO, "simulated sync failure")
                return real_fsync(descriptor)

            with mock.patch.object(
                nef_watch.os, "fsync", side_effect=fail_first_directory_sync
            ):
                with self.assertRaisesRegex(
                    nef_watch.TransientConversionError, "transaction"
                ):
                    nef_watch.commit_staged_outputs(
                        [("tiff", staged, final)],
                        output_root=root,
                        root_identity=identity,
                        allow_overwrite=True,
                    )
            self.assertEqual(final.read_bytes(), b"II*\0old")

    def test_owned_replacement_race_restores_foreign_file_without_unlinking_it(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            final = root / "photo.tif"
            staged = nef_watch.unique_partial(final)
            foreign = root / "foreign.tif"
            staged.write_bytes(b"II*\0new")
            final.write_bytes(b"II*\0owned")
            foreign.write_bytes(b"II*\0foreign")
            root_identity = nef_watch._directory_identity(root)
            owned = nef_watch._current_output_signature(
                final, "tiff", root, root_identity
            )
            real_signature = nef_watch._current_output_signature
            raced = False

            def replace_after_preflight(path, *signature_args, **signature_kwargs):
                nonlocal raced
                signature = real_signature(
                    path, *signature_args, **signature_kwargs
                )
                if Path(path) == final and not raced:
                    os.replace(foreign, final)
                    raced = True
                return signature

            with mock.patch.object(
                nef_watch,
                "_current_output_signature",
                side_effect=replace_after_preflight,
            ):
                with self.assertRaisesRegex(
                    nef_watch.OutputCollisionError, "changed"
                ):
                    nef_watch.commit_staged_outputs(
                        [("tiff", staged, final)],
                        output_root=root,
                        root_identity=root_identity,
                        owned_outputs=(owned,),
                    )

            self.assertEqual(final.read_bytes(), b"II*\0foreign")
            self.assertTrue(staged.exists())

    def test_post_prepare_handoff_race_restores_the_inode_actually_moved(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            final = root / "photo.tif"
            final.write_bytes(b"II*\0owned")
            staged = nef_watch.unique_partial(final)
            staged.write_bytes(b"II*\0new")
            foreign = root / "foreign.tif"
            foreign.write_bytes(b"II*\0foreign")
            identity = nef_watch._directory_identity(root)
            plan = nef_watch.prepare_publication(
                [("tiff", staged, final)],
                source_key="source",
                fingerprint="fingerprint",
                config_fingerprint="config",
                output_root=root,
                root_identity=identity,
                allow_overwrite=True,
            )
            real_replace = nef_watch._replace_output
            injected = False

            def inject_foreign_before_handoff(source, target, *args, **kwargs):
                nonlocal injected
                if Path(source) == final and not injected:
                    os.replace(foreign, final)
                    injected = True
                return real_replace(source, target, *args, **kwargs)

            with mock.patch.object(
                nef_watch,
                "_replace_output",
                side_effect=inject_foreign_before_handoff,
            ):
                with self.assertRaisesRegex(
                    nef_watch.OutputCollisionError, "handoff"
                ):
                    nef_watch.execute_publication(plan, root, identity)

            self.assertEqual(final.read_bytes(), b"II*\0foreign")
            self.assertFalse(Path(plan.outputs[0]["backup_path"]).exists())
            self.assertTrue(nef_watch.rollback_publication(plan, root, identity))

    def test_multi_output_handoff_race_rolls_back_every_exact_inode(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            finals = [root / "photo.tif", root / "photo.jpg"]
            stages = [nef_watch.unique_partial(final) for final in finals]
            finals[0].write_bytes(b"old-tiff")
            finals[1].write_bytes(b"old-jpeg")
            stages[0].write_bytes(b"new-tiff")
            stages[1].write_bytes(b"new-jpeg")
            foreign = root / "foreign.jpg"
            foreign.write_bytes(b"foreign-jpeg")
            identity = nef_watch._directory_identity(root)
            plan = nef_watch.prepare_publication(
                [
                    ("tiff", stages[0], finals[0]),
                    ("jpeg", stages[1], finals[1]),
                ],
                source_key="source",
                fingerprint="fingerprint",
                config_fingerprint="config",
                output_root=root,
                root_identity=identity,
                allow_overwrite=True,
            )
            real_replace = nef_watch._replace_output
            injected = False

            def race_second(source, target, *args, **kwargs):
                nonlocal injected
                if Path(source) == finals[1] and not injected:
                    os.replace(foreign, finals[1])
                    injected = True
                return real_replace(source, target, *args, **kwargs)

            with mock.patch.object(
                nef_watch, "_replace_output", side_effect=race_second
            ):
                with self.assertRaises(nef_watch.OutputCollisionError):
                    nef_watch.execute_publication(plan, root, identity)

            self.assertEqual(finals[0].read_bytes(), b"old-tiff")
            self.assertEqual(finals[1].read_bytes(), b"foreign-jpeg")
            self.assertTrue(nef_watch.rollback_publication(plan, root, identity))
            self.assertFalse(any(stage.exists() for stage in stages))
            self.assertFalse(
                any(
                    entry["backup_path"]
                    and Path(entry["backup_path"]).exists()
                    for entry in plan.outputs
                )
            )

    def test_maximum_length_final_name_does_not_expand_artifact_component(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            final = root / ("p" * 251 + ".tif")
            final.write_bytes(b"old")
            staged = nef_watch.unique_partial(final)
            staged.write_bytes(b"new")
            plan = nef_watch.prepare_publication(
                [("tiff", staged, final)],
                source_key="source",
                fingerprint="fingerprint",
                config_fingerprint="config",
                output_root=root,
                root_identity=nef_watch._directory_identity(root),
                allow_overwrite=True,
            )
            self.assertEqual(len(staged.name), 32)
            self.assertEqual(len(Path(plan.outputs[0]["backup_path"]).name), 32)

    def test_publish_failure_removes_newly_published_partial_set(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first_final = root / "first.tif"
            second_final = root / "second.jpg"
            first_tmp = nef_watch.unique_partial(first_final)
            second_tmp = nef_watch.unique_partial(second_final)
            first_tmp.write_bytes(b"II*\0one")
            second_tmp.write_bytes(b"\xff\xd8\xfftwo")
            real_publish = nef_watch._publish_output_no_replace
            calls = 0

            def fail_second(*publish_args, **publish_kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("disk went away")
                return real_publish(*publish_args, **publish_kwargs)

            staged = [
                ("tiff", first_tmp, first_final),
                ("jpeg", second_tmp, second_final),
            ]
            with mock.patch.object(
                nef_watch,
                "_publish_output_no_replace",
                side_effect=fail_second,
            ):
                with self.assertRaisesRegex(
                    nef_watch.TransientConversionError, "transaction"
                ):
                    nef_watch.commit_staged_outputs(staged)
            self.assertFalse(first_final.exists())
            self.assertFalse(second_final.exists())

    def test_rollback_never_unlinks_a_foreign_replacement_of_published_output(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first_final = root / "first.tif"
            second_final = root / "second.jpg"
            first_tmp = nef_watch.unique_partial(first_final)
            second_tmp = nef_watch.unique_partial(second_final)
            foreign = root / "foreign.tif"
            first_tmp.write_bytes(b"II*\0one")
            second_tmp.write_bytes(b"\xff\xd8\xfftwo")
            foreign.write_bytes(b"foreign-user-data")
            real_publish = nef_watch._publish_output_no_replace
            calls = 0

            def race_then_fail(*publish_args, **publish_kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    result = real_publish(*publish_args, **publish_kwargs)
                    os.replace(foreign, first_final)
                    return result
                raise OSError(nef_watch.errno.EIO, "disk went away")

            with mock.patch.object(
                nef_watch,
                "_publish_output_no_replace",
                side_effect=race_then_fail,
            ):
                with self.assertRaisesRegex(
                    nef_watch.TransientConversionError, "transaction"
                ):
                    nef_watch.commit_staged_outputs(
                        [
                            ("tiff", first_tmp, first_final),
                            ("jpeg", second_tmp, second_final),
                        ],
                        output_root=root,
                        root_identity=nef_watch._directory_identity(root),
                    )
            self.assertEqual(first_final.read_bytes(), b"foreign-user-data")


class StartupRecoveryTests(unittest.TestCase):
    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory read permissions")
    def test_unreadable_temp_root_reports_incomplete_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "temp"
            root.mkdir(mode=0o300)
            descriptor, path = tempfile.mkstemp(
                prefix="nef-watch-input-", dir=root
            )
            os.close(descriptor)
            recovery = nef_watch.TempArtifactRecovery(root)
            self.addCleanup(recovery.close)

            try:
                progress = recovery.recover_batch(
                    older_than_seconds=0, now=time.time(), max_entries=100
                )
                self.assertTrue(Path(path).exists())
            finally:
                root.chmod(0o700)

            self.assertFalse(progress.sweep_complete)
            self.assertEqual(progress.error_count, 1)

    def test_complete_recovery_sweep_fails_closed_at_global_ceiling(self):
        class EndlessRecovery:
            def recover_batch(self, *_args, **kwargs):
                return nef_watch.RecoveryProgress(
                    0, kwargs["max_entries"], False
                )

        progress = nef_watch.complete_recovery_sweep(
            EndlessRecovery(), 0, now=1, max_entries=5
        )

        self.assertEqual(progress.examined, 5)
        self.assertTrue(progress.limit_exceeded)
        self.assertFalse(progress.sweep_complete)

    def test_reserved_artifact_name_as_regular_file_degrades_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            obstruction = root / nef_watch.ARTIFACT_DIR_NAME
            obstruction.write_bytes(b"user-controlled obstruction")
            recovery = nef_watch.OutputArtifactRecovery(root, recursive=False)
            self.addCleanup(recovery.close)

            progress = recovery.recover_batch(
                older_than_seconds=1, now=10, max_entries=100
            )

            self.assertEqual(progress.error_count, 1)
            self.assertTrue(progress.sweep_complete)
            self.assertEqual(
                obstruction.read_bytes(), b"user-controlled obstruction"
            )

    def test_reserved_artifact_name_as_symlink_degrades_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "foreign-artifacts"
            target.mkdir()
            obstruction = root / nef_watch.ARTIFACT_DIR_NAME
            obstruction.symlink_to(target, target_is_directory=True)
            recovery = nef_watch.OutputArtifactRecovery(root, recursive=False)
            self.addCleanup(recovery.close)

            progress = recovery.recover_batch(
                older_than_seconds=1, now=10, max_entries=100
            )

            self.assertEqual(progress.error_count, 1)
            self.assertTrue(progress.sweep_complete)
            self.assertTrue(obstruction.is_symlink())

    def test_recovery_limit_is_incomplete_and_does_not_claim_a_clean_sweep(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for index in range(4):
                (root / f"entry-{index}").write_bytes(b"user")
            recovery = nef_watch.OutputArtifactRecovery(
                root, recursive=False, max_sweep_entries=2
            )
            self.addCleanup(recovery.close)

            progress = recovery.recover_batch(
                older_than_seconds=1, now=10, max_entries=100
            )

            self.assertTrue(progress.limit_exceeded)
            self.assertFalse(progress.sweep_complete)

    def test_legal_partial_like_final_is_never_treated_as_an_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            final = root / f"photo.{'a' * 32}.partial.tif"
            write_valid_tiff(final)
            os.utime(final, (1, 1))

            progress = nef_watch.OutputArtifactRecovery(
                root,
                recursive=False,
                prior_session_cutoff=10,
            ).recover_batch(older_than_seconds=1, now=10, max_entries=100)

            self.assertEqual(progress.recovered, 0)
            self.assertTrue(final.exists())

    def test_stale_temp_cleanup_is_prefix_age_and_link_safe(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old = root / "nef-watch-input-old.nef"
            recent = root / "nef-watch-render-recent.raw"
            unrelated = root / "camera-input-old.nef"
            symlink = root / "nef-watch-input-link.nef"
            old.write_bytes(b"old")
            recent.write_bytes(b"recent")
            unrelated.write_bytes(b"mine")
            symlink.symlink_to(unrelated)
            os.utime(old, (1, 1))
            removed = nef_watch.cleanup_stale_temp_files(
                root, older_than_seconds=600, now=10_000
            )
            self.assertEqual(removed, 1)
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(unrelated.exists())
            self.assertTrue(symlink.is_symlink())

    def test_temp_recovery_cursor_advances_past_unrelated_entries(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for index in range(4):
                (root / f"unrelated-{index}").write_bytes(b"mine")
            stale = [
                root / f"nef-watch-input-{index}.nef" for index in range(2)
            ]
            for path in stale:
                path.write_bytes(b"old")
                os.utime(path, (1, 1))
            recovery = nef_watch.TempArtifactRecovery(root)
            self.addCleanup(recovery.close)
            removed = 0
            for _ in range(4):
                progress = recovery.recover_batch(
                    older_than_seconds=600, now=10_000, max_entries=2
                )
                self.assertLessEqual(progress.examined, 2)
                removed += progress.recovered
                if removed == 2:
                    break
            self.assertEqual(removed, 2)
            self.assertFalse(any(path.exists() for path in stale))

    def test_unjournaled_backup_is_preserved_while_stale_stage_is_removed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            identity = nef_watch._directory_identity(root)
            final = root / "photo.tif"
            backup = nef_watch._artifact_path_for_final(
                final,
                nef_watch.ARTIFACT_BACKUP_DIR,
                root,
                identity,
                identifier="0" * 32,
            )
            partial = nef_watch.unique_partial(
                final,
                output_root=root,
                root_identity=identity,
            )
            write_valid_tiff(backup)
            partial.write_bytes(b"staged")
            os.utime(backup, (1, 1))
            os.utime(partial, (1, 1))
            recovery = nef_watch.OutputArtifactRecovery(root, recursive=False)
            self.addCleanup(recovery.close)
            progress = recovery.recover_batch(
                older_than_seconds=600, now=10_000, max_entries=100
            )
            self.assertEqual(progress.recovered, 1)
            self.assertEqual(progress.ambiguous_count, 1)
            self.assertTrue(backup.exists())
            self.assertFalse(final.exists())
            self.assertFalse(partial.exists())

    def test_nonrecursive_recovery_finds_prior_recursive_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            nested = root / "prior-recursive-output"
            nested.mkdir()
            identity = nef_watch._directory_identity(root)
            final = nested / "photo.tif"
            backup = nef_watch._artifact_path_for_final(
                final,
                nef_watch.ARTIFACT_BACKUP_DIR,
                root,
                identity,
                identifier="1" * 32,
            )
            write_valid_tiff(backup)
            os.utime(backup, (1, 1))
            recovery = nef_watch.OutputArtifactRecovery(root, recursive=False)
            self.addCleanup(recovery.close)

            progress = recovery.recover_batch(
                older_than_seconds=600, now=10_000, max_entries=100
            )

            self.assertTrue(progress.sweep_complete)
            self.assertEqual(progress.error_count, 0)
            self.assertEqual(progress.ambiguous_count, 1)
            self.assertTrue(backup.exists())
            self.assertFalse(final.exists())

    def test_output_recovery_is_batched_and_revisits_fresh_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifacts = [
                nef_watch.unique_partial(root / f"photo-{index}.tif")
                for index in range(5)
            ]
            for artifact in artifacts:
                artifact.write_bytes(b"staged")
            recovery = nef_watch.OutputArtifactRecovery(
                root, recursive=False, max_sweep_entries=100
            )
            self.addCleanup(recovery.close)

            first = recovery.recover_batch(
                older_than_seconds=600, now=10, max_entries=2
            )
            self.assertLessEqual(first.examined, 2)
            self.assertEqual(first.recovered, 0)
            self.assertEqual(sum(path.exists() for path in artifacts), 5)

            removed = 0
            for _ in range(10):
                progress = recovery.recover_batch(
                    older_than_seconds=600, now=time.time() + 601, max_entries=2
                )
                self.assertLessEqual(progress.examined, 2)
                removed += progress.recovered
                if removed == 5:
                    break
            self.assertEqual(removed, 5)
            self.assertFalse(any(path.exists() for path in artifacts))

    def test_prior_session_cutoff_reclaims_fresh_artifact_immediately(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact = nef_watch.unique_partial(root / "photo.tif")
            artifact.write_bytes(b"staged")
            cutoff = time.time() + 1
            recovery = nef_watch.OutputArtifactRecovery(
                root,
                recursive=False,
                max_sweep_entries=10,
                prior_session_cutoff=cutoff,
            )
            self.addCleanup(recovery.close)
            progress = recovery.recover_batch(
                older_than_seconds=600,
                now=cutoff,
                max_entries=10,
            )
            self.assertEqual(progress.recovered, 1)
            self.assertFalse(artifact.exists())


class StorageAndOwnershipTests(unittest.TestCase):
    def test_dng_output_ceiling_tracks_only_configured_embedded_source(self):
        args = SimpleNamespace(
            max_input_bytes=12345, dng_embed_original=False
        )
        base = (
            nef_watch.MAX_RASTER_OUTPUT_BYTES
            + nef_watch.MAX_DNG_CONTAINER_OVERHEAD_BYTES
        )
        self.assertEqual(nef_watch.output_size_limit("dng", args), base)
        args.dng_embed_original = True
        self.assertEqual(
            nef_watch.output_size_limit("dng", args), base + 12345
        )
        self.assertEqual(
            nef_watch.output_size_limit("tiff", args),
            nef_watch.MAX_RASTER_OUTPUT_BYTES,
        )

    def test_staged_output_stream_never_writes_past_format_ceiling(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_dir = root / "input"
            output_dir = root / "output"
            scratch = root / "scratch"
            for directory in (input_dir, output_dir, scratch):
                directory.mkdir()
            args = watcher_args(input_dir)
            args.output_root = output_dir
            args.output_root_identity = nef_watch._directory_identity(output_dir)
            real_open = nef_watch._open_regular_fd
            for kind, extension in nef_watch.EXTS.items():
                with self.subTest(kind=kind):
                    work = scratch / f"work{extension}"
                    work.write_bytes(b"II*\0" + b"x" * 28)

                    def stale_size_open(path, *open_args, **open_kwargs):
                        descriptor, metadata = real_open(
                            path, *open_args, **open_kwargs
                        )
                        if Path(path) != work:
                            return descriptor, metadata
                        return descriptor, SimpleNamespace(
                            st_dev=metadata.st_dev,
                            st_ino=metadata.st_ino,
                            st_mode=metadata.st_mode,
                            st_nlink=metadata.st_nlink,
                            st_size=4,
                            st_mtime_ns=metadata.st_mtime_ns,
                            st_ctime_ns=metadata.st_ctime_ns,
                        )

                    with mock.patch.object(
                        nef_watch, "output_size_limit", return_value=8
                    ), mock.patch.object(
                        nef_watch,
                        "_open_regular_fd",
                        side_effect=stale_size_open,
                    ):
                        with self.assertRaisesRegex(
                            nef_watch.UnsafeFileError, "grew beyond"
                        ):
                            nef_watch._copy_to_staged_output(
                                work,
                                output_dir / f"photo{extension}",
                                args,
                                kind=kind,
                            )
                    self.assertFalse(
                        any(path.is_file() for path in output_dir.rglob("*"))
                    )

    def test_disposable_temp_rejects_nested_and_symlink_aliases(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_dir = root / "input"
            output_dir = root / "output"
            state_dir = root / "state"
            temp_dir = root / "temp"
            for directory in (input_dir, output_dir, state_dir, temp_dir):
                directory.mkdir()
            nef_watch.validate_storage_layout(
                input_dir, output_dir, state_dir, temp_dir
            )

            nested_temp = input_dir / "scratch"
            nested_temp.mkdir()
            with self.assertRaisesRegex(nef_watch.StorageLayoutError, "input"):
                nef_watch.validate_storage_layout(
                    input_dir, output_dir, state_dir, nested_temp
                )

            alias = root / "temp-alias"
            alias.symlink_to(output_dir, target_is_directory=True)
            with self.assertRaisesRegex(nef_watch.StorageLayoutError, "output"):
                nef_watch.validate_storage_layout(
                    input_dir, output_dir, state_dir, alias
                )

            with self.assertRaisesRegex(nef_watch.StorageLayoutError, "state"):
                nef_watch.validate_storage_layout(
                    input_dir, output_dir, state_dir, state_dir / "scratch"
                )

    def test_output_lock_excludes_publishers_with_different_state(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "output"
            output.mkdir()
            first = nef_watch.OutputRootLock(output)
            self.addCleanup(first.close)
            with self.assertRaises(BlockingIOError):
                nef_watch.OutputRootLock(output)
            first.close()
            second = nef_watch.OutputRootLock(output)
            second.close()

    def test_recreated_public_lock_cannot_admit_a_second_state_owner(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "output"
            state_one = root / "state-one"
            state_two = root / "state-two"
            for directory in (output, state_one, state_two):
                directory.mkdir()
            first = nef_watch.OutputRootLock(output, state_one)
            self.addCleanup(first.close)
            first.lock_path.unlink()
            first.lock_path.write_text("replacement", encoding="utf-8")
            with self.assertRaisesRegex(nef_watch.UnsafeFileError, "changed"):
                first.assert_owned()
            with self.assertRaises(BlockingIOError):
                nef_watch.OutputRootLock(output, state_two)

    def test_output_stage_rejects_symlinked_ancestor_under_root_anchor(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_dir = root / "input"
            output_dir = root / "output"
            scratch = root / "scratch"
            outside = root / "outside"
            for directory in (input_dir, output_dir, scratch, outside):
                directory.mkdir()
            (output_dir / "camera").symlink_to(
                outside, target_is_directory=True
            )
            work = scratch / "work.tif"
            write_valid_tiff(work)
            args = watcher_args(input_dir)
            args.temp_dir = scratch
            args.output_root = output_dir
            args.output_root_identity = nef_watch._directory_identity(output_dir)

            with self.assertRaises(nef_watch.UnsafeFileError):
                nef_watch._copy_to_staged_output(
                    work, output_dir / "camera" / "photo.tif", args
                )
            self.assertEqual(list(outside.iterdir()), [])

    def test_reservation_key_never_resolves_output_symlink_spelling(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            actual = root / "actual"
            actual.mkdir()
            alias = root / "alias"
            alias.symlink_to(actual, target_is_directory=True)
            alias_key = nef_watch.reservation_key(alias / "Photo.TIF")
            actual_key = nef_watch.reservation_key(actual / "photo.tif")
            self.assertNotEqual(alias_key, actual_key)
            self.assertIn("alias", alias_key)

    def test_cancel_queued_jobs_removes_only_cancelled_snapshots(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            release = threading.Event()
            started = threading.Event()
            executor = nef_watch.cf.ThreadPoolExecutor(max_workers=1)

            def block():
                started.set()
                release.wait()

            running = executor.submit(block)
            self.assertTrue(started.wait(1))
            queued = executor.submit(lambda: None)

            running_path = root / "running.NEF"
            queued_path = root / "queued.NEF"
            running_path.write_bytes(b"II*\0running")
            queued_path.write_bytes(b"II*\0queued")
            source = root / "source.NEF"
            source.write_bytes(b"II*\0source")

            def job(snapshot_path):
                snapshot = nef_watch.InputSnapshot(
                    source=source,
                    path=snapshot_path,
                    fingerprint="fp",
                    stat_key=(1, 2, 3, 4, 5),
                )
                return nef_watch.PendingJob(
                    source=source,
                    canonical_source=nef_watch.canonical_path(source),
                    snapshot=snapshot,
                    fingerprint="fp",
                    config_fingerprint="cfg",
                    force=False,
                )

            jobs = {running: job(running_path), queued: job(queued_path)}
            active = {nef_watch.canonical_path(source)}
            try:
                self.assertEqual(nef_watch.cancel_queued_jobs(jobs, active), 1)
                self.assertTrue(queued.cancelled())
                self.assertFalse(queued_path.exists())
                self.assertTrue(running_path.exists())
                self.assertIn(running, jobs)
            finally:
                release.set()
                executor.shutdown(wait=True, cancel_futures=True)

    def test_stop_after_snapshot_prevents_executor_submission_and_deletes_copy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "photo.NEF"
            source.write_bytes(b"II*\0source")
            output = root / "output"
            output.mkdir()
            args = watcher_args(root)
            args.temp_dir.mkdir()
            args.input = root
            args.output_root = output
            args.output_root_identity = nef_watch._directory_identity(output)
            state = nef_watch.StateStore(root / "state")
            self.addCleanup(state.close)
            real_snapshot = nef_watch.create_input_snapshot

            def snapshot_then_stop(*snapshot_args, **snapshot_kwargs):
                snapshot = real_snapshot(*snapshot_args, **snapshot_kwargs)
                nef_watch.STOP_EVENT.set()
                return snapshot

            nef_watch.STOP_EVENT.clear()
            try:
                with mock.patch.object(
                    nef_watch,
                    "create_input_snapshot",
                    side_effect=snapshot_then_stop,
                ), mock.patch.object(nef_watch, "_submit") as submit:
                    result = nef_watch.run_once(
                        args, output, b"icc", state=state,
                        config_fingerprint="cfg",
                    )
                self.assertEqual(result, 130)
                submit.assert_not_called()
                self.assertEqual(
                    list(args.temp_dir.glob("nef-watch-input-*")), []
                )
            finally:
                nef_watch.STOP_EVENT.clear()
                nef_watch.FATAL_STOP_EVENT.clear()

    def test_watch_counts_permanent_failures_once_per_scan_not_per_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "output"
            output.mkdir()
            for index in range(20):
                (root / f"photo-{index}.NEF").write_bytes(b"II*\0source")
            args = watcher_args(root)
            args.input = root
            args.output_root = output
            args.output_root_identity = nef_watch._directory_identity(output)
            args.temp_dir.mkdir()
            state = nef_watch.StateStore(root / "state")
            self.addCleanup(state.close)
            calls = 0

            def quarantine_last(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                if calls == 20:
                    nef_watch.STOP_EVENT.set()
                return "quarantine", False, "test quarantine"

            nef_watch.STOP_EVENT.clear()
            try:
                with mock.patch.object(
                    nef_watch,
                    "source_action",
                    side_effect=quarantine_last,
                ), mock.patch.object(
                    state,
                    "count_permanent_failures",
                    wraps=state.count_permanent_failures,
                ) as count:
                    nef_watch.run_watch(
                        args,
                        output,
                        b"icc",
                        state=state,
                        config_fingerprint="cfg",
                    )
                self.assertLessEqual(count.call_count, 3)
                self.assertEqual(calls, 20)
            finally:
                nef_watch.STOP_EVENT.clear()


class ArgumentValidationTests(unittest.TestCase):
    def test_jobs_and_interval_have_safe_bounds(self):
        self.assertEqual(nef_watch.parse_jobs("1"), 1)
        self.assertEqual(nef_watch.parse_jobs(str(nef_watch.MAX_JOBS)), nef_watch.MAX_JOBS)
        for value in ("0", str(nef_watch.MAX_JOBS + 1)):
            with self.assertRaises(argparse.ArgumentTypeError):
                nef_watch.parse_jobs(value)
        for value in ("0", "nan", "inf", "3601"):
            with self.assertRaises(argparse.ArgumentTypeError):
                nef_watch.parse_interval(value)
        self.assertEqual(nef_watch.parse_max_input_mib("512"), 512)
        for value in ("0", str(nef_watch.MAX_INPUT_MIB + 1)):
            with self.assertRaises(argparse.ArgumentTypeError):
                nef_watch.parse_max_input_mib(value)
        self.assertEqual(
            nef_watch.parse_max_scan_entries("1000"), 1000
        )
        for value in ("0", str(nef_watch.MAX_SCAN_ENTRIES + 1)):
            with self.assertRaises(argparse.ArgumentTypeError):
                nef_watch.parse_max_scan_entries(value)


class DiscoveryAndSignalTests(unittest.TestCase):
    def test_scan_ceiling_fails_whole_scan_instead_of_returning_a_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for index in range(5):
                (root / f"entry-{index}.txt").write_text(
                    "x", encoding="utf-8"
                )
            (root / "last.NEF").write_bytes(b"II*\0raw")
            with self.assertRaisesRegex(
                nef_watch.ScanLimitError, "--max-scan-entries"
            ):
                nef_watch.find_nefs(
                    root, recursive=True, max_entries=3
                )

    def test_unreadable_subdirectory_marks_the_whole_scan_incomplete(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            blocked = root / "blocked"
            blocked.mkdir()
            (root / "visible.NEF").write_bytes(b"II*\0raw")
            real_scandir = os.scandir

            def deny_one_directory(path):
                if Path(path) == blocked:
                    raise PermissionError(nef_watch.errno.EACCES, "denied", path)
                return real_scandir(path)

            with mock.patch.object(
                nef_watch.os, "scandir", side_effect=deny_one_directory
            ):
                with self.assertRaisesRegex(
                    nef_watch.IncompleteScanError, "incomplete"
                ):
                    nef_watch.find_nefs(root, recursive=True)

    def test_incomplete_watch_scan_never_acknowledges_absence_or_reports_healthy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "output"
            output.mkdir()
            args = watcher_args(root)
            args.output_root = output
            args.output_root_identity = nef_watch._directory_identity(output)
            args.temp_dir.mkdir()
            state = nef_watch.StateStore(root / "state")
            self.addCleanup(state.close)

            def stop_with_incomplete_scan(*_args, **_kwargs):
                nef_watch.STOP_EVENT.set()
                raise nef_watch.IncompleteScanError(
                    nef_watch.errno.EACCES, "input scan incomplete"
                )

            nef_watch.STOP_EVENT.clear()
            try:
                with mock.patch.object(
                    nef_watch, "find_nefs", side_effect=stop_with_incomplete_scan
                ), mock.patch.object(
                    state,
                    "acknowledge_missing_failures",
                    wraps=state.acknowledge_missing_failures,
                ) as acknowledge, mock.patch.object(
                    state, "write_health", wraps=state.write_health
                ) as health:
                    nef_watch.run_watch(
                        args,
                        output,
                        b"icc",
                        state=state,
                        config_fingerprint="cfg",
                    )
                acknowledge.assert_not_called()
                statuses = [call.args[0] for call in health.call_args_list]
                self.assertIn("degraded", statuses)
                self.assertNotIn("healthy", statuses)
            finally:
                nef_watch.STOP_EVENT.clear()

    def test_unsafe_path_warning_cache_is_lru_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "target.bin"
            target.write_bytes(b"not raw")
            for index in range(8):
                (root / f"link-{index}.NEF").symlink_to(target)
            nef_watch.UNSAFE_PATH_WARNED.clear()
            with mock.patch.object(
                nef_watch, "MAX_UNSAFE_WARNING_CACHE", 3
            ), mock.patch.object(nef_watch, "log"):
                self.assertEqual(
                    nef_watch.find_nefs(
                        root, recursive=True, max_entries=20
                    ),
                    [],
                )
            self.assertEqual(len(nef_watch.UNSAFE_PATH_WARNED), 3)
            nef_watch.UNSAFE_PATH_WARNED.clear()

    def test_symlinked_raw_is_rejected_from_scan(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outside = root.parent / f"outside-{root.name}.NEF"
            outside.write_bytes(b"II*\0outside")
            self.addCleanup(outside.unlink, missing_ok=True)
            (root / "escaped.NEF").symlink_to(outside)
            self.assertEqual(nef_watch.find_nefs(root, recursive=True), [])

    def test_sigterm_handler_requests_cooperative_stop(self):
        nef_watch.STOP_EVENT.clear()
        previous = nef_watch.install_stop_signal_handlers()
        try:
            signal_handler = signal.getsignal(signal.SIGTERM)
            signal_handler(signal.SIGTERM, None)
            self.assertTrue(nef_watch.STOP_EVENT.is_set())
        finally:
            nef_watch.restore_signal_handlers(previous)
            nef_watch.STOP_EVENT.clear()


class ConfigFingerprintTests(unittest.TestCase):
    def test_runtime_adapter_identity_changes_render_config(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            args = watcher_args(root)
            args.render_bin.write_bytes(b"wrapper")
            runtime = root / "runtime"
            runtime.mkdir()
            adapter = runtime / "nef_render.exe"
            adapter.write_bytes(b"adapter-v1")
            with mock.patch.dict(os.environ, {"NIKON_RUNTIME_DIR": str(runtime)}, clear=False):
                first = nef_watch.render_config_fingerprint(args, b"icc", root / "out")
                adapter.write_bytes(b"adapter-v2")
                second = nef_watch.render_config_fingerprint(args, b"icc", root / "out")
            self.assertNotEqual(first, second)


class AdversarialStateBoundTests(unittest.TestCase):
    def test_source_ceiling_evicts_only_observed_absent_state_and_then_fails_closed(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"NEF_WATCH_MAX_STATE_SOURCES": "2"}, clear=False
        ):
            store = nef_watch.StateStore(Path(td) / "state")
            self.addCleanup(store.close)
            detail = nef_watch.FailureDetail(
                "bad", permanent=True, code="invalid-input"
            )
            store.record_failure("s1", "fp", "cfg", detail, permanent=True)
            store.record_failure("s2", "fp", "cfg", detail, permanent=True)
            store.observe_complete_scan({"s2"}, now=100)
            store.record_failure("s3", "fp", "cfg", detail, permanent=True)
            self.assertIsNone(store.get_failure("s1"))
            self.assertEqual(
                store.conn.execute(
                    "SELECT COUNT(*) FROM tracked_sources"
                ).fetchone()[0],
                2,
            )
            store.observe_complete_scan({"s2", "s3"}, now=200)
            with self.assertRaises(nef_watch.StateCapacityError):
                store.record_failure(
                    "s4", "fp", "cfg", detail, permanent=True
                )
            self.assertEqual(
                store.conn.execute(
                    "SELECT COUNT(*) FROM tracked_sources"
                ).fetchone()[0],
                2,
            )

    def test_failure_payloads_are_bounded_at_the_database_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            store = nef_watch.StateStore(Path(td) / "state")
            self.addCleanup(store.close)
            detail = nef_watch.FailureDetail(
                "🙂" * 5000,
                permanent=True,
                code="x" * 1000,
            )
            store.record_failure("source", "fp", "cfg", detail, permanent=True)
            row = store.get_failure("source")
            self.assertLessEqual(
                len(row["error_text"].encode("utf-8")),
                nef_watch.MAX_FAILURE_TEXT_BYTES,
            )
            self.assertLessEqual(
                len(row["error_code"].encode("utf-8")),
                nef_watch.MAX_FAILURE_CODE_BYTES,
            )

    def test_state_uses_exact_page_cap_and_no_wal_sidecars(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"NEF_WATCH_STATE_MAX_MIB": "32"}, clear=False
        ):
            state_dir = Path(td) / "state"
            store = nef_watch.StateStore(state_dir)
            page_size = store.conn.execute("PRAGMA page_size").fetchone()[0]
            expected = ((32 * 1024 * 1024 - 4 * 1024 * 1024) // 2) // page_size
            self.assertEqual(
                store.conn.execute("PRAGMA max_page_count").fetchone()[0],
                expected,
            )
            self.assertEqual(
                store.conn.execute("PRAGMA journal_mode").fetchone()[0],
                "delete",
            )
            store.close()
            self.assertFalse(Path(str(store.db_path) + "-wal").exists())
            self.assertFalse(Path(str(store.db_path) + "-shm").exists())

    def test_existing_database_larger_than_requested_hard_cap_is_rejected(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"NEF_WATCH_STATE_MAX_MIB": "32"}, clear=False
        ):
            state_dir = Path(td) / "state"
            state_dir.mkdir()
            database = state_dir / "state.sqlite3"
            connection = nef_watch.sqlite3.connect(database)
            connection.execute(
                "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO metadata VALUES('schema',?)",
                (str(nef_watch.STATE_SCHEMA),),
            )
            connection.execute("CREATE TABLE padding(value BLOB)")
            connection.execute(
                "INSERT INTO padding VALUES(zeroblob(?))", (16 * 1024 * 1024,)
            )
            connection.commit()
            connection.close()
            with self.assertRaises(nef_watch.StateCapacityError):
                nef_watch.StateStore(state_dir)

    def test_schema_rejection_happens_before_journal_mode_mutation(self):
        for marker in (None, nef_watch.STATE_SCHEMA + 1):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as td:
                state_dir = Path(td) / "state"
                state_dir.mkdir()
                database = state_dir / "state.sqlite3"
                connection = nef_watch.sqlite3.connect(database)
                connection.execute(
                    "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
                )
                connection.execute("CREATE TABLE sentinel(value TEXT)")
                if marker is not None:
                    connection.execute(
                        "INSERT INTO metadata VALUES('schema',?)", (str(marker),)
                    )
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.commit()
                connection.close()
                with self.assertRaises(nef_watch.sqlite3.DatabaseError):
                    nef_watch.StateStore(state_dir)
                connection = nef_watch.sqlite3.connect(database)
                try:
                    self.assertEqual(
                        connection.execute("PRAGMA journal_mode").fetchone()[0],
                        "delete",
                    )
                    self.assertIsNotNone(
                        connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE name='sentinel'"
                        ).fetchone()
                    )
                finally:
                    connection.close()
                self.assertFalse(Path(str(database) + "-wal").exists())

    def test_existing_tables_without_metadata_marker_are_not_adopted(self):
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td) / "state"
            state_dir.mkdir()
            database = state_dir / "state.sqlite3"
            connection = nef_watch.sqlite3.connect(database)
            connection.execute("CREATE TABLE foreign_data(value TEXT)")
            connection.execute("INSERT INTO foreign_data VALUES('preserve')")
            connection.commit()
            connection.close()
            with self.assertRaises(nef_watch.sqlite3.DatabaseError):
                nef_watch.StateStore(state_dir)
            connection = nef_watch.sqlite3.connect(database)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM foreign_data"
                    ).fetchone()[0],
                    "preserve",
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='sources'"
                    ).fetchone()
                )
            finally:
                connection.close()


class PrivateTempTreeRecoveryTests(unittest.TestCase):
    def test_stale_private_work_tree_is_reclaimed_incrementally_without_following_links(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outside = root / "outside"
            outside.write_text("preserve", encoding="utf-8")
            work = root / "nef-watch-render-abandoned"
            nested = work / "one" / "two"
            nested.mkdir(parents=True)
            (work / "raster.tif").write_bytes(b"temporary")
            (nested / "raw.bin").write_bytes(b"temporary")
            (nested / "outside-link").symlink_to(outside)
            os.utime(work, (1, 1))
            recovery = nef_watch.TempArtifactRecovery(root)
            self.addCleanup(recovery.close)
            removed = 0
            for _ in range(20):
                progress = recovery.recover_batch(
                    older_than_seconds=10,
                    now=100,
                    max_entries=2,
                )
                self.assertLessEqual(progress.examined, 2)
                removed += progress.recovered
                if not work.exists():
                    break
            self.assertGreaterEqual(removed, 4)
            self.assertFalse(work.exists())
            self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")


class DurablePublicationRecoveryTests(unittest.TestCase):
    def test_journal_rejects_artifact_from_a_different_final_parent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store, args, _source, _final, plan, _ = self._replacement(root)
            try:
                other_parent = args.output_root / "other"
                other_parent.mkdir()
                wrong_stage = nef_watch._artifact_path_for_parent(
                    other_parent,
                    nef_watch.ARTIFACT_STAGE_DIR,
                    args.output_root,
                    args.output_root_identity,
                )
                wrong_stage.write_bytes(b"preserve")
                payload = json.loads(plan.to_json())
                payload["outputs"][0]["staged_path"] = str(wrong_stage)
                with store.conn:
                    store.conn.execute(
                        "UPDATE publication_transactions SET plan_json=? "
                        "WHERE transaction_id=?",
                        (json.dumps(payload), plan.transaction_id),
                    )

                with self.assertRaisesRegex(
                    nef_watch.PublicationRecoveryError, "non-canonical"
                ):
                    nef_watch.recover_publication_transactions(
                        store, args.output_root
                    )

                self.assertEqual(wrong_stage.read_bytes(), b"preserve")
                self.assertIsNotNone(store.get_publication(plan.transaction_id))
            finally:
                store.close()

    def _replacement(self, root):
        input_dir = root / "input"
        output_dir = root / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        source = input_dir / "photo.NEF"
        source.write_bytes(b"II*\0source")
        final = output_dir / "photo.tif"
        write_valid_tiff(final, color=(1, 2, 3))
        args = watcher_args(input_dir)
        args.output_root = output_dir
        args.output_root_identity = nef_watch._directory_identity(output_dir)
        fingerprint, _ = nef_watch.fingerprint_source(source, args)
        source_key = nef_watch.source_state_key(source, input_dir)
        store = nef_watch.StateStore(root / "state")
        old_metadata = nef_watch.capture_output_metadata(
            [("tiff", final)],
            output_root=output_dir,
            root_identity=args.output_root_identity,
        )
        store.record_success(source_key, fingerprint, "cfg", old_metadata)
        inspected = nef_watch._inspect_output_file(
            final,
            "tiff",
            include_hash=True,
            output_root=output_dir,
            root_identity=args.output_root_identity,
        )
        owned = (nef_watch._owned_output_signature(final, "tiff", inspected),)
        staged = nef_watch.unique_partial(final)
        write_valid_tiff(staged, color=(9, 8, 7))
        plan = nef_watch.prepare_publication(
            [("tiff", staged, final)],
            source_key=source_key,
            fingerprint=fingerprint,
            config_fingerprint="cfg",
            output_root=output_dir,
            root_identity=args.output_root_identity,
            owned_outputs=owned,
        )
        store.begin_publication(plan)
        nef_watch.execute_publication(
            plan, output_dir, args.output_root_identity
        )
        return store, args, source, final, plan, old_metadata

    def _new_staged_job(self, root):
        input_dir = root / "input"
        output_dir = root / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        source = input_dir / "photo.NEF"
        source.write_bytes(b"II*\0source")
        args = watcher_args(input_dir)
        args.output_root = output_dir
        args.output_root_identity = nef_watch._directory_identity(output_dir)
        fingerprint, _ = nef_watch.fingerprint_source(source, args)
        key = nef_watch.source_state_key(source, input_dir)
        staged = nef_watch.unique_partial(output_dir / "photo.tif")
        write_valid_tiff(staged, color=(7, 8, 9))
        snapshot = nef_watch.InputSnapshot(
            source, source, fingerprint, nef_watch.stat_key(source)
        )
        job = nef_watch.PendingJob(
            source,
            key,
            snapshot,
            fingerprint,
            "cfg",
            False,
            source_key=key,
        )
        detail = nef_watch.StagedConversionDetail(
            "rendered",
            staged=(("tiff", staged, output_dir / "photo.tif"),),
        )
        return args, job, detail, staged

    def test_main_thread_publication_commits_success_and_cleans_journal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            args, job, detail, staged = self._new_staged_job(root)
            store = nef_watch.StateStore(root / "state")
            try:
                status, _ = nef_watch._record_conversion_result(
                    store, job, "staged", detail, args, args.output_root
                )
                self.assertEqual(status, "ok")
                self.assertIsNotNone(store.get_source(job.source_key))
                self.assertEqual(store.publication_rows(), [])
                self.assertFalse(staged.exists())
                self.assertTrue((args.output_root / "photo.tif").exists())
            finally:
                store.close()

    def test_uncertain_sqlite_commit_never_rolls_back_a_committed_publication(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            args, job, detail, _staged = self._new_staged_job(root)
            store = nef_watch.StateStore(root / "state")
            real_commit = store.record_success_and_commit_publication

            def commit_then_report_error(plan, metadata):
                real_commit(plan, metadata)
                raise nef_watch.sqlite3.OperationalError("uncertain commit result")

            nef_watch.STOP_EVENT.clear()
            try:
                with mock.patch.object(
                    store,
                    "record_success_and_commit_publication",
                    side_effect=commit_then_report_error,
                ):
                    status, failure = nef_watch._record_conversion_result(
                        store, job, "staged", detail, args, args.output_root
                    )
                self.assertEqual(status, "error")
                self.assertEqual(failure.code, "publication-recovery")
                rows = store.publication_rows()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["phase"], "committed")
                self.assertTrue((args.output_root / "photo.tif").exists())
                nef_watch.STOP_EVENT.clear()
                self.assertEqual(
                    nef_watch.recover_publication_transactions(
                        store, args.output_root
                    ),
                    1,
                )
                self.assertTrue((args.output_root / "photo.tif").exists())
            finally:
                nef_watch.STOP_EVENT.clear()
                nef_watch.FATAL_STOP_EVENT.clear()
                store.close()

    def test_prepared_restart_rolls_back_files_and_preserves_old_database_success(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store, args, source, final, plan, old_metadata = self._replacement(root)
            try:
                self.assertEqual(
                    store.get_publication(plan.transaction_id)["phase"], "prepared"
                )
                self.assertEqual(
                    nef_watch.recover_publication_transactions(store, args.output_root),
                    1,
                )
                self.assertEqual(
                    nef_watch.capture_output_metadata(
                        [("tiff", final)],
                        output_root=args.output_root,
                        root_identity=args.output_root_identity,
                    ),
                    old_metadata,
                )
                row = store.get_source(
                    nef_watch.source_state_key(source, args.input_root)
                )
                self.assertEqual(json.loads(row["output_metadata"]), old_metadata)
                self.assertIsNone(store.get_publication(plan.transaction_id))
            finally:
                store.close()

    def test_committed_restart_keeps_new_files_and_only_then_removes_backup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store, args, _source, final, plan, _ = self._replacement(root)
            try:
                metadata = nef_watch.capture_output_metadata(
                    [("tiff", final)],
                    output_root=args.output_root,
                    root_identity=args.output_root_identity,
                )
                store.record_success_and_commit_publication(plan, metadata)
                backup = Path(plan.outputs[0]["backup_path"])
                self.assertTrue(backup.exists())
                self.assertEqual(
                    nef_watch.recover_publication_transactions(store, args.output_root),
                    1,
                )
                self.assertTrue(final.exists())
                self.assertFalse(backup.exists())
                self.assertIsNone(store.get_publication(plan.transaction_id))
            finally:
                store.close()

    def test_prepared_recovery_preserves_foreign_final_and_owned_backup_on_ambiguity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store, args, _source, final, plan, _ = self._replacement(root)
            try:
                foreign = args.output_root / "foreign.tif"
                write_valid_tiff(foreign, color=(4, 5, 6))
                foreign_bytes = foreign.read_bytes()
                os.replace(foreign, final)
                backup = Path(plan.outputs[0]["backup_path"])
                with self.assertRaises(nef_watch.PublicationRecoveryError):
                    nef_watch.recover_publication_transactions(
                        store, args.output_root
                    )
                self.assertEqual(final.read_bytes(), foreign_bytes)
                self.assertTrue(backup.exists())
                self.assertIsNotNone(store.get_publication(plan.transaction_id))
            finally:
                store.close()

    def test_in_place_same_size_mtime_rewrite_is_never_unlinked_by_rollback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            final = root / "photo.tif"
            staged = nef_watch.unique_partial(final)
            final.write_bytes(b"II*\0old0")
            staged.write_bytes(b"II*\0new0")
            identity = nef_watch._directory_identity(root)
            plan = nef_watch.prepare_publication(
                [("tiff", staged, final)],
                source_key="source",
                fingerprint="fp",
                config_fingerprint="cfg",
                output_root=root,
                root_identity=identity,
                allow_overwrite=True,
            )
            nef_watch.execute_publication(plan, root, identity)
            published = tuple(plan.outputs[0]["new_identity"])
            final.write_bytes(b"II*\0evil")
            os.utime(final, ns=(published[3], published[3]))
            self.assertFalse(nef_watch.rollback_publication(plan, root, identity))
            self.assertEqual(final.read_bytes(), b"II*\0evil")
            self.assertTrue(Path(plan.outputs[0]["backup_path"]).exists())

    def test_generic_recovery_never_deletes_backup_beside_existing_final(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            final = root / "photo.tif"
            backup = root / f".photo.tif.{'a' * 32}.rollback"
            write_valid_tiff(final, color=(1, 1, 1))
            write_valid_tiff(backup, color=(2, 2, 2))
            os.utime(backup, (1, 1))
            recovered = nef_watch.cleanup_stale_output_artifacts(
                root, recursive=False, older_than_seconds=1, now=10
            )
            self.assertEqual(recovered, 0)
            self.assertTrue(final.exists())
            self.assertTrue(backup.exists())

    def test_foreign_worker_time_output_is_not_adopted_as_success(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "photo.NEF"
            source.write_bytes(b"II*\0source")
            output = root / "output"
            output.mkdir()
            write_valid_tiff(output / "photo.tif")
            args = watcher_args(root)
            args.output_root = output
            args.output_root_identity = nef_watch._directory_identity(output)
            fingerprint, _ = nef_watch.fingerprint_source(source, args)
            snapshot = nef_watch.InputSnapshot(
                source, source, fingerprint, nef_watch.stat_key(source)
            )
            key = nef_watch.source_state_key(source, root)
            job = nef_watch.PendingJob(
                source,
                key,
                snapshot,
                fingerprint,
                "cfg",
                False,
                source_key=key,
            )
            store = nef_watch.StateStore(root / "state")
            try:
                status, detail = nef_watch._record_conversion_result(
                    store,
                    job,
                    "skip",
                    nef_watch.StagedConversionDetail("raced output"),
                    args,
                    output,
                )
                self.assertEqual(status, "error")
                self.assertEqual(detail.code, "output-collision")
                self.assertIsNone(store.get_source(key))
                self.assertTrue((output / "photo.tif").exists())
            finally:
                store.close()


class AdversarialWatchLoopTests(unittest.TestCase):
    def test_run_once_quarantines_reserved_mapping_and_converts_safe_peer(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "output"
            temp_dir = root / "temp"
            output.mkdir()
            temp_dir.mkdir()
            safe = root / "safe.NEF"
            safe.write_bytes(b"II*\0safe")
            reserved_dir = root / nef_watch.ARTIFACT_DIR_NAME
            reserved_dir.mkdir()
            reserved = reserved_dir / "uploaded.NEF"
            reserved.write_bytes(b"II*\0reserved")
            args = watcher_args(root)
            args.input = root
            args.recursive = True
            args.output_root = output
            args.output_root_identity = nef_watch._directory_identity(output)
            args.temp_dir = temp_dir
            state = nef_watch.StateStore(root / "state")
            self.addCleanup(state.close)
            submitted_sources = []

            def submit_safe(*_args, **kwargs):
                submitted_sources.append(kwargs["source_path"])
                future = nef_watch.cf.Future()
                future.set_result(
                    ("staged", nef_watch.StagedConversionDetail("safe staged"))
                )
                return future

            with mock.patch.object(
                nef_watch, "_submit", side_effect=submit_safe
            ), mock.patch.object(
                nef_watch,
                "_record_conversion_result",
                return_value=("ok", "safe converted"),
            ):
                result = nef_watch.run_once(
                    args,
                    output,
                    b"icc",
                    state=state,
                    config_fingerprint="cfg",
                )

            self.assertEqual(result, 1)
            self.assertEqual(submitted_sources, [safe])
            failure = state.get_failure(
                nef_watch.source_state_key(reserved, root)
            )
            self.assertIsNotNone(failure)
            self.assertEqual(failure["error_code"], "source-output-mapping")
            self.assertFalse(nef_watch.FATAL_STOP_EVENT.is_set())

    def test_watch_quarantines_reserved_mapping_without_fatal_stop(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "output"
            temp_dir = root / "temp"
            output.mkdir()
            temp_dir.mkdir()
            safe = root / "safe.NEF"
            safe.write_bytes(b"II*\0safe")
            reserved_dir = root / nef_watch.ARTIFACT_DIR_NAME
            reserved_dir.mkdir()
            reserved = reserved_dir / "uploaded.NEF"
            reserved.write_bytes(b"II*\0reserved")
            args = watcher_args(root)
            args.input = root
            args.recursive = True
            args.output_root = output
            args.output_root_identity = nef_watch._directory_identity(output)
            args.temp_dir = temp_dir

            class CleanRecovery:
                def recover_batch(self, *_args, **_kwargs):
                    return nef_watch.RecoveryProgress(0, 0, True)

                def close(self):
                    pass

            args.output_recovery = CleanRecovery()
            args.temp_recovery = CleanRecovery()
            state = nef_watch.StateStore(root / "state")
            self.addCleanup(state.close)
            submitted_sources = []

            def submit_safe(*_args, **kwargs):
                submitted_sources.append(kwargs["source_path"])
                future = nef_watch.cf.Future()
                future.set_result(
                    ("staged", nef_watch.StagedConversionDetail("safe staged"))
                )
                return future

            def record_safe(*_args, **_kwargs):
                nef_watch.STOP_EVENT.set()
                return "ok", "safe converted"

            nef_watch.STOP_EVENT.clear()
            nef_watch.FATAL_STOP_EVENT.clear()
            try:
                with mock.patch.object(
                    nef_watch, "_submit", side_effect=submit_safe
                ), mock.patch.object(
                    nef_watch,
                    "_record_conversion_result",
                    side_effect=record_safe,
                ):
                    result = nef_watch.run_watch(
                        args,
                        output,
                        b"icc",
                        state=state,
                        config_fingerprint="cfg",
                    )
                self.assertEqual(result, 0)
                self.assertEqual(submitted_sources, [safe])
                failure = state.get_failure(
                    nef_watch.source_state_key(reserved, root)
                )
                self.assertIsNotNone(failure)
                self.assertEqual(
                    failure["error_code"], "source-output-mapping"
                )
                self.assertFalse(nef_watch.FATAL_STOP_EVENT.is_set())
            finally:
                nef_watch.STOP_EVENT.clear()
                nef_watch.FATAL_STOP_EVENT.clear()

    def test_completed_containment_failure_preempts_publishable_worker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "output"
            temp_dir = root / "temp"
            output.mkdir()
            temp_dir.mkdir()
            sources = [root / "one.NEF", root / "two.NEF"]
            for source in sources:
                source.write_bytes(b"II*\0source")
            args = watcher_args(root)
            args.input = root
            args.output_root = output
            args.output_root_identity = nef_watch._directory_identity(output)
            args.temp_dir = temp_dir
            args.jobs = 2
            args.max_pending = 2
            args.overwrite = False
            state = nef_watch.StateStore(root / "state")
            self.addCleanup(state.close)
            futures = []
            staged_future = nef_watch.cf.Future()
            staged_future.set_result(
                ("staged", nef_watch.StagedConversionDetail("stage-0"))
            )
            containment_future = nef_watch.cf.Future()
            containment_future.set_result(
                (
                    "error",
                    nef_watch.FailureDetail(
                        "supervisor failed", permanent=True, code="containment"
                    ),
                )
            )
            futures.extend((staged_future, containment_future))
            snapshots = []
            for index, source in enumerate(sources):
                path = temp_dir / f"snapshot-{index}"
                path.write_bytes(source.read_bytes())
                snapshots.append(
                    nef_watch.InputSnapshot(source, path, f"fp-{index}", (index,))
                )
            recorded = []

            def record_containment_first(
                _state, job, status, detail, *_args, **_kwargs
            ):
                recorded.append(job.source.name)
                if len(recorded) > 1:
                    self.fail("a second completed worker was published after fatal stop")
                self.assertEqual(status, "error")
                self.assertEqual(detail.code, "containment")
                nef_watch._request_fatal_stop()
                return status, detail

            nef_watch.STOP_EVENT.clear()
            nef_watch.FATAL_STOP_EVENT.clear()
            try:
                with mock.patch.object(
                    nef_watch, "find_nefs", return_value=sources
                ), mock.patch.object(
                    nef_watch,
                    "fingerprint_source",
                    side_effect=[("fp-0", ()), ("fp-1", ())],
                ), mock.patch.object(
                    nef_watch,
                    "source_action",
                    return_value=("convert", False, nef_watch.ActionDetail("new")),
                ), mock.patch.object(
                    state, "reserve_outputs"
                ), mock.patch.object(
                    nef_watch,
                    "create_input_snapshot",
                    side_effect=snapshots,
                ), mock.patch.object(
                    nef_watch, "_submit", side_effect=futures
                ), mock.patch.object(
                    nef_watch,
                    "_record_conversion_result",
                    side_effect=record_containment_first,
                ):
                    result = nef_watch.run_once(
                        args,
                        output,
                        b"icc",
                        state=state,
                        config_fingerprint="cfg",
                    )
            finally:
                nef_watch.STOP_EVENT.clear()
                nef_watch.FATAL_STOP_EVENT.clear()
            self.assertEqual(result, 130)
            self.assertEqual(recorded, ["two.NEF"])

    def test_incomplete_artifact_recovery_keeps_watch_health_degraded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "output"
            temp_dir = root / "temp"
            output.mkdir()
            temp_dir.mkdir()
            args = watcher_args(root)
            args.input = root
            args.output_root = output
            args.output_root_identity = nef_watch._directory_identity(output)
            args.temp_dir = temp_dir

            class BrokenRecovery:
                def recover_batch(self, *args, **kwargs):
                    return nef_watch.RecoveryProgress(
                        0, 1, False, error_count=1
                    )

                def close(self):
                    pass

            class CleanRecovery:
                def recover_batch(self, *args, **kwargs):
                    return nef_watch.RecoveryProgress(0, 0, True)

                def close(self):
                    pass

            args.output_recovery = BrokenRecovery()
            args.temp_recovery = CleanRecovery()
            state = nef_watch.StateStore(root / "state")
            self.addCleanup(state.close)
            statuses = []
            real_write_health = state.write_health

            def record_status(status, **kwargs):
                statuses.append(status)
                return real_write_health(status, **kwargs)

            def stop_after_scan(_interval):
                nef_watch.STOP_EVENT.set()
                return True

            nef_watch.STOP_EVENT.clear()
            nef_watch.FATAL_STOP_EVENT.clear()
            try:
                with mock.patch.object(
                    state, "write_health", side_effect=record_status
                ), mock.patch.object(
                    nef_watch.STOP_EVENT, "wait", side_effect=stop_after_scan
                ), mock.patch.object(
                    nef_watch.time, "monotonic", return_value=1.0
                ):
                    result = nef_watch.run_watch(
                        args,
                        output,
                        b"icc",
                        state=state,
                        config_fingerprint="cfg",
                    )
            finally:
                nef_watch.STOP_EVENT.clear()
                nef_watch.FATAL_STOP_EVENT.clear()
            self.assertEqual(result, 1)
            self.assertIn("degraded", statuses)
            self.assertNotIn("healthy", statuses)

    def test_normal_watch_stop_returns_success(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "output"
            temp_dir = root / "temp"
            output.mkdir()
            temp_dir.mkdir()
            args = watcher_args(root)
            args.output_root = output
            args.output_root_identity = nef_watch._directory_identity(output)
            args.temp_dir = temp_dir

            class CleanRecovery:
                def recover_batch(self, *args, **kwargs):
                    return nef_watch.RecoveryProgress(0, 0, True)

                def close(self):
                    pass

            args.output_recovery = CleanRecovery()
            args.temp_recovery = CleanRecovery()
            state = nef_watch.StateStore(root / "state")
            self.addCleanup(state.close)

            def request_normal_stop(_interval):
                nef_watch.STOP_EVENT.set()
                return True

            nef_watch.STOP_EVENT.clear()
            nef_watch.FATAL_STOP_EVENT.clear()
            try:
                with mock.patch.object(
                    nef_watch.STOP_EVENT,
                    "wait",
                    side_effect=request_normal_stop,
                ):
                    result = nef_watch.run_watch(
                        args,
                        output,
                        b"icc",
                        state=state,
                        config_fingerprint="cfg",
                    )
                self.assertEqual(result, 0)
                self.assertFalse(nef_watch.FATAL_STOP_EVENT.is_set())
            finally:
                nef_watch.STOP_EVENT.clear()
                nef_watch.FATAL_STOP_EVENT.clear()

    def test_new_duplicate_stems_quarantine_every_source_before_conversion(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = root / "photo.NEF"
            second = root / "photo.NRW"
            first.write_bytes(b"II*\0first")
            second.write_bytes(b"II*\0second")
            output = root / "output"
            output.mkdir()
            args = watcher_args(root)
            store = nef_watch.StateStore(root / "state")
            self.addCleanup(store.close)
            conflicts = nef_watch.duplicate_output_source_keys(
                [first, second], args, output
            )

            actions = []
            for source in (first, second):
                key = nef_watch.source_state_key(source, root)
                action, _, detail = nef_watch.source_action(
                    store,
                    source,
                    "fp-" + source.suffix,
                    "cfg",
                    args,
                    output,
                    source_key=key,
                    source_conflict=key in conflicts,
                )
                actions.append(action)
                self.assertEqual(detail.code, "source-output-collision")
                self.assertTrue(detail.persist_failure)

            self.assertEqual(actions, ["quarantine", "quarantine"])

    def test_duplicate_stem_dead_letter_rearms_when_peer_is_removed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "photo.NEF"
            source.write_bytes(b"II*\0source")
            output = root / "output"
            output.mkdir()
            args = watcher_args(root)
            store = nef_watch.StateStore(root / "state")
            try:
                key = nef_watch.source_state_key(source, root)
                detail = nef_watch.FailureDetail(
                    "duplicate stem",
                    permanent=True,
                    code="source-output-collision",
                )
                store.record_failure(key, "fp", "cfg", detail, permanent=True)
                self.assertEqual(
                    nef_watch.source_action(
                        store,
                        source,
                        "fp",
                        "cfg",
                        args,
                        output,
                        source_key=key,
                        source_conflict=True,
                    )[0],
                    "quarantine",
                )
                self.assertEqual(
                    nef_watch.source_action(
                        store,
                        source,
                        "fp",
                        "cfg",
                        args,
                        output,
                        source_key=key,
                        source_conflict=False,
                    )[0],
                    "convert",
                )
                self.assertIsNone(store.get_failure(key))
            finally:
                store.close()

    def test_unchanged_owned_collision_is_not_decoded_or_hashed_each_poll(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "photo.NEF"
            source.write_bytes(b"II*\0source")
            output_dir = root / "output"
            output_dir.mkdir()
            output = output_dir / "photo.tif"
            write_valid_tiff(output, color=(1, 2, 3))
            args = watcher_args(root)
            args.input = root
            args.output_root = output_dir
            args.output_root_identity = nef_watch._directory_identity(output_dir)
            args.temp_dir.mkdir()
            store = nef_watch.StateStore(root / "state")
            key = nef_watch.source_state_key(source, root)
            metadata = nef_watch.capture_output_metadata([("tiff", output)])
            store.record_success(key, "older-source", "cfg", metadata)
            write_valid_tiff(output, color=(9, 8, 7))
            waits = 0

            def stop_after_second_scan(_interval):
                nonlocal waits
                waits += 1
                if waits >= 2:
                    nef_watch.STOP_EVENT.set()
                return False

            nef_watch.STOP_EVENT.clear()
            try:
                with mock.patch.object(
                    nef_watch,
                    "_inspect_output_file",
                    wraps=nef_watch._inspect_output_file,
                ) as inspect, mock.patch.object(
                    nef_watch,
                    "_prove_durable_output",
                    wraps=nef_watch._prove_durable_output,
                ) as prove, mock.patch.object(
                    nef_watch.STOP_EVENT,
                    "wait",
                    side_effect=stop_after_second_scan,
                ):
                    nef_watch.run_watch(
                        args,
                        output_dir,
                        b"icc",
                        state=store,
                        config_fingerprint="cfg",
                    )
                self.assertEqual(inspect.call_count, 0)
                self.assertEqual(prove.call_count, 1)
            finally:
                nef_watch.STOP_EVENT.clear()
                store.close()


class Utf8AndHelperIsolationTests(unittest.TestCase):
    def test_logging_escapes_surrogates_and_terminal_controls(self):
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "watch.log"
            previous = nef_watch.LOG_FILE
            nef_watch.LOG_FILE = open(log_path, "a", encoding="utf-8")
            self.addCleanup(setattr, nef_watch, "LOG_FILE", previous)
            self.addCleanup(nef_watch.LOG_FILE.close)

            nef_watch.log("bad-\udcff\x1b[31m\x00name")

            payload = log_path.read_text(encoding="utf-8")
            self.assertIn(r"bad-\udcff", payload)
            self.assertIn(r"\x1b[31m\x00name", payload)
            self.assertNotIn("\x1b", payload)

    def test_parser_wrapper_preserves_literal_proc_self_and_minimizes_environment(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            launcher = root / "landlock.py"
            launcher.write_text("pass\n", encoding="utf-8")
            launcher.chmod(0o555)
            launcher.chmod(0o555)
            work = root / "work"
            work.mkdir()
            args = SimpleNamespace(
                landlock_exec=launcher,
                _allow_test_landlock_exec=True,
                process_env={
                    "PATH": "/usr/bin:/bin",
                    "LANG": "C.UTF-8",
                    "SECRET_TOKEN": "must-not-cross",
                    "AWS_SECRET_ACCESS_KEY": "must-not-cross",
                },
                temp_dir=root,
            )
            with mock.patch.object(nef_watch.sys, "platform", "linux"):
                command = nef_watch.sandbox_untrusted_parser(
                    ["/usr/bin/true"], args, read_write=(work,)
                )

            self.assertIn("/proc/self", command)
            self.assertNotIn(f"/proc/{os.getpid()}", command)
            environment = nef_watch.configured_parser_env(args, temp_dir=work)
            self.assertEqual(environment["PATH"], "/usr/bin:/bin")
            self.assertEqual(environment["TMPDIR"], str(work.resolve()))
            self.assertNotIn("SECRET_TOKEN", environment)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", environment)

    def test_parser_launcher_override_is_rejected_outside_explicit_test_mode(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            launcher = root / "fake-landlock.py"
            launcher.write_text("pass\n", encoding="utf-8")
            launcher.chmod(0o555)
            work = root / "work"
            work.mkdir()
            args = SimpleNamespace(landlock_exec=launcher, temp_dir=root)

            with mock.patch.object(nef_watch.sys, "platform", "linux"):
                with self.assertRaisesRegex(
                    nef_watch.PermanentConversionError, "packaged"
                ):
                    nef_watch.sandbox_untrusted_parser(
                        ["/usr/bin/true"], args, read_write=(work,)
                    )

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux Landlock probe")
    def test_active_parser_probe_rejects_launcher_that_does_not_sandbox(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            launcher = root / "fake-landlock.py"
            launcher.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "index = sys.argv.index('--')\n"
                "os.execv(sys.argv[index + 1], sys.argv[index + 1:])\n",
                encoding="utf-8",
            )
            launcher.chmod(0o555)
            temp_dir = root / "temp"
            temp_dir.mkdir()
            args = SimpleNamespace(
                exiftool=Path("/usr/bin/true"),
                dng_bin=None,
                landlock_exec=launcher,
                _allow_test_landlock_exec=True,
                temp_dir=temp_dir,
                kill_grace_seconds=0.05,
                process_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            )

            with self.assertRaisesRegex(
                nef_watch.PermanentConversionError, "cannot be enforced"
            ):
                nef_watch.verify_parser_sandbox(args)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux procfs")
    def test_dumpable_zero_blocks_same_uid_sibling_proc_secrets(self):
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                os.close(read_fd)
                nef_watch._set_linux_dumpable(False)
                os.write(write_fd, b"ready")
                time.sleep(5)
            finally:
                os._exit(0)
        os.close(write_fd)
        self.addCleanup(lambda: os.waitpid(pid, 0))
        self.addCleanup(lambda: os.kill(pid, signal.SIGKILL))
        self.assertEqual(os.read(read_fd, 5), b"ready")
        os.close(read_fd)
        for proc_name in ("environ", "mem"):
            with self.assertRaises(PermissionError):
                os.open(f"/proc/{pid}/{proc_name}", os.O_RDONLY)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux subreaper")
    def test_killed_supervisor_cannot_leave_setsid_descendant_alive(self):
        with tempfile.TemporaryDirectory() as td:
            pid_file = Path(td) / "escaped.pid"
            script = (
                "import os,signal,time; "
                "pid=os.fork(); "
                "(os.setsid(),open(" + repr(str(pid_file)) + ", 'w').write(str(os.getpid())),"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN),time.sleep(30)) "
                "if pid==0 else (time.sleep(.1),os.kill(os.getppid(),signal.SIGKILL))"
            )
            nef_watch._enable_linux_child_subreaper()
            with self.assertRaisesRegex(
                nef_watch.PermanentConversionError, "containment"
            ):
                nef_watch.run_process(
                    [sys.executable, "-c", script],
                    timeout=10,
                    label="killed supervisor",
                    kill_grace_seconds=0.05,
                )
            deadline = time.monotonic() + 2
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(pid_file.exists())
            escaped = int(pid_file.read_text(encoding="utf-8"))
            with self.assertRaises(ProcessLookupError):
                os.kill(escaped, 0)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux subreaper")
    def test_failed_supervisor_cleanup_protects_concurrently_started_sibling(self):
        with tempfile.TemporaryDirectory() as td:
            pid_file = Path(td) / "escaped.pid"
            failing_script = (
                "import os,signal,time; "
                "pid=os.fork(); "
                "(os.setsid(),open(" + repr(str(pid_file)) + ", 'w').write(str(os.getpid())),"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN),time.sleep(30)) "
                "if pid==0 else (time.sleep(.1),os.kill(os.getppid(),signal.SIGKILL))"
            )
            sibling_script = "import time; time.sleep(.4); print('sibling-survived')"
            sibling_outcome = {}
            sibling_thread = None
            launch_lock = threading.Lock()
            launched = False
            real_signal_process_ids = nef_watch._signal_process_ids

            def run_sibling():
                try:
                    sibling_outcome["result"] = nef_watch.run_process(
                        [sys.executable, "-c", sibling_script],
                        timeout=3,
                        label="concurrent sibling",
                        kill_grace_seconds=0.05,
                    )
                except BaseException as error:
                    sibling_outcome["error"] = error

            def start_sibling_during_cleanup(process_ids, signum):
                nonlocal launched, sibling_thread
                with launch_lock:
                    if not launched:
                        launched = True
                        sibling_thread = threading.Thread(target=run_sibling)
                        sibling_thread.start()
                        deadline = time.monotonic() + 2
                        while time.monotonic() < deadline:
                            with nef_watch._SUPERVISOR_REGISTRY_LOCK:
                                if nef_watch._ACTIVE_SUPERVISORS:
                                    break
                            time.sleep(0.005)
                        else:
                            self.fail("concurrent supervisor was not registered")
                return real_signal_process_ids(process_ids, signum)

            nef_watch._enable_linux_child_subreaper()
            with mock.patch.object(
                nef_watch,
                "_signal_process_ids",
                side_effect=start_sibling_during_cleanup,
            ):
                with self.assertRaisesRegex(
                    nef_watch.ProcessContainmentError, "containment"
                ):
                    nef_watch.run_process(
                        [sys.executable, "-c", failing_script],
                        timeout=10,
                        label="failed supervisor",
                        kill_grace_seconds=0.05,
                    )
            self.assertIsNotNone(sibling_thread)
            sibling_thread.join(timeout=4)
            self.assertFalse(sibling_thread.is_alive())
            self.assertNotIn("error", sibling_outcome)
            self.assertEqual(sibling_outcome["result"].returncode, 0)
            self.assertIn(
                "sibling-survived", sibling_outcome["result"].stdout
            )

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux byte filenames")
    def test_invalid_utf8_raw_filename_is_skipped_before_sqlite_keying(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_path = os.fsencode(root) + b"/bad-\xff.NEF"
            descriptor = os.open(raw_path, os.O_WRONLY | os.O_CREAT, 0o600)
            try:
                os.write(descriptor, b"II*\0source")
            finally:
                os.close(descriptor)
            with mock.patch.object(nef_watch, "log"):
                self.assertEqual(nef_watch.find_nefs(root, False), [])
            decoded = Path(os.fsdecode(raw_path))
            with self.assertRaises(nef_watch.UnsafeFileError):
                nef_watch.source_state_key(decoded, root)

    def test_linux_parser_allowlist_excludes_public_output_and_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            launcher = root / "landlock.py"
            launcher.write_text("pass\n", encoding="utf-8")
            launcher.chmod(0o555)
            source = root / "input" / "photo.NEF"
            work = root / "work" / "job"
            output = root / "output"
            state = root / "state"
            for directory in (source.parent, work, output, state):
                directory.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"II*\0source")
            args = SimpleNamespace(
                landlock_exec=launcher,
                _allow_test_landlock_exec=True,
            )
            with mock.patch.object(nef_watch.sys, "platform", "linux"):
                command = nef_watch.sandbox_untrusted_parser(
                    ["/usr/bin/true"],
                    args,
                    read_only=(source,),
                    read_write=(work,),
                )
            self.assertIn(str(source.resolve()), command)
            self.assertIn(str(work.resolve()), command)
            self.assertNotIn(str(output.resolve()), command)
            self.assertNotIn(str(state.resolve()), command)
            self.assertLess(command.index(str(source.resolve())), command.index("--"))
            self.assertLess(command.index(str(work.resolve())), command.index("--"))

    def test_linux_parser_sandbox_is_fail_closed_when_launcher_is_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "photo.NEF"
            work = root / "work"
            source.write_bytes(b"II*\0source")
            work.mkdir()
            args = SimpleNamespace(
                landlock_exec=root / "missing.py",
                _allow_test_landlock_exec=True,
            )
            with mock.patch.object(nef_watch.sys, "platform", "linux"):
                with self.assertRaises(nef_watch.PermanentConversionError):
                    nef_watch.sandbox_untrusted_parser(
                        ["/usr/bin/true"],
                        args,
                        read_only=(source,),
                        read_write=(work,),
                    )

    def test_parser_sandbox_probe_fails_startup_on_enforcement_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            temp_dir = root / "temp"
            temp_dir.mkdir()
            args = SimpleNamespace(
                exiftool=Path("/usr/bin/exiftool"),
                dng_bin=None,
                temp_dir=temp_dir,
                kill_grace_seconds=0.05,
            )
            failed = subprocess.CompletedProcess(
                ["probe"], 77, "", "Landlock unavailable"
            )
            with mock.patch.object(
                nef_watch.sys, "platform", "linux"
            ), mock.patch.object(
                nef_watch,
                "sandbox_untrusted_parser",
                return_value=["probe"],
            ), mock.patch.object(
                nef_watch, "run_process", return_value=failed
            ):
                with self.assertRaisesRegex(
                    nef_watch.PermanentConversionError, "Landlock unavailable"
                ):
                    nef_watch.verify_parser_sandbox(args)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux Landlock")
    def test_parser_process_cannot_read_state_outside_its_exact_allowlist(self):
        launcher = nef_watch.DEFAULT_LANDLOCK_EXEC
        if not launcher.is_file():
            self.skipTest("packaged Landlock launcher is unavailable")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_dir = root / "input"
            output_dir = root / "output"
            state_dir = root / "state"
            temp_dir = root / "temp"
            work = temp_dir / "job"
            for directory in (input_dir, output_dir, state_dir, work):
                directory.mkdir(parents=True, exist_ok=True)
            source = input_dir / "photo.NEF"
            source.write_bytes(b"II*\0source")
            secret = state_dir / "secret"
            secret.write_text("must-not-be-readable", encoding="utf-8")
            result = work / "result"
            forbidden_write = output_dir / "forbidden"
            script = (
                "from pathlib import Path; import os,sys,tempfile; "
                f"source=Path({str(source)!r}); secret=Path({str(secret)!r}); "
                f"result=Path({str(result)!r}); forbidden=Path({str(forbidden_write)!r}); "
                f"work=Path({str(work)!r}); "
                "data=source.read_bytes(); blocked_read=blocked_write=False; "
                "\ntry:\n secret.read_bytes()\n"
                "except PermissionError:\n blocked_read=True\n"
                "\ntry:\n forbidden.write_text('unsafe')\n"
                "except PermissionError:\n blocked_write=True\n"
                "\nfd,temp_name=tempfile.mkstemp(prefix='parser-temp-'); "
                "os.write(fd,b'ok'); os.close(fd); temp_path=Path(temp_name); "
                "temp_ok=(temp_path.parent==work and temp_path.read_bytes()==b'ok'); "
                "temp_path.unlink(); temp_ok=temp_ok and not temp_path.exists(); "
                "safe=blocked_read and blocked_write and temp_ok and data.startswith(b'II'); "
                "result.write_text('ok' if safe else 'unsafe'); sys.exit(0 if safe else 9)"
            )
            args = SimpleNamespace(
                landlock_exec=launcher,
                input_root=input_dir,
                output_root=output_dir,
                state_dir=state_dir,
                temp_dir=temp_dir,
            )
            command = nef_watch.sandbox_untrusted_parser(
                [sys.executable, "-c", script],
                args,
                read_only=(source,),
                read_write=(work,),
            )
            process = nef_watch.run_process(
                command,
                timeout=30,
                label="sandbox probe",
                env=nef_watch.configured_parser_env(args, temp_dir=work),
                kill_grace_seconds=0.05,
            )
            if process.returncode == 77:
                self.assertIn("Landlock", process.stderr)
                self.assertFalse(result.exists())
                return
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(result.read_text(encoding="utf-8"), "ok")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux subreaper")
    def test_setsid_descendant_cannot_escape_helper_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            pid_file = Path(td) / "escaped.pid"
            script = (
                "import os,signal,time; "
                "pid=os.fork(); "
                "(os.setsid(),os.close(1),os.close(2),"
                f"open({str(pid_file)!r},'w').write(str(os.getpid())),"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN),time.sleep(30)) "
                "if pid==0 else None"
            )
            nef_watch.run_process(
                [sys.executable, "-c", script],
                timeout=30,
                label="setsid helper",
                kill_grace_seconds=0.05,
            )
            deadline = time.monotonic() + 1
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(pid_file.exists())
            escaped = int(pid_file.read_text(encoding="utf-8"))
            with self.assertRaises(ProcessLookupError):
                os.kill(escaped, 0)


if __name__ == "__main__":
    unittest.main()
