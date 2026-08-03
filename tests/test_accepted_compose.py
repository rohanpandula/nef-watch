from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "docker" / "accepted_compose.py"
SPEC = importlib.util.spec_from_file_location("accepted_compose", MODULE_PATH)
accepted_compose = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = accepted_compose
SPEC.loader.exec_module(accepted_compose)

IMAGE_ID = "sha256:" + "a" * 64
FINGERPRINT = "b" * 64
REVISION = "c" * 40
ACCEPTANCE_REPORT = "/trusted/acceptance-reports/" + "a" * 64 + ".acceptance.json"


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def passing_report():
    baseline_path = REPO / "validation" / "baseline-v1.json"
    sdk_path = REPO / "docker" / "nikon-sdk-v1.46.sha256"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline_sha = file_sha256(baseline_path)
    sdk_sha = file_sha256(sdk_path)
    sdk_files = accepted_compose.parse_sdk_manifest(sdk_path)
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
        raster = image["raster"]
        checked_outputs.append(
            {
                "label": f"candidate:{image['tiff']}",
                "path": image["tiff"],
                "primary_series": 0,
                "ignored_series": 0,
                "source_axes": "YXS",
                **raster,
                "pixel_sha256": linux["pixel_sha256"],
                "icc_sha256": linux["icc_sha256"],
                "contract_errors": [],
                "file_size": index,
                "file_sha256": f"{index:064x}",
            }
        )
    report = {
        "schema_version": 1,
        "criterion": (
            "private-source-and-sdk-plus-trusted-local-image-and-recorded-linux-pixels"
        ),
        "passed": True,
        "errors": [],
        "manifest": {"file": "baseline-v1.json", "sha256": baseline_sha},
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
            "checked_files": sdk_files,
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
    arguments = {
        "image_id": IMAGE_ID,
        "fingerprint": FINGERPRINT,
        "revision": REVISION,
        "baseline": baseline,
        "baseline_sha256": baseline_sha,
        "sdk_manifest_sha256": sdk_sha,
        "sdk_files": sdk_files,
    }
    return report, arguments


def declared_resources():
    return {
        "memory": 4 * accepted_compose.GIB,
        "cpus": accepted_compose.Decimal("4"),
        "pids": 256,
        "render_file_size_mib": 2048,
        "temp_capacity": 16 * accepted_compose.GIB,
        "temp_free": 8 * accepted_compose.GIB,
        "interval": accepted_compose.Decimal("3"),
        "health_max_age": accepted_compose.Decimal("120"),
    }


def compose_model():
    resources = declared_resources()

    def service(volumes):
        return {
            "image": IMAGE_ID,
            "platform": "linux/amd64",
            "pull_policy": "never",
            "network_mode": "none",
            "read_only": True,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "mem_limit": str(resources["memory"]),
            "memswap_limit": str(resources["memory"]),
            "cpus": 4,
            "pids_limit": resources["pids"],
            "ulimits": {
                "fsize": {"hard": 2147483648, "soft": 2147483648},
                "nofile": {"hard": 512, "soft": 512},
            },
            "environment": {
                "NEF_WATCH_IMAGE_REFERENCE": IMAGE_ID,
                "NEF_WATCH_REQUIRE_ACCEPTANCE_REPORT": "1",
                "NEF_WATCH_PRE_ACCEPTANCE_FIXTURE_MODE": "0",
                "NEF_WATCH_REQUIRE_LANDLOCK": "1",
                "NEF_WATCH_RENDER_JOBS": "1",
                "NEF_WATCH_RENDER_FILE_SIZE_MIB": "2048",
                "NEF_WATCH_INTERVAL_SECONDS": "3",
                "NEF_WATCH_HEALTH_MAX_AGE": "120",
                "NEF_WATCH_TEMP_CAPACITY_BYTES": str(resources["temp_capacity"]),
                "NEF_WATCH_TEMP_MIN_FREE_BYTES": str(resources["temp_free"]),
            },
            "volumes": volumes,
        }

    input_volume = {
        "type": "bind",
        "source": "/photos/input",
        "target": "/input",
        "read_only": True,
        "bind": {"create_host_path": False},
    }
    output_volume = {
        "type": "bind",
        "source": "/photos/output",
        "target": "/output",
        "bind": {"create_host_path": False},
    }
    temp_volume = {
        "type": "bind",
        "source": "/nvme/nef-watch",
        "target": "/work/nef-watch",
        "bind": {"create_host_path": False},
    }
    state_volume = {
        "type": "volume",
        "source": "nef-watch-state",
        "target": "/var/lib/nef-watch",
    }
    acceptance_volume = {
        "type": "bind",
        "source": ACCEPTANCE_REPORT,
        "target": "/run/nef-watch-acceptance.json",
        "read_only": True,
        "bind": {"create_host_path": False},
    }
    runtime = service(
        [
            copy.deepcopy(input_volume),
            copy.deepcopy(output_volume),
            copy.deepcopy(temp_volume),
            copy.deepcopy(state_volume),
            copy.deepcopy(acceptance_volume),
        ]
    )
    runtime["user"] = "99:100"
    runtime["command"] = [
        "/input",
        "--out",
        "/output",
        "--interval",
        "3",
    ]
    initializer_input = copy.deepcopy(input_volume)
    initializer_output = copy.deepcopy(output_volume)
    initializer_output["read_only"] = True
    initializer = service(
        [
            initializer_input,
            initializer_output,
            {
                "type": "bind",
                "source": "/private/sdk",
                "target": "/nikon-sdk",
                "read_only": True,
                "bind": {"create_host_path": False},
            },
            copy.deepcopy(temp_volume),
            copy.deepcopy(state_volume),
            copy.deepcopy(acceptance_volume),
        ]
    )
    initializer["cap_add"] = [
        "CHOWN",
        "DAC_OVERRIDE",
        "FOWNER",
        "KILL",
        "SETGID",
        "SETPCAP",
        "SETUID",
    ]
    return {
        "name": "docker",
        "services": {"nef-watch": runtime, "nef-watch-init": initializer},
    }


class ResourceLimitTests(unittest.TestCase):
    def setUp(self):
        self.dotenv = {
            "IMAGE": IMAGE_ID,
            "MEMORY_LIMIT": "4g",
            "CPU_LIMIT": "4",
            "PIDS_LIMIT": "256",
            "RENDER_FILE_SIZE_MIB": "2048",
            "TEMP_CAPACITY_BYTES": str(16 * accepted_compose.GIB),
            "TEMP_MIN_FREE_BYTES": str(8 * accepted_compose.GIB),
            "INTERVAL": "3",
            "HEALTH_MAX_AGE_SECONDS": "120",
            "ACCEPTANCE_REPORT": ACCEPTANCE_REPORT,
        }

    def test_positive_finite_defaults_pass(self):
        parsed = accepted_compose.validate_declared_resources(
            self.dotenv, IMAGE_ID, ACCEPTANCE_REPORT
        )
        self.assertEqual(parsed, declared_resources())

    def test_zero_unlimited_nonfinite_and_below_floor_values_fail(self):
        cases = {
            "zero memory": ("MEMORY_LIMIT", "0"),
            "low memory": ("MEMORY_LIMIT", "1g"),
            "zero cpu": ("CPU_LIMIT", "0"),
            "nonfinite cpu": ("CPU_LIMIT", "NaN"),
            "unlimited pids": ("PIDS_LIMIT", "-1"),
            "low pids": ("PIDS_LIMIT", "127"),
            "low temp capacity": ("TEMP_CAPACITY_BYTES", str(4 * accepted_compose.GIB)),
            "low temp free": ("TEMP_MIN_FREE_BYTES", str(4 * accepted_compose.GIB)),
        }
        for label, (key, value) in cases.items():
            with self.subTest(label=label):
                candidate = dict(self.dotenv)
                candidate[key] = value
                with self.assertRaises(accepted_compose.AcceptanceError):
                    accepted_compose.validate_declared_resources(
                        candidate, IMAGE_ID, ACCEPTANCE_REPORT
                    )

    def test_render_file_and_health_boundaries_are_enforced(self):
        for key, value in (
            ("RENDER_FILE_SIZE_MIB", "2049"),
            ("HEALTH_MAX_AGE_SECONDS", "59"),
            ("HEALTH_MAX_AGE_SECONDS", "901"),
            ("INTERVAL", "841"),
        ):
            with self.subTest(key=key, value=value):
                candidate = dict(self.dotenv)
                candidate[key] = value
                with self.assertRaises(accepted_compose.AcceptanceError):
                    accepted_compose.validate_declared_resources(
                        candidate, IMAGE_ID, ACCEPTANCE_REPORT
                    )

        candidate = dict(self.dotenv)
        candidate["INTERVAL"] = "61"
        candidate["HEALTH_MAX_AGE_SECONDS"] = "120"
        with self.assertRaisesRegex(
            accepted_compose.AcceptanceError, "exceed INTERVAL"
        ):
            accepted_compose.validate_declared_resources(
                candidate, IMAGE_ID, ACCEPTANCE_REPORT
            )

    def test_effective_compose_binds_production_isolation_and_file_limit(self):
        for mutation in ("landlock", "fixture mode", "fsize env", "ulimit"):
            with self.subTest(mutation=mutation):
                candidate = compose_model()
                service = candidate["services"]["nef-watch"]
                if mutation == "landlock":
                    service["environment"]["NEF_WATCH_REQUIRE_LANDLOCK"] = "0"
                elif mutation == "fixture mode":
                    service["environment"]["NEF_WATCH_PRE_ACCEPTANCE_FIXTURE_MODE"] = "1"
                elif mutation == "fsize env":
                    service["environment"]["NEF_WATCH_RENDER_FILE_SIZE_MIB"] = "2049"
                else:
                    service["ulimits"]["fsize"]["hard"] = 4 * accepted_compose.GIB
                with self.assertRaises(accepted_compose.AcceptanceError):
                    accepted_compose.validate_compose_config(
                        candidate,
                        image_id=IMAGE_ID,
                        declared=declared_resources(),
                        acceptance_report=ACCEPTANCE_REPORT,
                    )

    def test_effective_compose_cannot_drop_or_change_limits(self):
        model = compose_model()
        storage = accepted_compose.validate_compose_config(
            model,
            image_id=IMAGE_ID,
            declared=declared_resources(),
            acceptance_report=ACCEPTANCE_REPORT,
        )
        self.assertEqual(storage["temp"], "/nvme/nef-watch")
        for field, value in (
            ("mem_limit", None),
            ("mem_limit", "0"),
            ("cpus", 0),
            ("pids_limit", -1),
        ):
            with self.subTest(field=field, value=value):
                candidate = copy.deepcopy(model)
                candidate["services"]["nef-watch"][field] = value
                with self.assertRaises(accepted_compose.AcceptanceError):
                    accepted_compose.validate_compose_config(
                        candidate,
                        image_id=IMAGE_ID,
                        declared=declared_resources(),
                        acceptance_report=ACCEPTANCE_REPORT,
                    )

    def test_report_path_and_read_only_bind_are_exact(self):
        for key, value in (
            ("ACCEPTANCE_REPORT", "/tmp/untrusted.json"),
            ("ACCEPTANCE_REPORT", ""),
        ):
            with self.subTest(value=value):
                candidate = dict(self.dotenv)
                candidate[key] = value
                with self.assertRaises(accepted_compose.AcceptanceError):
                    accepted_compose.validate_declared_resources(
                        candidate, IMAGE_ID, ACCEPTANCE_REPORT
                    )

        for mutation in ("source", "read_only", "create_host_path"):
            with self.subTest(mutation=mutation):
                candidate = compose_model()
                report_mount = candidate["services"]["nef-watch"]["volumes"][-1]
                if mutation == "source":
                    report_mount["source"] = "/tmp/untrusted.json"
                elif mutation == "read_only":
                    report_mount["read_only"] = False
                else:
                    report_mount["bind"]["create_host_path"] = True
                with self.assertRaises(accepted_compose.AcceptanceError):
                    accepted_compose.validate_compose_config(
                        candidate,
                        image_id=IMAGE_ID,
                        declared=declared_resources(),
                        acceptance_report=ACCEPTANCE_REPORT,
                    )


class ProductionSdkTests(unittest.TestCase):
    def test_sdk_files_are_rehashed_at_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = root / "Bin" / "example.dll"
            fixture.parent.mkdir()
            fixture.write_bytes(b"accepted SDK fixture")
            entries = [
                {
                    "path": "Bin/example.dll",
                    "sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
                }
            ]
            accepted_compose.validate_sdk_directory(root, entries)
            fixture.write_bytes(b"changed after acceptance")
            with self.assertRaisesRegex(
                accepted_compose.AcceptanceError, "changed after acceptance"
            ):
                accepted_compose.validate_sdk_directory(root, entries)

    def test_sdk_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            real = root / "real.dll"
            real.write_bytes(b"sdk")
            link = root / "link.dll"
            link.symlink_to(real)
            entries = [
                {
                    "path": "link.dll",
                    "sha256": hashlib.sha256(real.read_bytes()).hexdigest(),
                }
            ]
            with self.assertRaisesRegex(
                accepted_compose.AcceptanceError, "symbolic link"
            ):
                accepted_compose.validate_sdk_directory(root, entries)


class RetainedReportTests(unittest.TestCase):
    def test_exact_current_image_source_sdk_and_fixtures_pass(self):
        report, arguments = passing_report()
        accepted_compose.validate_acceptance_report(report, **arguments)

    def test_each_binding_is_fail_closed(self):
        report, arguments = passing_report()
        mutations = {
            "not passing": lambda value: value.update(passed=False),
            "wrong image": lambda value: value["candidate_engine"].update(
                docker_image_id="sha256:" + "f" * 64
            ),
            "wrong source": lambda value: value["candidate_engine"].update(
                docker_source_fingerprint="f" * 64
            ),
            "wrong baseline": lambda value: value["manifest"].update(sha256="f" * 64),
            "wrong sdk fixture": lambda value: value["sdk_manifest"]["checked_files"][0].update(
                sha256="f" * 64
            ),
            "wrong nef fixture": lambda value: value["checked_sources"][0].update(
                sha256="f" * 64
            ),
            "wrong pixels": lambda value: value["checked_outputs"][0].update(
                pixel_sha256="f" * 64
            ),
            "swapped raster dimensions": lambda value: value["checked_outputs"][0].update(
                shape_y_x_rgb=[4032, 6048, 3]
            ),
            "contract failure": lambda value: value["checked_outputs"][0].update(
                contract_errors=["mismatch"]
            ),
            "unexpected report field": lambda value: value.update(
                untrusted_extension=True
            ),
            "unexpected TIFF field": lambda value: value["checked_outputs"][0].update(
                untrusted_extension=True
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                candidate = copy.deepcopy(report)
                mutate(candidate)
                with self.assertRaises(accepted_compose.AcceptanceError):
                    accepted_compose.validate_acceptance_report(candidate, **arguments)

    def test_missing_report_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.acceptance.json"
            with self.assertRaises(accepted_compose.AcceptanceError):
                accepted_compose._load_json(missing)


class BaselineMetadataTests(unittest.TestCase):
    def test_raster_metadata_added_without_changing_recorded_artifact_hashes(self):
        baseline = json.loads(
            (REPO / "validation" / "baseline-v1.json").read_text(encoding="utf-8")
        )
        recorded = []
        expected_raster = {
            "shape_y_x_rgb": [6048, 4032, 3],
            "dtype": "uint8",
            "bits_per_sample": [8, 8, 8],
            "photometric": "RGB",
            "orientation": 1,
        }
        for image in baseline["images"]:
            self.assertEqual(image["raster"], expected_raster)
            for label in baseline["artifact_labels"]:
                artifact = image["artifacts"][label]
                recorded.append(
                    [
                        image["name"],
                        label,
                        artifact["pixel_sha256"],
                        artifact["icc_sha256"],
                    ]
                )
        digest = hashlib.sha256(
            json.dumps(recorded, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.assertEqual(
            digest,
            "e751c3d3bed0b36ae198159d8d347440b642197b710c1b91da1d623e996cd099",
        )


class CommandBoundaryTests(unittest.TestCase):
    def test_every_starting_command_requires_acceptance(self):
        for command in accepted_compose.START_COMMANDS:
            with self.subTest(command=command):
                self.assertTrue(accepted_compose.classify_command([command]))
        self.assertTrue(accepted_compose.classify_command(["up", "-d"]))
        self.assertTrue(accepted_compose.classify_command(["up", "--detach"]))

    def test_emergency_stop_and_diagnostics_remain_available(self):
        for command in ("down", "stop", "kill", "ps", "logs", "config"):
            with self.subTest(command=command):
                self.assertFalse(accepted_compose.classify_command([command]))
        self.assertFalse(
            accepted_compose.classify_command(["logs", "-f", "nef-watch"])
        )

    def test_every_daemon_touching_emergency_revalidates_local_socket(self):
        for command in accepted_compose.EMERGENCY_COMMANDS - {"config", "version"}:
            with self.subTest(command=command):
                self.assertTrue(
                    accepted_compose.command_touches_docker_daemon([command])
                )
        for arguments in (["config"], ["version"], ["--help"], ["--version"]):
            with self.subTest(arguments=arguments):
                self.assertFalse(
                    accepted_compose.command_touches_docker_daemon(arguments)
                )

    def test_compose_environment_pins_default_context_and_local_socket(self):
        _prefix, environment, _cwd = accepted_compose._compose_prefix(
            Path("/reviewed/tree"), Path("/trusted/root")
        )
        self.assertEqual(environment["DOCKER_CONTEXT"], "default")
        self.assertEqual(
            environment["DOCKER_HOST"], "unix:///var/run/docker.sock"
        )

    def test_alternate_files_projects_builds_and_pulls_are_denied(self):
        for arguments in (
            ["up", "--build"],
            ["up", "--pull=always"],
            ["--file", "evil.yaml", "up"],
            ["up", "--project-name=evil"],
            ["build"],
            ["pull"],
            ["exec", "nef-watch", "sh"],
            ["run", "--privileged", "nef-watch-init", "/bin/sh"],
            ["run", "-v", "/:/host", "nef-watch-init", "/bin/sh"],
            ["start"],
            ["restart"],
            ["create"],
            ["unpause"],
            ["up", "--no-deps", "nef-watch"],
            ["up", "-d", "nef-watch"],
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(accepted_compose.AcceptanceError):
                    accepted_compose.classify_command(arguments)


if __name__ == "__main__":
    unittest.main()
