# Браузер — плагин Пульса (CDP)

Управляемый Chromium для агентов: персистентные профили, прокси на сессию, snapshot/клики,
живой просмотр в UI, вкладки, ожидания. Владелец логинится сам; агент работает в уже авторизованной сессии.

## Стек

- Runtime: Docker-образ (`Dockerfile`) с Debian Chromium + Python FastAPI
- Протокол: raw CDP (не Playwright)
- Профили: `$PULSE_DATA_DIR/plugins/browser/data/profiles/<session>/`
- Артефакты: `@browser/<session>/` (screenshots, html, a11y, runs/)

## Команды

```bash
pulse browser sessions
pulse browser session create --name amazon --mode read-only --proxy "http://…"
pulse browser open --session amazon --url https://…
pulse browser snapshot --session amazon
pulse browser click --session amazon --selector "button#buy"   # нужен mode=write
pulse browser type --session amazon --selector "input" --text "…"
pulse browser confirm --session amazon --id c…
pulse browser close --session amazon
pulse browser check
```

### v0.2 — вкладки, ожидания, live view

```bash
pulse browser tabs --session amazon                       # номер, id, url, title, active (номера стабильны)
pulse browser tab new --session amazon --url https://…    # новая вкладка, становится активной
pulse browser tab select --session amazon --tab 1         # номер или id; дальше всё идёт в неё
pulse browser tab close --session amazon --tab 1          # последнюю закрыть нельзя — используйте close
pulse browser open --session amazon --url https://… --new_tab true
pulse browser click --session amazon --tab 0 --selector "a.next" --wait_idle true

pulse browser wait --session amazon --selector "#results" --text "Результаты" --networkidle true --timeout 20
pulse browser wait --session amazon --url "re:/dp/[A-Z0-9]{10}" --ms 500
```

- `--tab` есть у open, snapshot, view, click, type, press, select, wait; вкладка становится активной.
- Ожидания: `wait` — все указанные условия сразу; в open/click/type/press/select — `--wait_selector`,
  `--wait_url`, `--wait_text`, `--wait_idle`, `--wait_ms`, `--wait_timeout`. По таймауту — `ok: false`
  в ответе (`wait` внутри действия — поле `wait`), не ошибка: действие уже выполнено.
- Live view на странице [Браузер](/p/browser/main): CDP screencast (JPEG, long-poll `GET /sessions/<имя>/frame`),
  вкладки сверху, клики, колесо, клавиши и вставка текста идут в ту же сессию (`POST /sessions/<имя>/input`).
  Ввод владельца разрешён всегда (так он входит в аккаунты), режим `write` относится только к агенту.
  Screencast включается, пока страницу кто-то смотрит, и гаснет через ~20 с без запросов.

## Безопасность

- По умолчанию `read-only`; `write` включает владелец
- Checkout/Pay — confirm владельца (сигнал + UI)
- Один writer на сессию (lock)
- Cookies/пароли наружу не отдаются

## Разработка

Исходники живут в каталоге файлов Пульса: `browser/`. Установка с диска:

```bash
pulse plugins install /root/pulse_data/files/browser
# уже установлено — обновление той же/новой версии:
pulse plugins update browser --force
```

Сейчас на стенде: **0.2.0**, status running. Скрипт переустановки — `/root/pulse_data/scripts/browser-redeploy.sh`
(закрывает Chrome и чистит висячие `Singleton*` в профилях — иначе снимок данных при update падает). Потом — git remote и публикация в каталог плагинов.
