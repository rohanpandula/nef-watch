from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "docker" / "validate_acceptance_marker.py"
SPEC = importlib.util.spec_from_file_location("validate_acceptance_marker", MODULE_PATH)
acceptance_marker = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = acceptance_marker
SPEC.loader.exec_module(acceptance_marker)

IMAGE_ID = "sha256:" + "a" * 64
FINGERPRINT = "b" * 64
REVISION = "c" * 40


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def parse_sdk_manifest(path: Path) -> list[dict[str, str]]:
    return [
        {"path": line[66:], "sha256": line[:64]}
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def passing_report(baseline_path: Path, sdk_path: Path) -> dict[str, object]:
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    checked_sources = [
        {
            "name": image["name"],
            "path": image["source_nef"],
            "sha256": image["source_sha256"],
        }
        for image in baseline["images"]
    ]
    checked_outputs = []
    for index, image in enumerate(baseline["images"], 1):
        linux = image["artifacts"]["linux"]
        checked_outputs.append(
            {
                "label": f"candidate:{image['tiff']}",
                "path": image["tiff"],
                "primary_series": 0,
                "ignored_series": 0,
                "source_axes": "YXS",
                **image["raster"],
                "pixel_sha256": linux["pixel_sha256"],
                "icc_sha256": linux["icc_sha256"],
                "contract_errors": [],
                "file_size": index,
                "file_sha256": f"{index:064x}",
            }
        )
    sdk_sha = file_sha256(sdk_path)
    return {
        "schema_version": 1,
        "criterion": (
            "private-source-and-sdk-plus-trusted-local-image-and-recorded-linux-pixels"
        ),
        "passed": True,
        "errors": [],
        "manifest": {
            "file": "baseline-v1.json",
            "sha256": file_sha256(baseline_path),
        },
        "expected_artifact_label": "linux",
        "candidate_engine": {
            "identity_mode": "trusted-local-image",
            "docker_image_id": IMAGE_ID,
            "docker_source_fingerprint": FINGERPRINT,
            "expected_source_fingerprint": FINGERPRINT,
        },
        "sdk_manifest": {
            "file": "nikon-sdk-v1.46.sha256",
            "sha256": sdk_sha,
            "expected_sha256": sdk_sha,
            "checked_files": parse_sdk_manifest(sdk_path),
        },
        "docker_inspect": {
            "file": "docker-inspect.json",
            "sha256": "d" * 64,
            "platform": "linux/amd64",
            "revision": REVISION,
            "render_jobs": 1,
        },
        "sandboxed_tiff_inspection": {
            "file": "tiff-inspection.json",
            "sha256": "e" * 64,
            "output_count": len(checked_outputs),
        },
        "checked_sources": checked_sources,
        "checked_outputs": checked_outputs,
    }


class RuntimeAcceptanceMarkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.baseline = self.root / "validation-baseline-v1.json"
        self.sdk_manifest = self.root / "nikon-sdk-v1.46.sha256"
        self.identity = self.root / "source-identity.json"
        self.report = self.root / "acceptance.json"
        self.baseline.write_bytes(
            (REPO / "validation" / "baseline-v1.json").read_bytes()
        )
        self.sdk_manifest.write_bytes(
            (REPO / "docker" / "nikon-sdk-v1.46.sha256").read_bytes()
        )
        self.identity.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_fingerprint": FINGERPRINT,
                    "source_revision": REVISION,
                }
            ),
            encoding="utf-8",
        )
        self.report.write_text(
            json.dumps(passing_report(self.baseline, self.sdk_manifest)),
            encoding="utf-8",
        )
        for path in (self.baseline, self.sdk_manifest, self.identity, self.report):
            path.chmod(0o444)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def validate(self) -> None:
        acceptance_marker.validate_runtime_acceptance(
            self.report,
            image_reference=IMAGE_ID,
            identity_path=self.identity,
            baseline_path=self.baseline,
            sdk_manifest_path=self.sdk_manifest,
            expected_owner=os.getuid(),
        )

    def test_exact_read_only_report_and_immutable_image_evidence_pass(self) -> None:
        self.validate()

    def rewrite_json(self, path: Path, value: object) -> None:
        path.chmod(0o644)
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o444)

    def test_image_source_revision_baseline_and_sdk_bindings_fail_closed(self) -> None:
        mutations = {
            "image ID": lambda: acceptance_marker.validate_runtime_acceptance(
                self.report,
                image_reference="sha256:" + "f" * 64,
                identity_path=self.identity,
                baseline_path=self.baseline,
                sdk_manifest_path=self.sdk_manifest,
                expected_owner=os.getuid(),
            ),
            "source fingerprint": lambda: self.rewrite_json(
                self.identity,
                {
                    "schema_version": 1,
                    "source_fingerprint": "f" * 64,
                    "source_revision": REVISION,
                },
            ),
            "source revision": lambda: self.rewrite_json(
                self.identity,
                {
                    "schema_version": 1,
                    "source_fingerprint": FINGERPRINT,
                    "source_revision": "f" * 40,
                },
            ),
            "fixture baseline bytes": lambda: self.rewrite_json(
                self.baseline,
                {
                    **json.loads(self.baseline.read_text(encoding="utf-8")),
                    "captured_at": "2099-01-01",
                },
            ),
            "SDK manifest bytes": lambda: self.sdk_manifest.chmod(0o644),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                self.tearDown()
                self.setUp()
                if label == "image ID":
                    with self.assertRaises(acceptance_marker.AcceptanceMarkerError):
                        mutate()
                    continue
                mutate()
                if label == "SDK manifest bytes":
                    lines = self.sdk_manifest.read_text(encoding="utf-8").splitlines()
                    lines[0] = ("f" if lines[0][0] != "f" else "e") + lines[0][1:]
                    self.sdk_manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    self.sdk_manifest.chmod(0o444)
                with self.assertRaises(acceptance_marker.AcceptanceMarkerError):
                    self.validate()

    def test_report_must_be_owner_readable_but_not_writable_or_linked(self) -> None:
        self.report.chmod(0o644)
        with self.assertRaisesRegex(
            acceptance_marker.AcceptanceMarkerError, "mode-0444"
        ):
            self.validate()

        self.report.chmod(0o444)
        alias = self.root / "report-alias.json"
        os.link(self.report, alias)
        with self.assertRaisesRegex(
            acceptance_marker.AcceptanceMarkerError, "single-link"
        ):
            self.validate()

    def test_source_sdk_pixel_raster_and_schema_evidence_fail_closed(self) -> None:
        def source_hash(report: dict[str, object]) -> None:
            report["checked_sources"][0]["sha256"] = "f" * 64

        def sdk_hash(report: dict[str, object]) -> None:
            report["sdk_manifest"]["checked_files"][0]["sha256"] = "f" * 64

        def pixel_hash(report: dict[str, object]) -> None:
            report["checked_outputs"][0]["pixel_sha256"] = "f" * 64

        def raster_shape(report: dict[str, object]) -> None:
            report["checked_outputs"][0]["shape_y_x_rgb"] = [1, 1, 3]

        def contract_error(report: dict[str, object]) -> None:
            report["checked_outputs"][0]["contract_errors"] = ["mismatch"]

        def extra_field(report: dict[str, object]) -> None:
            report["untrusted_extension"] = True

        def extra_output_field(report: dict[str, object]) -> None:
            report["checked_outputs"][0]["untrusted_extension"] = True

        for label, mutate in (
            ("source hash", source_hash),
            ("SDK hash", sdk_hash),
            ("pixel hash", pixel_hash),
            ("raster shape", raster_shape),
            ("TIFF contract", contract_error),
            ("report schema", extra_field),
            ("TIFF evidence schema", extra_output_field),
        ):
            with self.subTest(label=label):
                self.tearDown()
                self.setUp()
                report = json.loads(self.report.read_text(encoding="utf-8"))
                mutate(report)
                self.rewrite_json(self.report, report)
                with self.assertRaises(acceptance_marker.AcceptanceMarkerError):
                    self.validate()


if __name__ == "__main__":
    unittest.main()
