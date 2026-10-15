"""Подтверждения владельца и права агента — без Chrome: оплату подтверждает только владелец, один раз и именно ту."""

import json
import os
import tempfile

os.environ.setdefault("PULSE_PLUGIN_DATA", tempfile.mkdtemp(prefix="browser-test-"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from browser_plugin import app as module  # noqa: E402
from browser_plugin.policy import PolicyError, check_mutation, needs_owner_confirm, parse_patterns  # noqa: E402
from browser_plugin.sessions import _disposition_name, _normalize_url, _ref_selector  # noqa: E402
from browser_plugin.store import Store  # noqa: E402

OWNER = {"X-Pulse-Caller": "owner"}
AGENT = {"X-Pulse-Caller": "agent"}


def test_mutation_policy(tmp_path):
    patterns = parse_patterns(None)
    with pytest.raises(PolicyError, match="read-only"):
        check_mutation({"name": "shop", "mode": "read-only"}, "https://shop.test/", patterns=patterns)
    write = {"name": "shop", "mode": "write", "allowlist": ["shop.test"]}
    assert check_mutation(write, "https://shop.test/cart", patterns=patterns) is False
    assert check_mutation(write, "https://shop.test/checkout/1", patterns=patterns) is True
    with pytest.raises(PolicyError, match="allowlist"):
        check_mutation(write, "https://evil.test/", patterns=patterns)


def test_approval_is_single_use_and_bound(tmp_path):
    store = Store(tmp_path)
    target = {"url": "https://shop.test/checkout", "label": "Оплатить 1 990 ₽"}
    cid = store.add_pending("shop", "click", {**target, "title": "Оформление"})
    with pytest.raises(PermissionError, match="ещё не подтвердил"):
        store.use_approval(cid, "shop", "click", target)
    store.decide_pending(cid, approve=True)
    with pytest.raises(PermissionError, match="другое действие"):
        store.use_approval(cid, "shop", "click", {**target, "label": "Оплатить 99 990 ₽"})
    assert store.use_approval(cid, "shop", "click", target)["status"] == "used"
    with pytest.raises(PermissionError, match="уже использовано"):
        store.use_approval(cid, "shop", "click", target)
    rejected = store.add_pending("shop", "click", target)
    store.decide_pending(rejected, approve=False)
    with pytest.raises(PermissionError, match="отклонил"):
        store.use_approval(rejected, "shop", "click", target)


@pytest.fixture
def client():
    return TestClient(module.app)


def test_only_owner_confirms(client):
    cid = module.store.add_pending("demo", "click", {"url": "https://shop.test/checkout", "label": "Оплатить"})
    denied = client.post(f"/confirm/{cid}", json={"approve": True}, headers=AGENT)
    assert denied.status_code == 403 and "владелец" in denied.json()["detail"]
    assert client.post(f"/confirm/{cid}", json={"approve": True}).status_code == 403     # без заголовка — агент
    ok = client.post(f"/confirm/{cid}", json={"approve": True}, headers=OWNER)
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    # агентской команды «подтвердить» больше нет
    assert client.post("/commands/confirm", json={"session": "demo", "id": cid}).status_code == 404
    status = client.post("/commands/pending", json={"id": cid}, headers=AGENT).json()
    assert status["status"] == "approved"


def test_write_mode_only_from_owner(client):
    created = client.post("/commands/session create", json={"name": "shop", "mode": "write"}, headers=AGENT)
    assert created.status_code == 400 and "включает владелец" in created.json()["detail"]
    assert client.post("/commands/session create", json={"name": "shop"}, headers=AGENT).json()["mode"] == "read-only"
    up = client.post("/commands/session update", json={"name": "shop", "mode": "write"}, headers=AGENT)
    assert up.status_code == 400
    assert client.patch("/sessions/shop", json={"mode": "write"}, headers=AGENT).status_code == 403
    assert client.patch("/sessions/shop", json={"mode": "write"}, headers=OWNER).json()["mode"] == "write"
    # открыть страницу плагина — не значит запустить Chrome
    assert client.get("/sessions/shop/view", headers=OWNER).json() == {"session": "shop", "running": False}
    assert client.get("/sessions/shop/frame", headers=OWNER).json()["running"] is False
    client.delete("/sessions/shop", headers=OWNER)


FULL = {"X-Pulse-Caller": "agent", "X-Pulse-Agent-Settings": '{"full_control":true}'}


def test_full_control_agent_gets_write(client):
    """Галочка агента (X-Pulse-Agent-Settings от ядра): сессия сразу write, явный write разрешён."""
    created = client.post("/commands/session create", json={"name": "auto"}, headers=FULL)
    assert created.status_code == 200 and created.json()["mode"] == "write"
    assert client.post(
        "/commands/session create", json={"name": "auto2", "mode": "write"}, headers=FULL,
    ).json()["mode"] == "write"
    client.delete("/sessions/auto", headers=OWNER)
    client.delete("/sessions/auto2", headers=OWNER)


def test_full_control_skips_checkout_confirm():
    patterns = parse_patterns(None)
    write = {"name": "shop", "mode": "write", "allowlist": []}
    assert needs_owner_confirm(write, "https://shop.test/checkout", patterns=patterns, full_control=False)
    assert not needs_owner_confirm(write, "https://shop.test/checkout", patterns=patterns, full_control=True)
    assert not needs_owner_confirm(write, "https://shop.test/cart", patterns=patterns, full_control=False)


def test_refs_and_urls():
    assert _ref_selector("e12") == '[data-pulse-ref="e12"]'
    assert _ref_selector("css:#buy") == "#buy"
    with pytest.raises(ValueError, match="snapshot"):
        _ref_selector("ax:5")
    assert _normalize_url("ozon.ru") == "https://ozon.ru"
    with pytest.raises(ValueError):
        _normalize_url("javascript:alert(1)")
    assert _disposition_name("attachment; filename*=UTF-8''%D0%B4%D0%BE%D0%BA.pdf") == "док.pdf"
    assert _disposition_name('attachment; filename="report.pdf"') == "report.pdf"


def test_commands_json_matches_code():
    from pathlib import Path

    names = {c["name"] for c in json.loads((Path(__file__).parents[2] / "commands.json").read_text(encoding="utf-8"))}
    assert names == set(module.plugin._commands)  # noqa: SLF001
