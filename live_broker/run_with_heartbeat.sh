#!/usr/bin/env bash
# Pre-python heartbeat — distinguishes scheduler-dead from python-crashed.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${TRULEO_DATA_DIR:-$ROOT/data}"
mkdir -p "$DATA_DIR"
echo "{\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"stage\":\"pre_python\"}" > "$DATA_DIR/heartbeat.json"
cd "$ROOT"
exec "${TRULEO_PYTHON:-$ROOT/live_broker/venv/bin/python}" -m live_broker.run_live "$@"