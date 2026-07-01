#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$HERE/.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="${PYTHON_FALLBACK:-python3}"
fi

DEST_DIR="${DEST_DIR:-$HERE/models}"
STATE_DIR="${STATE_DIR:-$DEST_DIR/_download_state}"
CONFIG_FILE="${CONFIG_FILE:-$HERE/download_config.env}"
CONCURRENCY="${CONCURRENCY:-2}"
PER_REPO_WORKERS="${PER_REPO_WORKERS:-8}"
RESERVE_SPACE="${RESERVE_SPACE:-100G}"
PROGRESS_INTERVAL="${PROGRESS_INTERVAL:-30}"

exec "$PYTHON_BIN" "$HERE/download_models.py" \
  --manifest "$HERE/ai4s_models_manifest.csv" \
  --dest-dir "$DEST_DIR" \
  --state-dir "$STATE_DIR" \
  --config "$CONFIG_FILE" \
  --concurrency "$CONCURRENCY" \
  --per-repo-workers "$PER_REPO_WORKERS" \
  --reserve-space "$RESERVE_SPACE" \
  --progress-interval "$PROGRESS_INTERVAL" \
  "$@"
