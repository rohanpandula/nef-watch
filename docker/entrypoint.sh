#!/usr/bin/env bash
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"
export WINEPREFIX="${WINEPREFIX:-/var/lib/nef-watch/wine}"
export WINEARCH=win64
export WINEDEBUG="${WINEDEBUG:--all}"
export HOME="${HOME:-/var/lib/nef-watch/home}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/nef-watch-runtime}"
export NEF_WATCH_WINE_SCHEMA="${NEF_WATCH_WINE_SCHEMA:-vc14-cc0ff0eb1dc3}"

# Use Microsoft's native, mutually-compatible MSVC runtime set once installed.
# Preserve caller overrides while making Nikon's required runtime explicit.
runtime_overrides='winemenubuilder.exe=d;msvcp140=n,b;msvcp140_1=n,b;msvcp140_2=n,b;concrt140=n,b;vcruntime140=n,b;vcruntime140_1=n,b'
export WINEDLLOVERRIDES="${WINEDLLOVERRIDES:+${WINEDLLOVERRIDES};}${runtime_overrides}"

if ! mkdir -p "$HOME" "$WINEPREFIX" "$XDG_RUNTIME_DIR" \
  || ! chmod 0700 "$HOME" "$XDG_RUNTIME_DIR"; then
  echo "Runtime state is not writable by uid $(id -u): $WINEPREFIX" >&2
  exit 73
fi

if [[ ! -w "$WINEPREFIX" ]]; then
  echo "Wine prefix is not writable by uid $(id -u): $WINEPREFIX" >&2
  exit 73
fi

xvfb_pid=""
app_pid=""
termination_status=0

# shellcheck disable=SC2329  # Invoked by the EXIT trap.
cleanup() {
  local status=$?
  trap - EXIT INT TERM HUP
  if [[ -n "$app_pid" ]]; then
    kill -TERM "$app_pid" >/dev/null 2>&1 || true
    wait "$app_pid" >/dev/null 2>&1 || true
  fi
  if [[ -n "$xvfb_pid" ]]; then
    kill -TERM "$xvfb_pid" >/dev/null 2>&1 || true
    wait "$xvfb_pid" >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT

# shellcheck disable=SC2329  # Invoked by the signal traps.
forward_signal() {
  local signal=$1
  termination_status=$2
  if [[ -n "$app_pid" ]]; then
    kill "-$signal" "$app_pid" >/dev/null 2>&1 || true
  fi
  if [[ -n "$xvfb_pid" ]]; then
    kill -TERM "$xvfb_pid" >/dev/null 2>&1 || true
  fi
}
trap 'forward_signal TERM 143' TERM
trap 'forward_signal INT 130' INT
trap 'forward_signal HUP 129' HUP

Xvfb "$DISPLAY" -screen 0 1024x768x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
xvfb_pid=$!

xvfb_ready=0
for _ in {1..100}; do
  if ! kill -0 "$xvfb_pid" >/dev/null 2>&1; then
    wait "$xvfb_pid" || true
    echo "Xvfb exited before becoming ready:" >&2
    sed -n '1,120p' /tmp/xvfb.log >&2 || true
    exit 70
  fi
  if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
    xvfb_ready=1
    break
  fi
  sleep 0.1
done

if [[ "$xvfb_ready" -ne 1 ]]; then
  echo "Xvfb did not become ready on $DISPLAY within 10 seconds" >&2
  sed -n '1,120p' /tmp/xvfb.log >&2 || true
  exit 70
fi

# A Wine prefix creates the Z: -> / mapping used by nef_render_wine.sh.  Keep
# the initialized prefix on a small persistent volume so container restarts are
# quick.  flock prevents two startup processes from racing on first boot.
(
  flock 9
  ready_marker="$WINEPREFIX/.nef-watch-ready-$NEF_WATCH_WINE_SCHEMA"
  if [[ ! -f "$ready_marker" ]]; then
    wineboot --init >/tmp/wineboot.log 2>&1
    set +e
    wine /opt/microsoft/VC_redist.x64.exe \
      /install /quiet /norestart /log C:\\vc-redist.log \
      >/tmp/vc-redist.log 2>&1
    vc_status=$?
    set -e
    wineserver -w
    # 3010 (194 after POSIX truncation) means success with restart requested;
    # /norestart makes that safe. Any other non-zero status is a hard failure.
    if [[ "$vc_status" -ne 0 && "$vc_status" -ne 194 ]]; then
      echo "Microsoft VC++ runtime installation failed (rc=$vc_status):" >&2
      sed -n '1,160p' /tmp/vc-redist.log >&2 || true
      exit 70
    fi
    if ! find "$WINEPREFIX/drive_c/windows/system32" -maxdepth 1 \
        -type f -iname msvcp140.dll -print -quit | grep -q .; then
      echo "Microsoft VC++ runtime installation produced no msvcp140.dll" >&2
      exit 70
    fi
    touch "$ready_marker"
  fi
) 9>"$WINEPREFIX/.init.lock"

profiles_dir="$WINEPREFIX/drive_c/Program Files/Common Files/Nikon/Profiles"
mkdir -p "$profiles_dir"
# Do not preserve the image's intentionally immutable 0555/0444 modes in the
# persistent Wine volume. The same non-root user must be able to refresh these
# allowlisted profiles after an image update and on every container restart.
chmod 0755 "$profiles_dir"
find "$profiles_dir" -maxdepth 1 -type f -exec chmod 0644 {} +
cp -f /opt/nikon/Profiles/* "$profiles_dir/"
chmod 0644 "$profiles_dir"/*

python3 /app/tool/nef_watch.py \
  --render-bin /app/tool/nef_render_wine.sh \
  --profile /opt/nikon/Profiles/NKsRGB.icm \
  "$@" &
app_pid=$!

finished_pid=""
set +e
wait -n -p finished_pid "$app_pid" "$xvfb_pid"
finished_status=$?
set -e

if [[ "$termination_status" -ne 0 ]]; then
  exit "$termination_status"
fi

if [[ "$finished_pid" == "$xvfb_pid" ]]; then
  echo "Xvfb stopped while nef-watch was running" >&2
  if [[ "$finished_status" -eq 0 ]]; then
    exit 70
  fi
  exit "$finished_status"
fi

exit "$finished_status"
