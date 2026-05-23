"""
End-to-end: drive the OSScreenObserver login.yaml scenario through
AutoGUI's ScreenObserverClient (REST). The Python client doesn't drive
the agent loop — it talks directly to OSO over the same wire AutoGUI
uses, exercising the integration contract.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.user, pytest.mark.integration]

ROOT = Path(__file__).resolve().parents[3]
OSO_ROOT = ROOT / "OSScreenObserver"
LOGIN_YAML = OSO_ROOT / "scenarios_examples" / "login.yaml"
sys.path.insert(0, str(ROOT))


@pytest.mark.asyncio
async def test_full_login_scenario_via_oso_client(oso_server_factory):
    """Spin up OSO, load login.yaml, drive find_element → click_element →
    set_value → assert oracle. This mirrors what AutoGUI does when it
    falls back to OSO for window-tree access."""
    if not LOGIN_YAML.exists():
        pytest.skip("login.yaml not present (submodule not initialised)")

    import aiohttp
    from urllib.parse import urlencode

    srv = oso_server_factory()
    base = srv["base_url"]

    async def post(session, path, body):
        async with session.post(base + path, json=body) as r:
            return r.status, await r.json()

    async def get(session, path, params=None):
        url = base + path
        if params:
            url += "?" + urlencode(params)
        async with session.get(url) as r:
            return r.status, await r.json()

    async with aiohttp.ClientSession() as s:
        # Load the scenario.
        status, body = await post(s, "/api/scenario/load",
                                   {"path": str(LOGIN_YAML)})
        assert status == 200 and body["ok"] is True

        # Drive the login: find each Edit, click it, type a value, click Login.
        _, ws = await get(s, "/api/windows")
        uid = ws["windows"][0]["window_uid"]

        for label, text in (("Username", "alice"), ("Password", "hunter2")):
            _, fe = await get(s, "/api/find_element",
                               {"window_uid": uid,
                                "selector": f'Window/Edit[name="{label}"]'})
            assert fe["ok"] is True
            await post(s, "/api/element/click",
                       {"window_uid": uid, "element_id": fe["element_id"]})
            await post(s, "/api/action", {"action": "type", "value": text})

        _, fe = await get(s, "/api/find_element",
                           {"window_uid": uid,
                            "selector": 'Window/Button[name="Login"]'})
        await post(s, "/api/element/click",
                   {"window_uid": uid, "element_id": fe["element_id"]})

        # Assert oracle pass.
        _, oracle = await post(s, "/api/assert_state",
                                {"predicate": [{"kind": "text_visible",
                                                  "regex": "Hello, alice"}]})
        assert oracle["all_passed"] is True
