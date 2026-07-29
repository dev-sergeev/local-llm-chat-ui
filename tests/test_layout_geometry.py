import re
from pathlib import Path


CSS_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "datalab_chat"
    / "static"
    / "assets"
    / "app.css"
)


def _block_contents(source: str, opening_brace: int) -> str:
    depth = 0
    for index in range(opening_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening_brace + 1 : index]
    raise AssertionError("CSS block is not closed")


def _rule_contents(source: str, selector: str) -> str:
    rule = re.search(rf"(?m)^\s*{re.escape(selector)}\s*\{{", source)
    assert rule is not None, f"CSS rule {selector!r} is missing"
    return _block_contents(source, rule.end() - 1)


def _media_contents(source: str, query: str) -> tuple[str, int]:
    media = re.search(rf"@media\s*\(\s*{re.escape(query)}\s*\)\s*\{{", source)
    assert media is not None, f"CSS media query {query!r} is missing"
    return _block_contents(source, media.end() - 1), media.start()


def test_app_shell_constrains_its_grid_row_on_desktop_and_mobile():
    css = CSS_PATH.read_text(encoding="utf-8")
    mobile_rules, mobile_start = _media_contents(css, "max-width: 820px")
    base_app_shell = _rule_contents(css[:mobile_start], ".app-shell")
    mobile_app_shell = _rule_contents(mobile_rules, ".app-shell")

    assert "grid-template-rows: minmax(0, 1fr);" in base_app_shell
    assert "grid-template-columns: minmax(0, 1fr);" in mobile_app_shell
    assert "display: block;" not in mobile_app_shell


def test_expanded_composer_stays_in_the_chat_grid_without_covering_messages():
    css = CSS_PATH.read_text(encoding="utf-8")
    mobile_rules, mobile_start = _media_contents(css, "max-width: 820px")
    base_rules = css[:mobile_start]
    chat_view = _rule_contents(base_rules, ".chat-view")
    composer = _rule_contents(base_rules, ".composer-wrap")
    messages = _rule_contents(base_rules, ".messages")
    messages_inner = _rule_contents(base_rules, ".messages-inner")
    mobile_messages_inner = _rule_contents(mobile_rules, ".messages-inner")

    assert "display: grid;" in chat_view
    assert "grid-template-rows: minmax(0, 1fr) auto;" in chat_view
    assert "grid-row: 1;" in messages
    assert "position: relative;" in composer
    assert "grid-row: 2;" in composer
    assert "position: absolute;" not in composer
    assert "padding: 34px 28px 36px;" in messages_inner
    assert "padding: 26px 15px 28px;" in mobile_messages_inner
    assert "190px" not in messages_inner
    assert "180px" not in mobile_messages_inner
