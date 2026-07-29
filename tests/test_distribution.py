from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_linux_scripts_are_executable_and_syntactically_valid():
    scripts = [
        Path("start.sh"),
        Path("scripts/build-wheelhouse.sh"),
        Path("scripts/test.sh"),
    ]
    for script in scripts:
        assert script.is_file()
        assert os.access(script, os.X_OK)
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_start_command_works_without_package_index():
    environment = os.environ.copy()
    environment["PIP_NO_INDEX"] = "1"
    result = subprocess.run(
        ["./start.sh", "--version"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0.1.0" in result.stdout


def test_example_environment_contains_no_credentials():
    example = Path(".env.example").read_text(encoding="utf-8")
    assert 'DATALAB_PROFILE_IDS=""' in example
    assert "secret" not in example.lower()
    assert "token=" not in example.lower()
