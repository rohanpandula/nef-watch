#!/usr/bin/env bash
set -euo pipefail
umask 077

export NEF_WATCH_UID="${NEF_WATCH_UID:-99}"
export NEF_WATCH_GID="${NEF_WATCH_GID:-100}"
export NEF_WATCH_APP_HOME="${NEF_WATCH_APP_HOME:-/var/lib/nef-watch/home}"

# Docker starts health probes as the image user (root is needed only for the
# serialized initializer). Mirror the main process's irreversible privilege
# drop before inspecting any attacker-influenced state or writable mounts.
if [[ "$(id -u)" -eq 0 && "${NEF_WATCH_HEALTH_PRIVILEGES_DROPPED:-0}" != 1 ]]; then
  exec setpriv --reuid "$NEF_WATCH_UID" --regid "$NEF_WATCH_GID" --clear-groups \
    --inh-caps=-all --ambient-caps=-all --bounding-set=-all --no-new-privs \
    env HOME="$NEF_WATCH_APP_HOME" PYTHONNOUSERSITE=1 PYTHONSAFEPATH=1 \
      NEF_WATCH_HEALTH_PRIVILEGES_DROPPED=1 "$0"
fi

fail() {
  echo "nef-watch healthcheck failed: $*" >&2
  exit 1
}

[[ "$(id -u)" == "$NEF_WATCH_UID" && "$(id -g)" == "$NEF_WATCH_GID" ]] \
  || fail "probe did not run as the unprivileged watcher uid/gid"
while read -r key value _; do
  case "$key" in
    CapEff:|CapPrm:|CapBnd:)
      [[ "$value" == 0000000000000000 ]] || fail "$key is not empty"
      ;;
    NoNewPrivs:)
      [[ "$value" == 1 ]] || fail "no_new_privs is not active"
      ;;
  esac
done < /proc/self/status

probe_writable_directory() {
  local directory=$1 label=$2 probe
  [[ -d "$directory" && -w "$directory" && -x "$directory" ]] \
    || fail "$label directory is not writable: $directory"
  probe="$(mktemp "$directory/.nef-watch-healthcheck.XXXXXX")" \
    || fail "$label directory rejected a create operation: $directory"
  if ! printf 'nef-watch-healthcheck\n' >"$probe"; then
    rm -f -- "$probe" >/dev/null 2>&1 || true
    fail "$label directory rejected a write operation: $directory"
  fi
  rm -f -- "$probe" || fail "$label directory rejected cleanup: $directory"
}

export NEF_WATCH_STATE_DIR="${NEF_WATCH_STATE_DIR:-/var/lib/nef-watch}"
export NEF_WATCH_WINE_SCHEMA="${NEF_WATCH_WINE_SCHEMA:-wine8-deb12-vc14-cc0ff0eb1dc3-landlock6-v3}"
export NEF_WATCH_WINE_TEMPLATE="${NEF_WATCH_WINE_TEMPLATE:-$NEF_WATCH_STATE_DIR/wine-template-$NEF_WATCH_WINE_SCHEMA}"
export NIKON_RUNTIME_DIR="${NIKON_RUNTIME_DIR:-$NEF_WATCH_STATE_DIR/nikon-runtime/current}"
export NEF_WATCH_HEALTH_FILE="${NEF_WATCH_HEALTH_FILE:-$NEF_WATCH_STATE_DIR/app-state/health.json}"
export NEF_WATCH_HEALTH_MAX_AGE="${NEF_WATCH_HEALTH_MAX_AGE:-120}"

landlock_launcher=/usr/local/libexec/nef-watch-landlock-exec.py
landlock_probe=/usr/local/libexec/nef-watch-landlock-probe.py
for component in "$landlock_launcher" "$landlock_probe"; do
  [[ -f "$component" && ! -L "$component" &&
     "$(stat -Lc '%u:%a:%h' -- "$component")" == 0:555:1 ]] \
    || fail "sealed Landlock launcher or probe is unavailable"
done
[[ "${NEF_WATCH_REQUIRE_LANDLOCK:-1}" == 1 ]] \
  || fail "Landlock is mandatory in production and pre-acceptance fixture modes"
/usr/local/bin/python3 -I "$landlock_probe" /work/nef-watch \
  >/dev/null || fail "Landlock ABI 6/seccomp runtime probe failed"

production_acceptance="${NEF_WATCH_REQUIRE_ACCEPTANCE_REPORT:-1}"
pre_acceptance_fixture="${NEF_WATCH_PRE_ACCEPTANCE_FIXTURE_MODE:-0}"
case "$production_acceptance:$pre_acceptance_fixture" in
  1:0)
    /usr/local/bin/python3 -I \
      /usr/local/libexec/nef-watch-validate-acceptance.py >/dev/null \
      || fail "retained production acceptance evidence is invalid"
    /usr/local/bin/python3 -I /usr/local/libexec/nef-watch-validate-storage.py --container --runtime \
      --input /input --output /output --temp /work/nef-watch \
      --state /var/lib/nef-watch/app-state \
      --acceptance-report /run/nef-watch-acceptance.json \
      --max-temp-filesystem-bytes "${NEF_WATCH_TEMP_CAPACITY_BYTES:?set NEF_WATCH_TEMP_CAPACITY_BYTES}" \
      --min-temp-filesystem-free-bytes "${NEF_WATCH_TEMP_MIN_FREE_BYTES:?set NEF_WATCH_TEMP_MIN_FREE_BYTES}" \
      >/dev/null || fail "runtime storage isolation or capacity changed"
    ;;
  0:1) ;;
  *) fail "production requires its acceptance report; only explicit pre-acceptance fixture mode may disable it" ;;
esac

runtime="$(realpath "$NIKON_RUNTIME_DIR" 2>/dev/null)" \
  || fail "private Nikon SDK runtime is not initialized"
case "$runtime" in
  "$NEF_WATCH_STATE_DIR"/nikon-runtime/sdk-*) ;;
  *) fail "active Nikon runtime escapes its sealed root" ;;
esac
for runtime_file in \
  "$runtime/.nef-watch-sdk-ready.json" \
  "$runtime/nef_render.exe" \
  "$runtime/NkImgSDK.dll" \
  "$runtime/Profiles/NKsRGB.icm"; do
  [[ -f "$runtime_file" && ! -L "$runtime_file" && -r "$runtime_file" ]] \
    || fail "sealed Nikon runtime file is missing: $runtime_file"
  [[ "$(stat -Lc '%u:%h' -- "$runtime_file")" == 0:1 && ! -w "$runtime_file" ]] \
    || fail "Nikon runtime file is not root-owned, single-link, and sealed: $runtime_file"
done
[[ -x "$runtime/nef_render.exe" ]] || fail "Nikon render adapter is not executable"

template="$NEF_WATCH_WINE_TEMPLATE"
marker="$template/.nef-watch-template-ready"
[[ -d "$template" && ! -L "$template" && ! -w "$template" ]] \
  || fail "sealed Wine template is unavailable"
[[ -f "$marker" && ! -L "$marker" && ! -w "$marker" ]] \
  || fail "Wine template marker is unavailable or writable"
[[ "$(stat -Lc '%u:%h' -- "$marker")" == 0:1 ]] \
  || fail "Wine template marker is not root-owned and single-link"
[[ "$(cat "$marker")" == "$NEF_WATCH_WINE_SCHEMA" ]] \
  || fail "Wine template schema does not match"
[[ -d "$template/dosdevices" && ! -L "$template/dosdevices" ]] \
  || fail "Wine template DOS map directory is unsafe"
[[ "$(readlink "$template/dosdevices/c:")" == ../drive_c ]] \
  || fail "Wine template C: mapping is unsafe"
mapping_count=0
while IFS= read -r -d '' mapping; do
  [[ "${mapping##*/}" == "c:" ]] || fail "Wine template exposes a DOS mapping other than C:"
  (( mapping_count += 1 ))
done < <(find -P "$template/dosdevices" -mindepth 1 -maxdepth 1 -print0)
[[ "$mapping_count" -eq 1 ]] || fail "Wine template must contain exactly one DOS mapping"
[[ -z "$(find -P "$template" -xdev ! -type l -writable -print -quit)" ]] \
  || fail "Wine template contains watcher-writable persistent state"
/usr/local/bin/python3 -I /usr/local/libexec/nef-watch-validate-wine-template.py \
  "$template" "$NEF_WATCH_APP_HOME" >/dev/null \
  || fail "Wine template contains an unsafe writable entry or symbolic link"

/usr/local/bin/python3 -I /usr/local/libexec/nef-watch-check-health-state.py \
  "$NEF_WATCH_HEALTH_FILE" "$NEF_WATCH_HEALTH_MAX_AGE" \
  || fail "watcher heartbeat reports no recent healthy progress"

if [[ -n "${NEF_WATCH_HEALTH_INPUT:-}" ]]; then
  [[ -r "$NEF_WATCH_HEALTH_INPUT" && -x "$NEF_WATCH_HEALTH_INPUT" ]] \
    || fail "input directory is not readable: $NEF_WATCH_HEALTH_INPUT"
fi
[[ -z "${NEF_WATCH_HEALTH_OUTPUT:-}" ]] \
  || probe_writable_directory "$NEF_WATCH_HEALTH_OUTPUT" output
[[ -z "${NEF_WATCH_HEALTH_TEMP:-}" ]] \
  || probe_writable_directory "$NEF_WATCH_HEALTH_TEMP" "temporary work"
