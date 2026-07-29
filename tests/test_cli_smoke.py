from __future__ import annotations

import os
import logging
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

from datalab_chat.__main__ import _configure_logging


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_module_starts_real_localhost_service_and_stops_cleanly(tmp_path):
    port = free_port()
    data_dir = tmp_path / ".data"
    env_file = tmp_path / ".env"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path.cwd() / "src")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "datalab_chat",
            "--port",
            str(port),
            "--no-browser",
            "--data-dir",
            str(data_dir),
            "--env-file",
            str(env_file),
        ],
        cwd=Path.cwd(),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 8
        last_error = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                with urlopen(
                    f"http://127.0.0.1:{port}/api/health", timeout=0.3
                ) as response:
                    assert response.status == 200
                    break
            except Exception as exc:
                last_error = exc
                time.sleep(0.05)
        else:
            raise AssertionError(f"server did not start: {last_error}")

        with urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
            html = response.read().decode("utf-8")
            assert "DataLab Risk Chat" in html
        assert stat.S_IMODE((data_dir / "chat.db").stat().st_mode) == 0o600
        assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=5)

    assert process.returncode == 0, output


def test_external_http_clients_cannot_write_profile_urls_to_info_log(tmp_path):
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    external = logging.getLogger("httpx")
    try:
        _configure_logging(tmp_path)
        external.warning("HTTP Request: POST https://secret.bank.local/v1")
        logging.getLogger("datalab_chat").info("safe application event")
        for handler in root.handlers:
            handler.flush()
        contents = (tmp_path / "app.log").read_text(encoding="utf-8")
    finally:
        for handler in root.handlers:
            if handler not in original_handlers:
                handler.close()
        root.handlers = original_handlers
        root.setLevel(original_level)

    assert "secret.bank.local" not in contents
    assert "safe application event" in contents
