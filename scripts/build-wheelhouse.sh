#!/usr/bin/env bash
set -Eeuo pipefail

DATALAB_PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
DATALAB_PYTHON_BIN="${DATALAB_PYTHON_BIN:-python3}"
DATALAB_WHEELHOUSE_DIR="$DATALAB_PROJECT_DIR/wheelhouse"
DATALAB_LOCK_FILE="$DATALAB_PROJECT_DIR/requirements-linux-py311.lock"
DATALAB_STAGING_DIR="$(mktemp -d "${TMPDIR:-/tmp}/datalab-wheelhouse.XXXXXX")"

cleanup() {
  rm -rf -- "$DATALAB_STAGING_DIR"
}
trap cleanup EXIT

mkdir -p "$DATALAB_WHEELHOUSE_DIR"

echo "Сборка wheelhouse для CPython 3.11, Linux x86_64, glibc 2.17+."
echo "Запускайте скрипт только там, где package index разрешён политикой организации."

"$DATALAB_PYTHON_BIN" -m pip download \
  --dest "$DATALAB_STAGING_DIR" \
  --only-binary=:all: \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.11 \
  --abi cp311 \
  --requirement "$DATALAB_LOCK_FILE"

"$DATALAB_PYTHON_BIN" "$DATALAB_PROJECT_DIR/scripts/wheelhouse_manifest.py" \
  write "$DATALAB_STAGING_DIR"

find "$DATALAB_WHEELHOUSE_DIR" -maxdepth 1 -type f \
  \( -name '*.whl' -o -name 'MANIFEST.sha256' \) -delete
cp "$DATALAB_STAGING_DIR"/*.whl "$DATALAB_WHEELHOUSE_DIR/"
cp "$DATALAB_STAGING_DIR/MANIFEST.sha256" "$DATALAB_WHEELHOUSE_DIR/"

echo "Готово: перенесите репозиторий вместе с wheelhouse/ в закрытый контур."
