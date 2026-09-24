"""The CSP is script-src 'self' / style-src 'self' with no 'unsafe-inline', so
inline style attributes and script-driven inline styles are blocked by a
browser. Presentation state must use CSS classes."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

WEB_DIR = Path(__file__).resolve().parents[2] / "app" / "web"


def test_templates_have_no_inline_style_attributes_or_inline_scripts():
    for template in (WEB_DIR / "templates").glob("*.html"):
        html = template.read_text(encoding="utf-8")
        assert not re.search(r"\sstyle\s*=", html), f"{template.name} has an inline style attribute"
        assert not re.search(r"<script(?![^>]*\ssrc=)", html), f"{template.name} has an inline script"


def test_scripts_do_not_set_inline_styles():
    for script in (WEB_DIR / "static" / "js").glob("*.js"):
        source = script.read_text(encoding="utf-8")
        assert not re.search(r"\.style\.\w+\s*=", source), f"{script.name} assigns to element.style"
        assert "setAttribute(\"style\"" not in source and "setAttribute('style'" not in source


def test_invite_page_suppresses_the_referrer_before_any_asset_loads():
    html = (WEB_DIR / "templates" / "invite.html").read_text(encoding="utf-8")
    assert html.index('name="referrer"') < html.index("<link")


def test_the_response_policy_is_strict():
    csp = TestClient(app).get("/invite").headers["content-security-policy"]
    assert "style-src 'self'" in csp and "unsafe-inline" not in csp
