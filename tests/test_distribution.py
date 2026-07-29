from __future__ import annotations

import configparser
import json
import os
import shutil
import subprocess
import sys
import zipfile
from email.parser import BytesParser
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REMOVED_BUNDLE_PATHS = (
    "requirements.txt",
    "requirements-linux-py311.lock",
    "start.sh",
    "scripts/build-wheelhouse.sh",
    "scripts/wheelhouse_manifest.py",
    "tests/test_wheelhouse_manifest.py",
    "wheelhouse",
)


def test_project_metadata_exposes_unpinned_runtime_dependencies_and_cli():
    metadata = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    project = metadata["project"]

    assert metadata["build-system"]["requires"] == ["setuptools"]
    assert project["name"] == "local-llm-chat-ui"
    assert project["requires-python"] == ">=3.10"
    assert project["dependencies"] == [
        "langchain-gigachat",
        "langchain-openai",
    ]
    assert project["scripts"] == {"local-llm-chat": "datalab_chat.__main__:main"}


def test_python_310_test_tools_are_optional_and_unpinned():
    metadata = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert metadata["project"]["optional-dependencies"]["test"] == [
        "pip",
        "pytest",
        "setuptools",
        "tomli; python_version < '3.11'",
        "wheel",
    ]


def test_frontend_test_package_uses_public_project_name():
    package = json.loads((PROJECT_ROOT / "package.json").read_text(encoding="utf-8"))
    assert package["name"] == "local-llm-chat-ui"


def test_local_uv_lock_is_not_tracked_as_a_dependency_version_source():
    ignored_paths = (
        (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    )
    assert "/uv.lock" in ignored_paths


def test_project_builds_wheel_without_downloading_runtime_dependencies(tmp_path):
    source_dir = tmp_path / "source"
    wheel_dir = tmp_path / "wheel"
    source_dir.mkdir()
    shutil.copy2(PROJECT_ROOT / "pyproject.toml", source_dir)
    shutil.copy2(PROJECT_ROOT / "README.md", source_dir)
    shutil.copytree(
        PROJECT_ROOT / "src",
        source_dir / "src",
        ignore=shutil.ignore_patterns("*.egg-info", "__pycache__"),
    )
    environment = os.environ.copy()
    environment["PIP_NO_INDEX"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            ".",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_dir),
        ],
        cwd=source_dir,
        env=environment,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    wheels = list(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as package:
        names = set(package.namelist())
        dist_info = next(
            name.removesuffix("/METADATA")
            for name in names
            if name.endswith(".dist-info/METADATA")
        )
        metadata = BytesParser().parsebytes(package.read(f"{dist_info}/METADATA"))
        entry_points = configparser.ConfigParser()
        entry_points.read_string(
            package.read(f"{dist_info}/entry_points.txt").decode("utf-8")
        )

    assert metadata["Name"] == "local-llm-chat-ui"
    assert metadata["Requires-Python"] == ">=3.10"
    runtime_requirements = [
        requirement
        for requirement in metadata.get_all("Requires-Dist")
        if "extra ==" not in requirement
    ]
    assert runtime_requirements == [
        "langchain-gigachat",
        "langchain-openai",
    ]
    assert metadata.get_all("Provides-Extra") == ["test"]
    assert entry_points["console_scripts"]["local-llm-chat"] == (
        "datalab_chat.__main__:main"
    )
    assert "datalab_chat/static/index.html" in names
    assert "datalab_chat/static/assets/app.css" in names
    assert "datalab_chat/static/assets/app.js" in names


def test_removed_dependency_bundle_is_not_part_of_source_tree():
    leftovers = [
        path for path in REMOVED_BUNDLE_PATHS if (PROJECT_ROOT / path).exists()
    ]
    assert leftovers == []


def test_example_environment_contains_no_credentials():
    example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert 'DATALAB_PROFILE_IDS=""' in example
    assert "secret" not in example.lower()
    assert "token=" not in example.lower()
