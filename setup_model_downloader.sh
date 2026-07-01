#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$HERE/.venv}"
RUNTIME_DIR="${RUNTIME_DIR:-$HERE/.runtime}"
BOOTSTRAP_PYTHON="${BOOTSTRAP_PYTHON:-1}"
MINIFORGE_URL="${MINIFORGE_URL:-https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh}"

python_ok() {
  command -v "$1" >/dev/null 2>&1 || return 1
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1
}

pick_python() {
  if [ "${PYTHON_BIN:-}" != "" ] && python_ok "$PYTHON_BIN"; then
    echo "$PYTHON_BIN"
    return 0
  fi
  for candidate in python3 python; do
    if python_ok "$candidate"; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

bootstrap_python() {
  mkdir -p "$RUNTIME_DIR"
  local installer="$RUNTIME_DIR/miniforge.sh"
  local prefix="$RUNTIME_DIR/miniforge3"
  if [ ! -x "$prefix/bin/python" ]; then
    echo "Python >= 3.8 not found. Bootstrapping Miniforge under $prefix" >&2
    if command -v curl >/dev/null 2>&1; then
      curl -L "$MINIFORGE_URL" -o "$installer"
    elif command -v wget >/dev/null 2>&1; then
      wget -O "$installer" "$MINIFORGE_URL"
    else
      echo "Neither curl nor wget is available; cannot download Miniforge." >&2
      exit 2
    fi
    bash "$installer" -b -p "$prefix"
  fi
  echo "$prefix/bin/python"
}

if ! PYTHON_SELECTED="$(pick_python)"; then
  if [ "$BOOTSTRAP_PYTHON" = "1" ]; then
    PYTHON_SELECTED="$(bootstrap_python)"
  else
    echo "Python >= 3.8 not found. Set BOOTSTRAP_PYTHON=1 or provide PYTHON_BIN." >&2
    exit 2
  fi
fi

if [ ! -d "$VENV_DIR" ]; then
  "$PYTHON_SELECTED" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
python -m pip install -U pip
python -m pip install -U "huggingface_hub>=0.23" "modelscope>=1.14"

python "$HERE/download_models.py" \
  --manifest "$HERE/ai4s_models_manifest.csv" \
  --dest-dir "${DEST_DIR:-$HERE/models}" \
  --check-deps

echo "Setup complete. Python: $VENV_DIR/bin/python"
