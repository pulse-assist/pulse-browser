"""Плагин «Браузер»: Chromium + CDP для агентов — веб-задачи, которые не решить запросом к серверу (поиск и покупка в
магазине, документы из открытой базы без API, сервисы со входом владельца) — и страница входа и живого просмотра.

Страница: GET /state, /sessions/{name}/view|frame|tabs, /pending/{id}/screenshot, POST /sessions, PATCH/DELETE
/sessions/{name}, POST /sessions/{name}/open|close|input|tabs/new|tabs/select|tabs/close, POST /confirm/{id}
(только владелец — X-Pulse-Caller: owner).
Агенты: pulse browser sessions|session create|open|snapshot|click|type|press|select|download|tabs|tab …|wait|pending|…
"""

from __future__ import annotations

from fastapi import Body, HTTPException
from pulse_plugin import CommandError, Plugin

from .cdp import CDPError
from .files_provider import ArtifactsFS
from .policy import NeedConfirm, PolicyError
from .sessions import BrowserService, NotRunning, SessionBusy
from .store import Store

plugin = Plugin()
app = plugin.app
store = Store(plugin.data_dir)


def _signal(title: str, body: str) -> None:
    try:
        plugin.signal(title, body=body, priority=85, tags=["browser"])
    except Exception:  # noqa: BLE001
        pass


def _full_control() -> bool:
    """Галочка агента agent_settings.full_control (контракт 1.8, заголовок X-Pulse-Agent-Settings)."""
    return bool(plugin.agent_setting("full_control"))


browser = BrowserService(
    store, plugin.settings, signal_fn=_signal, full_control_fn=_full_control,
)
plugin.provide_files(ArtifactsFS(store.artifacts, store.runs))


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'\"")) from exc
    except NotRunning as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, PolicyError, CDPError, SessionBusy) as exc:
        raise HTTPException(400, str(exc)) from exc
    except NeedConfirm as exc:
        raise HTTPException(409, str(exc)) from exc


def _cmd(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError as exc:
        raise CommandError(str(exc).strip("'\"")) from exc
    except (ValueError, PolicyError, CDPError, SessionBusy, NotRunning) as exc:
        raise CommandError(str(exc)) from exc
    except NeedConfirm as exc:
        raise CommandError(str(exc)) from exc


def _owner_for_write(mode) -> None:
    """Режим write включает владелец или агент с галочкой «полный контроль браузера» (agent_settings)."""
    if str(mode or "").strip().lower() != "write":
        return
    if plugin.caller() == "owner" or _full_control():
        return
    raise CommandError("режим write включает владелец: страница [Браузер](/p/browser/main) → режим сессии "
                       "«агент может действовать». Либо включите агенту «полный контроль браузера» "
                       "в Настройки → Агенты → доступ")


def _session_mode(explicit) -> str | None:
    """Без --mode: у агента с полным контролем — сразу write."""
    if explicit not in (None, ""):
        return str(explicit)
    if _full_control():
        return "write"
    return None


def _int(value) -> int | None:
    try:
        return int(value) if value not in (None, "", True) else None
    except (TypeError, ValueError):
        raise CommandError(f"ожидалось число, а не «{value}»") from None


def _flag(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "да", "on")


def _wait_opts(args: dict) -> dict | None:
    """wait_* аргументы (selector, url, text, idle, ms, timeout) → условия для ChromeSession.wait."""
    opts: dict = {}
    if args.get("wait_selector"):
        opts["selector"] = str(args["wait_selector"])
    if args.get("wait_url"):
        opts["url"] = str(args["wait_url"])
    if args.get("wait_text"):
        opts["text"] = str(args["wait_text"])
    if _flag(args.get("wait_idle")):
        opts["idle"] = True
    if args.get("wait_ms"):
        opts["ms"] = float(args["wait_ms"])
    if not opts:
        return None
    if args.get("wait_timeout"):
        opts["timeout"] = float(args["wait_timeout"])
    return opts


# --- health / page API ---------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"ok": True, "plugin": plugin.id, "version": plugin.version}


@app.get("/state")
def state() -> dict:
    return {
        "sessions": browser.sessions_out(),
        "pending": browser.pending(),
        "settings": {
            "default_mode": plugin.settings.get("default_mode") or "read-only",
            "headless": bool(plugin.settings.get("headless", True)),
            "confirm_patterns": plugin.settings.get("confirm_patterns") or "",
            "idle_minutes": browser._int_setting("idle_minutes", 15),  # noqa: SLF001
        },
    }


@app.post("/sessions")
def create_session(body: dict = Body(...)) -> dict:
    mode = _session_mode(body.get("mode"))
    if str(mode or "").lower() == "write" and not _full_control():
        plugin.require_owner("Режим write")
    return _run(
        browser.create_session,
        body.get("name") or "",
        mode=mode,
        proxy=body.get("proxy"),
        allowlist=body.get("allowlist"),
    )


@app.patch("/sessions/{name}")
def patch_session(name: str, body: dict = Body(...)) -> dict:
    fields = {k: body[k] for k in ("mode", "proxy", "allowlist") if k in body}
    if str(fields.get("mode") or "").lower() == "write" and not _full_control():
        plugin.require_owner("Режим write")
    return _run(browser.update_session, name, **fields)


@app.delete("/sessions/{name}")
def delete_session(name: str) -> dict:
    return _run(browser.delete_session, name)


@app.post("/sessions/{name}/open")
def open_session(name: str, body: dict = Body(...)) -> dict:
    url = body.get("url") or ""
    if not url:
        raise HTTPException(400, "нужен url")
    return _run(browser.open, name, url)


@app.post("/sessions/{name}/close")
def close_session(name: str) -> dict:
    return _run(browser.close, name)


@app.get("/sessions/{name}/view")
def view_session(name: str) -> dict:
    return _run(browser.view, name, start=False)


@app.get("/sessions/{name}/frame")
def frame_session(name: str, since: int = 0, wait: float = 2.0) -> dict:
    """Live view: long-poll следующего кадра screencast (jpeg base64)."""
    return _run(browser.frame, name, since, wait)


@app.get("/sessions/{name}/tabs")
def tabs_session(name: str) -> dict:
    return _run(browser.tabs, name)


@app.post("/sessions/{name}/tabs/new")
def tab_new_session(name: str, body: dict = Body(default={})) -> dict:
    return _run(browser.tab_new, name, (body or {}).get("url"))


@app.post("/sessions/{name}/tabs/select")
def tab_select_session(name: str, body: dict = Body(...)) -> dict:
    return _run(browser.tab_select, name, body.get("tab"))


@app.post("/sessions/{name}/tabs/close")
def tab_close_session(name: str, body: dict = Body(default={})) -> dict:
    return _run(browser.tab_close, name, (body or {}).get("tab"))


@app.post("/sessions/{name}/input")
def session_input(name: str, body: dict = Body(...)) -> dict:
    """Live view: быстрый ввод владельца (клик, колесо, клавиша, вставка текста)."""
    plugin.require_owner("Ввод в живом просмотре")
    return _run(browser.ui_input, name, body.get("kind") or "", body)


@app.post("/confirm/{confirm_id}")
def confirm_page(confirm_id: str, body: dict = Body(default={})) -> dict:
    """Решение по запросу агента (оплата, оформление заказа): только владелец, не агент сам себе."""
    plugin.require_owner("Оплату и оформление заказа")
    return _run(browser.decide, confirm_id, bool((body or {}).get("approve", True)))


@app.get("/pending/{confirm_id}/screenshot")
def pending_screenshot(confirm_id: str) -> dict:
    return _run(browser.pending_screenshot, confirm_id)


# --- agent commands ------------------------------------------------------------

@plugin.command("sessions", "Список профилей-сессий")
def cmd_sessions(args: dict) -> dict:
    return {"sessions": browser.sessions_out()}


@plugin.command("session create", "Создать именованный профиль")
def cmd_session_create(args: dict) -> dict:
    if not args.get("name"):
        raise CommandError("нужен --name")
    mode = _session_mode(args.get("mode"))
    _owner_for_write(mode)
    return _cmd(
        browser.create_session,
        args["name"],
        mode=mode,
        proxy=args.get("proxy"),
        allowlist=args.get("allowlist"),
    )


@plugin.command("session update", "Изменить режим, proxy или allowlist")
def cmd_session_update(args: dict) -> dict:
    if not args.get("name"):
        raise CommandError("нужен --name")
    fields = {}
    for key in ("mode", "proxy", "allowlist"):
        if key in args and args[key] is not None:
            fields[key] = args[key]
    _owner_for_write(fields.get("mode"))
    return _cmd(browser.update_session, args["name"], **fields)


@plugin.command("session delete", "Удалить сессию и профиль")
def cmd_session_delete(args: dict) -> dict:
    if not args.get("name"):
        raise CommandError("нужен --name")
    return _cmd(browser.delete_session, args["name"])


@plugin.command("open", "Открыть URL в сессии")
def cmd_open(args: dict) -> dict:
    if not args.get("session") or not args.get("url"):
        raise CommandError("нужны --session и --url")
    return _cmd(
        browser.open, args["session"], args["url"],
        tab=args.get("tab"), new_tab=_flag(args.get("new_tab")), wait=_wait_opts(args),
    )


@plugin.command("snapshot", "Что на странице: элементы со ссылками e1, e2… и текст страницы")
def cmd_snapshot(args: dict) -> dict:
    if not args.get("session"):
        raise CommandError("нужен --session")
    return _cmd(browser.snapshot, args["session"], args.get("format") or "page", tab=args.get("tab"),
                max_elements=_int(args.get("max_elements")), max_text=_int(args.get("max_text")))


@plugin.command("download", "Скачать файл по ссылке с куками и прокси сессии")
def cmd_download(args: dict) -> dict:
    if not args.get("session") or not (args.get("url") or args.get("ref") or args.get("selector")):
        raise CommandError("нужны --session и --url или --ref ссылки из snapshot")
    return _cmd(browser.download, args["session"], url=args.get("url"), ref=args.get("ref"),
                selector=args.get("selector"), filename=args.get("name"))


@plugin.command("click", "Клик по селектору/ref")
def cmd_click(args: dict) -> dict:
    if not args.get("session"):
        raise CommandError("нужен --session")
    return _cmd(
        browser.click,
        args["session"],
        selector=args.get("selector"),
        ref=args.get("ref"),
        confirm=args.get("confirm"),
        tab=args.get("tab"),
        wait=_wait_opts(args),
    )


@plugin.command("type", "Ввод текста")
def cmd_type(args: dict) -> dict:
    if not args.get("session") or args.get("text") is None:
        raise CommandError("нужны --session и --text")
    submit = str(args.get("submit") or "").lower() in ("1", "true", "yes", "да")
    return _cmd(
        browser.type_text,
        args["session"],
        args["text"],
        selector=args.get("selector"),
        ref=args.get("ref"),
        submit=submit,
        confirm=args.get("confirm"),
        tab=args.get("tab"),
        wait=_wait_opts(args),
    )


@plugin.command("press", "Нажать клавишу")
def cmd_press(args: dict) -> dict:
    if not args.get("session") or not args.get("key"):
        raise CommandError("нужны --session и --key")
    return _cmd(
        browser.press, args["session"], args["key"], confirm=args.get("confirm"),
        tab=args.get("tab"), wait=_wait_opts(args),
    )


@plugin.command("select", "Выбрать option в select")
def cmd_select(args: dict) -> dict:
    if not args.get("session") or args.get("value") is None:
        raise CommandError("нужны --session и --value")
    return _cmd(
        browser.select,
        args["session"],
        args["value"],
        selector=args.get("selector"),
        ref=args.get("ref"),
        confirm=args.get("confirm"),
        tab=args.get("tab"),
        wait=_wait_opts(args),
    )


@plugin.command("tabs", "Вкладки сессии")
def cmd_tabs(args: dict) -> dict:
    if not args.get("session"):
        raise CommandError("нужен --session")
    return _cmd(browser.tabs, args["session"])


@plugin.command("tab new", "Новая вкладка (активная)")
def cmd_tab_new(args: dict) -> dict:
    if not args.get("session"):
        raise CommandError("нужен --session")
    return _cmd(browser.tab_new, args["session"], args.get("url"), wait=_wait_opts(args))


@plugin.command("tab select", "Сделать вкладку активной")
def cmd_tab_select(args: dict) -> dict:
    if not args.get("session") or args.get("tab") in (None, ""):
        raise CommandError("нужны --session и --tab (номер или id из tabs)")
    return _cmd(browser.tab_select, args["session"], args["tab"])


@plugin.command("tab close", "Закрыть вкладку")
def cmd_tab_close(args: dict) -> dict:
    if not args.get("session"):
        raise CommandError("нужен --session")
    return _cmd(browser.tab_close, args["session"], args.get("tab"))


@plugin.command("wait", "Дождаться селектора, URL, текста, networkidle или паузы")
def cmd_wait(args: dict) -> dict:
    if not args.get("session"):
        raise CommandError("нужен --session")
    opts = _wait_opts({
        "wait_selector": args.get("selector"), "wait_url": args.get("url"),
        "wait_text": args.get("text"), "wait_idle": args.get("networkidle"),
        "wait_ms": args.get("ms"), "wait_timeout": args.get("timeout"),
    })
    if not opts:
        raise CommandError("нужно хотя бы одно условие: --selector, --url, --text, --networkidle или --ms")
    if "selector" in opts and _flag(args.get("hidden_ok")):
        opts["visible"] = False
    return _cmd(browser.wait_for, args["session"], tab=args.get("tab"), **opts)


@plugin.command("close", "Закрыть Chrome сессии")
def cmd_close(args: dict) -> dict:
    if not args.get("session"):
        raise CommandError("нужен --session")
    return _cmd(browser.close, args["session"])


@plugin.command("view", "Текущий кадр сессии")
def cmd_view(args: dict) -> dict:
    if not args.get("session"):
        raise CommandError("нужен --session")
    data = _cmd(browser.view, args["session"], tab=args.get("tab"))
    # keep CLI JSON smaller — drop huge base64 unless asked later
    data.pop("image_base64", None)
    return data


@plugin.command("pending", "Запросы подтверждения: все ожидающие или один по --id (pending, approved, rejected, used)")
def cmd_pending(args: dict) -> dict:
    if args.get("id"):
        return _cmd(browser.pending, confirm_id=str(args["id"]))
    return {"pending": browser.pending(args.get("session"))}


@plugin.command("check", "Проверить Chromium и CDP")
def cmd_check(args: dict) -> dict:
    return browser.check()
