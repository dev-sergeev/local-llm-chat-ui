from __future__ import annotations

import json
import logging
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from datalab_chat.application import ChatApplication
from datalab_chat.generation import GenerationPolicy
from datalab_chat.memory import SQLiteChatMemory
from datalab_chat.profiles import EnvProfileCatalog
from datalab_chat.web import create_server


class WebGateway:
    def __init__(self, answers):
        self.answers = list(answers)

    def complete(self, messages, *, timeout_seconds, on_chunk=None):
        answer = self.answers.pop(0)
        if on_chunk is not None:
            on_chunk(answer)
        return answer


class WebFactory:
    def __init__(self, gateway):
        self.gateway = gateway

    def create(self, connection):
        return self.gateway


@pytest.fixture
def running_server(tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text(
        "<!doctype html><title>DataLab</title>", encoding="utf-8"
    )
    (static / "app.js").write_text("console.log('ok')", encoding="utf-8")
    app = ChatApplication(
        EnvProfileCatalog(tmp_path / ".env"),
        SQLiteChatMemory(tmp_path / "chat.db"),
        WebFactory(WebGateway(["HTTP ответ", "OK"])),
        generation_policy=GenerationPolicy(
            total_timeout_seconds=1,
            max_attempts=3,
            base_backoff_seconds=0,
            max_backoff_seconds=0,
            jitter_ratio=0,
            poll_interval_seconds=0.005,
        ),
    )
    server = create_server(app, static_dir=static, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base_url
    finally:
        server.shutdown()
        server.server_close()
        app.shutdown()
        thread.join(1)


def request(base_url, method, path, payload=None, headers=None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    actual_headers = dict(headers or {})
    if payload is not None:
        actual_headers.setdefault("Content-Type", "application/json")
    req = Request(base_url + path, data=body, headers=actual_headers, method=method)
    try:
        with urlopen(req, timeout=2) as response:
            raw = response.read()
            parsed = json.loads(raw) if raw else None
            return response.status, dict(response.headers), parsed
    except HTTPError as error:
        raw = error.read()
        parsed = json.loads(raw) if raw else None
        return error.code, dict(error.headers), parsed


def test_static_app_and_health_have_local_security_headers(running_server):
    status, headers, body = request(running_server, "GET", "/api/health")
    assert status == 200
    assert body["status"] == "ok"
    assert body["version"]
    assert headers["Cache-Control"] == "no-store"

    with urlopen(running_server + "/", timeout=2) as response:
        html = response.read().decode("utf-8")
        assert response.status == 200
        assert "DataLab" in html
        assert "default-src 'self'" in response.headers["Content-Security-Policy"]
        assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_complete_chat_flow_over_http_never_returns_token(running_server):
    status, _, profile = request(
        running_server,
        "POST",
        "/api/profiles",
        {
            "display_name": "Giga PROD",
            "format": "gigachat",
            "base_url": "https://gateway.bank.local/v1",
            "token": "top-secret-token",
            "model_id": "risk-model",
        },
    )
    assert status == 201
    assert "token" not in profile
    profile_id = profile["id"]

    status, _, profiles = request(running_server, "GET", "/api/profiles")
    assert status == 200
    assert profiles == [profile]
    assert "top-secret-token" not in json.dumps(profiles)

    status, _, conversation = request(
        running_server,
        "POST",
        "/api/conversations",
        {"profile_id": profile_id},
    )
    assert status == 201
    conversation_id = conversation["id"]

    status, _, generation = request(
        running_server,
        "POST",
        f"/api/conversations/{conversation_id}/messages",
        {"content": "Вопрос через HTTP", "profile_id": profile_id},
    )
    assert status == 202

    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        _, _, current = request(
            running_server,
            "GET",
            f"/api/generations/{generation['id']}",
        )
        if current["status"] == "succeeded":
            break
        time.sleep(0.01)
    assert current["status"] == "succeeded"

    status, _, view = request(
        running_server,
        "GET",
        f"/api/conversations/{conversation_id}",
    )
    assert status == 200
    assert [message["content"] for message in view["messages"]] == [
        "Вопрос через HTTP",
        "HTTP ответ",
    ]
    assert "top-secret-token" not in json.dumps(view)

    status, _, checked = request(
        running_server,
        "POST",
        f"/api/profiles/{profile_id}/test",
        {},
    )
    assert status == 200
    assert checked["ok"] is True


def test_unsaved_profile_values_can_be_checked_without_persistence(running_server):
    status, _, checked = request(
        running_server,
        "POST",
        "/api/profiles/test",
        {
            "display_name": "Черновик",
            "format": "openai",
            "base_url": "https://draft.gateway.local/v1",
            "token": "draft-secret",
            "model_id": "draft-model",
            "timeout_seconds": 0.5,
        },
    )

    assert status == 200
    assert checked["ok"] is True
    assert "draft-secret" not in json.dumps(checked)
    _, _, profiles = request(running_server, "GET", "/api/profiles")
    assert profiles == []


def test_conversation_patch_rolls_back_all_fields_on_invalid_profile(running_server):
    _, _, profile = request(
        running_server,
        "POST",
        "/api/profiles",
        {
            "display_name": "Giga PROD",
            "format": "gigachat",
            "base_url": "https://gateway.bank.local/v1",
            "token": "top-secret-token",
            "model_id": "risk-model",
        },
    )
    _, _, conversation = request(
        running_server,
        "POST",
        "/api/conversations",
        {"profile_id": profile["id"]},
    )

    status, _, _ = request(
        running_server,
        "PATCH",
        f"/api/conversations/{conversation['id']}",
        {"title": "Не должно сохраниться", "profile_id": "0" * 32},
    )

    assert status == 404
    _, _, unchanged = request(
        running_server,
        "GET",
        f"/api/conversations/{conversation['id']}",
    )
    assert unchanged["title"] == "Новый чат"
    assert unchanged["active_profile_id"] == profile["id"]


def test_validation_errors_are_json_and_do_not_stop_server(running_server):
    status, _, error = request(
        running_server,
        "POST",
        "/api/profiles",
        {"display_name": "Missing fields"},
    )
    assert status == 400
    assert error["error"]["code"] == "validation_error"

    status, _, _ = request(running_server, "GET", "/api/health")
    assert status == 200


def test_query_values_are_not_written_to_request_log(running_server, caplog):
    caplog.set_level(logging.INFO, logger="datalab_chat.web")

    status, _, _ = request(
        running_server,
        "GET",
        "/api/conversations?query=confidential-search-value",
    )

    assert status == 200
    assert "confidential-search-value" not in caplog.text


def test_head_error_has_headers_but_no_body(running_server):
    status, headers, body = request(running_server, "HEAD", "/api/not-found")

    assert status == 404
    assert int(headers["Content-Length"]) > 0
    assert body is None


def test_mutations_reject_foreign_origin_and_non_json_body(running_server):
    status, _, error = request(
        running_server,
        "POST",
        "/api/conversations",
        {},
        headers={"Origin": "https://evil.example"},
    )
    assert status == 403
    assert error["error"]["code"] == "forbidden_origin"

    req = Request(
        running_server + "/api/conversations",
        data=b"profile_id=x",
        headers={"Content-Type": "text/plain"},
        method="POST",
    )
    with pytest.raises(HTTPError) as caught:
        urlopen(req, timeout=2)
    assert caught.value.code == 415


def test_mutations_accept_opaque_origin_from_loopback_ui(running_server):
    blocked_status, _, blocked_error = request(
        running_server,
        "POST",
        "/api/profiles",
        {},
        headers={"Origin": "null"},
    )
    assert blocked_status == 403
    assert blocked_error["error"]["code"] == "forbidden_origin"

    status, _, profile = request(
        running_server,
        "POST",
        "/api/profiles",
        {
            "display_name": "Embedded browser",
            "format": "openai",
            "base_url": "https://gateway.bank.local/v1",
            "token": "local-secret",
            "model_id": "risk-model",
        },
        headers={"Origin": "null", "X-DataLab-UI": "browser"},
    )

    assert status == 201
    assert profile["display_name"] == "Embedded browser"
    assert "token" not in profile


def test_mutations_accept_forwarded_origin_from_loopback_ui(running_server):
    status, _, conversation = request(
        running_server,
        "POST",
        "/api/conversations",
        {},
        headers={
            "Origin": "http://127.0.0.1:49152",
            "Sec-Fetch-Site": "same-origin",
            "X-DataLab-UI": "browser",
        },
    )

    assert status == 201
    assert conversation["title"] == "Новый чат"


def test_mutations_compare_origin_with_forwarded_loopback_host(running_server):
    status, _, conversation = request(
        running_server,
        "POST",
        "/api/conversations",
        {},
        headers={
            "Host": "127.0.0.1:49152",
            "Origin": "http://127.0.0.1:49152",
            "Sec-Fetch-Site": "same-origin",
        },
    )

    assert status == 201
    assert conversation["title"] == "Новый чат"


def test_cross_site_origin_stays_blocked_with_ui_marker(running_server):
    for origin in (
        "https://evil.example",
        running_server.replace("127.0.0.1", "localhost"),
    ):
        status, _, error = request(
            running_server,
            "POST",
            "/api/conversations",
            {},
            headers={
                "Origin": origin,
                "Sec-Fetch-Site": "cross-site",
                "X-DataLab-UI": "browser",
            },
        )

        assert status == 403
        assert error["error"]["code"] == "forbidden_origin"


def test_requests_reject_non_loopback_host(running_server):
    status, _, error = request(
        running_server,
        "GET",
        "/api/health",
        headers={"Host": "rebinding.example"},
    )

    assert status == 403
    assert error["error"]["code"] == "forbidden_host"

    status, _, error = request(
        running_server,
        "POST",
        "/api/profiles",
        {},
        headers={
            "Host": "rebinding.example",
            "Origin": "null",
            "X-DataLab-UI": "browser",
        },
    )

    assert status == 403
    assert error["error"]["code"] == "forbidden_host"


def test_unknown_and_traversal_paths_are_not_served(running_server):
    status, _, error = request(running_server, "GET", "/api/unknown")
    assert status == 404
    assert error["error"]["code"] == "not_found"

    status, _, _ = request(running_server, "GET", "/..%2F.env")
    assert status == 404
