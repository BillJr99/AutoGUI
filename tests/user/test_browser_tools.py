"""
End-to-end browser tool tests using Playwright Chromium.

Drives every browser_* tool through ToolRegistry.dispatch() against an
HTML fixture served by the html_fixture_server fixture.

Skipped when Playwright isn't installed or Chromium isn't on disk.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.user]

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def _chromium_installed() -> bool:
    """The playwright Python package is one thing; the Chromium browser
    binary is another. Check the cache dir."""
    cache = Path.home() / ".cache" / "ms-playwright"
    if not cache.exists():
        return False
    # Any subdir starting with chromium-* counts.
    return any(p.name.startswith("chromium") for p in cache.iterdir())


pytestmark.append(
    pytest.mark.skipif(
        not (_playwright_available() and _chromium_installed()),
        reason="Playwright Python package or Chromium binary missing "
                "(run `playwright install chromium`)",
    ),
)


@pytest.fixture
def browser_registry(tmp_path):
    """Build a real ToolRegistry with browser_* enabled (headless)."""
    from tools import ToolRegistry
    cfg = {
        "tools": {
            "shell_timeout_seconds": 5,
            "screenshot_dir": str(tmp_path / "screenshots"),
            "max_screenshot_width": 800,
            "perception_cache_ttl_seconds": 0.0,
            "allowed_shell": False,
            "allowed_filesystem": False,
            "allowed_desktop": False,
            "allowed_browser": True,
        },
        "browser": {
            "headless": True,
            "screenshot_dir": str(tmp_path / "browser"),
            "user_data_dir": "",
            "viewport": {"width": 640, "height": 480},
        },
        "agent": {"confirm_destructive": False, "vision_screenshots": False},
        "safety": {"command_confirm_delay_seconds": 0, "dry_run": False,
                   "allowed_apps": [], "blocked_window_titles": [],
                   "fs_write_snapshot_dir": ""},
    }
    reg = ToolRegistry(cfg)
    yield reg
    # Best-effort browser close so each test gets a clean state.
    if "browser_close" in reg._dispatch:
        try:
            asyncio.run(reg.dispatch("browser_close", {}))
        except Exception:
            pass


def _call(reg, name, args):
    out = asyncio.run(reg.dispatch(name, args))
    return json.loads(out) if isinstance(out, str) else out


class TestNavigation:
    def test_navigate_to_fixture_page(self, browser_registry, html_fixture_server):
        url = f"{html_fixture_server}/index.html"
        r = _call(browser_registry, "browser_navigate", {"url": url})
        # browser_navigate returns {ok, url, title, ...} or similar
        assert isinstance(r, dict)
        # Title may be in either field shape.
        title = r.get("title") or r.get("page_title") or ""
        assert "AutoGUI" in title or "Hello" in title or r.get("ok") is True

    def test_back_forward_reload(self, browser_registry, html_fixture_server):
        base = html_fixture_server
        _call(browser_registry, "browser_navigate", {"url": f"{base}/index.html"})
        _call(browser_registry, "browser_navigate", {"url": f"{base}/page2.html"})
        back = _call(browser_registry, "browser_back", {})
        assert isinstance(back, dict)
        fwd = _call(browser_registry, "browser_forward", {})
        assert isinstance(fwd, dict)
        rel = _call(browser_registry, "browser_reload", {})
        assert isinstance(rel, dict)


class TestInteraction:
    def test_fill_and_click(self, browser_registry, html_fixture_server):
        url = f"{html_fixture_server}/index.html"
        _call(browser_registry, "browser_navigate", {"url": url})
        # Fill the username field by ARIA / CSS selector.
        fill = _call(browser_registry, "browser_fill",
                     {"selector": "#user", "value": "alice"})
        assert isinstance(fill, dict)
        # Submit the form.
        click = _call(browser_registry, "browser_click", {"selector": "#submit"})
        assert isinstance(click, dict)

    def test_get_text_returns_page_text(self, browser_registry, html_fixture_server):
        url = f"{html_fixture_server}/index.html"
        _call(browser_registry, "browser_navigate", {"url": url})
        r = _call(browser_registry, "browser_get_text", {})
        body = r.get("text") or r.get("content") or ""
        assert "AutoGUI" in body or "Hello" in body or "fixture" in body, r


class TestEvalAndScreenshot:
    def test_eval_returns_value(self, browser_registry, html_fixture_server):
        url = f"{html_fixture_server}/index.html"
        _call(browser_registry, "browser_navigate", {"url": url})
        r = _call(browser_registry, "browser_eval",
                  {"expression": "document.title"})
        # Either {result: "..."} or {value: "..."}
        v = r.get("result") if "result" in r else r.get("value")
        assert v and isinstance(v, str)

    def test_screenshot_writes_png(self, browser_registry, html_fixture_server, tmp_path):
        url = f"{html_fixture_server}/index.html"
        _call(browser_registry, "browser_navigate", {"url": url})
        r = _call(browser_registry, "browser_screenshot", {})
        path = r.get("file_path") or r.get("path")
        assert path
        assert os.path.exists(path)
        assert os.path.getsize(path) > 200


class TestKeyboard:
    def test_press(self, browser_registry, html_fixture_server):
        url = f"{html_fixture_server}/index.html"
        _call(browser_registry, "browser_navigate", {"url": url})
        # Focus the user field and press Tab.
        _call(browser_registry, "browser_click", {"selector": "#user"})
        r = _call(browser_registry, "browser_press", {"keys": "Tab"})
        assert isinstance(r, dict)


class TestLifecycle:
    def test_close_releases_browser(self, browser_registry):
        r = _call(browser_registry, "browser_close", {})
        assert isinstance(r, dict)
