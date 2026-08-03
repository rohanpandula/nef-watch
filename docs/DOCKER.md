# Linux and Unraid deployment

`nef-watch` runs Nikon's unmodified x64 Windows Image SDK under Wine. Nikon
does not support that environment, so the safe deployment identity is an exact
image built and accepted on your server—not a moving tag.

Public CI is deliberately validation-only. It has no registry login, package
write permission, deployment artifact, or production tag. Build from the exact
commit you reviewed, pass the private six-image gate, and put the resulting
local Docker config ID (`sha256:<64 hex>`) in Compose.

## Requirements

- x86-64 Docker with Compose v2.
- Nikon NEF/NRW Image SDK 1.46.0, with `Image SDK/Library/win` available on the
  server. Nikon files never enter Git or the image.
- Landlock ABI 6 or newer. Signal isolation was added in ABI 6, so the renderer
  fails closed on older kernels. Current Unraid 7.2/7.3 kernels are new enough,
  but the acceptance probe is authoritative because Landlock must also be
  enabled in the running kernel and allowed by Docker. Production startup and
  every health probe repeat the ABI 6 check, so a kernel upgrade, restore, or
  server migration cannot rely on stale acceptance evidence.
- A dedicated capacity-bounded filesystem or dataset on the fastest NVMe pool.
  An ordinary folder on a large pool is intentionally rejected.
- The six private NEFs named by `validation/baseline-v1.json` for first-time or
  updated-image acceptance.

Unraid supports multiple NVMe-backed pools specifically to isolate and optimize
workloads; use the direct pool path `/mnt/<pool>/...`, never the merged
`/mnt/user/...` or `/mnt/user0/...` view, for scratch. The path must be absolute
and canonical, with no symlink component. See the official [cache-pool guide](https://docs.unraid.net/unraid-os/using-unraid-to/manage-storage/cache-pools/).

## 1. Prepare bounded NVMe scratch storage

Choose the fastest NVMe pool that is not needed for the protected photo
archive. Use a 16 GiB child filesystem/dataset with at least 8 GiB free. For a
ZFS pool, a child dataset with both a 16 GiB quota and refquota is a convenient
choice. A dedicated 16 GiB filesystem on an NVMe pool also works. The exact
mechanism depends on whether the pool is ZFS, Btrfs, or XFS; Unraid documents
the available pool filesystems in its [storage guide](https://docs.unraid.net/unraid-os/using-unraid-to/manage-storage/file-systems/).

Set `SCRATCH_FS` to the mountpoint of that bounded filesystem—not merely a
subdirectory on the full pool—and verify what applications will see:

```bash
NVME_POOL=/mnt/fastest-nvme
SCRATCH_FS="$NVME_POOL/nef-watch-scratch"
/usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root \
  LC_ALL=C LANG=C SCRATCH_FS="$SCRATCH_FS" \
  /bin/bash --noprofile --norc <<'PREPARE_SCRATCH'
set -euo pipefail
umask 077
test "$(id -u)" = 0

test -d "$SCRATCH_FS"
test ! -L "$SCRATCH_FS"
test "$(findmnt -T "$SCRATCH_FS" -n -o TARGET)" = "$(realpath "$SCRATCH_FS")"
chown 0:0 "$SCRATCH_FS"
chmod 0755 "$SCRATCH_FS"
test -z "$(find -P "$SCRATCH_FS" -maxdepth 0 \
  \( ! -user root -o -perm /022 -o -type l \) -print -quit)"
/usr/bin/python3.12 -I - "$SCRATCH_FS" <<'PY'
import os, pathlib, sys

root = pathlib.Path(sys.argv[1]).resolve(strict=True)
st = os.statvfs(root)
total = st.f_blocks * st.f_frsize
free = st.f_bavail * st.f_frsize
assert total <= 16 * 1024**3, f"scratch filesystem is too large: {total} bytes"
assert free >= 8 * 1024**3, f"scratch filesystem has only {free} bytes free"
print(f"bounded scratch: PASS ({total / 1024**3:.1f} GiB total, {free / 1024**3:.1f} GiB free)")
PY

install -d -o 99 -g 100 -m 0700 "$SCRATCH_FS/production"
install -d -o 0 -g 0 -m 0700 "$SCRATCH_FS/acceptance"
PREPARE_SCRATCH
```

Startup independently repeats the byte ceiling and free-space checks using
`statvfs`. This makes a wrong `/mnt/cache/appdata/...`-style directory fail
closed instead of allowing a compromised renderer to fill the whole NVMe pool.
The production wrapper also requires the temp directory and retained report to
remain beneath this exact mounted scratch root on the same backing filesystem;
the container repeats their backing-identity comparison after automatic
restarts.
Each Wine process also has a 2 GiB per-file limit; the host filesystem ceiling
is what bounds aggregate many-file writes.

## 2. Build an exact reviewed local image

Do this before exposing the SDK or any private NEF. Materialize exact Git blobs
inside a persistent root-only trust root so index flags, a dirty working tree,
or an adjacent account cannot replace the harness or its reports. The trust
root lives inside the dedicated scratch mount; only its mountpoint and children
form this trust path, so no command changes permissions on the parent Unraid
pool. Replace the commit and expected fingerprint with values recorded during
review.

```bash
SCRATCH_FS=/mnt/fastest-nvme/nef-watch-scratch
TRUST_ROOT="$SCRATCH_FS/.trust"
SOURCE_REPO="$TRUST_ROOT/source-repository"
REVIEWED_COMMIT=REPLACE_WITH_40_HEX_COMMIT
EXPECTED_SOURCE_FINGERPRINT=REPLACE_WITH_64_HEX_FINGERPRINT

/usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root \
  LC_ALL=C LANG=C SCRATCH_FS="$SCRATCH_FS" TRUST_ROOT="$TRUST_ROOT" \
  SOURCE_REPO="$SOURCE_REPO" \
  REVIEWED_COMMIT="$REVIEWED_COMMIT" \
  EXPECTED_SOURCE_FINGERPRINT="$EXPECTED_SOURCE_FINGERPRINT" \
  /bin/bash --noprofile --norc <<'BUILD_IMAGE'
set -euo pipefail
umask 077
test "$(id -u)" = 0
[[ "$REVIEWED_COMMIT" =~ ^[0-9a-f]{40}$ ]]
[[ "$EXPECTED_SOURCE_FINGERPRINT" =~ ^[0-9a-f]{64}$ ]]

assert_trusted_path() {
  /usr/bin/python3.12 -I - "$SCRATCH_FS" "$1" <<'PY'
import os, pathlib, stat, sys

anchor = pathlib.Path(sys.argv[1])
path = pathlib.Path(sys.argv[2])
assert anchor.is_absolute(), anchor
assert anchor == anchor.resolve(strict=True), anchor
anchor_entry = os.lstat(anchor)
assert stat.S_ISDIR(anchor_entry.st_mode), anchor
assert not stat.S_ISLNK(anchor_entry.st_mode), anchor
assert anchor_entry.st_uid == 0, anchor
assert anchor_entry.st_mode & 0o022 == 0, anchor
assert path.is_absolute(), path
assert path == path.resolve(strict=True), path
relative = path.relative_to(anchor)
current = anchor
for part in relative.parts:
    current /= part
    entry = os.lstat(current)
    assert stat.S_ISDIR(entry.st_mode), current
    assert not stat.S_ISLNK(entry.st_mode), current
    assert entry.st_uid == 0, current
    assert entry.st_mode & 0o022 == 0, current
    assert entry.st_dev == anchor_entry.st_dev, current
PY
}
test "$(findmnt -T "$SCRATCH_FS" -n -o TARGET)" = "$(realpath "$SCRATCH_FS")"
assert_trusted_path "$SCRATCH_FS"
install -d -o 0 -g 0 -m 0700 "$TRUST_ROOT"
assert_trusted_path "$TRUST_ROOT"
if [[ ! -e "$SOURCE_REPO" ]]; then
  env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root LC_ALL=C \
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    /usr/bin/git clone --no-checkout \
      https://github.com/rohanpandula/nef-watch.git "$SOURCE_REPO"
fi
chown -R 0:0 "$SOURCE_REPO"
find -P "$SOURCE_REPO" -type d -exec chmod 0700 {} +
find -P "$SOURCE_REPO" -type f -exec chmod 0600 {} +
assert_trusted_path "$SOURCE_REPO"
test -z "$(find -P "$SOURCE_REPO" -xdev \( ! -user root -o -perm /022 -o -type l \) -print -quit)"

REVIEWED_TREE="$TRUST_ROOT/reviewed/$REVIEWED_COMMIT"
ACCEPT_REPORTS="$TRUST_ROOT/acceptance-reports"
DOCKER_CONFIG="$TRUST_ROOT/docker-config"
export DOCKER_CONFIG
test ! -e "$REVIEWED_TREE"
install -d -o 0 -g 0 -m 0700 "$REVIEWED_TREE" "$ACCEPT_REPORTS" "$DOCKER_CONFIG"
assert_trusted_path "$REVIEWED_TREE"
assert_trusted_path "$ACCEPT_REPORTS"
assert_trusted_path "$DOCKER_CONFIG"

test "$(/usr/bin/docker context show)" = default
test "$(/usr/bin/docker context inspect default --format '{{(index .Endpoints "docker").Host}}')" = unix:///var/run/docker.sock
test -S /var/run/docker.sock
test ! -L /var/run/docker.sock
test "$(stat -c %u /var/run/docker.sock)" = 0

trusted_git() {
  env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    HOME=/root LC_ALL=C GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    GIT_NO_REPLACE_OBJECTS=1 \
    /usr/bin/git -C "$SOURCE_REPO" "$@"
}
trusted_git cat-file -e "$REVIEWED_COMMIT^{commit}"
trusted_git archive --format=tar "$REVIEWED_COMMIT" \
  | env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
      /usr/bin/tar -x -C "$REVIEWED_TREE" --no-same-owner --no-same-permissions
test -z "$(find -P "$REVIEWED_TREE" -type l -print -quit)"
chown -R 0:0 "$REVIEWED_TREE"
find -P "$REVIEWED_TREE" -type d -exec chmod 0555 {} +
find -P "$REVIEWED_TREE" -type f -exec chmod 0444 {} +

actual_fingerprint="$(/usr/bin/python3.12 -I "$REVIEWED_TREE/docker/source_fingerprint.py" \
  --root "$REVIEWED_TREE")"
test "$actual_fingerprint" = "$EXPECTED_SOURCE_FINGERPRINT"
test "$(/usr/bin/python3.12 -I "$REVIEWED_TREE/docker/source_fingerprint.py" \
  --repo "$SOURCE_REPO" --git-commit "$REVIEWED_COMMIT")" = "$actual_fingerprint"

image_tag="nef-watch:reviewed-${REVIEWED_COMMIT:0:12}"
/usr/bin/docker build --pull --platform linux/amd64 \
  --build-arg NEF_WATCH_UID=99 \
  --build-arg NEF_WATCH_GID=100 \
  --build-arg "NEF_WATCH_SOURCE_FINGERPRINT=$actual_fingerprint" \
  --build-arg "NEF_WATCH_SOURCE_REVISION=$REVIEWED_COMMIT" \
  --label "io.nef-watch.source.fingerprint=$actual_fingerprint" \
  --label "org.opencontainers.image.revision=$REVIEWED_COMMIT" \
  --file "$REVIEWED_TREE/docker/Dockerfile" \
  --tag "$image_tag" "$REVIEWED_TREE"

IMAGE_ID="$(/usr/bin/docker image inspect "$image_tag" --format '{{.Id}}')"
[[ "$IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]
test "$(/usr/bin/docker image inspect "$IMAGE_ID" --format '{{.Os}}/{{.Architecture}}')" = linux/amd64
test "$(/usr/bin/docker image inspect "$IMAGE_ID" --format '{{index .Config.Labels "io.nef-watch.source.fingerprint"}}')" = "$actual_fingerprint"
test "$(/usr/bin/docker image inspect "$IMAGE_ID" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = "$REVIEWED_COMMIT"
test "$(/usr/bin/docker image inspect "$IMAGE_ID" --format '{{index .Config.Labels "io.nef-watch.nikon-sdk.bundled"}}')" = false
test "$(/usr/bin/docker image inspect "$IMAGE_ID" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -cx 'NEF_WATCH_RENDER_JOBS=1')" = 1

state_tmp="$(mktemp "$TRUST_ROOT/.build-state-XXXXXXXX")"
printf 'IMAGE_ID=%s\nREVIEWED_COMMIT=%s\nEXPECTED_SOURCE_FINGERPRINT=%s\nREVIEWED_TREE=%s\n' \
  "$IMAGE_ID" "$REVIEWED_COMMIT" "$actual_fingerprint" "$REVIEWED_TREE" > "$state_tmp"
chmod 0400 "$state_tmp"
mv "$state_tmp" "$TRUST_ROOT/build-state.env"
printf 'exact local image: %s\n' "$IMAGE_ID"
BUILD_IMAGE
```

Every account, container, plug-in, or management service that can write to the
Docker daemon socket is part of this deployment's trusted root boundary: Docker
API access can replace images, alter containers, and mount the private SDK or
NEFs regardless of filesystem permissions. Do not put untrusted users in a
Docker-capable group or mount `/var/run/docker.sock` into an untrusted
container. The production wrapper rechecks that the selected context is the
root-owned local Unix socket, but Unix mode bits cannot make an already
authorized Docker principal unprivileged.

The build has network access only for pinned Debian/Microsoft inputs. Its
context excludes Nikon files, raw photos, TIFFs, DNGs, local environment files,
and Git metadata.

`docker/build-image.sh` also refuses a checkout whose image-input fingerprint
differs from its revision label. The root-owned Git archive above remains the
deployment path because it also removes unrelated dirty/untracked context.

## 3. Exact-image acceptance gate

Never let the candidate image make the final acceptance decision. The commands
below use the sealed reviewed tree as the host-side verifier, scan the exact image before private
mounts exist, stage only the six manifested NEFs and 33 manifested SDK files,
then initialize and render with no network or Docker socket. TIFF decoding runs
later in a separate unprivileged container that sees only the read-only TIFFs
and one size-limited report file—never the private NEFs, Nikon SDK, state, host
root, or Docker socket. Its report is bound to the complete TIFF hashes, and
the root-owned host verifier alone compares those measurements to the baseline.

Install Docker/Compose, Python 3.12, `findmnt`, `prlimit`, `timeout`, and Trivy
0.72.0 at `/usr/bin/trivy` on Unraid first. Run the block as root. Production must be stopped while
acceptance shares the bounded scratch filesystem.

```bash
SCRATCH_FS=/mnt/fastest-nvme/nef-watch-scratch
TRUST_ROOT="$SCRATCH_FS/.trust"
PRIVATE_SOURCES=/mnt/user/Media/.nef-watch-private/baseline-v1-sources
PRIVATE_SDK='/mnt/user/Media/.nef-watch-build/nikon-sdk'

/usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root \
  LC_ALL=C LANG=C TRUST_ROOT="$TRUST_ROOT" SCRATCH_FS="$SCRATCH_FS" \
  PRIVATE_SOURCES="$PRIVATE_SOURCES" PRIVATE_SDK="$PRIVATE_SDK" \
  /bin/bash --noprofile --norc <<'ACCEPT_IMAGE'
set -euo pipefail
umask 077

: "${SCRATCH_FS:?complete step 1}"

test "$(id -u)" = 0
assert_trusted_path() {
  /usr/bin/python3.12 -I - "$SCRATCH_FS" "$1" <<'PY'
import os, pathlib, stat, sys

anchor = pathlib.Path(sys.argv[1])
path = pathlib.Path(sys.argv[2])
assert anchor.is_absolute(), anchor
assert anchor == anchor.resolve(strict=True), anchor
anchor_entry = os.lstat(anchor)
assert stat.S_ISDIR(anchor_entry.st_mode), anchor
assert not stat.S_ISLNK(anchor_entry.st_mode), anchor
assert anchor_entry.st_uid == 0, anchor
assert anchor_entry.st_mode & 0o022 == 0, anchor
assert path.is_absolute(), path
assert path == path.resolve(strict=True), path
relative = path.relative_to(anchor)
current = anchor
for part in relative.parts:
    current /= part
    entry = os.lstat(current)
    assert stat.S_ISDIR(entry.st_mode), current
    assert not stat.S_ISLNK(entry.st_mode), current
    assert entry.st_uid == 0, current
    assert entry.st_mode & 0o022 == 0, current
    assert entry.st_dev == anchor_entry.st_dev, current
PY
}
test "$(findmnt -T "$SCRATCH_FS" -n -o TARGET)" = "$(realpath "$SCRATCH_FS")"
assert_trusted_path "$SCRATCH_FS"
assert_trusted_path "$TRUST_ROOT"
BUILD_STATE="$TRUST_ROOT/build-state.env"
test -f "$BUILD_STATE"
test ! -L "$BUILD_STATE"
test "$(stat -c %u "$BUILD_STATE")" = 0
test "$(stat -c %a "$BUILD_STATE")" = 400
test "$(stat -c %h "$BUILD_STATE")" = 1

IMAGE_ID=
REVIEWED_COMMIT=
EXPECTED_SOURCE_FINGERPRINT=
REVIEWED_TREE=
seen_image=0
seen_commit=0
seen_fingerprint=0
seen_tree=0
while IFS='=' read -r key value; do
  case "$key" in
    IMAGE_ID) test "$seen_image" = 0; IMAGE_ID="$value"; seen_image=1 ;;
    REVIEWED_COMMIT) test "$seen_commit" = 0; REVIEWED_COMMIT="$value"; seen_commit=1 ;;
    EXPECTED_SOURCE_FINGERPRINT)
      test "$seen_fingerprint" = 0
      EXPECTED_SOURCE_FINGERPRINT="$value"
      seen_fingerprint=1
      ;;
    REVIEWED_TREE) test "$seen_tree" = 0; REVIEWED_TREE="$value"; seen_tree=1 ;;
    *) echo "unexpected exact-build state field: $key" >&2; exit 78 ;;
  esac
done < "$BUILD_STATE"
test "$seen_image$seen_commit$seen_fingerprint$seen_tree" = 1111
[[ "$IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$REVIEWED_COMMIT" =~ ^[0-9a-f]{40}$ ]]
[[ "$EXPECTED_SOURCE_FINGERPRINT" =~ ^[0-9a-f]{64}$ ]]
test "$REVIEWED_TREE" = "$TRUST_ROOT/reviewed/$REVIEWED_COMMIT"

ACCEPT_REPORTS="$TRUST_ROOT/acceptance-reports"
DOCKER_CONFIG="$TRUST_ROOT/docker-config"
export DOCKER_CONFIG
for root in "$TRUST_ROOT" "$REVIEWED_TREE" "$ACCEPT_REPORTS" \
            "$DOCKER_CONFIG" "$SCRATCH_FS" "$SCRATCH_FS/acceptance"; do
  assert_trusted_path "$root"
done
test "$(/usr/bin/docker context show)" = default
test "$(/usr/bin/docker context inspect default --format '{{(index .Endpoints "docker").Host}}')" = unix:///var/run/docker.sock
test -S /var/run/docker.sock
test ! -L /var/run/docker.sock
test "$(stat -c %u /var/run/docker.sock)" = 0
for root in "$SCRATCH_FS" "$REVIEWED_TREE" "$PRIVATE_SOURCES" "$PRIVATE_SDK"; do
  test -d "$root"
  test ! -L "$root"
done
test "$(stat -c %u "$SCRATCH_FS")" = 0
test -z "$(find -P "$REVIEWED_TREE" -xdev \( ! -user root -o -perm /022 -o -type l \) -print -quit)"
test -z "$(find -P "$ACCEPT_REPORTS" -maxdepth 0 \( ! -user root -o -perm /022 -o -type l \) -print -quit)"

run_token="$(date +%s)-$$"
work_root="$(mktemp -d "$SCRATCH_FS/acceptance/run-$run_token-XXXXXXXX")"
landlock_name="nef-watch-accept-landlock-$run_token"
init_name="nef-watch-accept-init-$run_token"
smoke_name="nef-watch-accept-smoke-$run_token"
render_name="nef-watch-accept-render-$run_token"
inspect_name="nef-watch-accept-inspect-$run_token"
report_tmp=""

cleanup() {
  rc=$?
  trap - EXIT INT TERM HUP
  containers_clean=1
  if ! timeout --signal=KILL 15s /usr/bin/docker info >/dev/null 2>&1; then
    echo 'cannot contact Docker during acceptance cleanup' >&2
    containers_clean=0
    rc=74
  else
    for name in "$inspect_name" "$render_name" "$smoke_name" \
                "$init_name" "$landlock_name"; do
      if /usr/bin/docker container inspect "$name" >/dev/null 2>&1; then
        if ! timeout --signal=KILL 30s /usr/bin/docker rm -f "$name" >/dev/null 2>&1; then
          echo "could not remove acceptance container: $name" >&2
          containers_clean=0
          rc=74
        fi
      fi
    done
    if ! timeout --signal=KILL 15s /usr/bin/docker info >/dev/null 2>&1; then
      echo 'lost Docker while confirming acceptance cleanup' >&2
      containers_clean=0
      rc=74
    else
      for name in "$inspect_name" "$render_name" "$smoke_name" \
                  "$init_name" "$landlock_name"; do
        if /usr/bin/docker container inspect "$name" >/dev/null 2>&1; then
          echo "acceptance container survived cleanup: $name" >&2
          containers_clean=0
          rc=74
        fi
      done
    fi
  fi
  [[ -z "$report_tmp" ]] || unlink -- "$report_tmp" 2>/dev/null || true
  if [[ "$containers_clean" = 1 ]]; then
    case "$work_root" in
      "$SCRATCH_FS"/acceptance/run-$run_token-*)
        chmod -R u+rwX "$work_root" 2>/dev/null || true
        timeout --signal=KILL 2m rm -rf --one-file-system -- "$work_root" || rc=74
        ;;
      *) echo 'refusing unsafe acceptance cleanup path' >&2; rc=74 ;;
    esac
  else
    echo "leaving acceptance work tree for surviving container: $work_root" >&2
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

# Bind every following check to the exact local config ID.
/usr/bin/docker image inspect "$IMAGE_ID" > "$work_root/docker-inspect.json"
test "$(/usr/bin/docker image inspect "$IMAGE_ID" --format '{{.Id}}')" = "$IMAGE_ID"
test "$(/usr/bin/docker image inspect "$IMAGE_ID" --format '{{index .Config.Labels "io.nef-watch.source.fingerprint"}}')" = "$EXPECTED_SOURCE_FINGERPRINT"
test "$(/usr/bin/docker image inspect "$IMAGE_ID" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = "$REVIEWED_COMMIT"
test "$(/usr/bin/docker image inspect "$IMAGE_ID" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -cx 'NEF_WATCH_RENDER_JOBS=1')" = 1

# Scan exact bytes before the SDK or NEFs are exposed.  Empty, root-owned
# config/ignore/module/plugin homes and env -i make repository files, the
# caller's cwd/environment, and installed Trivy extensions irrelevant.
install -d -o 0 -g 0 -m 0700 \
  "$work_root/trivy-cache" "$work_root/trivy-home" \
  "$work_root/trivy-xdg-config" "$work_root/trivy-xdg-data" \
  "$work_root/trivy-modules"
printf '{}\n' > "$work_root/trivy-config.yaml"
: > "$work_root/trivy-ignore"
chmod 0400 "$work_root/trivy-config.yaml" "$work_root/trivy-ignore"
TRIVY_BIN="$(/usr/bin/realpath /usr/bin/trivy)"
test -x "$TRIVY_BIN"
test "$(/usr/bin/stat -c %u "$TRIVY_BIN")" = 0
test -z "$(/usr/bin/find "$TRIVY_BIN" -maxdepth 0 -perm /022 -print -quit)"
test "$("$TRIVY_BIN" --version | /usr/bin/sed -n 's/^Version: //p')" = 0.72.0
(
  cd "$work_root"
  /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    HOME="$work_root/trivy-home" \
    XDG_CONFIG_HOME="$work_root/trivy-xdg-config" \
    XDG_DATA_HOME="$work_root/trivy-xdg-data" \
    DOCKER_CONFIG="$DOCKER_CONFIG" LC_ALL=C LANG=C \
    /usr/bin/timeout --signal=TERM --kill-after=30s 20m \
      "$TRIVY_BIN" --config "$work_root/trivy-config.yaml" image \
        --cache-dir "$work_root/trivy-cache" \
        --ignorefile "$work_root/trivy-ignore" \
        --module-dir "$work_root/trivy-modules" \
        --image-src docker --scanners vuln --pkg-types os,library --no-progress \
        --exit-code 1 --ignore-unfixed --severity HIGH,CRITICAL "$IMAGE_ID"
)
/usr/bin/docker save --output "$work_root/image.tar" "$IMAGE_ID"
timeout --signal=TERM --kill-after=30s 10m \
  /usr/bin/python3.12 -I "$REVIEWED_TREE/.github/workflows/verify_sdk_free_image.py" \
    "$work_root/image.tar" "$REVIEWED_TREE/docker/nikon-sdk-v1.46.sha256"
unlink -- "$work_root/image.tar"

# ABI 6 and the metadata seccomp filter are required even if the kernel version
# appears new enough. The sealed probe verifies allowed ordinary I/O and EPERM
# for chmod/chown/utime/xattr mutation both inside and outside its writable rule.
timeout --signal=TERM --kill-after=5s 30s \
  /usr/bin/docker run --name "$landlock_name" --pull never --platform linux/amd64 \
    --user 99:100 --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --memory 128m --memory-swap 128m \
    --cpus 1 --pids-limit 32 --ulimit nofile=64:64 --log-driver none \
    --tmpfs /work:rw,noexec,nosuid,nodev,size=1m,mode=0700,uid=99,gid=100 \
    --entrypoint /usr/local/bin/python3 "$IMAGE_ID" -I \
      /usr/local/libexec/nef-watch-landlock-probe.py /work
/usr/bin/docker rm "$landlock_name" >/dev/null

# Copy and hash only the manifested private inputs into the bounded run tree.
/usr/bin/python3.12 -I \
  "$REVIEWED_TREE/tool/stage_runtime_acceptance.py" \
  --baseline "$REVIEWED_TREE/validation/baseline-v1.json" \
  --source-dir "$PRIVATE_SOURCES" \
  --sdk-manifest "$REVIEWED_TREE/docker/nikon-sdk-v1.46.sha256" \
  --sdk-dir "$PRIVATE_SDK" \
  --destination "$work_root/private" --json > "$work_root/staging-report.json"
install -d -o 99 -g 100 -m 0700 "$work_root/output" "$work_root/temp"
install -d -o 0 -g 0 -m 0755 "$work_root/state"

common_limits=(
  --pull never --platform linux/amd64 --network none
  --memory 4g --memory-swap 4g --cpus 2 --pids-limit 256
  --ulimit nofile=512:512 --ulimit fsize=2147483648:2147483648
  --read-only --cap-drop ALL --security-opt no-new-privileges:true
  --tmpfs /tmp:rw,noexec,nosuid,nodev,mode=1777,size=64m
  --tmpfs /tmp/.X11-unix:rw,noexec,nosuid,nodev,mode=1777,size=1m
  --tmpfs /run:rw,noexec,nosuid,nodev,mode=0755,size=8m
  --log-driver none
)
common_env=(
  --env "NEF_WATCH_IMAGE_REFERENCE=$IMAGE_ID"
  --env NEF_WATCH_TEMP_DIR=/work/nef-watch
  --env NEF_WATCH_TEMP_CAPACITY_BYTES=17179869184
  --env NEF_WATCH_TEMP_MIN_FREE_BYTES=8589934592
  --env NEF_WATCH_REQUIRE_ACCEPTANCE_REPORT=0
  --env NEF_WATCH_PRE_ACCEPTANCE_FIXTURE_MODE=1
  --env NEF_WATCH_REQUIRE_LANDLOCK=1
  --env NEF_WATCH_RENDER_JOBS=1
  --env NEF_WATCH_RENDER_MEMORY_MIB=1536
  --env NEF_WATCH_SDK_MEMORY_MIB=512
  --env NEF_WATCH_RENDER_FILE_SIZE_MIB=2048
)

# The report does not exist yet. This explicit mode accepts only the three
# reviewed fixture command shapes below; it cannot start the persistent watcher.

# Root exists only in this one-shot initializer. Its output mount is read-only.
(
  ulimit -f 16384
  timeout --signal=TERM --kill-after=30s 20m \
  /usr/bin/docker run --name "$init_name" "${common_limits[@]}" \
    --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add KILL \
    --cap-add SETGID --cap-add SETPCAP --cap-add SETUID \
    --mount type=bind,src="$work_root/private/sources",dst=/input,readonly \
    --mount type=bind,src="$work_root/output",dst=/output,readonly \
    --mount type=bind,src="$work_root/private/sdk",dst=/nikon-sdk,readonly \
    --mount type=bind,src="$work_root/temp",dst=/work/nef-watch \
    --mount type=bind,src="$work_root/state",dst=/var/lib/nef-watch \
    "${common_env[@]}" --entrypoint /usr/bin/tini "$IMAGE_ID" \
    -g -- /usr/local/bin/nef-watch-entrypoint --initialize-only \
    > "$work_root/init.log" 2>&1
)
/usr/bin/docker rm "$init_name" >/dev/null

# The accepted long-running shape starts as uid 99, has no capabilities, and
# cannot see the host SDK. This smoke also exercises Wine under Landlock.
(
  ulimit -f 16384
  timeout --signal=TERM --kill-after=15s 5m \
  /usr/bin/docker run --name "$smoke_name" "${common_limits[@]}" --user 99:100 \
    --mount type=bind,src="$work_root/temp",dst=/work/nef-watch \
    --mount type=bind,src="$work_root/state",dst=/var/lib/nef-watch \
    "${common_env[@]}" --env HOME=/var/lib/nef-watch/home \
    --env NEF_WATCH_INIT_COMPLETE=1 "$IMAGE_ID" --wine-isolation-smoke \
    > "$work_root/wine-smoke.log" 2>&1
)
/usr/bin/docker rm "$smoke_name" >/dev/null

# Render all six private files once. Host-side output is limited to 16 MiB.
(
  ulimit -f 16384
  timeout --signal=TERM --kill-after=30s 30m \
    /usr/bin/docker run --name "$render_name" "${common_limits[@]}" --user 99:100 \
      --stop-timeout 420 \
      --mount type=bind,src="$work_root/private/sources",dst=/input,readonly \
      --mount type=bind,src="$work_root/output",dst=/output \
      --mount type=bind,src="$work_root/temp",dst=/work/nef-watch \
      --mount type=bind,src="$work_root/state",dst=/var/lib/nef-watch \
      "${common_env[@]}" --env HOME=/var/lib/nef-watch/home \
      --env NEF_WATCH_INIT_COMPLETE=1 "$IMAGE_ID" \
      /input --out /output --once --recursive --overwrite --format tiff \
      --bits 8 --jobs 1 --max-scan-entries 100 \
      --temp-dir /work/nef-watch --state-dir /var/lib/nef-watch/app-state \
      > "$work_root/candidate.log" 2>&1
)
/usr/bin/docker rm "$render_name" >/dev/null

# Decode untrusted TIFF bytes without any NEF, SDK, state, temp, Docker socket,
# network, capability, or writable directory. Only this 1 MiB report file is
# writable, and the report is bound to each TIFF's complete SHA-256 afterward.
tiff_inspection="$work_root/tiff-inspection.json"
install -o 99 -g 100 -m 0600 /dev/null "$tiff_inspection"
timeout --signal=TERM --kill-after=30s 20m \
  /usr/bin/docker run --name "$inspect_name" --pull never --platform linux/amd64 \
    --user 99:100 --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --memory 2g --memory-swap 2g \
    --cpus 1 --pids-limit 64 --ulimit nofile=128:128 \
    --ulimit fsize=1048576:1048576 --log-driver none \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,mode=1777,size=16m \
    --mount type=bind,src="$work_root/output",dst=/output,readonly \
    --mount type=bind,src="$tiff_inspection",dst=/report.json \
    --env HOME=/tmp --entrypoint /usr/local/bin/python3 "$IMAGE_ID" -I \
      /app/tool/verify_runtime_acceptance.py --emit-tiff-inspection \
      --output-dir /output --report /report.json
/usr/bin/docker rm "$inspect_name" >/dev/null
chown 0:0 "$tiff_inspection"
chmod 0400 "$tiff_inspection"

# The root-owned verifier publishes an exclusive, fsynced report atomically.
report_id="${IMAGE_ID#sha256:}"
report_final="$ACCEPT_REPORTS/$report_id.acceptance.json"
test ! -e "$report_final"
report_tmp="$(mktemp "$ACCEPT_REPORTS/.acceptance-$report_id-XXXXXXXX")"
timeout --signal=TERM --kill-after=30s 20m \
  prlimit --as=2147483648:2147483648 --cpu=900:900 \
    --fsize=16777216:16777216 --nofile=256:256 --nproc=64:64 -- \
  /usr/bin/python3.12 -I \
    "$REVIEWED_TREE/tool/verify_runtime_acceptance.py" \
    "$REVIEWED_TREE/validation/baseline-v1.json" \
    --source-dir "$work_root/private/sources" \
    --output-dir "$work_root/output" \
    --sdk-dir "$work_root/private/sdk" \
    --sdk-manifest "$REVIEWED_TREE/docker/nikon-sdk-v1.46.sha256" \
    --docker-image-id "$IMAGE_ID" --trusted-local-image \
    --docker-source-fingerprint "$EXPECTED_SOURCE_FINGERPRINT" \
    --expected-source-fingerprint "$EXPECTED_SOURCE_FINGERPRINT" \
    --expected-revision "$REVIEWED_COMMIT" \
    --docker-inspect "$work_root/docker-inspect.json" \
    --tiff-inspection-report "$tiff_inspection" --json > "$report_tmp"

/usr/bin/python3.12 -I - "$report_tmp" "$report_final" <<'PY'
import json, os, pathlib, sys

temporary = pathlib.Path(sys.argv[1])
final = pathlib.Path(sys.argv[2])
with temporary.open("rb") as stream:
    report = json.load(stream)
assert report.get("passed") is True, report
fd = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
try:
    os.fsync(fd)
finally:
    os.close(fd)
# The parent trust directory remains root-only. Mode 0444 exposes only this
# exact bind-mounted report to the unprivileged production containers.
os.chmod(temporary, 0o444)
os.link(temporary, final)
temporary.unlink()
directory_fd = os.open(final.parent, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
report_tmp=""
test "$(stat -c %u "$report_final")" = 0
test "$(stat -c %a "$report_final")" = 444
test "$(stat -c %h "$report_final")" = 1
printf 'accepted exact local image: %s\nreport: %s\n' "$IMAGE_ID" "$report_final"
ACCEPT_IMAGE
```

Any mismatch in the source fingerprint, commit label, SDK manifest, source NEF
hash, TIFF set, strict one-series/one-page TIFF contract, decoded pixels, or
Nikon ICC profile fails the gate. Extra top-level metadata tags, excess
non-raster payload, and unreferenced trailing bytes also fail. The render has no
network or Docker socket, and the decoder has no visibility into the private
NEFs or SDK.

The exact reviewed commit is the trust boundary: this gate catches packaging,
runtime, provenance, decoder, and rendering mistakes, but no finite image test
can prove that intentionally malicious code you approved did not encode a few
secret bits into otherwise legitimate EXIF values or lossless-compression
choices. Review every
fingerprinted file before recording the commit and fingerprint; do not accept
an image built from code you did not review.

## 4. Configure Compose

Return to the sealed reviewed tree, copy the example, then edit `docker/.env` as
root:

```bash
set -euo pipefail
umask 077
test "$(/usr/bin/id -u)" = 0
SCRATCH_FS=/mnt/fastest-nvme/nef-watch-scratch
TRUST_ROOT="$SCRATCH_FS/.trust"
BUILD_STATE="$TRUST_ROOT/build-state.env"
test "$(/usr/bin/findmnt -T "$SCRATCH_FS" -n -o TARGET)" = \
  "$(/usr/bin/realpath "$SCRATCH_FS")"
/usr/bin/python3.12 -I - "$SCRATCH_FS" "$TRUST_ROOT" "$BUILD_STATE" <<'PY'
import os, pathlib, stat, sys

anchor, trust, state = map(pathlib.Path, sys.argv[1:])
assert anchor.is_absolute() and anchor == anchor.resolve(strict=True), anchor
anchor_entry = os.lstat(anchor)
assert stat.S_ISDIR(anchor_entry.st_mode), anchor
assert anchor_entry.st_uid == 0 and anchor_entry.st_mode & 0o022 == 0, anchor
assert trust.is_absolute() and trust == trust.resolve(strict=True), trust
assert trust.parent == anchor, trust
trust_entry = os.lstat(trust)
assert stat.S_ISDIR(trust_entry.st_mode), trust
assert trust_entry.st_uid == 0 and trust_entry.st_mode & 0o022 == 0, trust
assert trust_entry.st_dev == anchor_entry.st_dev, trust
assert state.parent == trust and state == state.resolve(strict=True), state
state_entry = os.lstat(state)
assert stat.S_ISREG(state_entry.st_mode), state
assert state_entry.st_uid == 0, state
assert stat.S_IMODE(state_entry.st_mode) == 0o400, state
assert state_entry.st_nlink == 1 and state_entry.st_dev == anchor_entry.st_dev, state
PY

IMAGE_ID=
REVIEWED_COMMIT=
EXPECTED_SOURCE_FINGERPRINT=
REVIEWED_TREE=
seen_image=0
seen_commit=0
seen_fingerprint=0
seen_tree=0
while IFS='=' read -r key value; do
  case "$key" in
    IMAGE_ID) test "$seen_image" = 0; IMAGE_ID="$value"; seen_image=1 ;;
    REVIEWED_COMMIT) test "$seen_commit" = 0; REVIEWED_COMMIT="$value"; seen_commit=1 ;;
    EXPECTED_SOURCE_FINGERPRINT)
      test "$seen_fingerprint" = 0
      EXPECTED_SOURCE_FINGERPRINT="$value"
      seen_fingerprint=1
      ;;
    REVIEWED_TREE) test "$seen_tree" = 0; REVIEWED_TREE="$value"; seen_tree=1 ;;
    *) echo "unexpected exact-build state field: $key" >&2; exit 78 ;;
  esac
done < "$BUILD_STATE"
test "$seen_image$seen_commit$seen_fingerprint$seen_tree" = 1111
[[ "$IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$REVIEWED_COMMIT" =~ ^[0-9a-f]{40}$ ]]
[[ "$EXPECTED_SOURCE_FINGERPRINT" =~ ^[0-9a-f]{64}$ ]]
test "$REVIEWED_TREE" = "$TRUST_ROOT/reviewed/$REVIEWED_COMMIT"
ACCEPTANCE_REPORT="$TRUST_ROOT/acceptance-reports/${IMAGE_ID#sha256:}.acceptance.json"
test -f "$ACCEPTANCE_REPORT"
test ! -L "$ACCEPTANCE_REPORT"
test "$(stat -c %u "$ACCEPTANCE_REPORT")" = 0
test "$(stat -c %a "$ACCEPTANCE_REPORT")" = 444
test "$(stat -c %h "$ACCEPTANCE_REPORT")" = 1
cd "$REVIEWED_TREE"
/usr/bin/install -o 0 -g 0 -m 0600 docker/.env.example docker/.env
/usr/bin/sed -i "s|^IMAGE=.*|IMAGE=$IMAGE_ID|" docker/.env
/usr/bin/sed -i "s|^ACCEPTANCE_REPORT=.*|ACCEPTANCE_REPORT=$ACCEPTANCE_REPORT|" docker/.env
```

```dotenv
NIKON_SDK_DIR=/mnt/user/Media/.nef-watch-build/nikon-sdk
NEF_INPUT=/mnt/user/Photos/NEF
NEF_OUTPUT=/mnt/user/Media/Photos/Nikon-TIFF
NEF_TEMP_DIR=/mnt/fastest-nvme/nef-watch-scratch/production
TEMP_CAPACITY_BYTES=17179869184
TEMP_MIN_FREE_BYTES=8589934592
IMAGE=sha256:REPLACE_WITH_ACCEPTED_LOCAL_IMAGE_ID
ACCEPTANCE_REPORT=/mnt/fastest-nvme/nef-watch-scratch/.trust/acceptance-reports/REPLACE_WITH_ACCEPTED_IMAGE_HEX.acceptance.json
JOBS=1
INTERVAL=3
HEALTH_MAX_AGE_SECONDS=120
MAX_SCAN_ENTRIES=100000
MEMORY_LIMIT=4g
CPU_LIMIT=4
PIDS_LIMIT=256
RENDER_MEMORY_MIB=1536
SDK_MEMORY_MIB=512
RENDER_FILE_SIZE_MIB=2048
```

`RENDER_FILE_SIZE_MIB` is accepted only from 768 through 2048, matching the
container's hard file-size limit. Production `INTERVAL` is 0.1–840 seconds;
`HEALTH_MAX_AGE_SECONDS` is 60–900 seconds and must be at least 60 seconds
greater than the interval.

Use the direct path to the NVMe filesystem for `NEF_TEMP_DIR`; input and final
TIFF output may remain normal Unraid shares. Pre-create every path because
Compose uses `create_host_path: false`:

```bash
/usr/bin/install -d -o 99 -g 100 -m 0700 "$SCRATCH_FS/production"
/usr/bin/install -d -o 99 -g 100 -m 0750 /mnt/user/Media/Photos/Nikon-TIFF

/usr/bin/python3.12 -I docker/validate_storage.py \
  --input /mnt/user/Photos/NEF \
  --output /mnt/user/Media/Photos/Nikon-TIFF \
  --temp /mnt/fastest-nvme/nef-watch-scratch/production \
  --sdk /mnt/user/Media/.nef-watch-build/nikon-sdk \
  --acceptance-report "$ACCEPTANCE_REPORT" \
  --trusted-scratch-root "$SCRATCH_FS" \
  --require-unraid-direct-temp \
  --max-temp-filesystem-bytes 17179869184 \
  --min-temp-filesystem-free-bytes 8589934592
test ! -L docker/.env
test "$(/usr/bin/stat -c %u docker/.env)" = 0
test "$(/usr/bin/stat -c %a docker/.env)" = 600
test "$(/usr/bin/grep -c '^IMAGE=' docker/.env)" = 1
test "$(/usr/bin/grep -Fxc "IMAGE=$IMAGE_ID" docker/.env)" = 1
test "$(/usr/bin/grep -c '^ACCEPTANCE_REPORT=' docker/.env)" = 1
test "$(/usr/bin/grep -Fxc "ACCEPTANCE_REPORT=$ACCEPTANCE_REPORT" docker/.env)" = 1
safe_compose() {
  /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root \
    LC_ALL=C LANG=C \
    /usr/bin/python3.12 -I \
      "$REVIEWED_TREE/docker/accepted_compose.py" \
      --trust-root "$TRUST_ROOT" -- "$@"
}
/usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root \
  LC_ALL=C LANG=C \
  /usr/bin/python3.12 -I "$REVIEWED_TREE/docker/accepted_compose.py" \
    --trust-root "$TRUST_ROOT" --verify-only
```

Use the same edited paths in the storage check and `.env`; never shell-source a
dotenv file as root. Keep this root shell open. `safe_compose` strips ambient
Docker/Compose/Python loader variables and fixes the Compose file, env file,
project directory, project name, Docker context, and local socket.

The wrapper repeats the complete gate before every `up`. It requires a
root-owned, mode-0444, single-link retained passing report named for the exact
image ID; recomputes the sealed source-tree, baseline, and SDK-manifest hashes;
rehashes all 33 production SDK files; and revalidates the report's six
source-NEF hash records against the current sealed baseline. It also requires
the recorded raster shape, dtype, bit depth, orientation, strict TIFF contract,
pixel, and ICC results; freshly inspects the local image;
and validates the effective Compose and storage models. A missing, stale,
failed, edited, or mismatched report is therefore a hard startup failure. In
particular, the current `DSC_0722` Linux pixel mismatch must be fixed and the
six-file gate rerun successfully; do not rebaseline it merely to permit
deployment.

`MEMORY_LIMIT`, `CPU_LIMIT`, and `PIDS_LIMIT` must remain finite and are checked
before Compose runs: 2–16 GiB, 1–16 CPUs, and 128–1024 PIDs respectively.
Temporary capacity must be 8–64 GiB with at least 8 GiB required free.
The render-file ceiling, health age, and polling interval are also parsed and
cross-checked against the effective Compose environment and hard ulimits. Stop and
diagnostic commands remain available if acceptance later becomes invalid;
every one that touches the Docker daemon first revalidates the exact root-owned
local socket and default context. The wrapper rejects `run`, `start`, `restart`, `create`, `unpause`,
build, pull, push, alternate Compose files/projects,
partial-service startup, and other production bypasses. `up` accepts only optional `-d`/`--detach`
and always applies the complete reviewed project,
including its initializer.

The report is mounted read-only into both production services at a fixed path.
At every initializer and watcher start, uid `99` validates it against the exact
image ID plus the source fingerprint, commit, baseline, and SDK manifest baked
into that image. Report validation is default-on in the image, and production
also refuses to disable Landlock. Only the explicitly named pre-acceptance
fixture mode may run before the report exists, and that mode accepts only the
three reviewed one-shot fixture commands. The health probe repeats the check. This makes an accidental
raw Compose or Unraid-UI start fail closed, but it is defense in depth: keep
using `safe_compose` because the root-owned host wrapper also rechecks the live
Docker image, daemon boundary, production SDK bytes, effective Compose model,
and host storage topology. The report is not a secret; its integrity comes from
root ownership, a single link, mode `0444`, and the root-only parent directory.

The unprivileged watcher also repeats storage mount-isolation, total-capacity,
available-space, and temp-to-report backing-identity validation on every
process start and every health probe.
Docker's `unless-stopped` restart path therefore cannot reuse an old initializer
result after an NVMe mount or free-space condition changes.

Compose has two services:

1. `nef-watch-init` is a one-shot root initializer with a narrow capability
   set. It validates storage, stages the SDK, and seals the Wine template.
2. `nef-watch` is the persistent service. Unprivileged Tini execs the watcher
   directly as `99:100`, with no capabilities and no SDK mount. Every render
   creates authenticated Wine and Xvfb sibling Landlock domains, so a hostile
   SDK process cannot kill the watcher or escape through the X server.
   Every Landlocked renderer, Xvfb, and parser also inherits a fail-closed
   amd64 seccomp-BPF filter that denies metadata-mutation syscall families and
   `io_uring`; this closes Landlock's unmediated metadata-operation paths to
   same-UID host-facing files.
   Linux/Wine is deliberately fixed at one render job because parallel
   renderers would share the container's local X socket namespace. Tini
   delivers shutdown directly to the watcher's cooperative drain handler.

The root filesystem is read-only, networking is disabled, logs rotate, memory,
swap, CPU, PIDs, file descriptors, scan work, render files, subprocess output,
and tool runtimes are bounded.

## 5. Start production

Only one watcher may own an output tree. Stop any old macOS or container watcher
that uses the same input/output before starting Unraid. Do not remove the old
watcher until you have confirmed the new deployment passes acceptance.

```bash
safe_compose down
safe_compose up -d
safe_compose ps
safe_compose logs -f nef-watch
```

There is no `docker compose pull`: `pull_policy: never` and the exact local
image ID prevent tag drift. First initialization can take several minutes.
Afterward, health requires a fresh scan, sealed runtime, no permanent dead
letters, a heartbeat bound to the live watcher's PID and Linux process-start
time, and successful output/NVMe create-write-delete probes. A render also fails
if its private X server cannot start.

Compose enables `--skip-existing` on the first state database, so the existing
backlog is baselined and only later arrivals or changed files are converted. If
you are importing the old marker from a different input root, set, for example:

```dotenv
LEGACY_INPUT_ROOT=/Volumes/FTPDropbox/sorted
```

Shutdown allows seven minutes for active work to drain:

```bash
safe_compose stop nef-watch
```

Do not use an ad-hoc Compose `run` to acknowledge dead letters: its service,
entrypoint, user, network, capabilities, and mounts can be overridden after
configuration validation. Correct or replace the failed source so its identity
changes and the watcher can retry it. A manual state-repair operation requires
a separately reviewed, narrowly bounded maintenance procedure; it is not part
of this production wrapper.

Keep the `nef-watch-state` volume across updates. It contains licensed staged
runtime files, the sealed Wine template, output provenance, retries, and the
one-time backlog baseline. Before changing `IMAGE`, stop the watcher, accept the
new exact ID, update `.env`, repeat the report/Compose checks above, and run
`safe_compose up -d`; the one-shot
initializer runs before the new unprivileged watcher.

## CLI help

Help needs no SDK or writable mount and remains bounded:

```bash
/usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root \
  LC_ALL=C LANG=C DOCKER_CONFIG="$TRUST_ROOT/docker-config" \
  /usr/bin/timeout --signal=TERM --kill-after=5s 30s \
  /usr/bin/docker run --rm --pull never --platform linux/amd64 \
    --user 99:100 --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --memory 256m --memory-swap 256m --cpus 1 --pids-limit 64 \
    --ulimit nofile=128:128 --ulimit fsize=16777216:16777216 \
    --log-driver none "$IMAGE_ID" --help
```

The production wrapper deliberately has no one-shot Compose mode. Put ordinary
work in the watched input tree; this preserves the accepted initializer,
unprivileged runtime, fixed mounts, and output ownership boundary.

## Expected hard failures

- `Landlock ABI ... is too old`: update to an Unraid/kernel build with ABI 6
  and Landlock enabled; do not disable `NEF_WATCH_REQUIRE_LANDLOCK`.
- `Landlock enforcement probe failed`: the running kernel/Docker combination
  could not enforce ABI 6 plus the metadata seccomp boundary, or the disposable
  probe tree could not be removed. Do not bypass the probe.
- `temporary filesystem ... exceeds ... ceiling`: `NEF_TEMP_DIR` is an ordinary
  directory on a larger pool. Point it at a dedicated quota-visible filesystem.
- `temporary storage ... direct Unraid pool path` or `backing filesystem`: use
  one canonical, symlink-free `/mnt/<pool>/...` scratch mount for both temp and
  the retained report; never use `/mnt/user`, a remote, or an unassigned disk.
- `RENDER_FILE_SIZE_MIB ... 768 and 2048` or a health/interval range error:
  restore the reviewed `.env` defaults or choose values inside the documented
  bounds; the production gate rejects internally inconsistent limits.
- SDK manifest mismatch: mount `Image SDK/Library/win` from the exact Nikon SDK
  1.46.0 package. There is no bypass; a newer SDK needs a reviewed manifest and
  new private pixel baseline.
- Unowned existing TIFF: move it aside or explicitly choose `--overwrite`.
  Watch mode no longer treats an unrelated pre-existing file as a successful
  conversion.

Nikon does not support Linux/Wine, `--deterministic` remains macOS-only, and DNG
output needs a separate Linux `dnglab` installation.
