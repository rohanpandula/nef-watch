#!/usr/bin/env python3
"""Verify NEF provenance, recorded TIFF hashes, and the exact color gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

try:
    from . import validate_tiffs
except ImportError:  # Direct execution: python3 tool/verify_validation_baseline.py
    import validate_tiffs


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RASTER_BINDING_FIELDS = {
    "shape_y_x_rgb",
    "dtype",
    "bits_per_sample",
    "photometric",
    "orientation",
}


def expected_raster_binding(value: object, field: str) -> dict:
    if not isinstance(value, dict) or set(value) != RASTER_BINDING_FIELDS:
        raise validate_tiffs.OperationalError(
            f"{field} must contain the exact raster metadata fields"
        )
    shape = value.get("shape_y_x_rgb")
    dtype = value.get("dtype")
    expected_bits = [8, 8, 8] if dtype == "uint8" else [16, 16, 16]
    if (
        not isinstance(shape, list)
        or len(shape) != 3
        or shape[2] != 3
        or any(
            not isinstance(item, int) or isinstance(item, bool) or item <= 0
            for item in shape
        )
        or dtype not in {"uint8", "uint16"}
        or value.get("bits_per_sample") != expected_bits
        or value.get("photometric") != "RGB"
        or value.get("orientation") != 1
    ):
        raise validate_tiffs.OperationalError(
            f"{field} is malformed or violates the raster contract"
        )
    return value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify each source NEF SHA-256 and recorded decoded TIFF/ICC hash, "
            "then run the exact cross-renderer equality gate."
        )
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument(
        "--docker-image-id",
        required=True,
        help="immutable Docker image ID reported by `docker image inspect`",
    )
    parser.add_argument(
        "--docker-source-fingerprint",
        required=True,
        help="io.nef-watch.source.fingerprint label from that image",
    )
    parser.add_argument(
        "artifacts",
        nargs="+",
        metavar="LABEL=TIFF_DIR",
        help="artifact directories whose labels match the manifest (for example nx=... mac=... linux=...)",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def verify(args: argparse.Namespace) -> tuple[dict, int]:
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise validate_tiffs.OperationalError(
            f"cannot read validation manifest {args.manifest}: {exc}"
        ) from exc
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("images"), list):
        raise validate_tiffs.OperationalError("unsupported or malformed validation manifest")

    specs = validate_tiffs.parse_specs(args.artifacts)
    if not all(spec.path.is_dir() for spec in specs):
        raise validate_tiffs.OperationalError("manifest verification requires TIFF directories")
    inventories = {
        spec.label: validate_tiffs._directory_tiffs(spec.path) for spec in specs
    }
    required_labels = set(manifest.get("artifact_labels", []))
    supplied_labels = {spec.label for spec in specs}
    if supplied_labels != required_labels:
        raise validate_tiffs.OperationalError(
            "artifact labels differ from manifest: "
            f"required={sorted(required_labels)} supplied={sorted(supplied_labels)}"
        )

    provenance_errors: list[str] = []
    engines = manifest.get("engines")
    if not isinstance(engines, dict):
        provenance_errors.append("manifest engine identity is missing")
        engines = {}
    expected_image_id = engines.get("docker_image_id")
    expected_source_fingerprint = engines.get("docker_source_fingerprint")
    if (
        not isinstance(expected_image_id, str)
        or not expected_image_id.startswith("sha256:")
        or not SHA256_RE.fullmatch(expected_image_id.removeprefix("sha256:"))
    ):
        provenance_errors.append(
            "manifest engine identity docker_image_id is not an immutable sha256 ID"
        )
    if (
        not isinstance(expected_source_fingerprint, str)
        or not SHA256_RE.fullmatch(expected_source_fingerprint)
    ):
        provenance_errors.append(
            "manifest engine identity docker_source_fingerprint is not a SHA-256 fingerprint"
        )
    if args.docker_image_id != expected_image_id:
        provenance_errors.append(
            f"Docker image ID {args.docker_image_id} != {expected_image_id}"
        )
    if args.docker_source_fingerprint != expected_source_fingerprint:
        provenance_errors.append(
            "Docker source fingerprint "
            f"{args.docker_source_fingerprint} != {expected_source_fingerprint}"
        )
    expected_tiffs = set()
    checked_sources = []
    checked_artifacts = []
    for image in manifest["images"]:
        name = image.get("name", "<unnamed>")
        source_relpath = image.get("source_nef")
        source_expected = image.get("source_sha256")
        if not isinstance(source_relpath, str) or not isinstance(source_expected, str):
            provenance_errors.append(f"{name}: source manifest fields are missing")
            continue
        source_path = args.source_dir / source_relpath
        if not source_path.is_file():
            provenance_errors.append(f"{name}: source NEF is missing: {source_path}")
        else:
            actual = file_sha256(source_path)
            checked_sources.append(
                {"name": name, "path": str(source_path), "sha256": actual}
            )
            if actual != source_expected:
                provenance_errors.append(
                    f"{name}: source SHA-256 {actual} != {source_expected}"
                )

        tiff_relpath = image.get("tiff")
        if not isinstance(tiff_relpath, str):
            provenance_errors.append(f"{name}: TIFF relative path is missing")
            continue
        tiff_key = tiff_relpath.casefold()
        expected_tiffs.add(tiff_key)
        expected_artifacts = image.get("artifacts", {})
        expected_raster = expected_raster_binding(
            image.get("raster"), f"{name}: raster"
        )
        for label in sorted(required_labels):
            expected = expected_artifacts.get(label, {})
            path = inventories[label].get(tiff_key)
            if path is None:
                provenance_errors.append(f"{name}: {label} TIFF is missing: {tiff_relpath}")
                continue
            raster = validate_tiffs.inspect_tiff(path, f"{label}:{tiff_relpath}")
            summary = raster.summary()
            del raster
            checked_artifacts.append(summary)
            for field in ("pixel_sha256", "icc_sha256"):
                if summary.get(field) != expected.get(field):
                    provenance_errors.append(
                        f"{name}: {label} {field} {summary.get(field)} != {expected.get(field)}"
                    )
            for field in sorted(RASTER_BINDING_FIELDS):
                if summary.get(field) != expected_raster[field]:
                    provenance_errors.append(
                        f"{name}: {label} raster {field} {summary.get(field)!r} "
                        f"!= {expected_raster[field]!r}"
                    )
            if summary["contract_errors"]:
                provenance_errors.extend(
                    f"{name}: {label} contract: {error}"
                    for error in summary["contract_errors"]
                )

    for label, inventory in inventories.items():
        actual = set(inventory)
        if actual != expected_tiffs:
            missing = sorted(expected_tiffs - actual)
            extra = sorted(actual - expected_tiffs)
            provenance_errors.append(
                f"{label}: TIFF set differs from manifest; missing={missing} extra={extra}"
            )

    equality = validate_tiffs.validate(specs)
    passed = not provenance_errors and bool(equality["passed"])
    report = {
        "schema_version": 1,
        "criterion": "source-provenance-and-exact-decoded-rgb-with-identical-icc",
        "manifest": str(args.manifest.resolve()),
        "passed": passed,
        "provenance_passed": not provenance_errors,
        "provenance_errors": provenance_errors,
        "engine_identity": {
            "docker_image_id": args.docker_image_id,
            "docker_source_fingerprint": args.docker_source_fingerprint,
        },
        "checked_sources": checked_sources,
        "checked_artifacts": checked_artifacts,
        "equality": equality,
    }
    return report, 0 if passed else 1


def print_human(report: dict) -> None:
    print(
        "validation provenance: "
        + ("PASS" if report["provenance_passed"] else "FAIL")
    )
    for error in report["provenance_errors"]:
        print(f"  {error}")
    validate_tiffs.print_human(report["equality"])


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.manifest = args.manifest.expanduser().resolve()
    args.source_dir = args.source_dir.expanduser().resolve()
    try:
        report, exit_code = verify(args)
    except validate_tiffs.OperationalError as exc:
        if args.json:
            print(json.dumps({"passed": False, "operational_error": str(exc)}, indent=2))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_human(report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
