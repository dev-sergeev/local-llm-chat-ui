from __future__ import annotations

import subprocess
import sys


SCRIPT = "scripts/wheelhouse_manifest.py"


def run_manifest(command: str, directory):
    return subprocess.run(
        [sys.executable, SCRIPT, command, str(directory)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_manifest_detects_missing_extra_and_modified_wheels(tmp_path):
    wheel = tmp_path / "example-1.0-py3-none-any.whl"
    wheel.write_bytes(b"first contents")
    assert run_manifest("write", tmp_path).returncode == 0
    assert run_manifest("verify", tmp_path).returncode == 0

    wheel.write_bytes(b"modified")
    assert run_manifest("verify", tmp_path).returncode == 1

    wheel.write_bytes(b"first contents")
    (tmp_path / "extra-1.0-py3-none-any.whl").write_bytes(b"extra")
    assert run_manifest("verify", tmp_path).returncode == 1
