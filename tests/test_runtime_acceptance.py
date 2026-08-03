from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import tifffile

from tool import validate_tiffs, verify_runtime_acceptance


IMAGE_ID = "sha256:" + "a" * 64
SOURCE_FINGERPRINT = "b" * 64
EXPECTED_REVISION = "d" * 40
REGISTRY_MANIFEST_BYTES = json.dumps(
    {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": IMAGE_ID,
            "size": 1,
        },
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "digest": "sha256:" + "e" * 64,
                "size": 1,
            }
        ],
    },
    sort_keys=True,
    separators=(",", ":"),
).encode()
IMAGE_DIGEST = hashlib.sha256(REGISTRY_MANIFEST_BYTES).hexdigest()
IMAGE_REFERENCE = "ghcr.io/rohanpandula/nef-watch@sha256:" + IMAGE_DIGEST


def rgb_icc(marker: int = 0) -> bytes:
    profile = bytearray(132)
    profile[0:4] = len(profile).to_bytes(4, "big")
    profile[12:16] = b"mntr"
    profile[16:20] = b"RGB "
    profile[20:24] = b"XYZ "
    profile[36:40] = b"acsp"
    profile[-1] = marker
    return bytes(profile)


def write_tiff(path: Path, pixels: np.ndarray, icc: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tifffile.imwrite(
        path,
        pixels,
        photometric="rgb",
        compression="lzw",
        metadata=None,
        extratags=[
            (274, "H", 1, 1, False),
            (34675, 7, len(icc), icc, False),
        ],
    )


class RuntimeAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.sources = self.root / "sources"
        self.outputs = self.root / "outputs"
        self.sources.mkdir()
        self.outputs.mkdir()
        self.source = self.sources / "nested" / "sample.NEF"
        self.source.parent.mkdir()
        self.source.write_bytes(b"synthetic NEF fixture")
        self.pixels = np.arange(6 * 7 * 3, dtype=np.uint8).reshape(6, 7, 3)
        self.icc = rgb_icc()
        self.output = self.outputs / "nested" / "sample.TIF"
        write_tiff(self.output, self.pixels, self.icc)
        self.inspection_report = self.root / "tiff-inspection.json"

        raster = validate_tiffs.inspect_tiff(self.output, "fixture")
        summary = raster.summary()
        del raster
        self.sdk_manifest = self.root / "nikon-sdk.sha256"
        self.sdk_dir = self.root / "sdk"
        self.sdk_file = self.sdk_dir / "Bin" / "example.dll"
        self.sdk_file.parent.mkdir(parents=True)
        self.sdk_file.write_bytes(b"synthetic sdk binary")
        sdk_file_hash = verify_runtime_acceptance.file_sha256(self.sdk_file)
        self.sdk_manifest.write_text(
            sdk_file_hash + "  Bin/example.dll\n", encoding="utf-8"
        )
        self.provenance = self.root / "verified-provenance.json"
        self.registry_manifest = self.root / "registry-manifest.json"
        self.registry_manifest.write_bytes(REGISTRY_MANIFEST_BYTES)
        self.docker_inspect = self.root / "docker-inspect.json"
        self.write_docker_inspect()
        self.provenance.write_text(
            json.dumps(
                [
                    {
                        "verificationResult": {
                            "statement": {
                                "subject": [
                                    {
                                        "name": "ghcr.io/rohanpandula/nef-watch",
                                        "digest": {"sha256": IMAGE_DIGEST},
                                    }
                                ]
                            }
                        }
                    }
                ]
            ),
            encoding="utf-8",
        )
        sdk_hash = verify_runtime_acceptance.file_sha256(self.sdk_manifest)
        self.manifest_data = {
            "schema_version": 1,
            "artifact_labels": ["nx", "linux"],
            "engines": {"nikon_sdk_manifest_sha256": sdk_hash},
            "images": [
                {
                    "name": "sample",
                    "source_nef": "nested/sample.NEF",
                    "source_sha256": verify_runtime_acceptance.file_sha256(self.source),
                    "tiff": "nested/sample.TIF",
                    "raster": {
                        field: summary[field]
                        for field in verify_runtime_acceptance.RASTER_BINDING_FIELDS
                    },
                    "artifacts": {
                        "linux": {
                            "pixel_sha256": summary["pixel_sha256"],
                            "icc_sha256": summary["icc_sha256"],
                        }
                    },
                }
            ],
        }
        self.manifest = self.root / "baseline.json"
        self.write_manifest()
        self.write_inspection_report()

    def write_docker_inspect(self, **overrides: object) -> None:
        evidence = {
            "Id": IMAGE_ID,
            "RepoDigests": [IMAGE_REFERENCE],
            "Os": "linux",
            "Architecture": "amd64",
            "Config": {
                "Env": ["NEF_WATCH_RENDER_JOBS=1"],
                "Labels": {
                    "io.nef-watch.source.fingerprint": SOURCE_FINGERPRINT,
                    "io.nef-watch.nikon-sdk.bundled": "false",
                    "org.opencontainers.image.revision": EXPECTED_REVISION,
                }
            },
        }
        evidence.update(overrides)
        self.docker_inspect.write_text(json.dumps([evidence]), encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_manifest(self) -> None:
        self.manifest.write_text(json.dumps(self.manifest_data), encoding="utf-8")

    def write_inspection_report(self) -> None:
        report = verify_runtime_acceptance.create_tiff_inspection_report(self.outputs)
        self.inspection_report.write_text(json.dumps(report), encoding="utf-8")

    def verify(self, *, refresh_inspection: bool = True) -> tuple[dict, int]:
        if refresh_inspection:
            self.write_inspection_report()
        return verify_runtime_acceptance.verify(
            manifest_path=self.manifest,
            source_dir=self.sources,
            output_dir=self.outputs,
            sdk_dir=self.sdk_dir,
            sdk_manifest=self.sdk_manifest,
            expected_label="linux",
            image_id=IMAGE_ID,
            image_reference=IMAGE_REFERENCE,
            source_fingerprint=SOURCE_FINGERPRINT,
            expected_source_fingerprint=SOURCE_FINGERPRINT,
            expected_revision=EXPECTED_REVISION,
            provenance_path=self.provenance,
            registry_manifest_path=self.registry_manifest,
            docker_inspect_path=self.docker_inspect,
            tiff_inspection_report_path=self.inspection_report,
        )

    def verify_local(self) -> tuple[dict, int]:
        self.write_inspection_report()
        return verify_runtime_acceptance.verify(
            manifest_path=self.manifest,
            source_dir=self.sources,
            output_dir=self.outputs,
            sdk_dir=self.sdk_dir,
            sdk_manifest=self.sdk_manifest,
            expected_label="linux",
            image_id=IMAGE_ID,
            source_fingerprint=SOURCE_FINGERPRINT,
            expected_source_fingerprint=SOURCE_FINGERPRINT,
            expected_revision=EXPECTED_REVISION,
            docker_inspect_path=self.docker_inspect,
            tiff_inspection_report_path=self.inspection_report,
            trusted_local_image=True,
        )

    def test_matching_private_render_passes_with_new_image_identity(self) -> None:
        report, exit_code = self.verify()

        self.assertEqual(exit_code, 0)
        self.assertTrue(report["passed"])
        self.assertEqual(report["candidate_engine"]["docker_image_id"], IMAGE_ID)
        self.assertEqual(
            report["candidate_engine"]["docker_image_reference"], IMAGE_REFERENCE
        )
        self.assertEqual(report["verified_provenance"]["matching_attestations"], 1)
        self.assertEqual(len(report["sdk_manifest"]["checked_files"]), 1)
        self.assertEqual(len(report["checked_sources"]), 1)
        self.assertEqual(len(report["checked_outputs"]), 1)
        self.assertEqual(report["checked_sources"][0]["path"], "nested/sample.NEF")
        self.assertEqual(report["checked_outputs"][0]["path"], "nested/sample.TIF")
        self.assertNotIn(str(self.root), json.dumps(report))

    def test_sandboxed_inspection_report_is_bound_to_exact_tiff_bytes(self) -> None:
        self.write_inspection_report()
        with self.output.open("ab") as stream:
            stream.write(b"changed after inspection")

        with self.assertRaisesRegex(
            verify_runtime_acceptance.OperationalError, "changed after sandboxed inspection"
        ):
            self.verify(refresh_inspection=False)

    def test_inspection_mode_writes_only_to_precreated_owned_file(self) -> None:
        report_path = self.root / "container-report.json"
        report_path.touch(mode=0o600)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = verify_runtime_acceptance.main(
                [
                    "--emit-tiff-inspection",
                    "--output-dir", str(self.outputs),
                    "--report", str(report_path),
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertIn("TIFF inspection: COMPLETE", output.getvalue())
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["outputs"][0]["path"], "nested/sample.TIF")

    def test_isolated_direct_script_can_load_sealed_tiff_validator(self) -> None:
        report_path = self.root / "isolated-report.json"
        report_path.touch(mode=0o600)
        # Homebrew installs this test's decoder wheels in the user site, which
        # ``-I`` deliberately excludes. Add only the already-imported wheel
        # roots so the subprocess still has no repository/CWD import path; the
        # verifier itself must locate its sealed sibling validator by file.
        dependency_roots = sorted(
            {
                str(Path(np.__file__).resolve().parents[1]),
                str(Path(tifffile.__file__).resolve().parents[1]),
            }
        )
        script = str(Path(verify_runtime_acceptance.__file__).resolve())
        arguments = [
            script,
            "--emit-tiff-inspection",
            "--output-dir", str(self.outputs),
            "--report", str(report_path),
        ]
        bootstrap = (
            "import runpy, sys\n"
            f"sys.path[:0] = {dependency_roots!r}\n"
            f"sys.argv = {arguments!r}\n"
            f"runpy.run_path({script!r}, run_name='__main__')\n"
        )

        result = subprocess.run(
            [sys.executable, "-I", "-c", bootstrap],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TIFF inspection: COMPLETE", result.stdout)

    def test_matching_trusted_local_image_needs_no_registry_evidence(self) -> None:
        self.write_docker_inspect(RepoDigests=[])

        report, exit_code = self.verify_local()

        self.assertEqual(exit_code, 0)
        self.assertTrue(report["passed"])
        self.assertEqual(
            report["candidate_engine"]["identity_mode"], "trusted-local-image"
        )
        self.assertEqual(report["candidate_engine"]["docker_image_id"], IMAGE_ID)
        self.assertNotIn("docker_image_reference", report["candidate_engine"])
        self.assertNotIn("registry_digest", report["candidate_engine"])
        self.assertNotIn("verified_provenance", report)
        self.assertNotIn("registry_manifest", report)

    def test_trusted_local_cli_passes_without_registry_arguments(self) -> None:
        self.write_docker_inspect(RepoDigests=[])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = verify_runtime_acceptance.main(
                [
                    str(self.manifest),
                    "--source-dir", str(self.sources),
                    "--output-dir", str(self.outputs),
                    "--sdk-dir", str(self.sdk_dir),
                    "--sdk-manifest", str(self.sdk_manifest),
                    "--docker-image-id", IMAGE_ID,
                    "--trusted-local-image",
                    "--docker-source-fingerprint", SOURCE_FINGERPRINT,
                    "--expected-source-fingerprint", SOURCE_FINGERPRINT,
                    "--expected-revision", EXPECTED_REVISION,
                    "--docker-inspect", str(self.docker_inspect),
                    "--tiff-inspection-report", str(self.inspection_report),
                    "--json",
                ]
            )

        self.assertEqual(exit_code, 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report["passed"])
        self.assertEqual(
            report["candidate_engine"]["identity_mode"], "trusted-local-image"
        )

    def test_trusted_local_mode_rejects_all_registry_evidence(self) -> None:
        common = {
            "manifest_path": self.manifest,
            "source_dir": self.sources,
            "output_dir": self.outputs,
            "sdk_dir": self.sdk_dir,
            "sdk_manifest": self.sdk_manifest,
            "expected_label": "linux",
            "image_id": IMAGE_ID,
            "image_reference": None,
            "source_fingerprint": SOURCE_FINGERPRINT,
            "expected_source_fingerprint": SOURCE_FINGERPRINT,
            "expected_revision": EXPECTED_REVISION,
            "provenance_path": None,
            "registry_manifest_path": None,
            "docker_inspect_path": self.docker_inspect,
            "tiff_inspection_report_path": self.inspection_report,
            "trusted_local_image": True,
        }
        for name, value in (
            ("image_reference", IMAGE_REFERENCE),
            ("provenance_path", self.provenance),
            ("registry_manifest_path", self.registry_manifest),
        ):
            with self.subTest(argument=name):
                arguments = {**common, name: value}
                with self.assertRaisesRegex(
                    verify_runtime_acceptance.OperationalError, "cannot be combined"
                ):
                    verify_runtime_acceptance.verify(**arguments)

    def test_registry_mode_requires_both_registry_evidence_files(self) -> None:
        common = {
            "manifest_path": self.manifest,
            "source_dir": self.sources,
            "output_dir": self.outputs,
            "sdk_dir": self.sdk_dir,
            "sdk_manifest": self.sdk_manifest,
            "expected_label": "linux",
            "image_id": IMAGE_ID,
            "image_reference": IMAGE_REFERENCE,
            "source_fingerprint": SOURCE_FINGERPRINT,
            "expected_source_fingerprint": SOURCE_FINGERPRINT,
            "expected_revision": EXPECTED_REVISION,
            "provenance_path": self.provenance,
            "registry_manifest_path": self.registry_manifest,
            "docker_inspect_path": self.docker_inspect,
            "tiff_inspection_report_path": self.inspection_report,
        }
        for name in ("provenance_path", "registry_manifest_path"):
            with self.subTest(argument=name):
                arguments = {**common, name: None}
                with self.assertRaisesRegex(
                    verify_runtime_acceptance.OperationalError, "registry acceptance requires"
                ):
                    verify_runtime_acceptance.verify(**arguments)

    def test_trusted_local_inspect_binds_id_source_revision_platform_and_sdk_label(
        self,
    ) -> None:
        mutations = (
            ({"Id": "sha256:" + "f" * 64}, "supplied exact image ID"),
            ({"Architecture": "arm64"}, "exactly linux/amd64"),
            (
                {
                    "Config": {
                        "Labels": {
                            "io.nef-watch.source.fingerprint": "f" * 64,
                            "io.nef-watch.nikon-sdk.bundled": "false",
                            "org.opencontainers.image.revision": EXPECTED_REVISION,
                        }
                    }
                },
                "source label",
            ),
            (
                {
                    "Config": {
                        "Labels": {
                            "io.nef-watch.source.fingerprint": SOURCE_FINGERPRINT,
                            "io.nef-watch.nikon-sdk.bundled": "false",
                            "org.opencontainers.image.revision": "f" * 40,
                        }
                    }
                },
                "reviewed commit",
            ),
            (
                {
                    "Config": {
                        "Labels": {
                            "io.nef-watch.source.fingerprint": SOURCE_FINGERPRINT,
                            "io.nef-watch.nikon-sdk.bundled": "true",
                            "org.opencontainers.image.revision": EXPECTED_REVISION,
                        }
                    }
                },
                "SDK-free label",
            ),
        )
        for overrides, message in mutations:
            with self.subTest(message=message):
                self.write_docker_inspect(RepoDigests=[], **overrides)
                with self.assertRaisesRegex(
                    verify_runtime_acceptance.OperationalError, message
                ):
                    self.verify_local()

    def test_image_must_pin_exactly_one_render_job(self) -> None:
        for environment in (
            [],
            ["NEF_WATCH_RENDER_JOBS=2"],
            ["NEF_WATCH_RENDER_JOBS=1", "NEF_WATCH_RENDER_JOBS=1"],
            ["NEF_WATCH_RENDER_JOBS=1", "NEF_WATCH_RENDER_JOBS=2"],
            ["NEF_WATCH_RENDER_JOBS=1", None],
        ):
            with self.subTest(environment=environment):
                inspect = json.loads(self.docker_inspect.read_text(encoding="utf-8"))
                inspect[0]["Config"]["Env"] = environment
                self.docker_inspect.write_text(json.dumps(inspect), encoding="utf-8")
                with self.assertRaisesRegex(
                    verify_runtime_acceptance.OperationalError,
                    "exactly NEF_WATCH_RENDER_JOBS=1",
                ):
                    self.verify_local()
                self.write_docker_inspect()

    def test_changed_decoded_pixel_fails(self) -> None:
        changed = self.pixels.copy()
        changed[2, 3, 1] += 1
        write_tiff(self.output, changed, self.icc)

        report, exit_code = self.verify()

        self.assertEqual(exit_code, 1)
        self.assertTrue(any("decoded pixel SHA-256" in error for error in report["errors"]))

    def test_same_pixel_bytes_with_swapped_dimensions_fails_raster_binding(self) -> None:
        swapped = self.pixels.reshape(7, 6, 3)
        self.assertEqual(
            validate_tiffs._canonical_pixel_hash(swapped),
            validate_tiffs._canonical_pixel_hash(self.pixels),
        )
        write_tiff(self.output, swapped, self.icc)

        report, exit_code = self.verify()

        self.assertEqual(exit_code, 1)
        self.assertFalse(
            any("decoded pixel SHA-256" in error for error in report["errors"])
        )
        self.assertTrue(
            any("raster shape_y_x_rgb" in error for error in report["errors"])
        )

    def test_wrong_source_and_sdk_manifest_fail_provenance(self) -> None:
        self.source.write_bytes(b"different source")
        self.sdk_file.write_bytes(b"changed sdk")

        report, exit_code = self.verify()

        self.assertEqual(exit_code, 1)
        self.assertTrue(any("source SHA-256" in error for error in report["errors"]))
        self.assertTrue(any("SDK file" in error for error in report["errors"]))

    def test_extra_tiff_fails_closed(self) -> None:
        write_tiff(self.outputs / "unexpected.tif", self.pixels, self.icc)

        report, exit_code = self.verify()

        self.assertEqual(exit_code, 1)
        self.assertTrue(any("extra=['unexpected.tif']" in error for error in report["errors"]))

    def test_unexpected_non_tiff_output_is_rejected(self) -> None:
        (self.outputs / "candidate.log").write_text("private output", encoding="utf-8")
        with self.assertRaisesRegex(
            verify_runtime_acceptance.OperationalError, "unexpected file"
        ):
            self.verify()

    def test_expected_output_lock_is_bounded_and_allowed(self) -> None:
        (self.outputs / ".nef-watch-output.lock").write_text("", encoding="utf-8")
        report, exit_code = self.verify()
        self.assertEqual(exit_code, 0)
        self.assertTrue(report["passed"])

    def test_output_lock_must_be_empty(self) -> None:
        (self.outputs / ".nef-watch-output.lock").write_text(
            "unexpected payload", encoding="utf-8"
        )
        with self.assertRaisesRegex(
            verify_runtime_acceptance.OperationalError, "unexpected file"
        ):
            self.verify()

    def test_manifest_path_escape_is_rejected(self) -> None:
        self.manifest_data["images"][0]["source_nef"] = "../outside.NEF"
        self.write_manifest()

        with self.assertRaisesRegex(verify_runtime_acceptance.OperationalError, "safe relative path"):
            self.verify()

    def test_empty_manifest_is_rejected(self) -> None:
        self.manifest_data["images"] = []
        self.write_manifest()

        with self.assertRaisesRegex(verify_runtime_acceptance.OperationalError, "malformed"):
            self.verify()

    def test_non_object_manifest_is_rejected_without_traceback(self) -> None:
        self.manifest.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(verify_runtime_acceptance.OperationalError, "malformed"):
            self.verify()

    def test_invalid_candidate_identity_is_rejected(self) -> None:
        with self.assertRaisesRegex(verify_runtime_acceptance.OperationalError, "immutable sha256"):
            verify_runtime_acceptance.verify(
                manifest_path=self.manifest,
                source_dir=self.sources,
                output_dir=self.outputs,
                sdk_dir=self.sdk_dir,
                sdk_manifest=self.sdk_manifest,
                expected_label="linux",
                image_id="nef-watch:latest",
                image_reference=IMAGE_REFERENCE,
                source_fingerprint=SOURCE_FINGERPRINT,
                expected_source_fingerprint=SOURCE_FINGERPRINT,
                expected_revision=EXPECTED_REVISION,
                provenance_path=self.provenance,
                registry_manifest_path=self.registry_manifest,
                docker_inspect_path=self.docker_inspect,
                tiff_inspection_report_path=self.inspection_report,
            )

    def test_mutable_candidate_reference_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            verify_runtime_acceptance.OperationalError, "exact registry reference"
        ):
            verify_runtime_acceptance.verify(
                manifest_path=self.manifest,
                source_dir=self.sources,
                output_dir=self.outputs,
                sdk_dir=self.sdk_dir,
                sdk_manifest=self.sdk_manifest,
                expected_label="linux",
                image_id=IMAGE_ID,
                image_reference="ghcr.io/rohanpandula/nef-watch:latest",
                source_fingerprint=SOURCE_FINGERPRINT,
                expected_source_fingerprint=SOURCE_FINGERPRINT,
                expected_revision=EXPECTED_REVISION,
                provenance_path=self.provenance,
                registry_manifest_path=self.registry_manifest,
                docker_inspect_path=self.docker_inspect,
                tiff_inspection_report_path=self.inspection_report,
            )

    def test_source_label_must_match_audited_checkout(self) -> None:
        report, exit_code = verify_runtime_acceptance.verify(
            manifest_path=self.manifest,
            source_dir=self.sources,
            output_dir=self.outputs,
            sdk_dir=self.sdk_dir,
            sdk_manifest=self.sdk_manifest,
            expected_label="linux",
            image_id=IMAGE_ID,
            image_reference=IMAGE_REFERENCE,
            source_fingerprint=SOURCE_FINGERPRINT,
            expected_source_fingerprint="e" * 64,
            expected_revision=EXPECTED_REVISION,
            provenance_path=self.provenance,
            registry_manifest_path=self.registry_manifest,
            docker_inspect_path=self.docker_inspect,
            tiff_inspection_report_path=self.inspection_report,
        )

        self.assertEqual(exit_code, 1)
        self.assertFalse(report["passed"])
        self.assertTrue(
            any("audited checkout" in error for error in report["errors"])
        )

    def test_provenance_must_name_exact_registry_digest(self) -> None:
        self.provenance.write_text(
            json.dumps(
                [
                    {
                        "verificationResult": {
                            "statement": {
                                "subject": [{"digest": {"sha256": "f" * 64}}]
                            }
                        }
                    }
                ]
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            verify_runtime_acceptance.OperationalError, "exact candidate registry digest"
        ):
            self.verify()

    def test_registry_manifest_config_must_equal_image_id(self) -> None:
        manifest = json.loads(REGISTRY_MANIFEST_BYTES)
        manifest["config"]["digest"] = "sha256:" + "c" * 64
        payload = json.dumps(
            manifest, sort_keys=True, separators=(",", ":")
        ).encode()
        self.registry_manifest.write_bytes(payload)
        mismatched_reference = (
            "ghcr.io/rohanpandula/nef-watch@sha256:"
            + hashlib.sha256(payload).hexdigest()
        )
        with self.assertRaisesRegex(
            verify_runtime_acceptance.OperationalError, "does not match the local image ID"
        ):
            verify_runtime_acceptance._verified_registry_manifest(
                self.registry_manifest,
                mismatched_reference.rsplit("sha256:", 1)[1],
                IMAGE_ID,
            )

    def test_docker_inspect_binds_platform_revision_and_source_label(self) -> None:
        self.write_docker_inspect(Architecture="arm64")
        with self.assertRaisesRegex(
            verify_runtime_acceptance.OperationalError, "exactly linux/amd64"
        ):
            self.verify()

        self.write_docker_inspect()
        inspect_data = json.loads(self.docker_inspect.read_text(encoding="utf-8"))
        inspect_data[0]["Config"]["Labels"][
            "org.opencontainers.image.revision"
        ] = "f" * 40
        self.docker_inspect.write_text(json.dumps(inspect_data), encoding="utf-8")
        with self.assertRaisesRegex(
            verify_runtime_acceptance.OperationalError, "reviewed commit"
        ):
            self.verify()

    def test_cli_requires_the_actual_sdk_directory(self) -> None:
        parser = verify_runtime_acceptance.build_parser()
        common = [
            str(self.manifest),
            "--source-dir", str(self.sources),
            "--output-dir", str(self.outputs),
            "--sdk-manifest", str(self.sdk_manifest),
            "--docker-image-id", IMAGE_ID,
            "--docker-image-reference", IMAGE_REFERENCE,
            "--docker-source-fingerprint", SOURCE_FINGERPRINT,
            "--expected-source-fingerprint", SOURCE_FINGERPRINT,
            "--expected-revision", EXPECTED_REVISION,
            "--verified-provenance", str(self.provenance),
            "--registry-manifest", str(self.registry_manifest),
            "--docker-inspect", str(self.docker_inspect),
            "--tiff-inspection-report", str(self.inspection_report),
        ]
        with self.assertRaises(SystemExit) as raised:
            parser.parse_args(common)
        self.assertEqual(raised.exception.code, 2)
        parsed = parser.parse_args([*common, "--sdk-dir", str(self.sdk_dir)])
        self.assertEqual(parsed.sdk_dir, self.sdk_dir)

    def test_cli_identity_modes_are_mutually_exclusive_and_required(self) -> None:
        parser = verify_runtime_acceptance.build_parser()
        common = [
            str(self.manifest),
            "--source-dir", str(self.sources),
            "--output-dir", str(self.outputs),
            "--sdk-dir", str(self.sdk_dir),
            "--sdk-manifest", str(self.sdk_manifest),
            "--docker-image-id", IMAGE_ID,
            "--docker-source-fingerprint", SOURCE_FINGERPRINT,
            "--expected-source-fingerprint", SOURCE_FINGERPRINT,
            "--expected-revision", EXPECTED_REVISION,
            "--docker-inspect", str(self.docker_inspect),
            "--tiff-inspection-report", str(self.inspection_report),
        ]

        parsed = parser.parse_args([*common, "--trusted-local-image"])
        self.assertTrue(parsed.trusted_local_image)
        self.assertIsNone(parsed.docker_image_reference)

        with self.assertRaises(SystemExit) as missing:
            parser.parse_args(common)
        self.assertEqual(missing.exception.code, 2)

        with self.assertRaises(SystemExit) as mixed:
            parser.parse_args(
                [
                    *common,
                    "--trusted-local-image",
                    "--docker-image-reference", IMAGE_REFERENCE,
                ]
            )
        self.assertEqual(mixed.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
