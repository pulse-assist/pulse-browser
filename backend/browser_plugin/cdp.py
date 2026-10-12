"""Minimal sync Chrome DevTools Protocol client over websocket."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import httpx
import websocket


class CDPError(Exception):
    pass


class CDP:
    """One CDP websocket (usually the page target). Thread-safe call()."""

    def __init__(self, ws_url: str, timeout: float = 30.0, on_event=None):
        self.ws_url = ws_url
        self.on_event = on_event
        self._send_lock = threading.Lock()
        self.timeout = timeout
        self._id = 0
        self._lock = threading.Lock()
        self._pending: dict[int, dict] = {}
        self._events: list[dict] = []
        self._ws: websocket.WebSocket | None = None
        self._reader: threading.Thread | None = None
        self._closed = threading.Event()

    def connect(self) -> None:
        self._ws = websocket.create_connection(self.ws_url, timeout=self.timeout, suppress_origin=True)
        self._ws.settimeout(1.0)
        self._reader = threading.Thread(target=self._read_loop, name="cdp-reader", daemon=True)
        self._reader.start()

    def close(self) -> None:
        self._closed.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:  # noqa: BLE001
            pass
        self._ws = None

    @property
    def alive(self) -> bool:
        return bool(self._ws and self._reader and self._reader.is_alive() and not self._closed.is_set())

    def send(self, method: str, params: dict | None = None) -> None:
        """Fire-and-forget (safe to use from the reader thread, e.g. screencast ack)."""
        ws = self._ws
        if not ws:
            return
        with self._lock:
            self._id += 1
            msg_id = self._id
        with self._send_lock:
            ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))

    def call(self, method: str, params: dict | None = None, timeout: float | None = None) -> Any:
        if not self._ws:
            raise CDPError("CDP не подключён")
        wait = timeout if timeout is not None else self.timeout
        with self._lock:
            self._id += 1
            msg_id = self._id
            event = threading.Event()
            self._pending[msg_id] = {"event": event, "result": None, "error": None}
            payload = {"id": msg_id, "method": method, "params": params or {}}
            with self._send_lock:
                self._ws.send(json.dumps(payload))
        if not event.wait(wait):
            with self._lock:
                self._pending.pop(msg_id, None)
            raise CDPError(f"таймаут CDP {method}")
        with self._lock:
            slot = self._pending.pop(msg_id, {})
        if slot.get("error"):
            err = slot["error"]
            raise CDPError(f"{method}: {err.get('message') or err}")
        return slot.get("result")

    def _read_loop(self) -> None:
        while not self._closed.is_set() and self._ws:
            try:
                raw = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:  # noqa: BLE001
                break
            if not raw:
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if "id" in msg:
                with self._lock:
                    slot = self._pending.get(msg["id"])
                    if not slot:
                        continue
                    if "error" in msg:
                        slot["error"] = msg["error"]
                    else:
                        slot["result"] = msg.get("result")
                    slot["event"].set()
            elif self.on_event:
                try:
                    self.on_event(msg)
                except Exception:  # noqa: BLE001
                    pass
        self._closed.set()


def wait_debugger(port: int, timeout: float = 30.0) -> dict:
    """Poll /json/version until Chrome CDP is ready."""
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/json/version"
    last_err = ""
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=2.0) as client:
                resp = client.get(url)
                if resp.status_code == 200:
                    return resp.json()
                last_err = f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
        time.sleep(0.2)
    raise CDPError(f"Chrome CDP не ответил на порту {port}: {last_err}")


def list_targets(port: int) -> list[dict]:
    with httpx.Client(timeout=5.0) as client:
        resp = client.get(f"http://127.0.0.1:{port}/json/list")
        resp.raise_for_status()
        return resp.json()


def page_ws_url(port: int) -> str:
    targets = list_targets(port)
    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        # open a blank page via new tab endpoint
        with httpx.Client(timeout=5.0) as client:
            client.put(f"http://127.0.0.1:{port}/json/new?about:blank")
        targets = list_targets(port)
        pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        raise CDPError("нет page-target в Chrome")
    # Prefer non-devtools pages; take the first page
    ws = pages[0].get("webSocketDebuggerUrl")
    if not ws:
        raise CDPError("у page-target нет webSocketDebuggerUrl")
    return ws
