#!/usr/bin/env python3
"""Create or verify the relative SHA-256 manifest for bundled wheels."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import sys
from pathlib import Path


MANIFEST_NAME = "MANIFEST.sha256"
LINE_PATTERN = re.compile(r"^([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9_.+-]*\.whl)$")


def wheel_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_manifest(directory: Path) -> None:
    wheels = sorted(directory.glob("*.whl"), key=lambda item: item.name)
    if not wheels:
        raise ValueError("wheelhouse does not contain wheels")
    contents = "".join(f"{wheel_hash(path)}  {path.name}\n" for path in wheels)
    temporary = directory / f".{MANIFEST_NAME}.{os.getpid()}"
    temporary.write_text(contents, encoding="ascii", newline="\n")
    os.replace(temporary, directory / MANIFEST_NAME)


def verify_manifest(directory: Path) -> None:
    manifest = directory / MANIFEST_NAME
    try:
        lines = manifest.read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise ValueError("wheelhouse manifest is missing") from exc
    expected: dict[str, str] = {}
    for line in lines:
        match = LINE_PATTERN.fullmatch(line)
        if match is None or match.group(2) in expected:
            raise ValueError("wheelhouse manifest has an invalid entry")
        expected[match.group(2)] = match.group(1)
    actual = {path.name for path in directory.glob("*.whl")}
    if not actual or actual != set(expected):
        raise ValueError("wheelhouse contents do not match its manifest")
    for name, digest in expected.items():
        if not hmac.compare_digest(wheel_hash(directory / name), digest):
            raise ValueError(f"wheelhouse checksum mismatch: {name}")


def main(arguments: list[str]) -> int:
    if len(arguments) != 2 or arguments[0] not in {"write", "verify"}:
        print(
            "usage: wheelhouse_manifest.py {write|verify} WHEELHOUSE", file=sys.stderr
        )
        return 2
    directory = Path(arguments[1]).resolve()
    try:
        if arguments[0] == "write":
            write_manifest(directory)
        else:
            verify_manifest(directory)
    except (OSError, ValueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    print(f"Wheelhouse {arguments[0]}: {directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
