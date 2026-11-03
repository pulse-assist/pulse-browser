"""Несколько агентов в одной сессии, каждый в своей вкладке: команда с --tab не попадает в чужую вкладку."""

import os
import tempfile
import threading
import time

os.environ.setdefault("PULSE_PLUGIN_DATA", tempfile.mkdtemp(prefix="browser-test-"))

from browser_plugin import sessions as sessions_module  # noqa: E402
from browser_plugin.sessions import BrowserService  # noqa: E402
from browser_plugin.store import Store  # noqa: E402


class FakeChrome:
    """Как ChromeSession: одно подключение к «текущей» вкладке, select_tab его переключает."""

    running = True

    def __init__(self):
        self.tab_id = "A"
        self.urls = {"A": "about:blank", "B": "about:blank"}
        self.user_agent = "test"

    def healthy(self):
        return True

    def ensure(self):
        pass

    def select_tab(self, ref):
        self.tab_id = str(ref)
        time.sleep(0.01)               # даём другому потоку вклиниться между выбором вкладки и действием
        return self.tab_id

    def navigate(self, url):
        tab = self.tab_id
        time.sleep(0.02)
        self.urls[tab] = url
        return {"url": url, "title": tab}

    def current(self):
        return {"url": self.urls[self.tab_id], "title": self.tab_id}

    def wait(self, **cond):
        time.sleep(0.01)
        return {"ok": True}

    def evaluate(self, expr):
        time.sleep(0.01)
        return {"elements": [], "text": self.urls[self.tab_id], "full_text": self.urls[self.tab_id],
                "text_chars": 1}

    def screenshot_png(self):
        return b"png"

    def tabs(self):
        return [{"id": tab, "url": url} for tab, url in self.urls.items()]


def make_service(tmp_path):
    store = Store(tmp_path)
    store.create("shared", mode="write")
    service = BrowserService(store, {}, reaper=False)
    service._chrome["shared"] = FakeChrome()
    return service


def test_parallel_tabs_do_not_cross(tmp_path):
    service = make_service(tmp_path)
    errors = []

    def agent(tab):
        for i in range(15):
            url = f"https://site-{tab}.test/{i}"
            opened = service.open("shared", url, tab=tab)
            snap = service.snapshot("shared", tab=tab)
            if opened["tab"] != tab or snap["tab"] != tab or snap["text"] != url or opened["url"] != url:
                errors.append((tab, i, opened["tab"], snap["tab"], snap["text"]))

    threads = [threading.Thread(target=agent, args=(tab,)) for tab in ("A", "B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []


def test_busy_tab_lock_times_out(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr(sessions_module, "TAB_LOCK_WAIT", 0.1)
    lock = service._lock("shared")
    held = threading.Event()
    release = threading.Event()

    def holder():
        with lock:
            held.set()
            release.wait(2)

    thread = threading.Thread(target=holder)
    thread.start()
    held.wait(1)
    try:
        try:
            service.open("shared", "https://site.test/", tab="B")
        except sessions_module.SessionBusy as exc:
            assert "занята" in str(exc)
        else:
            raise AssertionError("ждали SessionBusy")
    finally:
        release.set()
        thread.join()


def test_without_tab_uses_current_tab(tmp_path):
    """Без --tab поведение прежнее: команда идёт в текущую вкладку и её не переключает."""
    service = make_service(tmp_path)
    service._chrome["shared"].tab_id = "B"
    out = service.open("shared", "https://site.test/x")
    assert out["tab"] == "B" and service._chrome["shared"].urls["B"] == "https://site.test/x"
