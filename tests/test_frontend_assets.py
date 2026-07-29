from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path


STATIC = Path("src/datalab_chat/static")


class DocumentContract(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.scripts = []
        self.stylesheets = []
        self.inline_scripts = 0
        self._inside_script_without_src = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "id" in attributes:
            self.ids.add(attributes["id"])
        if tag == "script":
            self.scripts.append(attributes)
            self._inside_script_without_src = "src" not in attributes
        if tag == "link" and attributes.get("rel") == "stylesheet":
            self.stylesheets.append(attributes.get("href"))
        assert "style" not in attributes

    def handle_data(self, data):
        if self._inside_script_without_src and data.strip():
            self.inline_scripts += 1

    def handle_endtag(self, tag):
        if tag == "script":
            self._inside_script_without_src = False


def test_frontend_has_complete_offline_dom_contract():
    parser = DocumentContract()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))

    assert {
        "sidebar",
        "new-chat-button",
        "conversation-search",
        "conversation-list",
        "model-select",
        "chat-title",
        "welcome-state",
        "chat-view",
        "messages",
        "composer-form",
        "message-input",
        "send-button",
        "stop-button",
        "model-dialog",
        "profile-form",
        "toast-region",
    } <= parser.ids
    assert parser.stylesheets == ["/assets/app.css"]
    assert parser.scripts == [{"type": "module", "src": "/assets/app.js"}]
    assert parser.inline_scripts == 0


def test_frontend_assets_are_self_contained_and_do_not_reference_cdn():
    assets = [
        STATIC / "index.html",
        STATIC / "assets" / "app.css",
        STATIC / "assets" / "app.js",
        STATIC / "assets" / "markdown.js",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in assets)

    assert "https://" not in combined
    assert "http://" not in combined
    assert "cdn" not in combined.lower()


def test_mutating_requests_include_local_ui_marker():
    script = (STATIC / "assets" / "app.js").read_text(encoding="utf-8")

    assert 'request.headers["X-DataLab-UI"] = "browser"' in script
