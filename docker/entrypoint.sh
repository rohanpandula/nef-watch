#!/usr/bin/env bash
set -euo pipefail
umask 077

export DISPLAY="${DISPLAY:-:99}"
export WINEARCH=win64
export WINEDEBUG="${WINEDEBUG:--all}"
export NEF_WATCH_UID="${NEF_WATCH_UID:-99}"
export NEF_WATCH_GID="${NEF_WATCH_GID:-100}"
export NEF_WATCH_STATE_DIR="${NEF_WATCH_STATE_DIR:-/var/lib/nef-watch}"
export NEF_WATCH_APP_STATE_DIR="${NEF_WATCH_APP_STATE_DIR:-$NEF_WATCH_STATE_DIR/app-state}"
export NEF_WATCH_APP_HOME="${NEF_WATCH_APP_HOME:-$NEF_WATCH_STATE_DIR/home}"
export NEF_WATCH_WINE_SCHEMA="${NEF_WATCH_WINE_SCHEMA:-wine8-deb12-vc14-cc0ff0eb1dc3-landlock6-v3}"
export NEF_WATCH_WINE_TEMPLATE="${NEF_WATCH_WINE_TEMPLATE:-$NEF_WATCH_STATE_DIR/wine-template-$NEF_WATCH_WINE_SCHEMA}"
export NEF_WATCH_RENDER_JOBS="${NEF_WATCH_RENDER_JOBS:-1}"
export HOME="${HOME:-/root}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/nef-watch-runtime}"
export NIKON_SDK_DIR="${NIKON_SDK_DIR:-/nikon-sdk}"
export TMPDIR="${TMPDIR:-/work/nef-watch}"

LANDLOCK_LAUNCHER=/usr/local/libexec/nef-watch-landlock-exec.py
LANDLOCK_PROBE=/usr/local/libexec/nef-watch-landlock-probe.py

as_app() {
  setpriv --reuid "$NEF_WATCH_UID" --regid "$NEF_WATCH_GID" --clear-groups \
    --inh-caps=-all --ambient-caps=-all --bounding-set=-all --no-new-privs \
    env HOME="$NEF_WATCH_APP_HOME" PYTHONNOUSERSITE=1 PYTHONSAFEPATH=1 "$@"
}

exec_as_app() {
  exec setpriv --reuid "$NEF_WATCH_UID" --regid "$NEF_WATCH_GID" --clear-groups \
    --inh-caps=-all --ambient-caps=-all --bounding-set=-all --no-new-privs \
    env HOME="$NEF_WATCH_APP_HOME" PYTHONNOUSERSITE=1 PYTHONSAFEPATH=1 "$@"
}

probe_landlock_abi6() {
  local component
  for component in "$LANDLOCK_LAUNCHER" "$LANDLOCK_PROBE"; do
    [[ -f "$component" && ! -L "$component" &&
       "$(stat -Lc '%u:%a:%h' -- "$component")" == 0:555:1 ]] || {
      echo "sealed Landlock launcher or probe is unavailable" >&2
      return 78
    }
  done
  if [[ "$(id -u)" -eq 0 ]]; then
    as_app /usr/local/bin/python3 -I "$LANDLOCK_PROBE" "$TMPDIR"
  else
    /usr/local/bin/python3 -I "$LANDLOCK_PROBE" "$TMPDIR"
  fi
}

is_pre_acceptance_fixture_command() {
  if [[ "$#" -eq 1 && ( "$1" == --initialize-only || "$1" == --wine-isolation-smoke ) ]]; then
    return 0
  fi
  local expected=(
    /input --out /output --once --recursive --overwrite --format tiff
    --bits 8 --jobs 1 --max-scan-entries 100
    --temp-dir /work/nef-watch --state-dir /var/lib/nef-watch/app-state
  )
  [[ "$#" -eq "${#expected[@]}" ]] || return 1
  local index position
  for ((index = 0; index < ${#expected[@]}; index += 1)); do
    position=$((index + 1))
    [[ "${!position}" == "${expected[index]}" ]] || return 1
  done
}

for argument in "$@"; do
  if [[ "$argument" == "--help" || "$argument" == "-h" ]]; then
    if [[ "$(id -u)" -eq 0 ]]; then
      exec_as_app /usr/local/bin/python3 -I /app/tool/nef_watch.py "$@"
    fi
    exec /usr/local/bin/python3 -I /app/tool/nef_watch.py "$@"
  fi
done

if [[ "${1:-}" != "--wine-isolation-smoke" ]]; then
  image_reference="${NEF_WATCH_IMAGE_REFERENCE:-<unset>}"
  if [[ ! "$image_reference" =~ ^sha256:[0-9a-f]{64}$ &&
        ! "$image_reference" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]]; then
    echo "IMAGE must be an exact local image ID or registry @sha256 digest (NEF_WATCH_IMAGE_REFERENCE); got $image_reference" >&2
    exit 78
  fi
fi

if [[ ! "$NEF_WATCH_UID" =~ ^[0-9]+$ || ! "$NEF_WATCH_GID" =~ ^[0-9]+$ ||
      "$NEF_WATCH_UID" != 99 || "$NEF_WATCH_GID" != 100 ]]; then
  echo "this Unraid image requires NEF_WATCH_UID=99 and NEF_WATCH_GID=100" >&2
  exit 78
fi
expected_home=/root
[[ "${NEF_WATCH_INIT_COMPLETE:-0}" == 1 ]] && expected_home="$NEF_WATCH_APP_HOME"
if [[ "$NEF_WATCH_STATE_DIR" != /var/lib/nef-watch ||
      "$NEF_WATCH_APP_STATE_DIR" != /var/lib/nef-watch/app-state ||
      "$NEF_WATCH_APP_HOME" != /var/lib/nef-watch/home ||
      "$HOME" != "$expected_home" ||
      "$XDG_RUNTIME_DIR" != /tmp/nef-watch-runtime ||
      "$TMPDIR" != /work/nef-watch ||
      ! "$NEF_WATCH_WINE_SCHEMA" =~ ^[A-Za-z0-9._-]{1,80}$ ||
      "$NEF_WATCH_WINE_TEMPLATE" != "$NEF_WATCH_STATE_DIR/wine-template-$NEF_WATCH_WINE_SCHEMA" ]]; then
  echo "unsafe override of a privileged initialization path" >&2
  exit 78
fi
if [[ "$NEF_WATCH_RENDER_JOBS" != 1 ]]; then
  echo "NEF_WATCH_RENDER_JOBS must be exactly 1 for private X server isolation" >&2
  exit 78
fi

production_acceptance="${NEF_WATCH_REQUIRE_ACCEPTANCE_REPORT:-1}"
pre_acceptance_fixture="${NEF_WATCH_PRE_ACCEPTANCE_FIXTURE_MODE:-0}"
case "$production_acceptance:$pre_acceptance_fixture" in
  1:0)
    if [[ "$(id -u)" -eq 0 ]]; then
      as_app /usr/local/bin/python3 -I \
        /usr/local/libexec/nef-watch-validate-acceptance.py
    else
      /usr/local/bin/python3 -I \
        /usr/local/libexec/nef-watch-validate-acceptance.py
    fi
    ;;
  0:1)
    is_pre_acceptance_fixture_command "$@" || {
      echo "pre-acceptance fixture mode permits only the reviewed fixture commands" >&2
      exit 78
    }
    ;;
  *)
    echo "production requires its acceptance report; only explicit pre-acceptance fixture mode may disable it" >&2
    exit 78
    ;;
esac
if [[ "${NEF_WATCH_REQUIRE_LANDLOCK:-1}" != 1 ]]; then
  echo "Landlock is mandatory in production and pre-acceptance fixture modes" >&2
  exit 78
fi
probe_landlock_abi6 || {
  echo "Landlock ABI 6/seccomp startup probe failed" >&2
  exit 78
}
storage_acceptance_args=()
if [[ "$production_acceptance" == 1 ]]; then
  storage_acceptance_args=(
    --acceptance-report /run/nef-watch-acceptance.json
  )
fi

validate_seconds() {
  local name=$1 maximum=$2 value=${!1}
  [[ "$value" =~ ^[1-9][0-9]{0,3}$ ]] && (( 10#$value <= maximum )) || {
    echo "$name must be an integer from 1 to $maximum seconds" >&2
    exit 78
  }
}

export NEF_WATCH_SDK_BOOTSTRAP_TIMEOUT="${NEF_WATCH_SDK_BOOTSTRAP_TIMEOUT:-300}"
export NEF_WATCH_STARTUP_KILL_GRACE="${NEF_WATCH_STARTUP_KILL_GRACE:-10}"
export NEF_WATCH_VC_REDIST_TIMEOUT="${NEF_WATCH_VC_REDIST_TIMEOUT:-180}"
export NEF_WATCH_WINEBOOT_TIMEOUT="${NEF_WATCH_WINEBOOT_TIMEOUT:-120}"
export NEF_WATCH_WINESERVER_TIMEOUT="${NEF_WATCH_WINESERVER_TIMEOUT:-60}"
export NEF_WATCH_INIT_LOCK_TIMEOUT="${NEF_WATCH_INIT_LOCK_TIMEOUT:-30}"
validate_seconds NEF_WATCH_SDK_BOOTSTRAP_TIMEOUT 3600
validate_seconds NEF_WATCH_STARTUP_KILL_GRACE 300
validate_seconds NEF_WATCH_VC_REDIST_TIMEOUT 3600
validate_seconds NEF_WATCH_WINEBOOT_TIMEOUT 3600
validate_seconds NEF_WATCH_WINESERVER_TIMEOUT 3600
validate_seconds NEF_WATCH_INIT_LOCK_TIMEOUT 300

runtime_overrides='winemenubuilder.exe=d;msvcp140=n,b;msvcp140_1=n,b;msvcp140_2=n,b;concrt140=n,b;vcruntime140=n,b;vcruntime140_1=n,b'
export WINEDLLOVERRIDES="${WINEDLLOVERRIDES:+${WINEDLLOVERRIDES};}${runtime_overrides}"

seal_tree() {
  local root=$1 executable=${2:-}
  chown -R -h 0:0 "$root"
  find -P "$root" -xdev -type d -exec chmod 0555 {} +
  find -P "$root" -xdev -type f -exec chmod 0444 {} +
  [[ -z "$executable" ]] || chmod 0555 "$executable"
}

template_profiles_are_valid() {
  local source="$NIKON_RUNTIME_DIR/Profiles"
  local installed="$1/drive_c/Program Files/Common Files/Nikon/Profiles"
  local source_file target_file source_count=0 installed_count=0 unexpected
  # The SDK-free isolation smoke has no Nikon runtime and exercises Wine only.
  [[ "$NIKON_RUNTIME_DIR" != /usr ]] || return 0
  [[ -d "$source" && ! -L "$source" && -d "$installed" && ! -L "$installed" ]] \
    || return 1
  unexpected="$(find -P "$installed" -mindepth 1 -maxdepth 1 ! -type f -print -quit)"
  [[ -z "$unexpected" ]] || return 1
  while IFS= read -r -d '' source_file; do
    (( source_count += 1 ))
    target_file="$installed/${source_file##*/}"
    [[ -f "$target_file" && ! -L "$target_file" ]] || return 1
    [[ "$(stat -Lc '%u:%a:%h' -- "$source_file")" == 0:444:1 &&
       "$(stat -Lc '%u:%a:%h' -- "$target_file")" == 0:444:1 ]] || return 1
    cmp -s -- "$source_file" "$target_file" || return 1
  done < <(find -P "$source" -mindepth 1 -maxdepth 1 -type f -print0)
  while IFS= read -r -d '' target_file; do
    (( installed_count += 1 ))
  done < <(find -P "$installed" -mindepth 1 -maxdepth 1 -type f -print0)
  (( source_count > 0 && source_count == installed_count ))
}

template_is_valid() {
  local template=$1 marker="$1/.nef-watch-template-ready" mapping mappings=0 unsafe
  [[ -d "$template" && ! -L "$template" ]] || return 1
  [[ -f "$marker" && ! -L "$marker" ]] || return 1
  [[ "$(stat -c '%u:%a' "$template")" == 0:555 ]] || return 1
  [[ "$(stat -c '%u:%a' "$marker")" == 0:444 ]] || return 1
  [[ "$(stat -c '%h' "$marker")" == 1 ]] || return 1
  [[ "$(cat "$marker")" == "$NEF_WATCH_WINE_SCHEMA" ]] || return 1
  [[ -d "$template/dosdevices" && ! -L "$template/dosdevices" ]] || return 1
  [[ "$(readlink "$template/dosdevices/c:")" == ../drive_c ]] || return 1
  while IFS= read -r -d '' mapping; do
    [[ "${mapping##*/}" == "c:" ]] || return 1
    (( mappings += 1 ))
  done < <(find -P "$template/dosdevices" -mindepth 1 -maxdepth 1 -print0)
  [[ "$mappings" -eq 1 ]] || return 1
  unsafe="$(find -P "$template" -xdev \
    \( ! -user root -o \( ! -type l -perm /022 \) \) -print -quit)"
  [[ -z "$unsafe" ]] || return 1
  template_profiles_are_valid "$template" || return 1
  find "$template/drive_c/windows/system32" -maxdepth 1 -type f \
    -iname msvcp140.dll -print -quit | grep -q .
}

remove_managed_wine_path() {
  local path=$1 base=${1##*/}
  case "$base" in
    wine|wine-template-*|.wine-template.*) ;;
    *) echo "refusing to prune unmanaged state path: $path" >&2; return 78 ;;
  esac
  [[ "${path%/*}" == "$NEF_WATCH_STATE_DIR" ]] || return 78
  if [[ -L "$path" || -f "$path" ]]; then
    unlink -- "$path"
    return
  fi
  [[ -d "$path" ]] || return 0
  chmod -R u+rwX -- "$path" 2>/dev/null || true
  rm -rf --one-file-system -- "$path"
  [[ ! -e "$path" && ! -L "$path" ]]
}

prune_obsolete_wine_state() {
  local candidate
  if [[ -e "$NEF_WATCH_STATE_DIR/wine" || -L "$NEF_WATCH_STATE_DIR/wine" ]]; then
    remove_managed_wine_path "$NEF_WATCH_STATE_DIR/wine"
  fi
  while IFS= read -r -d '' candidate; do
    [[ "$candidate" == "$NEF_WATCH_WINE_TEMPLATE" ]] || \
      remove_managed_wine_path "$candidate"
  done < <(find -P "$NEF_WATCH_STATE_DIR" -mindepth 1 -maxdepth 1 \
    \( -name 'wine-template-*' -o -name '.wine-template.*' \) -print0)
}

prepare_app_directory() {
  local path=$1
  if [[ -e "$path" || -L "$path" ]]; then
    [[ -d "$path" && ! -L "$path" ]] || {
      echo "application state path must be a real directory: $path" >&2
      return 78
    }
  else
    mkdir "$path"
  fi
  [[ "${path%/*}" == "$NEF_WATCH_STATE_DIR" ]] || return 78
  chown "$NEF_WATCH_UID:$NEF_WATCH_GID" "$path"
  chmod 0700 "$path"
}

prepare_runtime_root() {
  local path="$NEF_WATCH_STATE_DIR/nikon-runtime" unsafe=0
  if [[ -e "$path" || -L "$path" ]]; then
    if [[ -L "$path" || ! -d "$path" || "$(stat -Lc %u -- "$path" 2>/dev/null)" != 0 ||
          -n "$(find -P "$path" -maxdepth 0 -perm /022 -print -quit 2>/dev/null)" ]]; then
      unsafe=1
    fi
  fi
  if (( unsafe != 0 )); then
    if [[ -L "$path" || -f "$path" ]]; then
      unlink -- "$path"
    else
      chmod -R u+rwX -- "$path" 2>/dev/null || true
      rm -rf --one-file-system -- "$path"
    fi
  fi
  [[ ! -e "$path" && ! -L "$path" ]] && mkdir "$path"
  [[ -d "$path" && ! -L "$path" ]] || {
    echo "Nikon runtime root is unsafe: $path" >&2
    return 78
  }
  chown 0:0 "$path"
  chmod 0750 "$path"
  printf '%s' "$path"
}

start_init_xvfb() {
  local display=$1 log=$2
  as_app env DISPLAY="$display" XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" \
    Xvfb "$display" -screen 0 1024x768x24 -nolisten tcp >"$log" 2>&1 &
  INIT_XVFB_PID=$!
  for _ in {1..100}; do
    kill -0 "$INIT_XVFB_PID" 2>/dev/null || return 1
    if as_app xdpyinfo -display "$display" >/dev/null 2>&1; then return 0; fi
    sleep 0.1
  done
  return 1
}

create_wine_template() (
  local template=$1 staging="" display=:98 log=/tmp/xvfb-init.log status
  cleanup_template_build() {
    local exit_status=$?
    trap - EXIT
    [[ -z "${INIT_XVFB_PID:-}" ]] || {
      kill -TERM "$INIT_XVFB_PID" 2>/dev/null || true
      wait "$INIT_XVFB_PID" 2>/dev/null || true
    }
    [[ -z "$staging" ]] || remove_managed_wine_path "$staging" || exit_status=74
    exit "$exit_status"
  }
  trap cleanup_template_build EXIT
  if [[ -e "$template" || -L "$template" ]]; then
    echo "sealed Wine template failed integrity validation: $template" >&2
    return 78
  fi
  staging="$(mktemp -d "$NEF_WATCH_STATE_DIR/.wine-template.XXXXXXXX")"
  chown "$NEF_WATCH_UID:$NEF_WATCH_GID" "$staging"
  mkdir -p "$XDG_RUNTIME_DIR"
  chown "$NEF_WATCH_UID:$NEF_WATCH_GID" "$XDG_RUNTIME_DIR"
  chmod 0700 "$XDG_RUNTIME_DIR"
  INIT_XVFB_PID=""
  if ! start_init_xvfb "$display" "$log"; then
    echo "temporary Xvfb failed during Wine template creation" >&2
    sed -n '1,80p' "$log" >&2 || true
    return 70
  fi
  set +e
  as_app env WINEPREFIX="$staging" DISPLAY="$display" \
    /usr/local/libexec/nef-watch-startup-timeout \
      "$NEF_WATCH_WINEBOOT_TIMEOUT" "$NEF_WATCH_STARTUP_KILL_GRACE" \
      wineboot --init >/tmp/wineboot.log 2>&1
  status=$?
  set -e
  [[ "$status" -eq 0 ]] || { kill -TERM "$INIT_XVFB_PID" 2>/dev/null || true; sed -n '1,120p' /tmp/wineboot.log >&2; return 70; }
  set +e
  as_app env WINEPREFIX="$staging" DISPLAY="$display" \
    /usr/local/libexec/nef-watch-startup-timeout \
      "$NEF_WATCH_VC_REDIST_TIMEOUT" "$NEF_WATCH_STARTUP_KILL_GRACE" \
      wine /opt/microsoft/VC_redist.x64.exe /install /quiet /norestart \
      >/tmp/vc-redist.log 2>&1
  status=$?
  set -e
  [[ "$status" -eq 0 || "$status" -eq 194 ]] || {
    kill -TERM "$INIT_XVFB_PID" 2>/dev/null || true; sed -n '1,160p' /tmp/vc-redist.log >&2; return 70;
  }
  as_app env WINEPREFIX="$staging" \
    /usr/local/libexec/nef-watch-startup-timeout \
      "$NEF_WATCH_WINESERVER_TIMEOUT" "$NEF_WATCH_STARTUP_KILL_GRACE" \
      wineserver -w >/dev/null 2>&1
  kill -TERM "$INIT_XVFB_PID" 2>/dev/null || true
  wait "$INIT_XVFB_PID" 2>/dev/null || true
  INIT_XVFB_PID=""
  find "$staging/drive_c/windows/system32" -maxdepth 1 -type f \
    -iname msvcp140.dll -print -quit | grep -q . || {
      echo "Wine template contains no native msvcp140.dll" >&2; return 70;
    }
  if [[ "$NIKON_RUNTIME_DIR" != /usr ]]; then
    profiles_dir="$staging/drive_c/Program Files/Common Files/Nikon/Profiles"
    mkdir -p -- "$profiles_dir"
    cp -R --reflink=auto --no-preserve=ownership -- \
      "$NIKON_RUNTIME_DIR/Profiles/." "$profiles_dir/"
  fi
  find -P "$staging/dosdevices" -mindepth 1 -maxdepth 1 -exec rm -f -- {} +
  ln -s ../drive_c "$staging/dosdevices/c:"
  printf '%s\n' "$NEF_WATCH_WINE_SCHEMA" > "$staging/.nef-watch-template-ready"
  seal_tree "$staging"
  mv -T "$staging" "$template"
  staging=""
  chmod 0555 "$NEF_WATCH_STATE_DIR"
)

if [[ "${NEF_WATCH_INIT_COMPLETE:-0}" != 1 ]]; then
  [[ "$(id -u)" -eq 0 ]] || { echo "privileged initialization must start as uid 0" >&2; exit 78; }
  mkdir -p "$NEF_WATCH_STATE_DIR"
  [[ ! -L "$NEF_WATCH_STATE_DIR" ]] || { echo "state root must not be a symlink" >&2; exit 78; }
  chown 0:0 "$NEF_WATCH_STATE_DIR"
  chmod 0755 "$NEF_WATCH_STATE_DIR"
  # Lock the already-validated directory inode itself. This avoids opening a
  # potentially attacker-planted lock file left by an older image.
  exec 9<"$NEF_WATCH_STATE_DIR"
  flock -w "$NEF_WATCH_INIT_LOCK_TIMEOUT" 9 || {
    echo "timed out waiting for the serialized initialization lock" >&2
    exit 75
  }
  prepare_app_directory "$NEF_WATCH_APP_STATE_DIR"
  prepare_app_directory "$NEF_WATCH_APP_HOME"
  as_app test -w "$TMPDIR" || { echo "NVMe work mount is not writable by uid $NEF_WATCH_UID" >&2; exit 73; }

  if [[ "${1:-}" != "--wine-isolation-smoke" ]]; then
    /usr/local/bin/python3 -I /usr/local/libexec/nef-watch-validate-storage.py --container \
      --input /input --output /output --temp "$TMPDIR" --sdk "$NIKON_SDK_DIR" \
      --state "$NEF_WATCH_APP_STATE_DIR" \
      "${storage_acceptance_args[@]}" \
      --max-temp-filesystem-bytes "${NEF_WATCH_TEMP_CAPACITY_BYTES:?set NEF_WATCH_TEMP_CAPACITY_BYTES}" \
      --min-temp-filesystem-free-bytes "${NEF_WATCH_TEMP_MIN_FREE_BYTES:?set NEF_WATCH_TEMP_MIN_FREE_BYTES}"
  fi

  prune_obsolete_wine_state

  runtime_root="$(prepare_runtime_root)"
  find -P "$runtime_root" -xdev -type d -exec chmod 0750 {} +

  if [[ "${1:-}" == "--wine-isolation-smoke" ]]; then
    NIKON_RUNTIME_DIR=/usr
  else
    set +e
    NIKON_RUNTIME_DIR="$(/usr/local/libexec/nef-watch-startup-timeout \
      "$NEF_WATCH_SDK_BOOTSTRAP_TIMEOUT" "$NEF_WATCH_STARTUP_KILL_GRACE" \
      /usr/local/bin/python3 -I /usr/local/libexec/nef-watch-bootstrap-sdk.py)"
    bootstrap_status=$?
    set -e
    [[ "$bootstrap_status" -eq 0 ]] || { echo "Nikon SDK bootstrap failed (rc=$bootstrap_status)" >&2; exit 70; }
    seal_tree "$runtime_root" "$NIKON_RUNTIME_DIR/nef_render.exe"
  fi

  if ! template_is_valid "$NEF_WATCH_WINE_TEMPLATE"; then
    create_wine_template "$NEF_WATCH_WINE_TEMPLATE"
  fi
  template_is_valid "$NEF_WATCH_WINE_TEMPLATE" || { echo "Wine template sealing failed" >&2; exit 78; }

  flock -u 9
  exec 9>&-
  if [[ "${1:-}" == "--initialize-only" ]]; then
    [[ "$#" -eq 1 ]] || { echo "--initialize-only takes no arguments" >&2; exit 64; }
    echo "nef-watch initialization: PASS"
    exit 0
  fi
  exec_as_app env NEF_WATCH_INIT_COMPLETE=1 \
    NIKON_RUNTIME_DIR="$NIKON_RUNTIME_DIR" "$0" "$@"
fi

[[ "$(id -u)" == "$NEF_WATCH_UID" && "$(id -g)" == "$NEF_WATCH_GID" ]] || {
  echo "runtime privilege drop did not reach the configured uid/gid" >&2; exit 78;
}
while read -r key value _; do
  case "$key" in
    CapEff:|CapPrm:|CapBnd:) [[ "$value" == 0000000000000000 ]] || { echo "$key was not cleared" >&2; exit 78; } ;;
    NoNewPrivs:) [[ "$value" == 1 ]] || { echo "no_new_privs was not set" >&2; exit 78; } ;;
  esac
done < /proc/self/status

if [[ "$production_acceptance" == 1 ]]; then
  /usr/local/bin/python3 -I /usr/local/libexec/nef-watch-validate-storage.py --container --runtime \
    --input /input --output /output --temp "$TMPDIR" \
    --state "$NEF_WATCH_APP_STATE_DIR" \
    --acceptance-report /run/nef-watch-acceptance.json \
    --max-temp-filesystem-bytes "${NEF_WATCH_TEMP_CAPACITY_BYTES:?set NEF_WATCH_TEMP_CAPACITY_BYTES}" \
    --min-temp-filesystem-free-bytes "${NEF_WATCH_TEMP_MIN_FREE_BYTES:?set NEF_WATCH_TEMP_MIN_FREE_BYTES}"
fi

if [[ "${1:-}" == "--wine-isolation-smoke" ]]; then
  exec /app/tool/nef_render_wine.sh --isolation-smoke
fi

set +e
NIKON_RUNTIME_DIR="$(/usr/local/bin/python3 -I \
  /usr/local/libexec/nef-watch-bootstrap-sdk.py --resolve-active-runtime)"
runtime_status=$?
set -e
if [[ "$runtime_status" -ne 0 || ! "$NIKON_RUNTIME_DIR" =~ /nikon-runtime/sdk- ]]; then
  echo "sealed active Nikon runtime validation failed (rc=$runtime_status)" >&2
  exit 78
fi
export NIKON_RUNTIME_DIR

validate_single_job_arguments() {
  local argument expect_value=0
  for argument in "$@"; do
    if (( expect_value != 0 )); then
      [[ "$argument" == 1 ]] || {
        echo "container --jobs must be exactly 1 for private X server isolation" >&2
        return 64
      }
      expect_value=0
      continue
    fi
    case "$argument" in
      --jobs|-j) expect_value=1 ;;
      --jobs=1|-j1) ;;
      --jobs=*|-j*|--j*)
        echo "container --jobs must be exactly 1 for private X server isolation" >&2
        return 64
        ;;
      --)
        echo "the container entrypoint does not accept an option terminator" >&2
        return 64
        ;;
    esac
  done
  (( expect_value == 0 )) || {
    echo "container --jobs requires the value 1" >&2
    return 64
  }
}
validate_single_job_arguments "$@"

# Every render gets authenticated Wine and Xvfb sibling Landlock domains. With
# no shared display process to supervise, exec lets unprivileged Tini deliver
# Docker's stop signal directly to the watcher's cooperative drain handler.
exec /usr/local/bin/python3 -I /app/tool/nef_watch.py \
  --render-bin /app/tool/nef_render_wine.sh \
  --profile "$NIKON_RUNTIME_DIR/Profiles/NKsRGB.icm" "$@" --jobs 1
