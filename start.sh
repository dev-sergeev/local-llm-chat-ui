#!/usr/bin/env bash
set -Eeuo pipefail

DATALAB_PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$DATALAB_PROJECT_DIR"

DATALAB_PYTHON_BIN="${DATALAB_PYTHON_BIN:-python3}"
DATALAB_VENV_DIR="${DATALAB_VENV_DIR:-$DATALAB_PROJECT_DIR/.venv}"
DATALAB_WHEELHOUSE_DIR="$DATALAB_PROJECT_DIR/wheelhouse"
DATALAB_REQUIREMENTS_FILE="$DATALAB_PROJECT_DIR/requirements-linux-py311.lock"

if ! command -v "$DATALAB_PYTHON_BIN" >/dev/null 2>&1; then
  echo "Ошибка: Python 3.11+ не найден." >&2
  exit 2
fi

if ! "$DATALAB_PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "Ошибка: требуется Python 3.11 или новее." >&2
  exit 2
fi

DATALAB_BUNDLE_COMPATIBLE=0
if "$DATALAB_PYTHON_BIN" -c 'import platform, sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) and platform.system() == "Linux" and platform.machine().lower() in {"x86_64", "amd64"} else 1)'; then
  DATALAB_BUNDLE_COMPATIBLE=1
fi

if [[ ! -x "$DATALAB_VENV_DIR/bin/python" ]]; then
  if [[ "$DATALAB_BUNDLE_COMPATIBLE" -eq 1 ]]; then
    "$DATALAB_PYTHON_BIN" -m venv "$DATALAB_VENV_DIR"
  elif "$DATALAB_PYTHON_BIN" -c 'import langchain_gigachat, langchain_openai' >/dev/null 2>&1; then
    "$DATALAB_PYTHON_BIN" -m venv --system-site-packages "$DATALAB_VENV_DIR"
  else
    "$DATALAB_PYTHON_BIN" -m venv "$DATALAB_VENV_DIR"
  fi
fi

export PIP_NO_INDEX=1
export PIP_DISABLE_PIP_VERSION_CHECK=1

if compgen -G "$DATALAB_WHEELHOUSE_DIR/*.whl" >/dev/null; then
  "$DATALAB_VENV_DIR/bin/python" "$DATALAB_PROJECT_DIR/scripts/wheelhouse_manifest.py" \
    verify "$DATALAB_WHEELHOUSE_DIR"
fi

if [[ "$DATALAB_BUNDLE_COMPATIBLE" -eq 1 ]]; then
  if compgen -G "$DATALAB_WHEELHOUSE_DIR/*.whl" >/dev/null; then
    if ! "$DATALAB_VENV_DIR/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)'; then
      echo "Ошибка: существующее виртуальное окружение создано другой версией Python." >&2
      exit 2
    fi
    "$DATALAB_VENV_DIR/bin/python" -m pip install \
      --no-index \
      --find-links "$DATALAB_WHEELHOUSE_DIR" \
      --requirement "$DATALAB_REQUIREMENTS_FILE"
    "$DATALAB_VENV_DIR/bin/python" -m pip check
  else
    echo "Ошибка: полный локальный wheelhouse не найден." >&2
    exit 2
  fi
elif ! "$DATALAB_VENV_DIR/bin/python" -c 'import langchain_gigachat, langchain_openai' >/dev/null 2>&1; then
  echo "Ошибка: встроенный wheelhouse рассчитан на CPython 3.11, Linux x86_64 (glibc 2.17+)." >&2
  exit 2
fi

export PYTHONPATH="$DATALAB_PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$DATALAB_VENV_DIR/bin/python" -m datalab_chat "$@"
