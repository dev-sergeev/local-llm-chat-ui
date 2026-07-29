from __future__ import annotations

import os
import subprocess
import sys
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


def test_offline_linux_lock_and_wheelhouse_manifest_are_complete():
    lock = Path("requirements-linux-py311.lock").read_text(encoding="utf-8")
    requirements = [
        line for line in lock.splitlines() if line and not line.startswith("#")
    ]
    assert "langchain-gigachat==0.5.1" in requirements
    assert "langchain-openai==1.1.10" in requirements
    assert len(requirements) >= 38

    wheels = list(Path("wheelhouse").glob("*.whl"))
    assert len(wheels) >= 38
    subprocess.run(
        [
            sys.executable,
            "scripts/wheelhouse_manifest.py",
            "verify",
            "wheelhouse",
        ],
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--dry-run",
            "--ignore-installed",
            "--no-index",
            "--find-links",
            "wheelhouse",
            "--only-binary=:all:",
            "--platform",
            "manylinux2014_x86_64",
            "--implementation",
            "cp",
            "--python-version",
            "3.11",
            "--abi",
            "cp311",
            "--requirement",
            "requirements-linux-py311.lock",
        ],
        check=True,
    )
