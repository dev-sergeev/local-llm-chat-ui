#!/usr/bin/env bash
set -Eeuo pipefail

DATALAB_PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$DATALAB_PROJECT_DIR"

export PYTHONPATH="$DATALAB_PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
python3 -m pytest

if command -v node >/dev/null 2>&1; then
  npm test
  node --check src/datalab_chat/static/assets/app.js
else
  echo "Предупреждение: Node.js не найден, frontend-тесты пропущены." >&2
fi

