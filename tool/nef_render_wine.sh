#!/usr/bin/env bash
# Run one Nikon SDK render in a disposable Wine prefix and Landlock allowlist.
set -euo pipefail
umask 077

if [[ $# -lt 3 && "${1:-}" != "--isolation-smoke" ]]; then
  echo "usage: $0 <input.nef> <output.raw> <profile.icm> [bits=8] [expcomp_ev=0]" >&2
  exit 1
fi
if [[ $# -gt 5 && "${1:-}" != "--isolation-smoke" ]]; then
  echo "too many renderer arguments" >&2
  exit 1
fi

SMOKE_MODE=0
if [[ "${1:-}" == "--isolation-smoke" ]]; then
  [[ $# -eq 1 ]] || { echo "--isolation-smoke takes no arguments" >&2; exit 64; }
  SMOKE_MODE=1
fi

absolute_path() {
  local path="$1"
  if [[ "$path" != /* ]]; then
    path="$PWD/$path"
  fi
  printf '%s' "$path"
}

require_real_directory() {
  local label="$1" candidate="$2" resolved
  candidate="$(absolute_path "$candidate")"
  if [[ -L "$candidate" || ! -d "$candidate" ]]; then
    echo "$label must be a real directory: $candidate" >&2
    return 72
  fi
  resolved="$(realpath "$candidate")"
  [[ -n "$resolved" && "$resolved" == /* ]] || return 72
  printf '%s' "$resolved"
}

RUNTIME_DIR="${NIKON_RUNTIME_DIR:-/var/lib/nef-watch/nikon-runtime/current}"
WINE_SCHEMA="${NEF_WATCH_WINE_SCHEMA:-wine8-deb12-vc14-cc0ff0eb1dc3-landlock6-v3}"
WINE_TEMPLATE="${NEF_WATCH_WINE_TEMPLATE:-/var/lib/nef-watch/wine-template-$WINE_SCHEMA}"
LANDLOCK_EXEC="${NEF_WATCH_LANDLOCK_EXEC:-/usr/local/libexec/nef-watch-landlock-exec.py}"
WINE_SANDBOX_HELPER="${NEF_WATCH_WINE_SANDBOX_HELPER:-/app/tool/nef_wine_sandbox.sh}"
RENDER_SUPERVISOR="${NEF_WATCH_RENDER_SUPERVISOR:-/usr/local/libexec/nef-watch-render-supervisor.py}"
WORK_ROOT="$(require_real_directory "render temporary directory" \
  "${NEF_WATCH_TEMP_DIR:-${TMPDIR:-/work/nef-watch}}")"
WINE_TEMPLATE="$(require_real_directory "sealed Wine template" "$WINE_TEMPLATE")"
X_SOCKET_DIR="$(require_real_directory "private X socket directory" \
  "${NEF_WATCH_X_SOCKET_DIR:-/tmp/.X11-unix}")"

if [[ "${NEF_WATCH_REQUIRE_LANDLOCK:-1}" != "0" && \
      "$X_SOCKET_DIR" != /tmp/.X11-unix ]]; then
  echo "required Landlock mode uses only the dedicated /tmp/.X11-unix namespace" >&2
  exit 77
fi
export NEF_WATCH_X_SOCKET_DIR="$X_SOCKET_DIR"

if [[ ! -w "$WORK_ROOT" || ! -x "$WORK_ROOT" ]]; then
  echo "render temporary directory is not writable and traversable: $WORK_ROOT" >&2
  exit 73
fi

if [[ "$SMOKE_MODE" -eq 0 ]]; then
  RUNTIME_DIR="$(require_real_directory "Nikon runtime directory" "$RUNTIME_DIR")"
  RENDER_EXE="${NIKON_RENDER_EXE:-$RUNTIME_DIR/nef_render.exe}"
  render_candidate="$(absolute_path "$RENDER_EXE")"
  if [[ -L "$render_candidate" || ! -f "$render_candidate" ]]; then
    echo "Windows render adapter is missing or is a symbolic link: $RENDER_EXE" >&2
    exit 127
  fi
  RENDER_EXE="$(realpath "$render_candidate")"
  if [[ "${RENDER_EXE%/*}" != "$RUNTIME_DIR" ||
        ( "${NEF_WATCH_TEST_ALLOW_UNSEALED_TEMPLATE:-0}" != 1 &&
          ( -w "$RENDER_EXE" || -w "$RUNTIME_DIR" ||
            "$(stat -Lc %u -- "$RUNTIME_DIR")" != 0 ) ) ]]; then
    echo "Windows render adapter must be sealed directly inside $RUNTIME_DIR" >&2
    exit 127
  fi
else
  RUNTIME_DIR=/usr
  RENDER_EXE=""
fi

if [[ -n "${WINE_BIN:-}" ]]; then
  WINE_COMMAND="$(absolute_path "$WINE_BIN")"
elif command -v wine64 >/dev/null 2>&1; then
  WINE_COMMAND="$(command -v wine64)"
elif [[ -x /usr/lib/wine/wine64 ]]; then
  WINE_COMMAND=/usr/lib/wine/wine64
elif command -v wine >/dev/null 2>&1; then
  WINE_COMMAND="$(command -v wine)"
else
  echo "wine64 is not installed" >&2
  exit 127
fi
if [[ ! -x "$WINE_COMMAND" ]]; then
  echo "Wine command is not executable: $WINE_COMMAND" >&2
  exit 127
fi

if [[ "${NEF_WATCH_REQUIRE_LANDLOCK:-1}" != "0" ]]; then
  if [[ "$LANDLOCK_EXEC" != /usr/local/libexec/nef-watch-landlock-exec.py ||
        ! -x "$LANDLOCK_EXEC" || -w "$LANDLOCK_EXEC" ]]; then
    echo "required sealed Landlock launcher is unavailable: $LANDLOCK_EXEC" >&2
    exit 77
  fi
  if [[ "$WINE_COMMAND" != /usr/* && "$WINE_COMMAND" != /bin/* ]]; then
    echo "required Landlock mode accepts Wine only from the sealed system image" >&2
    exit 77
  fi
  if [[ "$WINE_SANDBOX_HELPER" != /app/tool/nef_wine_sandbox.sh ||
        ! -x "$WINE_SANDBOX_HELPER" || -w "$WINE_SANDBOX_HELPER" ]]; then
    echo "required sealed Wine sandbox helper is unavailable: $WINE_SANDBOX_HELPER" >&2
    exit 77
  fi
  if [[ "$RENDER_SUPERVISOR" != /usr/local/libexec/nef-watch-render-supervisor.py ||
        ! -x "$RENDER_SUPERVISOR" || -w "$RENDER_SUPERVISOR" ]]; then
    echo "required sealed render supervisor is unavailable: $RENDER_SUPERVISOR" >&2
    exit 77
  fi
else
  [[ -x "$WINE_SANDBOX_HELPER" ]] || {
    echo "Wine sandbox helper is unavailable: $WINE_SANDBOX_HELPER" >&2
    exit 77
  }
  [[ -x "$RENDER_SUPERVISOR" ]] || {
    echo "render supervisor is unavailable: $RENDER_SUPERVISOR" >&2
    exit 77
  }
fi
export NEF_WATCH_LANDLOCK_EXEC="$LANDLOCK_EXEC"

stat_identity() {
  stat -Lc '%d:%i:%s:%Y:%Z:%u:%h:%F' -- "$1"
}

require_private_regular_file() {
  local label="$1" candidate="$2" expected_parent="$3" resolved metadata
  candidate="$(absolute_path "$candidate")"
  if [[ -L "$candidate" || ! -f "$candidate" ]]; then
    echo "$label is missing, not regular, or is a symbolic link: $candidate" >&2
    return 66
  fi
  resolved="$(realpath "$candidate")"
  if [[ "${resolved%/*}" != "$expected_parent" ]]; then
    echo "$label must be a direct child of $expected_parent" >&2
    return 66
  fi
  metadata="$(stat -Lc '%u:%h' -- "$resolved")"
  if [[ "$metadata" != "$(id -u):1" ]]; then
    echo "$label must be owned by uid $(id -u) with one hard link: $resolved" >&2
    return 66
  fi
  printf '%s' "$resolved"
}

require_sealed_profile() {
  local candidate resolved metadata
  candidate="$(absolute_path "$1")"
  if [[ -L "$candidate" || ! -f "$candidate" ]]; then
    echo "ICC profile is missing, not regular, or is a symbolic link: $candidate" >&2
    return 66
  fi
  resolved="$(realpath "$candidate")"
  case "$resolved" in
    "$RUNTIME_DIR"/Profiles/*) ;;
    *) echo "ICC profile must be beneath the sealed runtime Profiles directory" >&2; return 66 ;;
  esac
  metadata="$(stat -Lc '%u:%h' -- "$resolved")"
  local expected_owner=0
  [[ "${NEF_WATCH_TEST_ALLOW_UNSEALED_TEMPLATE:-0}" != 1 ]] || expected_owner="$(id -u)"
  if [[ "$metadata" != "$expected_owner:1" ||
        ( "${NEF_WATCH_TEST_ALLOW_UNSEALED_TEMPLATE:-0}" != 1 && -w "$resolved" ) ]]; then
    echo "ICC profile is not a sealed root-owned single-link file: $resolved" >&2
    return 66
  fi
  printf '%s' "$resolved"
}

sha256_file() {
  sha256sum -- "$1" | awk '{print $1}'
}

abandoned_job_minutes() {
  local render_timeout="${NEF_WATCH_RENDER_TIMEOUT:-300}"
  local kill_grace="${NEF_WATCH_KILL_GRACE_SECONDS:-5}"
  if [[ ! "$render_timeout" =~ ^[0-9]+([.][0-9]+)?$ || \
        ! "$kill_grace" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "render timeout and kill grace must be non-negative seconds" >&2
    return 64
  fi
  awk -v render="$render_timeout" -v grace="$kill_grace" \
    'BEGIN { printf "%d", (render + grace + 300 + 59) / 60 }'
}

safe_remove_job() {
  local path="$1" base metadata
  base="${path##*/}"
  if [[ "$path" != "$WORK_ROOT"/nef-watch-job.* || \
        ! "$base" =~ ^nef-watch-job\.[A-Za-z0-9]{8,}$ || \
        -L "$path" || ! -d "$path" ]]; then
    echo "refusing to remove unsafe Wine job path: $path" >&2
    return 74
  fi
  metadata="$(stat -Lc '%u:%F' -- "$path")"
  if [[ "$metadata" != "$(id -u):directory" ]]; then
    echo "refusing to remove unowned Wine job path: $path" >&2
    return 74
  fi
  chmod -R u+rwX -- "$path" 2>/dev/null || true
  rm -rf --one-file-system -- "$path"
  [[ ! -e "$path" && ! -L "$path" ]]
}

cleanup_abandoned_jobs() {
  local stale_minutes lock candidate cleanup_failed=0
  stale_minutes="$(abandoned_job_minutes)"
  lock="$WORK_ROOT/.nef-watch-temp-cleanup.lock"
  if [[ -L "$lock" || ( -e "$lock" && ! -f "$lock" ) ]]; then
    echo "unsafe temporary cleanup lock: $lock" >&2
    return 74
  fi
  command -v flock >/dev/null 2>&1 || {
    echo "flock is required for safe temporary cleanup" >&2
    return 70
  }
  (
    flock -x 9
    while IFS= read -r -d '' candidate; do
      safe_remove_job "$candidate" || cleanup_failed=1
    done < <(
      find -P "$WORK_ROOT" -xdev -mindepth 1 -maxdepth 1 -type d \
        -user "$(id -u)" -mmin "+$stale_minutes" -name 'nef-watch-job.*' -print0
    )
    (( cleanup_failed == 0 )) || exit 74
  ) 9>>"$lock"
  chmod 0600 "$lock"
}

clean_x_socket_namespace() {
  local foreign residual
  if [[ -L "$X_SOCKET_DIR" || ! -d "$X_SOCKET_DIR" ]]; then
    echo "private X socket namespace is unavailable: $X_SOCKET_DIR" >&2
    return 74
  fi
  # This is a dedicated bounded tmpfs and all renders are serialized.  Once
  # the subreaper drains a job, its uid-owned entries are disposable.  Never
  # remove an entry owned by a different uid.
  # Only directories need a mode repair before the bottom-up deletion.  With
  # find -P, `-type d` excludes symlinks; passing a symlink to chmod would
  # dereference it and could modify a target outside this namespace.
  if ! find -P "$X_SOCKET_DIR" -xdev -mindepth 1 -type d -user "$(id -u)" \
    -exec chmod u+rwx -- {} \;; then
    echo "cannot traverse private X socket namespace for cleanup" >&2
    return 74
  fi
  if ! foreign="$(find -P "$X_SOCKET_DIR" -xdev -mindepth 1 \
    ! -user "$(id -u)" -print -quit)"; then
    echo "cannot inspect private X socket namespace ownership" >&2
    return 74
  fi
  if [[ -n "$foreign" ]]; then
    echo "private X socket namespace contains a foreign-owned entry: $foreign" >&2
    return 74
  fi
  if ! find -P "$X_SOCKET_DIR" -xdev -mindepth 1 -depth -delete; then
    echo "cannot delete private X socket namespace artifacts" >&2
    return 74
  fi
  if ! residual="$(find -P "$X_SOCKET_DIR" -xdev -mindepth 1 -print -quit)"; then
    echo "cannot verify private X socket namespace cleanup" >&2
    return 74
  fi
  if [[ -n "$residual" ]]; then
    echo "private X socket namespace did not drain: $residual" >&2
    return 74
  fi
}

clean_private_tmp_namespace() {
  local foreign residual
  # Required isolation always supplies /tmp as a fresh, bounded container
  # tmpfs, with the X socket directory mounted as a separate nested tmpfs.
  # Xvfb refuses -nolock for an unprivileged uid and therefore needs to create
  # its display lock directly in /tmp.  Scrub every other uid-owned entry before
  # and after the serialized render so a hostile renderer cannot persist poison
  # or consume the bounded namespace across jobs.
  if [[ -L /tmp || ! -d /tmp || "$(stat -Lc '%u:%a' -- /tmp)" != 0:1777 ]]; then
    echo "private /tmp namespace is not the expected root-owned 1777 directory" >&2
    return 74
  fi
  if ! find -P /tmp -xdev -mindepth 1 ! -path "$X_SOCKET_DIR" \
    -type d -user "$(id -u)" -exec chmod u+rwx -- {} \;; then
    echo "cannot traverse private /tmp namespace for cleanup" >&2
    return 74
  fi
  if ! foreign="$(find -P /tmp -xdev -mindepth 1 ! -path "$X_SOCKET_DIR" \
    ! -user "$(id -u)" -print -quit)"; then
    echo "cannot inspect private /tmp namespace ownership" >&2
    return 74
  fi
  if [[ -n "$foreign" ]]; then
    echo "private /tmp namespace contains a foreign-owned entry: $foreign" >&2
    return 74
  fi
  if ! find -P /tmp -xdev -mindepth 1 ! -path "$X_SOCKET_DIR" \
    -depth -delete; then
    echo "cannot delete private /tmp artifacts" >&2
    return 74
  fi
  if ! residual="$(find -P /tmp -xdev -mindepth 1 ! -path "$X_SOCKET_DIR" \
    -print -quit)"; then
    echo "cannot verify private /tmp cleanup" >&2
    return 74
  fi
  if [[ -n "$residual" ]]; then
    echo "private /tmp namespace did not drain: $residual" >&2
    return 74
  fi
}

read_unsigned_file() {
  local value
  [[ -r "$1" ]] || return 1
  IFS= read -r value < "$1" || [[ -n "$value" ]] || return 1
  [[ "$value" =~ ^[0-9]+$ ]] || return 1
  printf '%s' "$value"
}

configure_render_memory() {
  local total="${NEF_WATCH_RENDER_MEMORY_MIB:-1536}"
  local sdk="${NEF_WATCH_SDK_MEMORY_MIB:-512}"
  local jobs="${NEF_WATCH_RENDER_JOBS:-1}"
  if [[ ! "$total" =~ ^[0-9]+$ ]] || (( total < 768 || total > 16384 )); then
    echo "NEF_WATCH_RENDER_MEMORY_MIB must be a total per-job budget from 768 to 16384" >&2
    return 64
  fi
  if [[ ! "$sdk" =~ ^[0-9]+$ ]] || (( sdk < 256 || sdk > 4096 )); then
    echo "NEF_WATCH_SDK_MEMORY_MIB must be an integer from 256 to 4096" >&2
    return 64
  fi
  if [[ "$jobs" != 1 ]]; then
    echo "NEF_WATCH_RENDER_JOBS must be exactly 1 for X server isolation" >&2
    return 64
  fi

  local cgroup_root="${NEF_WATCH_CGROUP_ROOT:-/sys/fs/cgroup}"
  local available=0 limit=0 usage=0 host_available=0 value
  if [[ -r "$cgroup_root/memory.max" ]]; then
    IFS= read -r value < "$cgroup_root/memory.max" || [[ -n "$value" ]] || value=""
    if [[ "$value" =~ ^[0-9]+$ ]]; then
      limit="$value"
      usage="$(read_unsigned_file "$cgroup_root/memory.current" || printf '0')"
    fi
  elif [[ -r "$cgroup_root/memory/memory.limit_in_bytes" ]]; then
    limit="$(read_unsigned_file "$cgroup_root/memory/memory.limit_in_bytes" || printf '0')"
    usage="$(read_unsigned_file "$cgroup_root/memory/memory.usage_in_bytes" || printf '0')"
    (( limit < 1152921504606846976 )) || limit=0
  fi
  if (( limit > usage )); then
    available=$((limit - usage))
  elif [[ -r /proc/meminfo ]]; then
    host_available="$(awk '/^MemAvailable:/ { printf "%.0f", $2 * 1024; exit }' \
      /proc/meminfo 2>/dev/null || printf '0')"
    [[ "$host_available" =~ ^[0-9]+$ ]] && available="$host_available"
  fi

  if (( available > 0 )); then
    # The total budget includes Nikon's VM arena, the returned pixel buffer, and
    # Wine/adapter overhead. Keep a separate 384 MiB for the watcher and Xvfb.
    local shared_reserve=$((384 * 1024 * 1024)) per_job_mib=0
    if (( available > shared_reserve )); then
      per_job_mib=$(((available - shared_reserve) / 1024 / 1024))
    fi
    if (( per_job_mib < 768 )); then
      echo "insufficient cgroup memory: less than 768 MiB total per render job" >&2
      return 70
    fi
    (( total <= per_job_mib )) || total="$per_job_mib"
  fi

  local sdk_cap=$((total - 256 - 256))
  (( sdk <= sdk_cap )) || sdk="$sdk_cap"
  if (( sdk < 256 )); then
    echo "total render budget leaves less than 256 MiB for the Nikon SDK" >&2
    return 70
  fi
  export NEF_WATCH_RENDER_MEMORY_MIB="$total"
  export NEF_WATCH_SDK_MEMORY_MIB="$sdk"
}

acquire_single_render_lock() {
  local lock="$WORK_ROOT/.nef-watch-single-render.lock" metadata
  if [[ -L "$lock" || ( -e "$lock" && ! -f "$lock" ) ]]; then
    echo "unsafe single-render lock: $lock" >&2
    return 74
  fi
  exec 8>>"$lock"
  metadata="$(stat -Lc '%u:%h' -- "$lock")"
  if [[ -L "$lock" || ! -f "$lock" || "$metadata" != "$(id -u):1" ]]; then
    echo "single-render lock must be an owned one-link regular file" >&2
    return 74
  fi
  chmod 0600 "$lock"
  flock -n -x 8 || {
    echo "a sibling render is already active; refusing shared X socket namespace" >&2
    return 75
  }
}

configure_render_file_limit() {
  local file_size_mib="${NEF_WATCH_RENDER_FILE_SIZE_MIB:-2048}"
  if [[ ! "$file_size_mib" =~ ^[0-9]+$ ]] || \
     (( file_size_mib < 768 || file_size_mib > 2048 )); then
    echo "NEF_WATCH_RENDER_FILE_SIZE_MIB must be an integer from 768 to 2048" >&2
    return 64
  fi
  PRLIMIT_BIN=/usr/bin/prlimit
  if [[ ! -x "$PRLIMIT_BIN" || -w "$PRLIMIT_BIN" ]]; then
    echo "sealed prlimit executable is unavailable: $PRLIMIT_BIN" >&2
    return 70
  fi
  RENDER_FILE_SIZE_BYTES=$((file_size_mib * 1024 * 1024))
  export NEF_WATCH_RENDER_FILE_SIZE_MIB="$file_size_mib"
}

validate_wine_template() {
  local marker="$WINE_TEMPLATE/.nef-watch-template-ready" mapping mappings=0 unsafe
  [[ "$(cat "$marker" 2>/dev/null)" == "$WINE_SCHEMA" ]] || {
    echo "Wine template marker does not match schema $WINE_SCHEMA" >&2
    return 78
  }
  [[ -d "$WINE_TEMPLATE/dosdevices" && ! -L "$WINE_TEMPLATE/dosdevices" ]] || return 78
  [[ "$(readlink "$WINE_TEMPLATE/dosdevices/c:")" == ../drive_c ]] || return 78
  while IFS= read -r -d '' mapping; do
    [[ "${mapping##*/}" == "c:" ]] || {
      echo "Wine template exposes a DOS mapping other than C:" >&2
      return 78
    }
    (( mappings += 1 ))
  done < <(find -P "$WINE_TEMPLATE/dosdevices" -mindepth 1 -maxdepth 1 -print0)
  [[ "$mappings" -eq 1 ]] || return 78
  if [[ "${NEF_WATCH_TEST_ALLOW_UNSEALED_TEMPLATE:-0}" == "1" ]]; then
    [[ -f "$marker" ]] || return 78
  elif [[ -w "$WINE_TEMPLATE" || ! -f "$marker" || -L "$marker" || -w "$marker" ]]; then
    echo "Wine template is not sealed: $WINE_TEMPLATE" >&2
    return 78
  elif [[ "$(stat -Lc '%u:%h' -- "$marker")" != "0:1" ]]; then
    echo "Wine template marker is not root-owned and single-link" >&2
    return 78
  else
    unsafe="$(find -P "$WINE_TEMPLATE" -xdev \
      \( ! -user root -o \( ! -type l -perm /022 \) \) -print -quit)"
    if [[ -n "$unsafe" ]]; then
      echo "Wine template contains an unsealed entry: $unsafe" >&2
      return 78
    fi
  fi

  if [[ "$SMOKE_MODE" -eq 0 ]]; then
    local source="$RUNTIME_DIR/Profiles"
    local installed="$WINE_TEMPLATE/drive_c/Program Files/Common Files/Nikon/Profiles"
    local source_file target_file source_count=0 installed_count=0 unexpected
    [[ -d "$installed" && ! -L "$installed" ]] || {
      echo "Wine template has no private Nikon profile installation" >&2
      return 78
    }
    unexpected="$(find -P "$installed" -mindepth 1 -maxdepth 1 ! -type f -print -quit)"
    [[ -z "$unexpected" ]] || {
      echo "Wine template Nikon profile installation contains a non-file" >&2
      return 78
    }
    while IFS= read -r -d '' source_file; do
      (( source_count += 1 ))
      target_file="$installed/${source_file##*/}"
      [[ -f "$target_file" && ! -L "$target_file" ]] && \
        cmp -s -- "$source_file" "$target_file" || {
          echo "Wine template Nikon profiles do not match the sealed runtime" >&2
          return 78
        }
      if [[ "${NEF_WATCH_TEST_ALLOW_UNSEALED_TEMPLATE:-0}" != 1 ]] &&
         [[ "$(stat -Lc '%u:%a:%h' -- "$source_file")" != 0:444:1 ||
            "$(stat -Lc '%u:%a:%h' -- "$target_file")" != 0:444:1 ]]; then
        echo "Wine template Nikon profiles are not sealed root-owned files" >&2
        return 78
      fi
    done < <(find -P "$source" -mindepth 1 -maxdepth 1 -type f -print0)
    while IFS= read -r -d '' target_file; do
      (( installed_count += 1 ))
    done < <(find -P "$installed" -mindepth 1 -maxdepth 1 -type f -print0)
    (( source_count > 0 && source_count == installed_count )) || {
      echo "Wine template Nikon profile set is incomplete" >&2
      return 78
    }
  fi
}

configure_render_memory
acquire_single_render_lock
cleanup_abandoned_jobs
if [[ "${NEF_WATCH_REQUIRE_LANDLOCK:-1}" != 0 ]]; then
  clean_private_tmp_namespace
fi
clean_x_socket_namespace
configure_render_file_limit
validate_wine_template

source_path=""
raw_path=""
profile_path=""
source_hash=""
source_identity=""
if [[ "$SMOKE_MODE" -eq 0 ]]; then
  source_path="$(require_private_regular_file "input snapshot" "$1" "$WORK_ROOT")"
  raw_path="$(require_private_regular_file "raw output placeholder" "$2" "$WORK_ROOT")"
  profile_path="$(require_sealed_profile "$3")"
  source_identity="$(stat_identity "$source_path")"
  source_hash="$(sha256_file "$source_path")"
  # The watcher pre-creates this placeholder. Remove only the validated
  # single-link file; the Windows adapter then uses CREATE_NEW and never follows
  # an attacker-supplied reparse point or symlink.
  rm -f -- "$raw_path"
  if [[ -e "$raw_path" || -L "$raw_path" ]]; then
    echo "cannot remove raw output placeholder safely: $raw_path" >&2
    exit 74
  fi
fi

job_root="$(mktemp -d "$WORK_ROOT/nef-watch-job.XXXXXXXX")"
chmod 0700 "$job_root"
trap 'status=$?; trap - EXIT; safe_remove_job "$job_root" || status=74; exit "$status"' EXIT
mkdir -m 0700 "$job_root/input" "$job_root/output" "$job_root/swap" \
  "$job_root/prefix" "$job_root/home" "$job_root/xdg"
touch "$job_root/.nef-watch-job-owner"
chmod 0600 "$job_root/.nef-watch-job-owner"

private_input=""
if [[ "$SMOKE_MODE" -eq 0 ]]; then
  private_input="$job_root/input/source.${source_path##*.}"
  cp --reflink=auto --no-dereference --no-preserve=ownership -- \
    "$source_path" "$private_input"
  if [[ -L "$private_input" || ! -f "$private_input" || \
        "$(stat -Lc '%u:%h' -- "$private_input")" != "$(id -u):1" ]]; then
    echo "private input copy is not a safe single-link file" >&2
    exit 74
  fi
  chmod 0400 "$private_input"
  if [[ "$(sha256_file "$private_input")" != "$source_hash" || \
        "$(stat_identity "$source_path")" != "$source_identity" ]]; then
    echo "input snapshot changed while creating the private render copy" >&2
    exit 75
  fi
fi

cp -a --reflink=auto --no-preserve=ownership -- "$WINE_TEMPLATE/." "$job_root/prefix/"
chmod -R u+rwX,go-rwx -- "$job_root/prefix"
dosdevices="$job_root/prefix/dosdevices"
if [[ -L "$dosdevices" || ! -d "$dosdevices" ]]; then
  echo "copied Wine prefix has an unsafe dosdevices directory" >&2
  exit 78
fi
while IFS= read -r -d '' mapping; do
  if [[ ! -L "$mapping" ]]; then
    echo "copied Wine prefix contains a non-link DOS mapping: $mapping" >&2
    exit 78
  fi
  unlink -- "$mapping"
done < <(find -P "$dosdevices" -mindepth 1 -maxdepth 1 -print0)
ln -s ../drive_c "$dosdevices/c:"
ln -s "$job_root" "$dosdevices/t:"
ln -s "$RUNTIME_DIR" "$dosdevices/r:"

validate_job_mappings() {
  local mapping name count=0
  while IFS= read -r -d '' mapping; do
    [[ -L "$mapping" ]] || return 1
    name="${mapping##*/}"
    case "$name" in
      c:) [[ "$(readlink "$mapping")" == ../drive_c ]] || return 1 ;;
      t:) [[ "$(realpath "$mapping")" == "$job_root" ]] || return 1 ;;
      r:) [[ "$(realpath "$mapping")" == "$RUNTIME_DIR" ]] || return 1 ;;
      *) return 1 ;;
    esac
    (( count += 1 ))
  done < <(find -P "$dosdevices" -mindepth 1 -maxdepth 1 -print0)
  [[ "$count" -eq 3 ]]
}

validate_job_mappings || {
  echo "failed to establish narrow C:/T:/R: Wine mappings" >&2
  exit 78
}

export WINEPREFIX="$job_root/prefix"
export HOME="$job_root/home"
export XDG_RUNTIME_DIR="$job_root/xdg"
export TMPDIR="$job_root/swap"
export NEF_WATCH_TEMP_DIR="$job_root"
export NEF_WATCH_WINE_TEMP_DIR='T:\swap'
export TEMP='T:\swap'
export TMP='T:\swap'
export NEF_WATCH_JOB_ROOT="$job_root"

# Give each render a private X server and a fresh 128-bit authorization cookie.
# The helper starts Xvfb in a sibling Landlock domain. Wine cannot signal that
# sibling or the outer watcher, while a compromised X server still cannot reach
# the watcher's input, output, or durable-state mounts.
private_display=$((1000 + ($$ % 50000)))
export DISPLAY=":$private_display"
export XAUTHORITY="$job_root/.Xauthority"
export NEF_WATCH_XVFB_LOG="$job_root/output/xvfb.log"
/usr/local/bin/python3 -I - "$XAUTHORITY" "$private_display" <<'PY'
import os
import struct
import sys

path, display = sys.argv[1:]
fields = (
    (0xFFFF).to_bytes(2, "big"),  # FamilyWild
    b"",
    display.encode("ascii"),
    b"MIT-MAGIC-COOKIE-1",
    os.urandom(16),
)
payload = fields[0] + b"".join(struct.pack(">H", len(value)) + value for value in fields[1:])
descriptor = os.open(
    path,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
    0o600,
)
try:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("short Xauthority write")
        offset += written
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY

swap_path="$(mktemp "$job_root/swap/nkr-nef-watch.XXXXXXXX.tmp")"
chmod 0600 "$swap_path"
export NEF_WATCH_WINE_SWAP_PATH="T:\\swap\\${swap_path##*/}"

wine_pid=""
job_removed=0

drain_wineserver() {
  local timeout_seconds="${NEF_WATCH_WINESERVER_TIMEOUT:-30}"
  local wineserver_bin
  wineserver_bin="$(command -v wineserver || true)"
  [[ -n "$wineserver_bin" ]] || return 0
  timeout --signal=TERM --kill-after=5 "$timeout_seconds" \
    "$wineserver_bin" -k >/dev/null 2>&1 || true
  timeout --signal=TERM --kill-after=5 "$timeout_seconds" \
    "$wineserver_bin" -w >/dev/null 2>&1
}

cleanup_job() {
  local cleanup_status=0
  if (( job_removed != 0 )); then
    return 0
  fi
  drain_wineserver || true
  clean_x_socket_namespace || cleanup_status=74
  if [[ "${NEF_WATCH_REQUIRE_LANDLOCK:-1}" != 0 ]]; then
    clean_private_tmp_namespace || cleanup_status=74
  fi
  safe_remove_job "$job_root" || cleanup_status=74
  job_removed=1
  return "$cleanup_status"
}

cleanup_on_exit() {
  local status=$?
  trap - EXIT
  cleanup_job || status=74
  exit "$status"
}
trap cleanup_on_exit EXIT

forward_signal() {
  local signal="$1" status="$2"
  trap - "$signal"
  if [[ -n "$wine_pid" ]]; then
    kill "-$signal" "$wine_pid" >/dev/null 2>&1 || true
  fi
  drain_wineserver || true
  [[ -z "$wine_pid" ]] || wait "$wine_pid" >/dev/null 2>&1 || true
  exit "$status"
}
trap 'forward_signal TERM 143' TERM
trap 'forward_signal INT 130' INT
trap 'forward_signal HUP 129' HUP

landlock_command=()
if [[ "${NEF_WATCH_REQUIRE_LANDLOCK:-1}" != "0" ]]; then
  landlock_command=(/usr/local/bin/python3 -I "$LANDLOCK_EXEC")
  # Wine spans wineserver and Windows service PIDs. A rule anchored to the
  # launcher's /proc/self inode does not follow that process family and stalls
  # real Nikon renders. Read-only procfs is therefore the narrow compatible
  # boundary; Landlock still checks magic-link targets and prevents this more-
  # restricted domain from ptracing the less-restricted watcher. Sysfs remains
  # outside the renderer domain.
  for read_path in /usr /etc /proc "$RUNTIME_DIR" "$job_root" \
    "$X_SOCKET_DIR"; do
    [[ -e "$read_path" ]] && landlock_command+=(--ro "$read_path")
  done
  for read_path in /lib /lib64; do
    [[ -e "$read_path" ]] && landlock_command+=(--ro "$read_path")
  done
  for write_path in /dev/null /dev/zero /dev/full /dev/random /dev/urandom \
    "$job_root/prefix" "$job_root/output" \
    "$job_root/swap" "$job_root/home" "$job_root/xdg"; do
    [[ -e "$write_path" ]] && landlock_command+=(--rw "$write_path")
  done
  landlock_command+=(--)
else
  echo "warning: Landlock isolation was explicitly disabled" >&2
fi

set +e
resource_command=(
  "$PRLIMIT_BIN"
  "--fsize=${RENDER_FILE_SIZE_BYTES}:${RENDER_FILE_SIZE_BYTES}"
  "--nofile=512:512"
  --
)
# Nikon's OpenLibrary resolves prm.bin and related payloads from the Windows
# process working directory. The sealed R: mapping names this exact directory.
cd -- "$RUNTIME_DIR"
if [[ "$SMOKE_MODE" -eq 1 ]]; then
  /usr/local/bin/python3 -I "$RENDER_SUPERVISOR" -- \
    "${resource_command[@]}" "$WINE_SANDBOX_HELPER" \
    "${landlock_command[@]}" "$WINE_COMMAND" \
    'C:\windows\system32\cmd.exe' /d /c 'echo NEF_WATCH_WINE_SMOKE_OK' &
else
  /usr/local/bin/python3 -I "$RENDER_SUPERVISOR" -- \
    "${resource_command[@]}" "$WINE_SANDBOX_HELPER" \
    "${landlock_command[@]}" "$WINE_COMMAND" 'R:\nef_render.exe' \
    'T:\input\source.'"${source_path##*.}" \
    'T:\output\render.raw' \
    "R:\\Profiles\\${profile_path##*/}" \
    "${4:-8}" "${5:-0}" &
fi
wine_pid=$!
wait "$wine_pid"
wine_status=$?
wine_pid=""
set -e

if ! drain_wineserver; then
  echo "per-render Wine server did not drain cleanly" >&2
  exit 74
fi
if [[ "$wine_status" -ne 0 ]]; then
  exit "$wine_status"
fi
if ! validate_job_mappings; then
  echo "Wine introduced a forbidden DOS mapping during rendering" >&2
  exit 78
fi

if [[ "$SMOKE_MODE" -eq 0 ]]; then
  private_raw="$job_root/output/render.raw"
  if [[ -L "$private_raw" || ! -f "$private_raw" || \
        "$(stat -Lc '%u:%h' -- "$private_raw")" != "$(id -u):1" ]]; then
    echo "renderer did not produce a safe single-link raw output" >&2
    exit 74
  fi
  # Validate both the watcher-owned snapshot and the private copy after Wine has
  # closed them. This detects replacement, mutation, or a same-UID temp race
  # before any pixels are accepted by the watcher.
  if [[ "$(sha256_file "$source_path")" != "$source_hash" || \
        "$(sha256_file "$private_input")" != "$source_hash" || \
        "$(stat_identity "$source_path")" != "$source_identity" ]]; then
    echo "input snapshot changed during Nikon rendering" >&2
    exit 75
  fi
  if [[ -e "$raw_path" || -L "$raw_path" ]]; then
    echo "raw output destination was recreated during rendering" >&2
    exit 75
  fi
  mv -T -- "$private_raw" "$raw_path"
  chmod 0600 "$raw_path"
  if [[ -L "$raw_path" || ! -f "$raw_path" || \
        "$(stat -Lc '%u:%h' -- "$raw_path")" != "$(id -u):1" ]]; then
    echo "raw output publication failed its final safety check" >&2
    exit 74
  fi
fi

exit 0
