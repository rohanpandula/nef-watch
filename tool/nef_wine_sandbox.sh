#!/usr/bin/env bash
# Run Wine beside its authenticated Xvfb in sibling signal-scoped Landlock domains.
set -euo pipefail
umask 077

if [[ $# -lt 1 ]]; then
  echo "wine sandbox command is required" >&2
  exit 64
fi
: "${DISPLAY:?DISPLAY must name the private X server}"
: "${XAUTHORITY:?XAUTHORITY must name the private cookie file}"
: "${NEF_WATCH_XVFB_LOG:?NEF_WATCH_XVFB_LOG is required}"
: "${NEF_WATCH_JOB_ROOT:?NEF_WATCH_JOB_ROOT is required}"
: "${NEF_WATCH_X_SOCKET_DIR:?NEF_WATCH_X_SOCKET_DIR is required}"

[[ "$DISPLAY" =~ ^:[1-9][0-9]{0,4}$ ]] && (( 10#${DISPLAY#:} <= 65535 )) || {
  echo "unsafe private X display: $DISPLAY" >&2
  exit 64
}
[[ "$XAUTHORITY" == "$NEF_WATCH_JOB_ROOT"/* &&
   "$NEF_WATCH_XVFB_LOG" == "$NEF_WATCH_JOB_ROOT"/* &&
   -f "$XAUTHORITY" && ! -L "$XAUTHORITY" ]] || {
  echo "private X server paths escape the render job" >&2
  exit 64
}

xvfb_domain=()
if [[ "${NEF_WATCH_REQUIRE_LANDLOCK:-1}" != 0 ]]; then
  landlock_exec="${NEF_WATCH_LANDLOCK_EXEC:-}"
  if [[ "$landlock_exec" != /usr/local/libexec/nef-watch-landlock-exec.py ||
        ! -x "$landlock_exec" || -w "$landlock_exec" ||
        "$NEF_WATCH_X_SOCKET_DIR" != /tmp/.X11-unix ]]; then
    echo "sealed Xvfb Landlock launcher or socket namespace is unavailable" >&2
    exit 77
  fi
  xvfb_domain=(/usr/local/bin/python3 -I "$landlock_exec")
  # The launcher execs Xvfb in-place, so this rule follows the X server PID but
  # exposes neither sibling process metadata nor the host-facing sysfs tree.
  for read_path in /usr /etc /proc/self "$NEF_WATCH_JOB_ROOT"; do
    [[ -e "$read_path" ]] && xvfb_domain+=(--ro "$read_path")
  done
  for read_path in /lib /lib64; do
    [[ -e "$read_path" ]] && xvfb_domain+=(--ro "$read_path")
  done
  for write_path in /dev/null /dev/zero /dev/full /dev/random /dev/urandom \
    /tmp "$NEF_WATCH_X_SOCKET_DIR" "$NEF_WATCH_JOB_ROOT/home" \
    "$NEF_WATCH_JOB_ROOT/xdg"; do
    [[ -e "$write_path" ]] && xvfb_domain+=(--rw "$write_path")
  done
  xvfb_domain+=(--)
fi

xvfb_pid="" command_pid=""
cleanup() {
  local status=$?
  trap - EXIT INT TERM HUP
  [[ -z "$command_pid" ]] || {
    kill -TERM "$command_pid" 2>/dev/null || true
    wait "$command_pid" 2>/dev/null || true
  }
  [[ -z "$xvfb_pid" ]] || {
    kill -TERM "$xvfb_pid" 2>/dev/null || true
    wait "$xvfb_pid" 2>/dev/null || true
  }
  exit "$status"
}
trap cleanup EXIT
forward_signal() {
  local signal=$1 status=$2
  trap - "$signal"
  [[ -z "$command_pid" ]] || kill "-$signal" "$command_pid" 2>/dev/null || true
  exit "$status"
}
trap 'forward_signal TERM 143' TERM
trap 'forward_signal INT 130' INT
trap 'forward_signal HUP 129' HUP

"${xvfb_domain[@]}" Xvfb "$DISPLAY" -screen 0 1024x768x24 -nolisten tcp -nolock \
  -auth "$XAUTHORITY" >"$NEF_WATCH_XVFB_LOG" 2>&1 &
xvfb_pid=$!
for _ in {1..100}; do
  kill -0 "$xvfb_pid" 2>/dev/null || {
    echo "private Xvfb exited during startup" >&2
    sed -n '1,80p' "$NEF_WATCH_XVFB_LOG" >&2 || true
    exit 70
  }
  xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 && break
  sleep 0.1
done
xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 || {
  echo "private Xvfb did not become ready" >&2
  exit 70
}

"$@" &
command_pid=$!
set +e
wait "$command_pid"
command_status=$?
set -e
command_pid=""
exit "$command_status"
