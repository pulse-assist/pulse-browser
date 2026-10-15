"""Сессии браузера: Chromium на именованный профиль, один пишущий за раз, политика агента, подтверждения владельца.

Агент работает по ссылкам на элементы: snapshot помечает видимые интерактивные элементы атрибутом data-pulse-ref
(e1, e2, …) и отдаёт их списком вместе с текстом страницы; click/type/select принимают --ref. Клик — настоящими
событиями мыши в центр элемента (сайты отличают их от el.click()), ввод — Input.insertText (его понимают React и
прочие «управляемые» поля).
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

import httpx

from .cdp import CDPError
from .chrome import ChromeSession, find_chrome
from .policy import NeedConfirm, PolicyError, needs_owner_confirm, parse_patterns, require_url_allowed
from .store import Store

SNAPSHOT_ELEMENTS = 150
SNAPSHOT_TEXT = 8000
DOWNLOAD_MAX = 100 * 1024 * 1024
# страницы проверки «вы не робот»: агенту их не пройти — нужен владелец в живом просмотре или другой прокси
CHALLENGES = ("ddos-guard", "just a moment", "checking your browser", "attention required", "captcha",
              "подтвердите, что вы не робот", "вы не робот", "доступ ограничен", "access denied",
              "are you a robot", "проверка браузера", "antibot")


class SessionBusy(Exception):
    pass


class NotRunning(Exception):
    pass


SNAPSHOT_JS = r"""
(() => {
  const MAX = %(max)d, TEXT = %(text)d;
  document.querySelectorAll('[data-pulse-ref]').forEach(e => e.removeAttribute('data-pulse-ref'));
  const sel = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[role=button],[role=link],'
    + '[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=option],[role=switch],[role=combobox],'
    + '[contenteditable=""],[contenteditable=true],[onclick],[tabindex]:not([tabindex="-1"])';
  const visible = el => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0';
  };
  const clean = s => String(s || '').replace(/\s+/g, ' ').trim().slice(0, 100);
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    if (out.length >= MAX) break;
    if (!visible(el)) continue;
    if (el.parentElement && el.parentElement.closest('[data-pulse-ref]') && !el.matches('input,select,textarea'))
      continue;  // кнопка внутри уже помеченной ссылки — один элемент
    const ref = 'e' + (++n);
    el.setAttribute('data-pulse-ref', ref);
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const label = clean(el.getAttribute('aria-label') || el.innerText
      || (el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute('placeholder')
      || el.getAttribute('title') || el.getAttribute('alt') || (type !== 'password' ? el.value : '')
      || (el.querySelector('img[alt]') && el.querySelector('img[alt]').getAttribute('alt'))
      || el.getAttribute('name') || el.id);
    const item = {ref, tag, label};
    if (el.getAttribute('role')) item.role = el.getAttribute('role');
    if (type) item.type = type;
    if (tag === 'a' && el.href) item.href = el.href;
    if (['input', 'textarea', 'select'].includes(tag) && type !== 'password') {
      const v = clean(tag === 'select' && el.selectedOptions[0] ? el.selectedOptions[0].text : el.value);
      if (v) item.value = v;
    }
    if (type === 'checkbox' || type === 'radio') item.checked = el.checked;
    if (el.disabled) item.disabled = true;
    const r = el.getBoundingClientRect();
    if (r.bottom < 0 || r.top > innerHeight) item.offscreen = true;
    out.push(item);
  }
  const text = (document.body ? document.body.innerText : '').replace(/\n{3,}/g, '\n\n').trim();
  return {elements: out, text: text.slice(0, TEXT), text_chars: text.length, full_text: text};
})()
"""

PROBE_JS = r"""
(() => {
  const el = document.querySelector(%(sel)s);
  if (!el) return null;
  el.scrollIntoView({block: 'center', inline: 'center'});
  const r = el.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const top = document.elementFromPoint(x, y);
  const label = String(el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('placeholder')
    || el.getAttribute('title') || '').replace(/\s+/g, ' ').trim().slice(0, 120);
  return {x, y, label, tag: el.tagName.toLowerCase(), disabled: !!el.disabled,
          covered: !(top && (top === el || el.contains(top) || top.contains(el)))};
})()
"""


class BrowserService:
    def __init__(self, store: Store, settings: dict, signal_fn: Callable | None = None, *,
                 full_control_fn: Callable[[], bool] | None = None, reaper: bool = True):
        self.store = store
        self.settings = settings
        self.signal_fn = signal_fn
        # контракт 1.8: агент с галочкой «полный контроль браузера» — без confirm на checkout
        self._full_control_fn = full_control_fn or (lambda: False)
        self._chrome: dict[str, ChromeSession] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._writers: dict[str, str] = {}
        self._global = threading.RLock()
        if reaper:
            threading.Thread(target=self._reap_loop, name="browser-idle", daemon=True).start()

    # --- настройки ------------------------------------------------------------------------------------------

    def _patterns(self) -> list[str]:
        return parse_patterns(self.settings.get("confirm_patterns"))

    def _headless(self) -> bool:
        val = self.settings.get("headless")
        return True if val is None else bool(val)

    def _default_mode(self) -> str:
        return (self.settings.get("default_mode") or "read-only").strip()

    def _int_setting(self, key: str, default: int) -> int:
        try:
            return max(int(self.settings.get(key) or default), 0)
        except (TypeError, ValueError):
            return default

    def _lock(self, name: str) -> threading.RLock:
        with self._global:
            return self._locks.setdefault(name, threading.RLock())

    # --- сессии ---------------------------------------------------------------------------------------------

    def sessions_out(self) -> list[dict]:
        out = []
        for name, meta in sorted(self.store.list_sessions().items()):
            chrome = self._chrome.get(name)
            running = bool(chrome and chrome.running)
            out.append({
                **meta,
                "proxy_set": bool(meta.get("proxy")),
                "proxy": _proxy_label(meta.get("proxy")),
                "running": running,
                "locked": name in self._writers,
                "url": meta.get("last_url"),
                "title": meta.get("last_title"),
            })
        return out

    def create_session(self, name: str, *, mode: str | None = None, proxy: str | None = None,
                       allowlist: str | None = None) -> dict:
        item = self.store.create(name, mode=mode or self._default_mode(), proxy=proxy,
                                 allowlist=_split_csv(allowlist))
        return self._public(item)

    def update_session(self, name: str, **fields: Any) -> dict:
        if "allowlist" in fields and isinstance(fields["allowlist"], str):
            fields["allowlist"] = _split_csv(fields["allowlist"])
        item = self.store.update(name, **fields)
        if "proxy" in fields and name in self._chrome:
            self.close(name)                        # прокси задаётся при запуске Chrome
        return self._public(item)

    def delete_session(self, name: str) -> dict:
        self.close(name)
        self.store.delete(name)
        return {"deleted": name}

    def ensure_chrome(self, name: str, *, start: bool = True) -> ChromeSession:
        meta = self.store.get(name)
        with self._lock(name):
            chrome = self._chrome.get(name)
            if chrome and chrome.running:
                if chrome.healthy():
                    chrome.ensure()
                    return chrome
                chrome.stop()                       # завис рендерер (бывает после таймаута сайта) — перезапуск
                self._chrome.pop(name, None)
            if not start:
                raise NotRunning(f"Chrome сессии «{name}» не запущен")
            chrome = ChromeSession(name, self.store.profile_dir(name), proxy=meta.get("proxy"),
                                   headless=self._headless(), timezone=self.settings.get("timezone") or None)
            chrome.start()
            self._chrome[name] = chrome
            return chrome

    def _chrome_for(self, name: str, tab: str | int | None = None) -> ChromeSession:
        chrome = self.ensure_chrome(name)
        if tab not in (None, ""):
            chrome.select_tab(tab)
        return chrome

    def close(self, name: str) -> dict:
        with self._lock(name):
            chrome = self._chrome.pop(name, None)
            self._writers.pop(name, None)
            if chrome:
                chrome.stop()
        return {"session": name, "closed": True}

    def _reap_loop(self) -> None:
        """Chrome без дела дольше idle_minutes закрывается: профиль (вход, куки) остаётся на диске."""
        while True:
            time.sleep(60)
            idle = self._int_setting("idle_minutes", 15) * 60
            if not idle:
                continue
            for name, chrome in list(self._chrome.items()):
                if name not in self._writers and time.monotonic() - chrome.last_used > idle:
                    try:
                        self.close(name)
                    except Exception:  # noqa: BLE001 — следующий проход попробует снова
                        pass

    # --- чтение ---------------------------------------------------------------------------------------------

    def open(self, name: str, url: str, *, tab: str | int | None = None, new_tab: bool = False,
             wait: dict | None = None) -> dict:
        meta = self.store.get(name)
        url = _normalize_url(url)
        require_url_allowed(url, meta)
        chrome = self._chrome_for(name, tab)
        if new_tab:
            chrome.new_tab()
        info = chrome.navigate(url)
        waited = chrome.wait(**wait) if wait else None
        return self._page_result(name, chrome, "open", info, waited)

    def _page_result(self, name: str, chrome: ChromeSession, action: str, info: dict | None = None,
                     waited: dict | None = None, extra: dict | None = None) -> dict:
        cur = info or chrome.current()
        shot = self._save_shot(name, action, chrome.screenshot_png())
        self.store.update(name, last_url=cur.get("url"), last_title=cur.get("title"))
        self.store.audit(name, action, url=cur.get("url"), title=cur.get("title"), screenshot=shot,
                         tab=chrome.tab_id, **{k: v for k, v in (extra or {}).items() if k != "action"})
        out = {"session": name, "tab": chrome.tab_id, "url": cur.get("url"), "title": cur.get("title"),
               "screenshot": shot, **(extra or {})}
        if waited:
            out["wait"] = waited
        blocked = self._challenge(chrome, cur)
        if blocked:
            out["blocked"] = blocked
            out["hint"] = ("Сайт показал проверку «вы не робот». Её проходит владелец: попроси его открыть сессию "
                           "на странице [Браузер](/p/browser/main) и пройти проверку в живом просмотре; "
                           "не помогает — сессии нужен прокси (российский для российских сайтов).")
        return out

    @staticmethod
    def _challenge(chrome: ChromeSession, cur: dict) -> str | None:
        title = str(cur.get("title") or "").lower()
        hit = next((c for c in CHALLENGES if c in title), None)
        if hit:
            return cur.get("title")
        try:
            head = str(chrome.evaluate("document.body ? document.body.innerText.slice(0, 600) : ''") or "").lower()
        except CDPError:
            return None
        if len(head) < 600 and any(c in head for c in CHALLENGES):
            return (cur.get("title") or "проверка") + " — " + head[:120].replace("\n", " ")
        return None

    def snapshot(self, name: str, fmt: str = "page", *, tab: str | int | None = None,
                 max_elements: int | None = None, max_text: int | None = None) -> dict:
        chrome = self._chrome_for(name, tab)
        fmt = (fmt or "page").lower()
        result = self._page_result(name, chrome, "snapshot")
        folder = self.store.artifact_dir(name)
        if fmt in ("page", "both"):
            data = chrome.evaluate(SNAPSHOT_JS % {"max": int(max_elements or SNAPSHOT_ELEMENTS),
                                                  "text": int(max_text or SNAPSHOT_TEXT)}) or {}
            (folder / "last_text.md").write_text(data.get("full_text") or "", encoding="utf-8")
            (folder / "last_elements.json").write_text(json.dumps(data.get("elements") or [], ensure_ascii=False,
                                                                  indent=1), encoding="utf-8")
            result.update(elements=data.get("elements") or [], text=data.get("text") or "",
                          text_chars=data.get("text_chars") or 0, text_file=f"@browser/{name}/last_text.md")
            if (data.get("text_chars") or 0) > len(data.get("text") or ""):
                result["text_truncated"] = f"текст страницы целиком — {result['text_file']}"
        if fmt in ("a11y", "both"):
            try:
                nodes = _ax_summary(chrome.cdp.call("Accessibility.getFullAXTree").get("nodes") or [])  # type: ignore[union-attr]
                (folder / "last_a11y.json").write_text(json.dumps(nodes, ensure_ascii=False, indent=1), encoding="utf-8")
                result["a11y"] = f"@browser/{name}/last_a11y.json"
                result["nodes"] = nodes[:80]
            except CDPError as exc:
                result["a11y"] = f"недоступно: {exc}"
        if fmt == "html":
            html = chrome.evaluate("document.documentElement.outerHTML") or ""
            (folder / "last.html").write_text(str(html)[:2_000_000], encoding="utf-8")
            result["html"] = f"@browser/{name}/last.html"
        return result

    def view(self, name: str, *, tab: str | int | None = None, start: bool = True) -> dict:
        """Текущий кадр. Страница плагина зовёт с start=False: открыть страницу не значит запустить Chrome."""
        try:
            chrome = self.ensure_chrome(name, start=start)
        except NotRunning:
            return {"session": name, "running": False}
        if tab not in (None, ""):
            chrome.select_tab(tab)
        cur = chrome.current()
        png = chrome.screenshot_png()
        return {"session": name, "running": True, "url": cur.get("url"), "title": cur.get("title"),
                "image_base64": base64.b64encode(png).decode("ascii"), "mime": "image/png",
                "width": 1280, "height": 800}

    def tabs(self, name: str) -> dict:
        chrome = self.ensure_chrome(name)
        return {"session": name, "active": chrome.tab_id, "tabs": chrome.tabs()}

    def tab_new(self, name: str, url: str | None = None, *, wait: dict | None = None) -> dict:
        if url:
            url = _normalize_url(url)
            require_url_allowed(url, self.store.get(name))
        chrome = self.ensure_chrome(name)
        with self._lock(name):
            chrome.new_tab(url)
            waited = chrome.wait(**wait) if wait else None
        self.store.audit(name, "tab_new", url=url, tab=chrome.tab_id)
        return {**self.tabs(name), **({"wait": waited} if waited else {})}

    def tab_select(self, name: str, tab: str | int) -> dict:
        chrome = self.ensure_chrome(name)
        with self._lock(name):
            chrome.select_tab(tab)
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
        """Живой просмотр: следующий кадр screencast новее since (long-poll). Chrome не запускает."""
        chrome = self._chrome.get(name)
        if not chrome or not chrome.running:
            return {"running": False, "seq": since, "changed": False}
        chrome.touch()
        frame = chrome.wait_frame(since, min(max(timeout, 0.0), 10.0))
        if frame is None:                          # кадров ещё нет (статичная страница) — скриншот
            png = chrome.screenshot_png()
            return {"running": True, "seq": since, "image_base64": base64.b64encode(png).decode("ascii"),
                    "mime": "image/png", "width": 1280, "height": 800, "tab": chrome.tab_id, "changed": True}
        return {"running": True, **frame, "changed": frame["seq"] > since}

    def download(self, name: str, *, url: str | None = None, ref: str | None = None,
                 selector: str | None = None, filename: str | None = None) -> dict:
        """Файл по ссылке — с куками и прокси сессии (документы из базы, где нужен вход или нет API)."""
        meta = self.store.get(name)
        chrome = self.ensure_chrome(name)
        if not url:
            sel = selector or _ref_selector(ref)
            if not sel:
                raise ValueError("нужен --url или --ref ссылки из snapshot")
            url = chrome.evaluate(f"(() => {{ const el = document.querySelector({json.dumps(sel)}); "
                                  f"return el ? (el.href || el.src || el.getAttribute('data-href') || '') : null; }})()")
            if url is None:
                raise CDPError(f"элемент не найден: {ref or selector} — сделай snapshot заново")
            if not url:
                raise ValueError("у элемента нет ссылки на файл — кликни по нему (click) и смотри новую вкладку")
        url = _normalize_url(url)
        require_url_allowed(url, meta)
        cookies = chrome.cdp.call("Network.getCookies", {"urls": [url]}).get("cookies") or []  # type: ignore[union-attr]
        cur = chrome.current()
        headers = {"User-Agent": chrome.user_agent or "Mozilla/5.0", "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                   "Cookie": "; ".join(f"{c['name']}={c['value']}" for c in cookies)}
        if cur.get("url", "").startswith("http"):
            headers["Referer"] = cur["url"]
        folder = self.store.artifact_dir(name) / "downloads"
        folder.mkdir(parents=True, exist_ok=True)
        with httpx.Client(proxy=meta.get("proxy") or None, timeout=httpx.Timeout(120, connect=20),
                          follow_redirects=True) as client:
            with client.stream("GET", url, headers=headers) as response:
                if response.status_code >= 400:
                    raise CDPError(f"сайт ответил {response.status_code} на {url}")
                name_out = _safe_name(filename or _disposition_name(response.headers.get("content-disposition"))
                                      or unquote(Path(urlparse(str(response.url)).path).name) or "download")
                target = _unique(folder / name_out)
                size = 0
                with target.open("wb") as out:
                    for chunk in response.iter_bytes(1024 * 1024):
                        size += len(chunk)
                        if size > DOWNLOAD_MAX:
                            out.close()
                            target.unlink(missing_ok=True)
                            raise ValueError(f"файл больше {DOWNLOAD_MAX // 1024 // 1024} МБ")
                        out.write(chunk)
                mime = response.headers.get("content-type", "").split(";")[0]
        path = f"@browser/{name}/downloads/{target.name}"
        self.store.audit(name, "download", url=url, file=path, size=size)
        return {"session": name, "file": path, "name": target.name, "size": size, "mime": mime, "url": url,
                "hint": f"забрать к себе: pulse files get \"{path}\"; в папку Пульса: pulse files put"}

    # --- действия агента ------------------------------------------------------------------------------------

    def click(self, name: str, *, selector: str | None, ref: str | None, confirm: str | None = None,
              tab: str | int | None = None, wait: dict | None = None) -> dict:
        return self._mutate(name, "click", confirm, tab=tab, wait=wait, selector=selector, ref=ref)

    def type_text(self, name: str, text: str, *, selector: str | None, ref: str | None, submit: bool = False,
                  confirm: str | None = None, tab: str | int | None = None, wait: dict | None = None) -> dict:
        return self._mutate(name, "type", confirm, tab=tab, wait=wait, selector=selector, ref=ref, text=text,
                            submit=submit)

    def press(self, name: str, key: str, *, confirm: str | None = None, tab: str | int | None = None,
              wait: dict | None = None) -> dict:
        return self._mutate(name, "press", confirm, tab=tab, wait=wait, key=key)

    def select(self, name: str, value: str, *, selector: str | None, ref: str | None, confirm: str | None = None,
               tab: str | int | None = None, wait: dict | None = None) -> dict:
        return self._mutate(name, "select", confirm, tab=tab, wait=wait, selector=selector, ref=ref, value=value)

    def _mutate(self, name: str, action: str, confirm: str | None, *, tab: str | int | None = None,
                wait: dict | None = None, **kwargs: Any) -> dict:
        meta = self.store.get(name)
        chrome = self._chrome_for(name, tab)
        url = chrome.current().get("url") or meta.get("last_url") or ""
        sel = None
        probe: dict = {}
        if action in ("click", "type", "select"):
            sel = kwargs.get("selector") or _ref_selector(kwargs.get("ref"))
            if not sel:
                raise ValueError("нужен --ref из snapshot (или --selector)")
            probe = chrome.evaluate(PROBE_JS % {"sel": json.dumps(sel)}) or {}
            if not probe:
                raise CDPError(f"элемент не найден: {kwargs.get('ref') or sel} — страница изменилась, сделай snapshot")
        target = {"url": url, "label": probe.get("label") or kwargs.get("key")}
        need_confirm = needs_owner_confirm(
            meta, url, patterns=self._patterns(), full_control=bool(self._full_control_fn()),
        )
        if need_confirm:
            if not confirm:
                raise self._ask_confirm(name, chrome, action, target, kwargs)
            try:
                self.store.use_approval(confirm, name, action, target)
            except PermissionError as exc:
                raise PolicyError(str(exc)) from exc

        with self._writer(name, holder=f"agent:{action}"):
            if action == "click":
                self._do_click(chrome, sel, probe)
            elif action == "type":
                self._do_type(chrome, sel, str(kwargs.get("text") or ""), bool(kwargs.get("submit")))
            elif action == "press":
                _press_key(chrome.cdp, str(kwargs.get("key") or "Enter"))
            elif action == "select":
                self._do_select(chrome, sel, str(kwargs.get("value") or ""))
            time.sleep(0.4)
            waited = chrome.wait(**wait) if wait else None
            extra = {"action": action, "element": probe.get("label")} if probe else {"action": action}
            if confirm:
                extra["confirm"] = confirm
            return self._page_result(name, chrome, action, waited=waited, extra=extra)

    def _ask_confirm(self, name: str, chrome: ChromeSession, action: str, target: dict, kwargs: dict) -> NeedConfirm:
        detail = {**target, "title": chrome.current().get("title")}
        if action == "type":
            detail["text_chars"] = len(str(kwargs.get("text") or ""))
        if action == "select":
            detail["value"] = kwargs.get("value")
        confirm_id = self.store.add_pending(name, action, detail)
        shot = self.store.artifact_dir(name) / "pending" / f"{confirm_id}.png"
        shot.parent.mkdir(parents=True, exist_ok=True)
        shot.write_bytes(chrome.screenshot_png())
        what = f"«{target.get('label') or action}»"
        if self.signal_fn:
            try:
                self.signal_fn(f"Браузер — подтвердите {what}",
                               f"Агент в сессии «{name}» хочет нажать {what} на странице «{detail.get('title')}» "
                               f"({target.get('url')}). Это оплата или оформление заказа — решение за вами: "
                               f"[Браузер](/p/browser/main) → «Разрешить один раз» или «Отклонить».")
            except Exception:  # noqa: BLE001 — сигнал не обязателен: запрос виден на странице плагина
                pass
        return NeedConfirm(
            f"нужно подтверждение владельца: {what} на {target.get('url')}. Запрос {confirm_id} отправлен владельцу "
            f"(страница «Браузер», сигнал). Дождись решения: pulse browser pending --id {confirm_id}; одобрено — "
            f"повтори ровно это действие с --confirm {confirm_id}. Сам подтвердить нельзя.",
            {"confirm_id": confirm_id, "session": name, "url": target.get("url"), "action": action,
             "element": target.get("label")})

    def decide(self, confirm_id: str, approve: bool) -> dict:
        item = self.store.decide_pending(confirm_id, approve)
        self.store.audit(item["session"], "approve" if approve else "reject", confirm_id=confirm_id,
                         detail=item.get("detail"))
        return item

    def pending(self, session: str | None = None, confirm_id: str | None = None) -> Any:
        if confirm_id:
            item = self.store.get_pending(confirm_id)
            return {k: item.get(k) for k in ("id", "session", "action", "status", "detail", "created_at", "decided_at")}
        return self.store.list_pending(session)

    def pending_screenshot(self, confirm_id: str) -> dict:
        item = self.store.get_pending(confirm_id)
        path = self.store.artifact_dir(item["session"]) / "pending" / f"{confirm_id}.png"
        if not path.is_file():
            raise KeyError("скриншота нет")
        return {"image_base64": base64.b64encode(path.read_bytes()).decode("ascii"), "mime": "image/png"}

    def _do_click(self, chrome: ChromeSession, sel: str, probe: dict) -> None:
        if probe.get("disabled"):
            raise CDPError(f"элемент «{probe.get('label')}» неактивен (disabled)")
        if probe.get("covered"):
            # элемент перекрыт (баннер, оверлей) — клик через DOM; не помогло — пусть агент закроет оверлей
            chrome.evaluate(f"document.querySelector({json.dumps(sel)}).click()")
            return
        x, y = float(probe["x"]), float(probe["y"])
        cdp = chrome.cdp
        assert cdp is not None
        cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "button": "none"})
        for ev in ("mousePressed", "mouseReleased"):
            cdp.call("Input.dispatchMouseEvent", {"type": ev, "x": x, "y": y, "button": "left", "clickCount": 1})

    def _do_type(self, chrome: ChromeSession, sel: str, text: str, submit: bool) -> None:
        focused = chrome.evaluate(f"""(() => {{
          const el = document.querySelector({json.dumps(sel)});
          if (!el) return false;
          el.scrollIntoView({{block: 'center'}});
          el.focus();
          if (typeof el.select === 'function') el.select();
          else if (el.isContentEditable) document.execCommand('selectAll', false, null);
          return true;
        }})()""")
        if not focused:
            raise CDPError("элемент не найден — сделай snapshot заново")
        chrome.cdp.call("Input.insertText", {"text": text})  # type: ignore[union-attr]
        # поле не приняло ввод (необычный виджет) — значение напрямую, как это сделал бы React
        chrome.evaluate(f"""(() => {{
          const el = document.querySelector({json.dumps(sel)});
          if (!el || !('value' in el) || el.value === {json.dumps(text)}) return;
          const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
          Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, {json.dumps(text)});
          el.dispatchEvent(new Event('input', {{bubbles: true}}));
          el.dispatchEvent(new Event('change', {{bubbles: true}}));
        }})()""")
        if submit:
            _press_key(chrome.cdp, "Enter")

    def _do_select(self, chrome: ChromeSession, sel: str, value: str) -> None:
        ok = chrome.evaluate(f"""(() => {{
          const el = document.querySelector({json.dumps(sel)});
          if (!el || el.tagName !== 'SELECT') return 'not-select';
          const want = {json.dumps(value)};
          const opt = [...el.options].find(o => o.value === want || o.text.trim() === want);
          if (!opt) return 'no-option';
          Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(el, opt.value);
          el.dispatchEvent(new Event('input', {{bubbles: true}}));
          el.dispatchEvent(new Event('change', {{bubbles: true}}));
          return 'ok';
        }})()""")
        if ok == "not-select":
            raise CDPError("это не <select> — открой список кликом и выбери пункт кликом")
        if ok == "no-option":
            raise CDPError(f"в списке нет пункта «{value}» — варианты смотри в snapshot")

    # --- ввод владельца (живой просмотр) -------------------------------------------------------------------

    def ui_input(self, name: str, kind: str, payload: dict) -> dict:
        """Владелец входит в аккаунт, проходит проверку «не робот»: его ввод политика агента не ограничивает."""
        if kind == "navigate":
            return self.open(name, str(payload.get("url") or ""))
        chrome = self.ensure_chrome(name)
        with self._writer(name, holder="owner-ui", timeout=5):
            cdp = chrome.cdp
            assert cdp is not None
            if kind in ("click", "mousedown", "mouseup", "move"):
                x, y = float(payload["x"]), float(payload["y"])
                button = payload.get("button") if payload.get("button") in ("left", "right", "middle") else "left"
                steps = {"click": ("mousePressed", "mouseReleased"), "mousedown": ("mousePressed",),
                         "mouseup": ("mouseReleased",), "move": ("mouseMoved",)}[kind]
                for ev in steps:
                    cdp.call("Input.dispatchMouseEvent", {
                        "type": ev, "x": x, "y": y, "button": "none" if ev == "mouseMoved" else button,
                        "clickCount": 0 if ev == "mouseMoved" else 1})
            elif kind == "wheel":
                cdp.call("Input.dispatchMouseEvent", {
                    "type": "mouseWheel", "x": float(payload["x"]), "y": float(payload["y"]),
                    "deltaX": float(payload.get("dx") or 0), "deltaY": float(payload.get("dy") or 0)})
            elif kind == "type":
                text = str(payload.get("text") or "")
                if text:
                    cdp.call("Input.insertText", {"text": text})
            elif kind in ("press", "key"):
                _press_key(cdp, str(payload.get("key") or "Enter"), int(payload.get("modifiers") or 0))
            else:
                raise ValueError(f"неизвестное действие: {kind}")
        return {"ok": True}

    # --- проверка ------------------------------------------------------------------------------------------

    def check(self) -> dict:
        checks = []
        try:
            checks.append({"title": "Chromium", "ok": True, "detail": find_chrome()})
        except Exception as exc:  # noqa: BLE001
            checks.append({"title": "Chromium", "ok": False, "detail": str(exc)})
        checks.append({"title": "Профили", "ok": self.store.profiles.is_dir(), "detail": str(self.store.profiles)})
        if checks[0]["ok"]:
            tmp = Path(tempfile.mkdtemp(prefix="browser-check-"))
            chrome = None
            try:
                chrome = ChromeSession("check", tmp, headless=True)
                chrome.start()
                info = chrome.navigate("about:blank")
                checks.append({"title": "Chrome отвечает (CDP)", "ok": True, "detail": info.get("url") or "about:blank"})
            except Exception as exc:  # noqa: BLE001
                checks.append({"title": "Chrome отвечает (CDP)", "ok": False, "detail": str(exc)})
            finally:
                if chrome:
                    chrome.stop()
                shutil.rmtree(tmp, ignore_errors=True)
        for meta in self.store.list_sessions().values():
            if meta.get("proxy"):
                checks.append(_proxy_check(meta))
        return {"ok": all(c["ok"] for c in checks), "checks": checks}

    # --- служебное ------------------------------------------------------------------------------------------

    def _writer(self, name: str, holder: str, timeout: float = 60):
        lock = self._lock(name)
        service = self

        class _Ctx:
            def __enter__(_self):
                if not lock.acquire(timeout=timeout):
                    raise SessionBusy(f"сессия «{name}» занята: {service._writers.get(name) or 'другое действие'}")
                service._writers[name] = holder
                return _self

            def __exit__(_self, *exc):
                if service._writers.get(name) == holder:
                    service._writers.pop(name, None)
                lock.release()
                return False

        return _Ctx()

    def _save_shot(self, name: str, action: str, png: bytes) -> str:
        folder = self.store.artifact_dir(name)
        stamp = time.strftime("%Y%m%dT%H%M%S")
        path = _unique(folder / f"{stamp}_{action}.png")
        path.write_bytes(png)
        (folder / "last.png").write_bytes(png)
        keep = self._int_setting("keep_screenshots", 200)
        shots = sorted(p for p in folder.glob("2*_*.png"))
        for old in shots[:-keep] if keep else []:
            old.unlink(missing_ok=True)
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


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("нужен --url")
    if not re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):
        url = "https://" + url                    # «ozon.ru» → https://ozon.ru
    if not url.startswith(("http://", "https://", "about:", "data:")):
        raise ValueError("адрес — http(s)://…")
    return url


def _proxy_label(proxy: str | None) -> str | None:
    if not proxy:
        return None
    if "@" in proxy:
        scheme, rest = proxy.split("://", 1) if "://" in proxy else ("", proxy)
        host = rest.rsplit("@", 1)[-1]
        return f"{scheme}://***@{host}" if scheme else f"***@{host}"
    return proxy


def _proxy_check(meta: dict) -> dict:
    title = f"Прокси сессии «{meta['name']}»: {_proxy_label(meta['proxy'])}"
    try:
        with httpx.Client(proxy=meta["proxy"], timeout=httpx.Timeout(15, connect=10)) as client:
            response = client.get("https://www.google.com/generate_204")
        return {"title": title, "ok": True, "detail": f"отвечает (HTTP {response.status_code})"}
    except Exception as exc:  # noqa: BLE001
        return {"title": title, "ok": False, "detail": f"не отвечает: {exc.__class__.__name__}"}


def _ref_selector(ref: str | None) -> str | None:
    """Ссылка из snapshot (e12) → CSS-селектор помеченного элемента; css:… — селектор как есть."""
    if not ref:
        return None
    ref = str(ref).strip()
    if ref.startswith("css:"):
        return ref[4:]
    if re.fullmatch(r"e\d+", ref):
        return f'[data-pulse-ref="{ref}"]'
    raise ValueError(f"ссылка «{ref}» не из snapshot — нужны e1, e2… из последнего pulse browser snapshot")


def _disposition_name(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", value) or re.search(r'filename\s*=\s*"?([^";]+)"?', value)
    return unquote(match.group(1)).strip() if match else None


def _safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .") or "download"
    return name[:150]


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not candidate.exists():
            return candidate
    return path


def _ax_summary(nodes: list[dict]) -> list[dict]:
    out = []
    for node in nodes:
        role = (node.get("role") or {}).get("value") if isinstance(node.get("role"), dict) else node.get("role")
        name = (node.get("name") or {}).get("value") if isinstance(node.get("name"), dict) else node.get("name")
        if not name or role in ("none", "generic", "InlineTextBox", "StaticText"):
            continue
        out.append({"role": role, "name": str(name)[:120]})
    return out[:400]
