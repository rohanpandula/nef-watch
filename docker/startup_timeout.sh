#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "usage: nef-watch-startup-timeout TIMEOUT_SECONDS KILL_GRACE_SECONDS COMMAND [ARG ...]" >&2
  exit 64
fi

timeout_seconds=$1
kill_grace_seconds=$2
shift 2

validate_seconds() {
  local label=$1
  local value=$2
  local maximum=$3
  if [[ ! "$value" =~ ^[1-9][0-9]{0,3}$ ]]; then
    echo "$label must be a positive integer no greater than $maximum" >&2
    exit 78
  fi
  if (( 10#$value > maximum )); then
    echo "$label must be a positive integer no greater than $maximum" >&2
    exit 78
  fi
}

validate_seconds TIMEOUT_SECONDS "$timeout_seconds" 3600
validate_seconds KILL_GRACE_SECONDS "$kill_grace_seconds" 300

exec timeout --signal=TERM \
  --kill-after="${kill_grace_seconds}s" \
  "${timeout_seconds}s" "$@"
