#!/usr/bin/env bash
# Translate native Linux paths to Wine's Z: drive and invoke the Windows Nikon
# Image SDK adapter.  The command-line and NKRAW1 output contract match the
# native macOS tool/nef_render helper.
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "usage: $0 <input.nef> <output.raw> <profile.icm> [bits=8] [expcomp_ev=0]" >&2
  exit 1
fi

RUNTIME_DIR="${NIKON_RUNTIME_DIR:-/opt/nikon}"
RENDER_EXE="${NIKON_RENDER_EXE:-$RUNTIME_DIR/nef_render.exe}"

if [[ -n "${WINE_BIN:-}" ]]; then
  WINE_COMMAND="$WINE_BIN"
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

to_wine_path() {
  local path="$1"
  if [[ "$path" != /* ]]; then
    path="$(realpath -m -- "$path")"
  fi
  path="${path//\//\\}"
  printf 'Z:%s' "$path"
}

if [[ ! -f "$RENDER_EXE" ]]; then
  echo "Windows render adapter not found: $RENDER_EXE" >&2
  exit 127
fi

# Nikon leaves an empty nkn*.tmp in the Wine user's Temp directory after some
# renders.  Remove only old, empty files before starting a new SDK process.  The
# age guard protects concurrent renders; the fixed lock avoids noisy delete
# races between workers while keeping the cleanup best-effort.
cleanup_old_nikon_temp() {
  local wine_prefix="${WINEPREFIX:-/var/lib/nef-watch/wine}"
  local users_dir="$wine_prefix/drive_c/users"
  [[ -d "$users_dir" ]] || return 0
  if command -v flock >/dev/null 2>&1; then
    (
      flock -n 9 || exit 0
      find "$users_dir" -path '*/Temp/nkn*.tmp' -type f -empty -mmin +10 \
        -delete 2>/dev/null || true
    ) 9>"$wine_prefix/.nef-watch-temp-cleanup.lock"
  else
    find "$users_dir" -path '*/Temp/nkn*.tmp' -type f -empty -mmin +10 \
      -delete 2>/dev/null || true
  fi
}

cleanup_old_nikon_temp

nef_path="$(to_wine_path "$1")"
raw_path="$(to_wine_path "$2")"
profile_path="$(to_wine_path "$3")"

cd "$RUNTIME_DIR"
exec "$WINE_COMMAND" "$RENDER_EXE" \
  "$nef_path" \
  "$raw_path" \
  "$profile_path" \
  "${4:-8}" \
  "${5:-0}"
