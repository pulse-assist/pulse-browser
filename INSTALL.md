# Установка плагина «Браузер»

Инструкция для агента Пульса после `pulse plugins install …`. Пройди её целиком.

## 1. Проверь, что плагин запустился

```bash
pulse plugins list            # у browser — status: running
pulse browser --help          # команды sessions, open, snapshot…
pulse browser check           # Chromium, CDP, каталоги профилей
```

Если status: error — `pulse plugins logs browser --lines 80`, расскажи владельцу, что в журнале.
Плагину нужен Docker (runtime.kind = docker): образ с Chromium собирается при установке (первый раз дольше).

Права при установке: `files:provide` (@browser), `signals:create` (confirm на оплату), `notify`, `network`.

## 2. Закрепи страницу

```bash
pulse plugins pin browser main
```

Страница: [Браузер](/p/browser/main).

## 3. Создай сессию и войди сам

Владелец логинится **сам** на странице плагина (клики по скриншоту + ввод текста). Пароли и 2FA
в чат агенту не передавать.

```bash
pulse browser session create --name demo --mode read-only
pulse browser open --session demo --url https://example.com
pulse browser snapshot --session demo
```

Скриншоты и дампы — в [@browser](/files?path=@browser).

Вкладки и ожидания (0.2):

```bash
pulse browser tabs --session demo
pulse browser tab new --session demo --url https://example.com --wait_selector body
pulse browser wait --session demo --text "Example" --networkidle true
```

Живой просмотр — на странице [Браузер](/p/browser/main): выберите сессию, нажмите «Открыть».

Прокси на профиль (РФ / EU и т.п.):

```bash
pulse browser session create --name ru-services --proxy "http://user:pass@host:port"
pulse browser session update --name eu --proxy ""
```

Режим записи для агента (клики) — только когда нужно:

```bash
pulse browser session update --name demo --mode write
```

Оплата / checkout — агент получит `confirm_id`; после явного «да» владельца:

```bash
pulse browser confirm --session demo --id <confirm_id>
```

## 3а. Обновление

`pulse plugins update browser` (из каталога/папки). Если update падает на `Singleton*` в профилях —
закрой Chrome (`pulse browser close`) и удали висячие симлинки `data/profiles/*/Singleton*`.

## 4. Что сказать владельцу

- Плагин «Браузер» установлен; меню «Плагины ▾» → Браузер.
- Агенты: `pulse browser …`; артефакты в `@browser/<session>/`.
- Вход в сайты — только на странице плагина, не через чат.
