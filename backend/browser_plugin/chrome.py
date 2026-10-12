"""Launch and stop Chromium with persistent profile + optional proxy."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from .cdp import CDP, CDPError, list_targets, wait_debugger


def find_chrome() -> str:
    env = os.environ.get("CHROME_BIN")
    if env and Path(env).exists():
        return env
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        path = shutil.which(name)
        if path:
            return path
    raise FileNotFoundError("Chromium не найден в контейнере (CHROME_BIN / PATH)")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ChromeSession:
    """One Chromium process bound to a named profile directory."""

    def __init__(
        self,
        name: str,
        profile_dir: Path,
        *,
        proxy: str | None = None,
        headless: bool = True,
        timezone: str | None = None,
    ):
        self.name = name
        self.profile_dir = Path(profile_dir)
        self.proxy = proxy
        self.headless = headless
        self.timezone = timezone
        self.user_agent: str | None = None
        self.last_used = time.monotonic()
        self.port: int | None = None
        self.proc: subprocess.Popen | None = None
        self.cdp: CDP | None = None
        self.chrome_bin = find_chrome()
        self.tab_id: str | None = None
        self._order: list[str] = []  # порядок вкладок по первому появлению — номера не «прыгают»
        self._frame: dict | None = None
        self._frame_cond = threading.Condition()
        self._screencast_on = False
        self._want_until = 0.0
        self._inflight: set[str] = set()
        self._last_net = time.monotonic()

    @property
    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def start(self) -> None:
        if self.running:
            return
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.port = free_port()
        args = [
            self.chrome_bin,
            f"--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-features=Translate,MediaRouter",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-gpu",
            "--window-size=1280,800",
            # обычный Chrome, а не «робот»: без флага автоматизации, русский язык интерфейса и сайтов
            "--disable-blink-features=AutomationControlled",
            "--lang=ru-RU",
            "about:blank",
        ]
        if self.headless:
            args.insert(1, "--headless=new")
        if self.proxy:
            args.append(f"--proxy-server={self.proxy}")
        extra = (os.environ.get("CHROME_FLAGS") or "").split()
        args[1:1] = [f for f in extra if f and f not in args]

        self.proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            version = wait_debugger(self.port, timeout=45)
            # headless-Chrome подписывается «HeadlessChrome» — многие сайты по этому слову сразу показывают капчу
            self.user_agent = str(version.get("User-Agent") or "").replace("HeadlessChrome", "Chrome") or None
            self._attach(None)
        except Exception:
            self.stop()
            raise

    # --- tabs ---------------------------------------------------------------------

    def pages(self) -> list[dict]:
        if not self.port:
            return []
        found = [
            t for t in list_targets(self.port)
            if t.get("type") == "page" and not str(t.get("url", "")).startswith("devtools://")
        ]
        ids = {t["id"] for t in found}
        self._order = [i for i in self._order if i in ids]
        for t in found:
            if t["id"] not in self._order:
                self._order.append(t["id"])
        return sorted(found, key=lambda t: self._order.index(t["id"]))

    def _attach(self, target_id: str | None) -> None:
        """Connect the CDP client to a page target (default: the first page)."""
        assert self.port
        pages = self.pages()
        if not pages:
            with httpx.Client(timeout=5.0) as client:
                client.put(f"http://127.0.0.1:{self.port}/json/new?about:blank")
            pages = self.pages()
        if not pages:
            raise CDPError("нет page-target в Chrome")
        target = next((t for t in pages if t.get("id") == target_id), pages[0])
        ws = target.get("webSocketDebuggerUrl")
        if not ws:
            raise CDPError("у вкладки нет webSocketDebuggerUrl")
        old = self.cdp
        self.cdp = None
        if old:
            old.close()
        self._screencast_on = False
        self._inflight = set()
        self._last_net = time.monotonic()
        cdp = CDP(ws, on_event=lambda msg: self._on_event(cdp, msg))
        cdp.connect()
        self.cdp = cdp
        self.tab_id = target["id"]
        cdp.call("Page.enable")
        cdp.call("DOM.enable")
        cdp.call("Runtime.enable")
        cdp.call("Network.enable")
        if self.user_agent:
            cdp.call("Network.setUserAgentOverride", {"userAgent": self.user_agent,
                                                      "acceptLanguage": "ru-RU,ru;q=0.9,en;q=0.8"})
        if self.timezone:
            try:
                cdp.call("Emulation.setTimezoneOverride", {"timezoneId": self.timezone})
            except CDPError:
                pass
        for method, params in (
            ("Accessibility.enable", None),
            ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
        ):
            try:
                cdp.call(method, params)
            except CDPError:
                pass
        if time.monotonic() < self._want_until:
            self._start_screencast(cdp)

    def tabs(self) -> list[dict]:
        out = []
        for i, t in enumerate(self.pages()):
            out.append({
                "index": i,
                "id": t.get("id"),
                "url": t.get("url"),
                "title": t.get("title"),
                "active": t.get("id") == self.tab_id,
            })
        return out

    def resolve_tab(self, ref: str | int) -> str:
        pages = self.pages()
        text = str(ref).strip()
        if text.isdigit() and len(text) < 4:
            idx = int(text)
            if idx >= len(pages):
                raise ValueError(f"вкладки №{idx} нет (всего {len(pages)}); список — pulse browser tabs")
            return pages[idx]["id"]
        hits = [t["id"] for t in pages if t["id"] == text] or \
               [t["id"] for t in pages if t["id"].startswith(text)]
        if len(hits) != 1:
            raise ValueError(f"вкладка «{text}» не найдена или неоднозначна; список — pulse browser tabs")
        return hits[0]

    def select_tab(self, ref: str | int) -> str:
        self.ensure()
        tid = self.resolve_tab(ref)
        if tid != self.tab_id or not (self.cdp and self.cdp.alive):
            self._attach(tid)
        try:
            self.cdp.call("Page.bringToFront")  # type: ignore[union-attr]
        except CDPError:
            pass
        return tid

    def new_tab(self, url: str | None = None) -> str:
        self.ensure()
        with httpx.Client(timeout=10.0) as client:
            resp = client.put(f"http://127.0.0.1:{self.port}/json/new?about:blank")
            resp.raise_for_status()
            tid = resp.json()["id"]
        self._attach(tid)
        try:
            self.cdp.call("Page.bringToFront")  # type: ignore[union-attr]
        except CDPError:
            pass
        if url and url != "about:blank":
            self.navigate(url)
        return tid

    def close_tab(self, ref: str | int | None = None) -> str:
        self.ensure()
        tid = self.resolve_tab(ref) if ref not in (None, "") else (self.tab_id or "")
        if len(self.pages()) <= 1:
            raise ValueError("это последняя вкладка — закройте всю сессию: pulse browser close")
        with httpx.Client(timeout=5.0) as client:
            client.get(f"http://127.0.0.1:{self.port}/json/close/{tid}")
        if tid == self.tab_id:
            time.sleep(0.2)
            self._attach(None)
        return tid

    # --- screencast ---------------------------------------------------------------

    def _start_screencast(self, cdp: CDP) -> None:
        cdp.call("Page.startScreencast", {
            "format": "jpeg", "quality": 60, "maxWidth": 1280, "maxHeight": 800, "everyNthFrame": 1,
        })
        self._screencast_on = True

    def want_frames(self, ttl: float = 20.0) -> None:
        """Keep the screencast running for ttl seconds (it stops itself when nobody asks)."""
        self._want_until = time.monotonic() + ttl
        cdp = self.ensure()
        if not self._screencast_on:
            self._start_screencast(cdp)

    def wait_frame(self, since: int = 0, timeout: float = 2.0) -> dict | None:
        self.want_frames()
        deadline = time.monotonic() + timeout
        with self._frame_cond:
            while True:
                frame = self._frame
                if frame and frame["seq"] > since and frame["tab"] == self.tab_id:
                    return frame
                left = deadline - time.monotonic()
                if left <= 0:
                    return frame if frame and frame["tab"] == self.tab_id else None
                self._frame_cond.wait(left)

    def _on_event(self, cdp: CDP, msg: dict) -> None:
        if cdp is not self.cdp:
            return
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "Page.screencastFrame":
            cdp.send("Page.screencastFrameAck", {"sessionId": params.get("sessionId")})
            meta = params.get("metadata") or {}
            with self._frame_cond:
                seq = (self._frame or {}).get("seq", 0) + 1
                self._frame = {
                    "seq": seq,
                    "image_base64": params.get("data"),
                    "mime": "image/jpeg",
                    "width": int(meta.get("deviceWidth") or 1280),
                    "height": int(meta.get("deviceHeight") or 800),
                    "tab": self.tab_id,
                }
                self._frame_cond.notify_all()
            if time.monotonic() > self._want_until:
                self._screencast_on = False
                cdp.send("Page.stopScreencast")
        elif method == "Network.requestWillBeSent":
            self._inflight.add(params.get("requestId", ""))
            self._last_net = time.monotonic()
        elif method in ("Network.loadingFinished", "Network.loadingFailed"):
            self._inflight.discard(params.get("requestId", ""))
            self._last_net = time.monotonic()

    # --- waits --------------------------------------------------------------------

    def wait(
        self,
        *,
        selector: str | None = None,
        url: str | None = None,
        text: str | None = None,
        idle: bool = False,
        ms: float | None = None,
        timeout: float = 15.0,
        visible: bool = True,
    ) -> dict:
        """Wait until ALL given conditions hold (or timeout). Never raises on timeout."""
        started = time.monotonic()
        if ms:
            time.sleep(min(float(ms), 60000) / 1000.0)
        conds = [k for k, v in (("selector", selector), ("url", url), ("text", text)) if v] + \
                (["networkidle"] if idle else [])
        deadline = started + max(0.1, float(timeout))
        status: dict[str, bool] = {}
        while conds:
            status = {}
            if selector:
                status["selector"] = bool(self._safe_eval(
                    f"""(() => {{
                      const el = document.querySelector({json.dumps(selector)});
                      if (!el) return false;
                      if (!{json.dumps(visible)}) return true;
                      const r = el.getBoundingClientRect(), st = getComputedStyle(el);
                      return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
                    }})()"""))
            if url:
                href = str(self._safe_eval("location.href") or "")
                if url.startswith("re:"):
                    status["url"] = bool(re.search(url[3:], href))
                else:
                    status["url"] = url in href
            if text:
                status["text"] = bool(self._safe_eval(
                    f"document.body ? document.body.innerText.includes({json.dumps(text)}) : false"))
            if idle:
                status["networkidle"] = not self._inflight and time.monotonic() - self._last_net >= 0.5
            if all(status.values()):
                break
            if time.monotonic() >= deadline:
                return {
                    "ok": False,
                    "timeout": True,
                    "error": "не дождались: " + ", ".join(k for k, v in status.items() if not v),
                    "waited_sec": round(time.monotonic() - started, 2),
                }
            time.sleep(0.2)
        return {"ok": True, "waited_sec": round(time.monotonic() - started, 2), "conditions": conds}

    def _safe_eval(self, expression: str) -> Any:
        try:
            return self.evaluate(expression)
        except CDPError:
            return None

    def stop(self) -> None:
        if self.cdp:
            try:
                self.cdp.close()
            except Exception:  # noqa: BLE001
                pass
            self.cdp = None
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
        self.proc = None
        self.port = None
        self.tab_id = None
        self._order = []
        self._screencast_on = False
        self._frame = None
        # Chromium leaves dangling Singleton* symlinks that break plugin data snapshots
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            path = self.profile_dir / name
            try:
                if path.is_symlink() or path.exists():
                    path.unlink()
            except OSError:
                pass

    def touch(self) -> None:
        self.last_used = time.monotonic()

    def healthy(self) -> bool:
        """Отвечает ли вкладка: после таймаута сайта рендерер бывает «повисшим» — тогда Chrome перезапускают."""
        if not self.running:
            return False
        if not self.cdp or not self.cdp.alive:
            return True                                   # переподключится в ensure()
        try:
            self.cdp.call("Runtime.evaluate", {"expression": "1", "returnByValue": True}, timeout=5)
            return True
        except CDPError:
            return False

    def ensure(self) -> CDP:
        self.last_used = time.monotonic()
        if not self.running:
            self.start()
        elif not self.cdp or not self.cdp.alive:
            self._attach(self.tab_id)  # вкладку закрыла страница или она упала
        assert self.cdp is not None
        return self.cdp

    def navigate(self, url: str, timeout: float = 60.0) -> dict:
        cdp = self.ensure()
        try:
            result = cdp.call("Page.navigate", {"url": url}, timeout=timeout) or {}
        except CDPError as exc:
            if "таймаут" not in str(exc):
                raise
            raise CDPError(f"сайт не ответил за {int(timeout)} с — сервер Пульса не достаёт до него (блокировка по "
                           "стране?): задайте сессии прокси") from None
        if result.get("errorText"):
            error = result["errorText"]
            hint = (" — сервер Пульса не достаёт до сайта (блокировка по стране?): задайте сессии прокси"
                    if any(e in error for e in ("TIMED_OUT", "CONNECTION", "NAME_NOT_RESOLVED", "TUNNEL", "PROXY"))
                    else "")
            raise CDPError(f"сайт не открылся: {error}{hint}")
        # Wait for load event roughly
        deadline = time.monotonic() + min(timeout, 45)
        last = {"url": url, "title": ""}
        while time.monotonic() < deadline:
            try:
                info = self.evaluate(
                    "({url: location.href, title: document.title, ready: document.readyState})"
                )
                if isinstance(info, dict):
                    last = info
                    if info.get("ready") in ("interactive", "complete"):
                        break
            except CDPError:
                pass
            time.sleep(0.25)
        return last

    def evaluate(self, expression: str) -> Any:
        cdp = self.ensure()
        result = cdp.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            text = detail.get("text") or detail.get("exception", {}).get("description") or "ошибка JS"
            raise CDPError(text)
        return (result.get("result") or {}).get("value")

    def screenshot_png(self) -> bytes:
        import base64

        cdp = self.ensure()
        result = cdp.call("Page.captureScreenshot", {"format": "png", "fromSurface": True})
        data = result.get("data") or ""
        return base64.b64decode(data)

    def current(self) -> dict:
        try:
            info = self.evaluate(
                "({url: location.href, title: document.title, ready: document.readyState})"
            )
            if isinstance(info, dict):
                return info
        except CDPError:
            pass
        return {"url": None, "title": None, "ready": None}
