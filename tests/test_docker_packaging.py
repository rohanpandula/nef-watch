from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
FINGERPRINT_SCRIPT = REPO / "docker" / "source-fingerprint.sh"
FINGERPRINT_MODULE = REPO / "docker" / "source_fingerprint.py"
BUILD_IMAGE_SCRIPT = REPO / "docker" / "build-image.sh"
HEALTH_STATE_SCRIPT = REPO / "docker" / "check_health_state.py"
STARTUP_TIMEOUT_SCRIPT = REPO / "docker" / "startup_timeout.sh"


class SourceFingerprintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "docker").mkdir()
        (self.root / "tool").mkdir()
        for relative in (
            ".github/workflows/verify_sdk_free_image.py",
            ".dockerignore",
            "requirements.txt",
            "pyproject.toml",
            "tool/nef_watch.py",
            "tool/nef_render_win.cpp",
            "tool/nef_render_wine.sh",
            "tool/nef_wine_sandbox.sh",
            "tool/stage_runtime_acceptance.py",
            "tool/validate_tiffs.py",
            "tool/verify_runtime_acceptance.py",
            "validation/baseline-v1.json",
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
        shutil.copy2(FINGERPRINT_MODULE, self.root / "docker" / FINGERPRINT_MODULE.name)
        first = self.fingerprint()
        (self.root / "docker" / ".env").write_text("PRIVATE=path\n", encoding="utf-8")
        self.assertEqual(self.fingerprint(), first)
        cache = self.root / "docker" / "__pycache__"
        cache.mkdir()
        (cache / "bootstrap.cpython-312.pyc").write_bytes(b"local bytecode")
        self.assertEqual(self.fingerprint(), first)

        verifier = self.root / "tool" / "verify_runtime_acceptance.py"
        verifier.write_text("changed acceptance verifier\n", encoding="utf-8")
        self.assertNotEqual(self.fingerprint(), first)
        verifier.write_text(
            "contents of tool/verify_runtime_acceptance.py\n", encoding="utf-8"
        )
        self.assertEqual(self.fingerprint(), first)

        scanner = self.root / ".github" / "workflows" / "verify_sdk_free_image.py"
        scanner.write_text("changed layer scanner\n", encoding="utf-8")
        self.assertNotEqual(self.fingerprint(), first)
        scanner.write_text(
            "contents of .github/workflows/verify_sdk_free_image.py\n",
            encoding="utf-8",
        )
        self.assertEqual(self.fingerprint(), first)

        (self.root / "tool" / "nef_render_win.cpp").write_text(
            "changed adapter\n", encoding="utf-8"
        )
        self.assertNotEqual(self.fingerprint(), first)
        self.assertRegex(first, r"^[0-9a-f]{64}$")

    def test_build_script_rejects_inputs_that_differ_from_revision_label(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is unavailable")
        for source in (FINGERPRINT_SCRIPT, FINGERPRINT_MODULE, BUILD_IMAGE_SCRIPT):
            shutil.copy2(source, self.root / "docker" / source.name)
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.name", "test"], check=True
        )
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "--quiet", "-m", "fixture"],
            check=True,
        )
        environment = {**os.environ, "VERIFY_SOURCE_ONLY": "1"}
        environment.pop("NEF_WATCH_SOURCE_REVISION", None)
        clean = subprocess.run(
            ["bash", str(self.root / "docker" / "build-image.sh")],
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(clean.returncode, 0, clean.stderr)

        (self.root / "tool" / "validate_tiffs.py").write_text(
            "modified after review\n", encoding="utf-8"
        )
        dirty = subprocess.run(
            ["bash", str(self.root / "docker" / "build-image.sh")],
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(dirty.returncode, 2)
        self.assertIn("modified or untracked image inputs", dirty.stderr)


class PublicImagePackagingTests(unittest.TestCase):
    def test_image_build_is_sdk_free_and_compose_injects_sdk_read_only(self) -> None:
        dockerfile = (REPO / "docker" / "Dockerfile").read_text(encoding="utf-8")
        compose = (REPO / "docker" / "compose.yaml").read_text(encoding="utf-8")
        build_script = (REPO / "docker" / "build-image.sh").read_text(encoding="utf-8")
        entrypoint = (REPO / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
        fingerprint_script = FINGERPRINT_SCRIPT.read_text(encoding="utf-8")
        fingerprint_module = FINGERPRINT_MODULE.read_text(encoding="utf-8")

        self.assertNotIn("--build-context", dockerfile)
        self.assertNotIn("NkImgSDK.dll", dockerfile)
        self.assertNotIn("additional_contexts", compose)
        self.assertNotIn("    build:", compose)
        self.assertIn("target: /nikon-sdk", compose)
        self.assertIn("read_only: true", compose)
        self.assertIn("image: ${IMAGE:?", compose)
        self.assertIn('user: "99:100"', compose)
        runtime_service = compose.split("  nef-watch:\n", 1)[1]
        self.assertNotIn("target: /nikon-sdk", runtime_service)
        self.assertIn("NEF_WATCH_INIT_COMPLETE", runtime_service)
        self.assertIn("cap_add:", compose)
        self.assertIn("- SETPCAP", compose)
        self.assertIn("source: nef-watch-state", compose)
        self.assertIn("target: /var/lib/nef-watch", compose)
        self.assertNotIn("wine-prefix", compose)
        self.assertIn("g++-mingw-w64-x86-64", dockerfile)
        self.assertIn("bootstrap_sdk.py", dockerfile)
        self.assertIn("landlock_exec.py", dockerfile)
        self.assertIn("landlock_probe.py", dockerfile)
        self.assertIn("render_supervisor.py", dockerfile)
        self.assertIn("validate_storage.py", dockerfile)
        self.assertIn("validate_acceptance_marker.py", dockerfile)
        self.assertIn("validate_wine_template.py", dockerfile)
        self.assertIn("source-identity.json", dockerfile)
        self.assertIn("USER root", dockerfile)
        self.assertIn("HOME=/root", dockerfile)
        self.assertIn("NEF_WATCH_APP_HOME=/var/lib/nef-watch/home", dockerfile)
        self.assertIn("NEF_WATCH_RENDER_JOBS=1", dockerfile)
        self.assertIn(
            "https://snapshot.debian.org/archive/debian/20260802T000000Z",
            dockerfile,
        )
        self.assertIn(
            "https://snapshot.debian.org/archive/debian-security/20260802T000000Z",
            dockerfile,
        )
        self.assertIn("Acquire::Check-Valid-Until=false", dockerfile)
        self.assertIn('io.nef-watch.debian.snapshot="20260802T000000Z"', dockerfile)
        for package in (
            "libcurl4=7.88.1-10+deb12u15",
            "libgl1-mesa-dri=22.3.6-1+deb12u2",
            "libglapi-mesa=22.3.6-1+deb12u2",
            "libglx-mesa0=22.3.6-1+deb12u2",
            "libxfont2=1:2.0.6-1+deb12u1",
        ):
            self.assertIn(package, dockerfile)
        self.assertIn('org.opencontainers.image.revision="${NEF_WATCH_SOURCE_REVISION}"', dockerfile)
        self.assertIn("PYTHONNOUSERSITE=1", dockerfile)
        self.assertIn("PYTHONSAFEPATH=1", dockerfile)
        self.assertIn("/usr/local/bin/python3 -I", entrypoint)
        self.assertNotIn('chmod 0600 "$runtime_root/.bootstrap.lock"', entrypoint)
        self.assertNotIn("WINEPREFIX=/var/lib/nef-watch", dockerfile)
        self.assertIn("tool/verify_runtime_acceptance.py", dockerfile)
        self.assertIn("tool/nef_wine_sandbox.sh", dockerfile)
        self.assertIn("tool/validate_tiffs.py", dockerfile)
        self.assertIn("validation/baseline-v1.json", dockerfile)
        self.assertIn("source_fingerprint.py", fingerprint_script)
        self.assertIn(".github/workflows/verify_sdk_free_image.py", fingerprint_module)
        self.assertIn("tool/verify_runtime_acceptance.py", fingerprint_module)
        self.assertIn("tool/stage_runtime_acceptance.py", fingerprint_module)
        self.assertIn("tool/validate_tiffs.py", fingerprint_module)
        self.assertIn("validation/baseline-v1.json", fingerprint_module)
        self.assertNotIn('VOLUME ["/var/lib/nef-watch"]', dockerfile)
        self.assertIn("source-fingerprint.sh", build_script)
        self.assertNotIn("SDK_DIR", build_script)

        workflow = (REPO / ".github" / "workflows" / "container.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('cron: "17 11 * * 1"', workflow)
        self.assertIn("--exit-code 1 --ignore-unfixed --severity HIGH,CRITICAL", workflow)

        example_env = (REPO / "docker" / ".env.example").read_text(encoding="utf-8")
        self.assertNotIn("PUID=", example_env)
        self.assertNotIn("PGID=", example_env)
        self.assertNotIn("SOURCE_FINGERPRINT=", example_env)
        self.assertIn("IMAGE=sha256:REPLACE_WITH_64_HEX_LOCAL_IMAGE_ID", example_env)
        self.assertIn("ACCEPTANCE_REPORT=/mnt/fastest-nvme/", example_env)

    def test_compose_uses_explicit_fast_work_mount_and_safe_defaults(self) -> None:
        dockerfile = (REPO / "docker" / "Dockerfile").read_text(encoding="utf-8")
        compose = (REPO / "docker" / "compose.yaml").read_text(encoding="utf-8")
        example_env = (REPO / "docker" / ".env.example").read_text(
            encoding="utf-8"
        )

        self.assertIn("source: ${NEF_TEMP_DIR:", compose)
        self.assertIn("target: /work/nef-watch", compose)
        self.assertEqual(compose.count("create_host_path: false"), 9)
        self.assertEqual(compose.count("target: /run/nef-watch-acceptance.json"), 2)
        self.assertEqual(compose.count("source: ${ACCEPTANCE_REPORT:?"), 2)
        self.assertIn('NEF_WATCH_REQUIRE_ACCEPTANCE_REPORT: "1"', compose)
        self.assertIn('NEF_WATCH_PRE_ACCEPTANCE_FIXTURE_MODE: "0"', compose)
        self.assertIn("NEF_WATCH_INTERVAL_SECONDS: ${INTERVAL:-3}", compose)
        self.assertIn("- --temp-dir\n      - /work/nef-watch", compose)
        self.assertIn(
            "- --state-dir\n      - /var/lib/nef-watch/app-state", compose
        )
        self.assertIn("TMPDIR: /work/nef-watch", compose)
        self.assertIn("NEF_WATCH_TEMP_DIR: /work/nef-watch", compose)
        self.assertIn("size=64m", compose)
        self.assertNotIn("size=1g", compose)
        self.assertNotIn("${JOBS:-2}", compose)
        self.assertIn('NEF_WATCH_RENDER_JOBS: "1"', compose)
        self.assertIn('- --jobs\n      - "1"', compose)
        self.assertIn("${MEMORY_LIMIT:-4g}", compose)
        self.assertIn("fsize:\n      soft: 2147483648", compose)
        self.assertIn("stop_grace_period: ${STOP_GRACE_PERIOD:-7m}", compose)
        self.assertIn(
            "NEF_TEMP_DIR=/mnt/fastest-nvme/nef-watch-scratch/production",
            example_env,
        )
        self.assertIn("TEMP_CAPACITY_BYTES=17179869184", example_env)
        self.assertIn("TEMP_MIN_FREE_BYTES=8589934592", example_env)
        self.assertIn("JOBS=1", example_env)
        self.assertIn("RENDER_MEMORY_MIB=1536", example_env)
        self.assertIn("RENDER_FILE_SIZE_MIB=2048", example_env)
        self.assertIn("RENDER_TIMEOUT_SECONDS=300", example_env)
        self.assertIn("NEF_WATCH_SETTLE_SECONDS: ${SETTLE_SECONDS:-5}", compose)
        self.assertNotIn("NEF_WATCH_RENDER_JOBS: ${JOBS", compose)
        self.assertIn("NEF_WATCH_RENDER_MEMORY_MIB: ${RENDER_MEMORY_MIB:-1536}", compose)
        self.assertIn("NEF_WATCH_SDK_MEMORY_MIB: ${SDK_MEMORY_MIB:-512}", compose)
        self.assertIn("NEF_WATCH_RENDER_FILE_SIZE_MIB: ${RENDER_FILE_SIZE_MIB:-2048}", compose)
        self.assertIn("NEF_WATCH_TEMP_CAPACITY_BYTES: ${TEMP_CAPACITY_BYTES:-17179869184}", compose)
        self.assertIn("NEF_WATCH_TEMP_MIN_FREE_BYTES: ${TEMP_MIN_FREE_BYTES:-8589934592}", compose)
        self.assertIn("NEF_WATCH_LEGACY_INPUT_ROOT: ${LEGACY_INPUT_ROOT:-}", compose)
        self.assertIn("- --skip-existing", compose)
        self.assertIn("- --max-scan-entries", compose)
        self.assertIn("NEF_WATCH_IMAGE_REFERENCE: ${IMAGE:?", compose)
        self.assertGreater(
            dockerfile.index("ENV TMPDIR=/work/nef-watch"),
            dockerfile.index("pip install"),
        )

    def test_container_health_is_progress_aware_and_probes_writable_mounts(
        self,
    ) -> None:
        dockerfile = (REPO / "docker" / "Dockerfile").read_text(encoding="utf-8")
        compose = (REPO / "docker" / "compose.yaml").read_text(encoding="utf-8")
        healthcheck = (REPO / "docker" / "healthcheck.sh").read_text(
            encoding="utf-8"
        )
        entrypoint = (REPO / "docker" / "entrypoint.sh").read_text(
            encoding="utf-8"
        )
        wine_sandbox = (REPO / "tool" / "nef_wine_sandbox.sh").read_text(
            encoding="utf-8"
        )
        landlock_launcher = (REPO / "docker" / "landlock_exec.py").read_text(
            encoding="utf-8"
        )
        docs = (REPO / "docs" / "DOCKER.md").read_text(encoding="utf-8")

        self.assertIn("nef-watch-check-health-state.py", dockerfile)
        self.assertIn(
            'ENTRYPOINT ["/usr/bin/tini", "--", '
            '"/usr/local/bin/nef-watch-entrypoint"]',
            dockerfile,
        )
        self.assertIn("  nef-watch-init:", compose)
        self.assertIn("      - -g", compose)
        self.assertNotIn('/usr/bin/tini -- "$0" "$@"', entrypoint)
        self.assertIn("--timeout=15s --start-period=15m", dockerfile)
        self.assertIn("Every render gets authenticated Wine and Xvfb", entrypoint)
        self.assertIn('-auth "$XAUTHORITY"', wine_sandbox)
        self.assertIn('-nolisten tcp -nolock', wine_sandbox)
        self.assertIn("IMAGE must be an exact local image ID", entrypoint)
        self.assertIn(
            'production_acceptance="${NEF_WATCH_REQUIRE_ACCEPTANCE_REPORT:-1}"',
            entrypoint,
        )
        self.assertIn("NEF_WATCH_PRE_ACCEPTANCE_FIXTURE_MODE", entrypoint)
        self.assertIn("is_pre_acceptance_fixture_command", entrypoint)
        self.assertIn("Landlock ABI 6/seccomp startup probe failed", entrypoint)
        timeout_wrapper = STARTUP_TIMEOUT_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("timeout --signal=TERM", timeout_wrapper)
        self.assertIn('--kill-after="${kill_grace_seconds}s"', timeout_wrapper)
        self.assertIn('"$NEF_WATCH_WINEBOOT_TIMEOUT"', entrypoint)
        self.assertIn('"$NEF_WATCH_VC_REDIST_TIMEOUT"', entrypoint)
        self.assertIn('"$NEF_WATCH_WINESERVER_TIMEOUT"', entrypoint)
        self.assertIn('"$NEF_WATCH_SDK_BOOTSTRAP_TIMEOUT"', entrypoint)
        self.assertIn('flock -w "$NEF_WATCH_INIT_LOCK_TIMEOUT"', entrypoint)
        runtime_tail = entrypoint.split("# Every render gets", 1)[1]
        self.assertIn("exec /usr/local/bin/python3 -I /app/tool/nef_watch.py", runtime_tail)
        self.assertNotIn("Xvfb", runtime_tail.split("exec ", 1)[1])
        self.assertIn("NEF_WATCH_HEALTH_FILE", healthcheck)
        self.assertIn("NEF_WATCH_HEALTH_MAX_AGE", healthcheck)
        self.assertIn("mktemp", healthcheck)
        self.assertIn("NEF_WATCH_HEALTH_OUTPUT", healthcheck)
        self.assertIn("NEF_WATCH_HEALTH_TEMP", healthcheck)
        self.assertIn("Landlock ABI 6/seccomp runtime probe failed", healthcheck)
        self.assertIn("--acceptance-report /run/nef-watch-acceptance.json", healthcheck)
        self.assertNotIn("xdpyinfo", healthcheck)
        self.assertNotIn("/proc/[0-9]*/cmdline", healthcheck)
        self.assertNotIn("has_process_argument", healthcheck)
        health_state_script = HEALTH_STATE_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("watcher_start_ticks", health_state_script)
        self.assertIn("watcher_pid", health_state_script)
        self.assertIn("--bounding-set=-all --no-new-privs", healthcheck)
        self.assertIn("nef-watch-landlock-exec.py", dockerfile)
        self.assertIn("nef-watch-landlock-probe.py", dockerfile)
        self.assertIn("nef-watch-render-supervisor.py", dockerfile)
        self.assertIn("nef-watch-validate-storage.py", dockerfile)
        self.assertIn("nef-watch-validate-storage.py --container", entrypoint)
        self.assertIn("nef-watch-validate-acceptance.py", entrypoint)
        self.assertIn("nef-watch-validate-acceptance.py", healthcheck)
        self.assertIn("nef-watch-validate-storage.py --container --runtime", entrypoint)
        self.assertIn("nef-watch-validate-storage.py --container --runtime", healthcheck)
        for token in ("AUDIT_ARCH_X86_64", "X32_SYSCALL_BIT", "SYS_IO_URING_SETUP"):
            self.assertIn(token, landlock_launcher)
        for phrase in ("seccomp-BPF", "io_uring", "metadata-mutation"):
            self.assertIn(phrase, docs)
        self.assertIn("nef-watch-validate-wine-template.py", healthcheck)
        self.assertIn("! -type l -writable", healthcheck)
        self.assertIn("--resolve-active-runtime", entrypoint)
        self.assertIn("validate_single_job_arguments", entrypoint)
        self.assertIn("Program Files/Common Files/Nikon/Profiles", entrypoint)
        self.assertIn('"$NIKON_RUNTIME_DIR/Profiles/."', entrypoint)

    def test_raw_extensions_are_ignored_in_every_case(self) -> None:
        for ignore_file in (REPO / ".gitignore", REPO / ".dockerignore"):
            contents = ignore_file.read_text(encoding="utf-8")
            self.assertIn("*.[Nn][Ee][Ff]", contents)
            self.assertIn("*.[Nn][Rr][Ww]", contents)

    def test_acceptance_uses_audited_local_image_and_atomic_report(self) -> None:
        docs = (REPO / "docs" / "DOCKER.md").read_text(encoding="utf-8")

        self.assertIn("Never let the candidate image make the final acceptance decision", docs)
        self.assertIn("tool/verify_runtime_acceptance.py", docs)
        self.assertIn("tool/stage_runtime_acceptance.py", docs)
        self.assertIn("/app/tool/verify_runtime_acceptance.py --emit-tiff-inspection", docs)
        self.assertIn("--tiff-inspection-report", docs)
        self.assertIn("--mount type=bind,src=\"$work_root/output\",dst=/output,readonly", docs)
        self.assertNotIn("gh attestation verify", docs)
        self.assertIn("--trusted-local-image", docs)
        self.assertIn("--expected-source-fingerprint", docs)
        self.assertIn("--docker-image-id", docs)
        self.assertIn("--docker-source-fingerprint", docs)
        self.assertIn("--sdk-dir", docs)
        self.assertIn("--docker-inspect", docs)
        self.assertIn("--expected-revision", docs)
        self.assertIn('src="$work_root/state",dst=/var/lib/nef-watch', docs)
        self.assertIn("NEF_WATCH_TEMP_CAPACITY_BYTES=17179869184", docs)
        self.assertIn("NEF_WATCH_TEMP_MIN_FREE_BYTES=8589934592", docs)
        self.assertIn("findmnt -T", docs)
        self.assertIn("env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin", docs)
        self.assertIn("assert_trusted_path", docs)
        self.assertIn('TRUST_ROOT="$SCRATCH_FS/.trust"', docs)
        self.assertIn('/usr/bin/python3.12 -I - "$SCRATCH_FS" "$1"', docs)
        self.assertNotIn('. "$TRUST_ROOT/build-state.env"', docs)
        self.assertIn("unexpected exact-build state field", docs)
        self.assertNotIn('chown 0:0 "$NVME_POOL"', docs)
        self.assertNotIn('chmod 0755 "$NVME_POOL"', docs)
        self.assertIn("docker/accepted_compose.py", docs)
        self.assertIn('--trust-root "$TRUST_ROOT" --verify-only', docs)
        self.assertIn("wrapper repeats the complete gate before every", docs)
        self.assertIn("current `DSC_0722` Linux pixel mismatch", docs)
        self.assertIn("do not rebaseline it", docs)
        self.assertNotIn("safe_compose run", docs)
        self.assertIn("wrapper rejects `run`, `start`, `restart`, `create`, `unpause`", docs)
        self.assertIn("`up` accepts only optional `-d`/`--detach`", docs)
        self.assertIn("2–16 GiB, 1–16 CPUs, and 128–1024 PIDs", docs)
        self.assertIn("rehashes all 33 production SDK files", docs)
        self.assertIn(
            "revalidates the report's six\nsource-NEF hash records", docs
        )
        self.assertIn("mode-0444", docs)
        self.assertIn("raw Compose or Unraid-UI start fail closed", docs)
        self.assertIn("acceptance container survived cleanup", docs)
        self.assertIn("/usr/bin/python3.12 -I", docs)
        self.assertIn("--log-driver none", docs)
        self.assertIn("prlimit --as=", docs)
        self.assertIn("report_tmp=\"$(mktemp", docs)
        self.assertIn("os.link(temporary, final)", docs)
        self.assertIn("os.fsync(directory_fd)", docs)
        self.assertIn("--initialize-only", docs)
        self.assertIn("--user 99:100", docs)
        self.assertIn("unix:///var/run/docker.sock", docs)
        self.assertIn("Every account, container, plug-in, or management service", docs)
        self.assertNotIn("src=/var/run/docker.sock", docs)
        self.assertNotIn("self-hosted", docs)
        self.assertIn('--config "$work_root/trivy-config.yaml" image', docs)
        self.assertIn('--ignorefile "$work_root/trivy-ignore"', docs)
        self.assertIn('--module-dir "$work_root/trivy-modules"', docs)
        self.assertIn('XDG_DATA_HOME="$work_root/trivy-xdg-data"', docs)
        self.assertIn('TRIVY_BIN="$(/usr/bin/realpath /usr/bin/trivy)"', docs)
        self.assertIn('test "$(/usr/bin/stat -c %u "$TRIVY_BIN")" = 0', docs)
        self.assertIn("--image-src docker --scanners vuln --pkg-types os,library", docs)
        self.assertIn("--scanners vuln --pkg-types os,library", docs)

    def test_public_workflow_is_secretless_validation_only(self) -> None:
        build = (REPO / ".github" / "workflows" / "container.yml").read_text(
            encoding="utf-8"
        )

        self.assertEqual(build.count("aquasecurity/setup-trivy@"), 1)
        self.assertNotIn("aquasecurity/trivy-action@", build)
        self.assertIn("SDK-free container validation", build)
        self.assertIn("Secretless unit and script validation", build)
        self.assertIn("Isolated SDK-free build and exact-image scan", build)
        self.assertIn('IMAGE_ID: ${{ steps.image.outputs.image_id }}', build)
        self.assertIn(
            "NEF_WATCH_SOURCE_REVISION=${{ github.sha }}", build
        )
        self.assertIn("Install pinned Trivy scanner without repository configuration", build)
        self.assertIn("trivy.yaml, .trivyignore, ambient TRIVY_* variable", build)
        self.assertIn("path: ${{ runner.temp }}/nef-watch-trivy-install", build)
        self.assertIn('trivy_bin="$RUNNER_TEMP/nef-watch-trivy-install/trivy-bin/trivy"', build)
        self.assertIn('test "$(/usr/bin/realpath "$trivy_bin")" = "$trivy_bin"', build)
        self.assertIn("refusing repository-provided Trivy executable", build)
        self.assertIn("/usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin", build)
        self.assertIn('--config "$policy_root/config.yaml" image', build)
        self.assertIn('--ignorefile "$policy_root/ignore"', build)
        self.assertIn('--module-dir "$policy_root/modules"', build)
        self.assertIn("--image-src docker --scanners vuln --pkg-types os,library", build)
        self.assertIn("cache: false", build)
        self.assertIn('"$IMAGE_ID" --help', build)
        self.assertIn("version: v0.34.1", build)
        self.assertIn("image=moby/buildkit@sha256:", build)
        self.assertNotIn("docker/login-action", build)
        self.assertNotIn("docker push", build)
        self.assertNotIn("actions/attest", build)
        self.assertNotIn("attest-build-provenance", build)
        self.assertNotIn("packages: write", build)
        self.assertNotIn("id-token: write", build)
        self.assertFalse(
            (REPO / ".github" / "workflows" / "promote-container.yml").exists()
        )
        self.assertNotIn("self-hosted", build)

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
            entrypoint = root / "entrypoint.sh"
            entrypoint.write_text(
                (REPO / "docker" / "entrypoint.sh")
                .read_text(encoding="utf-8")
                .replace("/usr/local/bin/python3", str(fake_python)),
                encoding="utf-8",
            )
            entrypoint.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{root}:/usr/bin:/bin",
                "HELP_INVOCATION": str(invocation),
                "NIKON_SDK_DIR": str(root / "does-not-exist"),
            }

            subprocess.run(
                [str(entrypoint), "--help"],
                check=True,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(
                invocation.read_text(encoding="utf-8").strip(),
                "-I /app/tool/nef_watch.py --help",
            )

    def test_compose_entrypoint_rejects_a_mutable_image_reference(self) -> None:
        result = subprocess.run(
            [
                str(REPO / "docker" / "entrypoint.sh"),
                "/input",
                "--out",
                "/output",
            ],
            env={
                **os.environ,
                "NEF_WATCH_IMAGE_REFERENCE": (
                    "ghcr.io/rohanpandula/nef-watch:linux-amd64"
                ),
            },
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 78)
        self.assertIn("exact local image ID", result.stderr)

        result = subprocess.run(
            [str(REPO / "docker" / "entrypoint.sh"), "/input", "--out", "/output"],
            env={
                **os.environ,
                "NEF_WATCH_IMAGE_REFERENCE": "sha256:" + "a" * 64,
                "NEF_WATCH_UID": "98",
            },
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 78)
        self.assertIn("requires NEF_WATCH_UID=99", result.stderr)
        self.assertNotIn("exact local image ID", result.stderr)

        env = {**os.environ}
        env.pop("NEF_WATCH_IMAGE_REFERENCE", None)
        result = subprocess.run(
            [
                str(REPO / "docker" / "entrypoint.sh"),
                "/input",
                "--out",
                "/output",
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 78)
        self.assertIn("<unset>", result.stderr)

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


class HealthStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.health_file = self.root / "health.json"
        self.proc_root = self.root / "proc"
        self.watcher_pid = os.getpid()
        self.watcher_start_ticks = 987654321
        process = self.proc_root / str(self.watcher_pid)
        process.mkdir(parents=True)
        fields_3_through_22 = [
            "S",
            *("0" for _ in range(18)),
            str(self.watcher_start_ticks),
        ]
        (process / "stat").write_text(
            f"{self.watcher_pid} (nef watch ) helper) "
            + " ".join(fields_3_through_22)
            + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_health(self, **overrides: object) -> None:
        now = time.time()
        payload: dict[str, object] = {
            "schema": 2,
            "watcher_pid": self.watcher_pid,
            "watcher_start_ticks": self.watcher_start_ticks,
            "updated_at": now,
            "last_scan_at": now,
            "last_success_at": None,
            "pending": 0,
            "inflight": 0,
            "permanent_failures": 0,
            "status": "healthy",
        }
        payload.update(overrides)
        self.health_file.write_text(json.dumps(payload), encoding="utf-8")
        self.health_file.chmod(0o600)

    def check(
        self, max_age: int = 120, *, interval: str = "3"
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["NEF_WATCH_INTERVAL_SECONDS"] = interval
        return subprocess.run(
            [
                sys.executable,
                str(HEALTH_STATE_SCRIPT),
                str(self.health_file),
                str(max_age),
                str(self.proc_root),
            ],
            capture_output=True,
            text=True,
            env=environment,
        )

    def test_accepts_fresh_healthy_scan(self) -> None:
        self.write_health()
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_stale_heartbeat_or_scan(self) -> None:
        stale = time.time() - 121
        self.write_health(updated_at=stale, last_scan_at=stale)
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("stale", result.stderr)

        self.write_health(last_scan_at=stale)
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("last scan is stale", result.stderr)

    def test_rejects_unbounded_health_age_and_interval_mismatch(self) -> None:
        self.write_health(
            updated_at=time.time() - 86400,
            last_scan_at=time.time() - 86400,
        )
        result = self.check(max_age=999_999_999)
        self.assertEqual(result.returncode, 1)
        self.assertIn("between 60 and 900", result.stderr)

        self.write_health()
        result = self.check(max_age=120, interval="61")
        self.assertEqual(result.returncode, 1)
        self.assertIn("watch interval", result.stderr)

    def test_rejects_degraded_state_and_permanent_failures(self) -> None:
        self.write_health(status="degraded", permanent_failures=1)
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("status is degraded", result.stderr)

        self.write_health(permanent_failures=2)
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("2 conversion(s) need attention", result.stderr)

    def test_accepts_only_a_short_lived_starting_state(self) -> None:
        self.write_health(status="starting", last_scan_at=None)
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)

        self.write_health(
            status="starting", updated_at=time.time() - 121, last_scan_at=None
        )
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("stale", result.stderr)

    def test_rejects_malformed_schema_and_counters(self) -> None:
        self.write_health(schema=1)
        self.assertEqual(self.check().returncode, 1)

        self.write_health(schema=True)
        self.assertEqual(self.check().returncode, 1)

        self.write_health(inflight=-1)
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("inflight", result.stderr)

    def test_rejects_dead_reused_or_unreadable_watcher_identity(self) -> None:
        self.write_health(watcher_start_ticks=self.watcher_start_ticks + 1)
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("start time", result.stderr)

        self.write_health(watcher_pid=999_999_999)
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("not running", result.stderr)

        self.write_health()
        (self.proc_root / str(self.watcher_pid) / "stat").unlink()
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("process stat", result.stderr)

    def test_rejects_mutable_or_linked_heartbeat_evidence(self) -> None:
        self.write_health()
        self.health_file.chmod(0o644)
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("mode-0600", result.stderr)

        self.health_file.chmod(0o600)
        alias = self.root / "health-alias.json"
        os.link(self.health_file, alias)
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("single-link", result.stderr)


class StartupTimeoutTests(unittest.TestCase):
    def test_terminates_a_stuck_startup_command(self) -> None:
        started = time.monotonic()
        result = subprocess.run(
            ["bash", str(STARTUP_TIMEOUT_SCRIPT), "1", "1", "sleep", "30"],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertLess(time.monotonic() - started, 5)

    def test_rejects_unbounded_or_invalid_durations(self) -> None:
        for value in ("0", "3601", "forever"):
            result = subprocess.run(
                ["bash", str(STARTUP_TIMEOUT_SCRIPT), value, "1", "true"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 78)
            self.assertIn("TIMEOUT_SECONDS", result.stderr)


if __name__ == "__main__":
    unittest.main()
