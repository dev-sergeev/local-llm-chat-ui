#!/usr/bin/env bash
set -Eeuo pipefail

DATALAB_PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
DATALAB_PYTHON_BIN="${DATALAB_PYTHON_BIN:-python3}"
DATALAB_WHEELHOUSE_DIR="$DATALAB_PROJECT_DIR/wheelhouse"

mkdir -p "$DATALAB_WHEELHOUSE_DIR"

echo "Сборка wheelhouse для текущей ОС, архитектуры и версии Python."
echo "Запускайте этот скрипт на совместимой Linux-машине с доступом к внутреннему или внешнему package index."

"$DATALAB_PYTHON_BIN" -m pip download \
  --dest "$DATALAB_WHEELHOUSE_DIR" \
  --requirement "$DATALAB_PROJECT_DIR/requirements.txt"

"$DATALAB_PYTHON_BIN" -m pip hash "$DATALAB_WHEELHOUSE_DIR"/*.whl > "$DATALAB_WHEELHOUSE_DIR/SHA256SUMS.pip"

echo "Готово: перенесите репозиторий вместе с wheelhouse/ в закрытый контур."

