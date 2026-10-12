"""Session manager: one Chromium + writer lock per named profile."""

from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .cdp import CDPError
from .chrome import ChromeSession
from .policy import NeedConfirm, PolicyError, check_mutation, parse_patterns, require_url_allowed
from .store import Store, utc_now


class SessionBusy(Exception):
    pass


class BrowserService:
    def __init__(self, store: Store, settings: dict, signal_fn: Callable | None = None):
        self.store = store
        self.settings = settings
        self.signal_fn = signal_fn
        self._chrome: dict[str, ChromeSession] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._writers: dict[str, str] = {}  # session -> holder id
        self._global = threading.RLock()
        self._approved: set[str] = set()

    def _lock(self, name: str) -> threading.RLock:
        with self._global:
            if name not in self._locks:
                self._locks[name] = threading.RLock()
            return self._locks[name]

    def _patterns(self) -> list[str]:
        return parse_patterns(self.settings.get("confirm_patterns"))

    def _headless(self) -> bool:
        val = self.settings.get("headless")
        return True if val is None else bool(val)

    def _default_mode(self) -> str:
        return (self.settings.get("default_mode") or "read-only").strip()

    def sessions_out(self) -> list[dict]:
        out = []
        for name, meta in sorted(self.store.list_sessions().items()):
            chrome = self._chrome.get(name)
            running = bool(chrome and chrome.running)
            current = chrome.current() if running else {}
            out.append({
                **meta,
                "proxy_set": bool(meta.get("proxy")),
                # never echo full proxy with credentials to agents/UI lists
                "proxy": _proxy_label(meta.get("proxy")),
                "running": running,
                "locked": name in self._writers,
                "url": current.get("url") or meta.get("last_url"),
                "title": current.get("title") or meta.get("last_title"),
                "cdp_port": chrome.port if running else None,
            })
        return out

    def create_session(
        self,
        name: str,
        *,
        mode: str | None = None,
        proxy: str | None = None,
        allowlist: str | None = None,
    ) -> dict:
        domains = _split_csv(allowlist)
        item = self.store.create(
            name,
            mode=mode or self._default_mode(),
            proxy=proxy,
            allowlist=domains,
        )
        return self._public(item)

    def update_session(self, name: str, **fields: Any) -> dict:
        if "allowlist" in fields and isinstance(fields["allowlist"], str):
            fields["allowlist"] = _split_csv(fields["allowlist"])
        # restart chrome if proxy changed while running
        proxy_changed = "proxy" in fields
        item = self.store.update(name, **fields)
        if proxy_changed and name in self._chrome:
            self.close(name)
        return self._public(item)

    def delete_session(self, name: str) -> dict:
        self.close(name)
        self.store.delete(name)
        return {"deleted": name}

    def ensure_chrome(self, name: str) -> ChromeSession:
        meta = self.store.get(name)
        with self._lock(name):
            chrome = self._chrome.get(name)
            if chrome and chrome.running:
                chrome.ensure()  # переподключит CDP, если вкладку закрыла страница
                return chrome
            chrome = ChromeSession(
                name,
                self.store.profile_dir(name),
                proxy=meta.get("proxy"),
                headless=self._headless(),
            )
            chrome.start()
            self._chrome[name] = chrome
            return chrome

    def _chrome_for(self, name: str, tab: str | int | None = None) -> ChromeSession:
        chrome = self.ensure_chrome(name)
        if tab not in (None, ""):
            chrome.select_tab(tab)
        return chrome

    @staticmethod
    def _post_wait(chrome: ChromeSession, wait: dict | None) -> dict | None:
        return chrome.wait(**wait) if wait else None

    def open(self, name: str, url: str, *, tab: str | int | None = None, new_tab: bool = False,
             wait: dict | None = None) -> dict:
        meta = self.store.get(name)
        require_url_allowed(url, meta)
        chrome = self._chrome_for(name, tab)
        before = chrome.screenshot_png()
        if new_tab:
            chrome.new_tab()
        info = chrome.navigate(url)
        waited = self._post_wait(chrome, wait)
        after = chrome.screenshot_png()
        self.store.update(name, last_url=info.get("url") or url, last_title=info.get("title"))
        shot = self._save_shot(name, "open", after)
        self.store.audit(
            name,
            "open",
            url=info.get("url") or url,
            title=info.get("title"),
            screenshot=shot,
            before_b64_len=len(before),
            tab=chrome.tab_id,
        )
        return {
            "session": name,
            "tab": chrome.tab_id,
            "url": info.get("url") or url,
            "title": info.get("title"),
            "screenshot": shot,
            "mode": meta.get("mode"),
            **({"wait": waited} if waited else {}),
        }

    def snapshot(self, name: str, fmt: str = "a11y", *, tab: str | int | None = None) -> dict:
        chrome = self._chrome_for(name, tab)
        current = chrome.current()
        png = chrome.screenshot_png()
        shot = self._save_shot(name, "snapshot", png)
        fmt = (fmt or "a11y").lower()
        tree = None
        html_path = None
        nodes_out = []
        if fmt in ("a11y", "both"):
            try:
                ax = chrome.cdp.call("Accessibility.getFullAXTree")  # type: ignore[union-attr]
                nodes = ax.get("nodes") or []
                nodes_out = _ax_summary(nodes)
                tree_path = self.store.artifact_dir(name) / "last_a11y.json"
                tree_path.write_text(json.dumps(nodes_out, ensure_ascii=False, indent=2), encoding="utf-8")
                tree = f"@browser/{name}/last_a11y.json"
            except CDPError as exc:
                tree = f"a11y unavailable: {exc}"
        if fmt in ("html", "both"):
            html = chrome.evaluate("document.documentElement.outerHTML") or ""
            path = self.store.artifact_dir(name) / "last.html"
            path.write_text(str(html)[:2_000_000], encoding="utf-8")
            html_path = f"@browser/{name}/last.html"
        self.store.update(name, last_url=current.get("url"), last_title=current.get("title"))
        self.store.audit(name, "snapshot", url=current.get("url"), screenshot=shot, format=fmt)
        return {
            "session": name,
            "tab": chrome.tab_id,
            "url": current.get("url"),
            "title": current.get("title"),
            "screenshot": shot,
            "a11y": tree,
            "nodes": nodes_out[:80],
            "html": html_path,
        }

    def view(self, name: str, *, tab: str | int | None = None) -> dict:
        chrome = self._chrome_for(name, tab)
        current = chrome.current()
        png = chrome.screenshot_png()
        shot = self._save_shot(name, "view", png)
        b64 = base64.b64encode(png).decode("ascii")
        return {
            "session": name,
            "url": current.get("url"),
            "title": current.get("title"),
            "screenshot": shot,
            "image_base64": b64,
            "width": 1280,
            "height": 800,
        }

    def tabs(self, name: str) -> dict:
        chrome = self.ensure_chrome(name)
        return {"session": name, "active": chrome.tab_id, "tabs": chrome.tabs()}

    def tab_new(self, name: str, url: str | None = None, *, wait: dict | None = None) -> dict:
        if url:
            require_url_allowed(url, self.store.get(name))
        chrome = self.ensure_chrome(name)
        with self._lock(name):
            chrome.new_tab(url)
            waited = self._post_wait(chrome, wait)
        self.store.audit(name, "tab_new", url=url, tab=chrome.tab_id)
        return {**self.tabs(name), **({"wait": waited} if waited else {})}

    def tab_select(self, name: str, tab: str | int) -> dict:
        chrome = self.ensure_chrome(name)
        with self._lock(name):
            chrome.select_tab(tab)
        self.store.audit(name, "tab_select", tab=chrome.tab_id)
        return self.tabs(name)

    def tab_close(self, name: str, tab: str | int | None = None) -> dict:
        chrome = self.ensure_chrome(name)
        with self._lock(name):
            closed = chrome.close_tab(tab)
        self.store.audit(name, "tab_close", tab=closed)
        return {**self.tabs(name), "closed": closed}

    def wait_for(self, name: str, *, tab: str | int | None = None, **cond: Any) -> dict:
        chrome = self._chrome_for(name, tab)
        result = chrome.wait(**cond)
        cur = chrome.current()
        return {"session": name, "tab": chrome.tab_id, "url": cur.get("url"), "title": cur.get("title"), **result}

    def frame(self, name: str, since: int = 0, timeout: float = 2.0) -> dict:
        """Live view: next screencast frame newer than `since` (long-poll)."""
        chrome = self.ensure_chrome(name)
        frame = chrome.wait_frame(since, min(max(timeout, 0.0), 10.0))
        if frame is None:  # no frames yet (static page) — fall back to a screenshot
            png = chrome.screenshot_png()
            return {"seq": since, "image_base64": base64.b64encode(png).decode("ascii"),
                    "mime": "image/png", "width": 1280, "height": 800, "tab": chrome.tab_id,
                    "changed": True}
        return {**frame, "changed": frame["seq"] > since}

    def close(self, name: str) -> dict:
        with self._lock(name):
            chrome = self._chrome.pop(name, None)
            self._writers.pop(name, None)
            if chrome:
                chrome.stop()
        self.store.audit(name, "close")
        return {"session": name, "closed": True}

    def click(self, name: str, *, selector: str | None, ref: str | None, confirm: str | None,
              tab: str | int | None = None, wait: dict | None = None) -> dict:
        return self._mutate(name, "click", confirm, tab=tab, wait=wait, selector=selector, ref=ref)

    def type_text(
        self,
        name: str,
        text: str,
        *,
        selector: str | None,
        ref: str | None,
        submit: bool = False,
        confirm: str | None = None,
        tab: str | int | None = None,
        wait: dict | None = None,
    ) -> dict:
        return self._mutate(
            name, "type", confirm, tab=tab, wait=wait,
            selector=selector, ref=ref, text=text, submit=submit,
        )

    def press(self, name: str, key: str, *, confirm: str | None = None,
              tab: str | int | None = None, wait: dict | None = None) -> dict:
        return self._mutate(name, "press", confirm, tab=tab, wait=wait, key=key)

    def select(self, name: str, value: str, *, selector: str | None, ref: str | None,
               confirm: str | None = None, tab: str | int | None = None,
               wait: dict | None = None) -> dict:
        return self._mutate(name, "select", confirm, tab=tab, wait=wait,
                            selector=selector, ref=ref, value=value)

    def confirm(self, name: str, confirm_id: str) -> dict:
        item = self.store.consume_pending(confirm_id, name)
        self._approved.add(confirm_id)
        self.store.audit(name, "confirm", confirm_id=confirm_id, detail=item.get("detail"))
        return {"ok": True, "confirm_id": confirm_id, "session": name, "detail": item.get("detail")}

    def pending(self, session: str | None = None) -> list[dict]:
        return self.store.list_pending(session)

    def ui_input(self, name: str, kind: str, payload: dict, *, fast: bool = False) -> dict:
        """Owner UI actions (live view) — always allowed: the owner is logging in."""
        chrome = self.ensure_chrome(name)
        if kind == "navigate":
            return self.open(name, str(payload.get("url") or ""))
        with self._writer(name, holder="owner-ui", timeout=5):
            cdp = chrome.cdp
            assert cdp is not None
            if kind in ("click", "mousedown", "mouseup", "move"):
                x, y = float(payload["x"]), float(payload["y"])
                button = payload.get("button") if payload.get("button") in ("left", "right", "middle") else "left"
                steps = {
                    "click": ("mousePressed", "mouseReleased"),
                    "mousedown": ("mousePressed",),
                    "mouseup": ("mouseReleased",),
                    "move": ("mouseMoved",),
                }[kind]
                for ev in steps:
                    cdp.call("Input.dispatchMouseEvent", {
                        "type": ev, "x": x, "y": y, "button": "none" if ev == "mouseMoved" else button,
                        "clickCount": 0 if ev == "mouseMoved" else 1,
                    })
            elif kind == "wheel":
                cdp.call("Input.dispatchMouseEvent", {
                    "type": "mouseWheel", "x": float(payload["x"]), "y": float(payload["y"]),
                    "deltaX": float(payload.get("dx") or 0), "deltaY": float(payload.get("dy") or 0),
                })
            elif kind == "type":
                text = str(payload.get("text") or "")
                if text:
                    cdp.call("Input.insertText", {"text": text})
            elif kind in ("press", "key"):
                _press_key(cdp, str(payload.get("key") or "Enter"), int(payload.get("modifiers") or 0))
            else:
                raise ValueError(f"неизвестное действие UI: {kind}")
        if fast:
            return {"ok": True}
        time.sleep(0.15)
        return self.view(name)

    def check(self) -> dict:
        checks = []
        try:
            from .chrome import find_chrome
            bin_path = find_chrome()
            checks.append({"title": "Chromium", "ok": True, "detail": bin_path})
        except Exception as exc:  # noqa: BLE001
            checks.append({"title": "Chromium", "ok": False, "detail": str(exc)})
        checks.append({
            "title": "Профили",
            "ok": self.store.profiles.is_dir(),
            "detail": str(self.store.profiles),
        })
        checks.append({
            "title": "Артефакты @browser",
            "ok": self.store.artifacts.is_dir(),
            "detail": str(self.store.artifacts),
        })
        # smoke CDP if possible
        try:
            if checks[0]["ok"]:
                name = "healthcheck"
                if name not in self.store.list_sessions():
                    self.store.create(name, mode="read-only")
                chrome = self.ensure_chrome(name)
                info = chrome.navigate("about:blank")
                self.close(name)
                checks.append({
                    "title": "CDP open about:blank",
                    "ok": True,
                    "detail": info.get("url") or "about:blank",
                })
        except Exception as exc:  # noqa: BLE001
            checks.append({"title": "CDP open about:blank", "ok": False, "detail": str(exc)})
        return {"ok": all(c["ok"] for c in checks), "checks": checks}

    # --- internals -----------------------------------------------------------------

    def _mutate(self, name: str, action: str, confirm: str | None, *,
                tab: str | int | None = None, wait: dict | None = None, **kwargs: Any) -> dict:
        meta = self.store.get(name)
        chrome = self._chrome_for(name, tab)
        current = chrome.current()
        url = current.get("url") or meta.get("last_url") or ""
        approved = set(self._approved)
        if confirm:
            approved.add(confirm)
        need = check_mutation(
            meta, url, confirm_id=confirm, patterns=self._patterns(), approved=approved
        )
        if need == "needs_confirm":
            confirm_id = self.store.add_pending(name, action, {"url": url, **kwargs})
            if self.signal_fn:
                try:
                    self.signal_fn(
                        f"Браузер — нужно подтверждение в «{name}»",
                        body=(
                            f"Агент хочет «{action}» на {url}. "
                            f"Если согласны: pulse browser confirm --session {name} --id {confirm_id} "
                            f"или кнопка на [Браузер](/p/browser/main)."
                        ),
                    )
                except Exception:  # noqa: BLE001
                    pass
            raise NeedConfirm(
                f"нужно подтверждение владельца (checkout/Pay): --confirm {confirm_id}",
                {"confirm_id": confirm_id, "session": name, "url": url, "action": action},
            )

        with self._writer(name, holder=f"agent:{action}"):
            before = chrome.screenshot_png()
            if action == "click":
                self._do_click(chrome, kwargs.get("selector"), kwargs.get("ref"))
            elif action == "type":
                self._do_type(
                    chrome,
                    str(kwargs.get("text") or ""),
                    kwargs.get("selector"),
                    kwargs.get("ref"),
                    bool(kwargs.get("submit")),
                )
            elif action == "press":
                _press_key(chrome.cdp, str(kwargs.get("key") or "Enter"))
            elif action == "select":
                self._do_select(
                    chrome, str(kwargs.get("value") or ""), kwargs.get("selector"), kwargs.get("ref")
                )
            time.sleep(0.3)
            waited = self._post_wait(chrome, wait)
            after = chrome.screenshot_png()
            now = chrome.current()
            shot = self._save_shot(name, action, after)
            self.store.update(name, last_url=now.get("url"), last_title=now.get("title"))
            self.store.audit(
                name,
                action,
                url=now.get("url"),
                screenshot=shot,
                before_bytes=len(before),
                confirm=confirm,
                tab=chrome.tab_id,
                **{k: v for k, v in kwargs.items() if k != "text" or action != "type"},
            )
            return {
                "session": name,
                "action": action,
                "tab": chrome.tab_id,
                "url": now.get("url"),
                "title": now.get("title"),
                "screenshot": shot,
                **({"wait": waited} if waited else {}),
            }

    def _do_click(self, chrome: ChromeSession, selector: str | None, ref: str | None) -> None:
        sel = selector or _ref_to_selector(ref)
        if not sel:
            raise ValueError("нужен --selector или --ref из snapshot")
        ok = chrome.evaluate(
            f"""(() => {{
              const el = document.querySelector({json.dumps(sel)});
              if (!el) return false;
              el.scrollIntoView({{block: 'center', inline: 'center'}});
              el.click();
              return true;
            }})()"""
        )
        if not ok:
            raise CDPError(f"элемент не найден: {sel}")

    def _do_type(
        self,
        chrome: ChromeSession,
        text: str,
        selector: str | None,
        ref: str | None,
        submit: bool,
    ) -> None:
        sel = selector or _ref_to_selector(ref)
        if not sel:
            raise ValueError("нужен --selector или --ref")
        ok = chrome.evaluate(
            f"""(() => {{
              const el = document.querySelector({json.dumps(sel)});
              if (!el) return false;
              el.focus();
              if ('value' in el) {{
                el.value = {json.dumps(text)};
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
              }} else {{
                el.textContent = {json.dumps(text)};
              }}
              return true;
            }})()"""
        )
        if not ok:
            raise CDPError(f"элемент не найден: {sel}")
        if submit:
            _press_key(chrome.cdp, "Enter")

    def _do_select(
        self, chrome: ChromeSession, value: str, selector: str | None, ref: str | None
    ) -> None:
        sel = selector or _ref_to_selector(ref)
        if not sel:
            raise ValueError("нужен --selector или --ref")
        ok = chrome.evaluate(
            f"""(() => {{
              const el = document.querySelector({json.dumps(sel)});
              if (!el) return false;
              el.value = {json.dumps(value)};
              el.dispatchEvent(new Event('input', {{bubbles: true}}));
              el.dispatchEvent(new Event('change', {{bubbles: true}}));
              return true;
            }})()"""
        )
        if not ok:
            raise CDPError(f"элемент не найден: {sel}")

    def _writer(self, name: str, holder: str, timeout: float = 60):
        lock = self._lock(name)

        class _Ctx:
            def __enter__(_self):
                if not lock.acquire(timeout=timeout):
                    raise SessionBusy(f"сессия «{name}» занята другим writer")
                current = self._writers.get(name)
                if current and current != holder:
                    lock.release()
                    raise SessionBusy(f"сессия «{name}» уже пишет: {current}")
                self._writers[name] = holder
                return _self

            def __exit__(_self, *exc):
                if self._writers.get(name) == holder:
                    self._writers.pop(name, None)
                lock.release()
                return False

        return _Ctx()

    def _save_shot(self, name: str, action: str, png: bytes) -> str:
        stamp = time.strftime("%Y%m%dT%H%M%S")
        path = self.store.artifact_dir(name) / f"{stamp}_{action}.png"
        path.write_bytes(png)
        latest = self.store.artifact_dir(name) / "last.png"
        latest.write_bytes(png)
        return f"@browser/{name}/{path.name}"

    def _public(self, item: dict) -> dict:
        out = dict(item)
        out["proxy_set"] = bool(out.get("proxy"))
        out["proxy"] = _proxy_label(out.get("proxy"))
        return out


_KEYS = {
    "Backspace": 8, "Tab": 9, "Enter": 13, "Escape": 27, "PageUp": 33, "PageDown": 34, "End": 35,
    "Home": 36, "ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39, "ArrowDown": 40, "Delete": 46,
}


def _press_key(cdp, key: str, modifiers: int = 0) -> None:
    """Нажатие клавиши через CDP: у спец-клавиш нужен windowsVirtualKeyCode, у Enter ещё и text."""
    base: dict = {"key": key, "modifiers": modifiers}
    if key in _KEYS:
        base["windowsVirtualKeyCode"] = _KEYS[key]
        base["code"] = key
        down = {**base, "type": "keyDown"}
        if key == "Enter":
            down["text"] = "\r"
    elif len(key) == 1:
        down = {**base, "type": "keyDown", "text": key}
    else:
        down = {**base, "type": "keyDown"}
    cdp.call("Input.dispatchKeyEvent", down)
    cdp.call("Input.dispatchKeyEvent", {**base, "type": "keyUp"})


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


def _proxy_label(proxy: str | None) -> str | None:
    if not proxy:
        return None
    # hide credentials
    if "@" in proxy:
        scheme, rest = proxy.split("://", 1) if "://" in proxy else ("", proxy)
        host = rest.rsplit("@", 1)[-1]
        return f"{scheme}://***@{host}" if scheme else f"***@{host}"
    return proxy


def _ref_to_selector(ref: str | None) -> str | None:
    if not ref:
        return None
    # snapshot refs look like "css:..." or plain css
    if ref.startswith("css:"):
        return ref[4:]
    return ref


def _ax_summary(nodes: list[dict]) -> list[dict]:
    out = []
    for i, node in enumerate(nodes):
        props = {p.get("name"): p.get("value", {}).get("value") for p in (node.get("properties") or [])
                 if isinstance(p, dict)}
        role = (node.get("role") or {}).get("value") if isinstance(node.get("role"), dict) else node.get("role")
        name = (node.get("name") or {}).get("value") if isinstance(node.get("name"), dict) else node.get("name")
        if not role and not name:
            continue
        if role in ("none", "generic", "InlineTextBox") and not name:
            continue
        out.append({
            "ref": f"css:[data-pulse-ref='{i}']" if False else f"ax:{i}",
            "role": role,
            "name": name,
            "ignored": node.get("ignored"),
            "backendDOMNodeId": node.get("backendDOMNodeId"),
            "props": {k: v for k, v in props.items() if v not in (None, "", False)},
        })
    # Prefer interactive roles
    interactive = {"button", "link", "textbox", "searchbox", "combobox", "checkbox", "radio", "menuitem"}
    ranked = [n for n in out if (n.get("role") or "").lower() in interactive] + \
             [n for n in out if (n.get("role") or "").lower() not in interactive]
    # Attach usable CSS refs via backendDOMNodeId is hard without DOM.getNodeForLocation;
    # expose name/role for agent; click still uses --selector.
    for n in ranked:
        if n.get("name") and n.get("role"):
            # heuristic selector hint
            n["hint"] = f"{n['role']}: {n['name']}"
    return ranked[:200]
