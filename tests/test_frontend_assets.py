from __future__ import annotations

import base64
import hashlib
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

from datalab_chat.web import PATH_PREFIX_BOOTSTRAP_SHA256


STATIC = Path("src/datalab_chat/static")


class DocumentContract(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.scripts = []
        self.stylesheets = []
        self.inline_scripts = []
        self._inside_script_without_src = False
        self._inline_script_parts = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "id" in attributes:
            self.ids.add(attributes["id"])
        if tag == "script":
            self.scripts.append(attributes)
            self._inside_script_without_src = "src" not in attributes
            self._inline_script_parts = []
        if tag == "link" and attributes.get("rel") == "stylesheet":
            self.stylesheets.append(attributes.get("href"))
        assert "style" not in attributes

    def handle_data(self, data):
        if self._inside_script_without_src:
            self._inline_script_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script":
            if self._inside_script_without_src:
                self.inline_scripts.append("".join(self._inline_script_parts))
            self._inside_script_without_src = False


def test_frontend_has_complete_dom_contract():
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
    assert parser.stylesheets == ["assets/app.css"]
    assert parser.scripts == [
        {},
        {"type": "module", "src": "assets/app.js"},
    ]
    assert len(parser.inline_scripts) == 1


def test_frontend_assets_are_self_contained_and_do_not_reference_cdn():
    assets = [
        STATIC / "index.html",
        STATIC / "assets" / "app.css",
        STATIC / "assets" / "app.js",
        STATIC / "assets" / "markdown.js",
        STATIC / "assets" / "ui-state.js",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in assets)

    assert "https://" not in combined
    assert "http://" not in combined
    assert "cdn" not in combined.lower()


def test_frontend_assets_follow_a_path_prefixed_localhost_proxy():
    parser = DocumentContract()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    forwarded_page = "https://preview.example/forwarded/8765/"

    assert urljoin(forwarded_page, parser.stylesheets[0]) == (
        forwarded_page + "assets/app.css"
    )
    assert urljoin(forwarded_page, parser.scripts[1]["src"]) == (
        forwarded_page + "assets/app.js"
    )


def test_frontend_normalizes_a_forwarded_prefix_without_trailing_slash():
    parser = DocumentContract()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))

    bootstrap = parser.inline_scripts[0]
    digest = base64.b64encode(hashlib.sha256(bootstrap.encode("utf-8")).digest())
    assert bootstrap == (
        'if (location.pathname.slice(-1) !== "/") '
        'location.replace(location.pathname + "/" + location.search + location.hash);'
    )
    assert PATH_PREFIX_BOOTSTRAP_SHA256 == f"sha256-{digest.decode('ascii')}"


def test_frontend_does_not_depend_on_a_proxy_security_marker():
    script = (STATIC / "assets" / "app.js").read_text(encoding="utf-8")

    assert "X-DataLab-UI" not in script


def test_frontend_modules_and_api_follow_the_loaded_application_root():
    script = (STATIC / "assets" / "app.js").read_text(encoding="utf-8")

    assert 'from "./markdown.js"' in script
    assert 'from "./ui-state.js"' in script
    assert 'const APP_ROOT_URL = new URL("../", import.meta.url);' in script
    assert "new URL(relativePath, APP_ROOT_URL)" in script


def test_frontend_keeps_fallbacks_for_older_mainstream_browsers():
    css = (STATIC / "assets" / "app.css").read_text(encoding="utf-8")
    markdown = (STATIC / "assets" / "markdown.js").read_text(encoding="utf-8")

    assert "height: 100vh;\n  height: 100dvh;" in css
    assert (
        "height: min(680px, calc(100vh - 40px));\n"
        "  height: min(680px, calc(100dvh - 40px));"
    ) in css
    assert (
        "outline: 3px solid var(--accent);\n"
        "  outline: 3px solid color-mix("
    ) in css
    assert ".replaceAll(" not in markdown
    assert "-webkit-backdrop-filter: blur(14px);\n  backdrop-filter: blur(14px);" in css
