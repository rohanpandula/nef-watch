#!/usr/bin/env bash
set -euo pipefail

fail() {
  echo "nef-watch healthcheck failed: $*" >&2
  exit 1
}

has_process_argument() {
  local expected=$1
  local cmdline arg
  for cmdline in /proc/[0-9]*/cmdline; do
    while IFS= read -r -d '' arg; do
      if [[ "$arg" == "$expected" ]]; then
        return 0
      fi
    done <"$cmdline" 2>/dev/null || true
  done
  return 1
}

export DISPLAY="${DISPLAY:-:99}"
export WINEPREFIX="${WINEPREFIX:-/var/lib/nef-watch/wine}"
export NEF_WATCH_WINE_SCHEMA="${NEF_WATCH_WINE_SCHEMA:-vc14-cc0ff0eb1dc3}"
export NIKON_RUNTIME_DIR="${NIKON_RUNTIME_DIR:-/var/lib/nef-watch/nikon-runtime/current}"

[[ -r "$NIKON_RUNTIME_DIR/.nef-watch-sdk-ready.json" ]] \
  || fail "private Nikon SDK runtime is not initialized"
[[ -x "$NIKON_RUNTIME_DIR/nef_render.exe" ]] \
  || fail "Nikon render adapter is missing"
[[ -r "$NIKON_RUNTIME_DIR/NkImgSDK.dll" ]] \
  || fail "Nikon runtime DLL is missing"
[[ -r "$NIKON_RUNTIME_DIR/Profiles/NKsRGB.icm" ]] \
  || fail "Nikon sRGB profile is missing"
[[ -f "$WINEPREFIX/.nef-watch-ready-$NEF_WATCH_WINE_SCHEMA" ]] \
  || fail "Wine prefix initialization is incomplete"
[[ -w "$WINEPREFIX" ]] \
  || fail "Wine prefix is not writable"
xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 \
  || fail "Xvfb is not responding on $DISPLAY"
has_process_argument /app/tool/nef_watch.py \
  || fail "nef-watch parser process is not running"

if [[ -n "${NEF_WATCH_HEALTH_INPUT:-}" ]]; then
  [[ -r "$NEF_WATCH_HEALTH_INPUT" && -x "$NEF_WATCH_HEALTH_INPUT" ]] \
    || fail "input directory is not readable: $NEF_WATCH_HEALTH_INPUT"
fi

if [[ -n "${NEF_WATCH_HEALTH_OUTPUT:-}" ]]; then
  [[ -w "$NEF_WATCH_HEALTH_OUTPUT" && -x "$NEF_WATCH_HEALTH_OUTPUT" ]] \
    || fail "output directory is not writable: $NEF_WATCH_HEALTH_OUTPUT"
fi
