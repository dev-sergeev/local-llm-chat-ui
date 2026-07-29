#!/usr/bin/env bash
set -Eeuo pipefail

DATALAB_PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$DATALAB_PROJECT_DIR"

DATALAB_PYTHON_BIN="${DATALAB_PYTHON_BIN:-python3}"
DATALAB_VENV_DIR="$DATALAB_PROJECT_DIR/.venv"
DATALAB_WHEELHOUSE_DIR="$DATALAB_PROJECT_DIR/wheelhouse"
DATALAB_REQUIREMENTS_FILE="$DATALAB_PROJECT_DIR/requirements.txt"

if ! command -v "$DATALAB_PYTHON_BIN" >/dev/null 2>&1; then
  echo "Ошибка: Python 3.11+ не найден." >&2
  exit 2
fi

if ! "$DATALAB_PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "Ошибка: требуется Python 3.11 или новее." >&2
  exit 2
fi

if [[ ! -x "$DATALAB_VENV_DIR/bin/python" ]]; then
  if compgen -G "$DATALAB_WHEELHOUSE_DIR/*.whl" >/dev/null; then
    "$DATALAB_PYTHON_BIN" -m venv "$DATALAB_VENV_DIR"
  else
    "$DATALAB_PYTHON_BIN" -m venv --system-site-packages "$DATALAB_VENV_DIR"
  fi
fi

export PIP_NO_INDEX=1
export PIP_DISABLE_PIP_VERSION_CHECK=1

if ! "$DATALAB_VENV_DIR/bin/python" -c 'import langchain_gigachat, langchain_openai' >/dev/null 2>&1; then
  if compgen -G "$DATALAB_WHEELHOUSE_DIR/*.whl" >/dev/null; then
    "$DATALAB_VENV_DIR/bin/python" -m pip install \
      --no-index \
      --find-links "$DATALAB_WHEELHOUSE_DIR" \
      --requirement "$DATALAB_REQUIREMENTS_FILE"
  else
    echo "Ошибка: LLM-библиотеки не найдены." >&2
    echo "Добавьте Linux wheels в wheelhouse/ или установите зависимости в системный Python." >&2
    exit 2
  fi
fi

export PYTHONPATH="$DATALAB_PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$DATALAB_VENV_DIR/bin/python" -m datalab_chat "$@"

