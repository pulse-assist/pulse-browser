---
name: Браузер
description: Управляемый Chrome (CDP) для агентов — pulse browser; сессии владельца, snapshot, клики с policy
---

# Браузер

Плагин даёт агентам **управляемый Chrome** через CDP. Владелец сам логинится на странице
[Браузер](/p/browser/main); ты работаешь в уже авторизованной сессии и **не принимаешь пароли/2FA в чат**.

```bash
pulse browser sessions
pulse browser open --session NAME --url https://…
pulse browser snapshot --session NAME          # a11y + screenshot → @browser/NAME/
pulse browser click|type|press|select …        # только если mode=write
pulse browser view --session NAME
pulse browser tabs|tab new|tab select|tab close --session NAME   # вкладки; --tab N у остальных команд
pulse browser wait --session NAME --selector … --text … --networkidle true --timeout 15
pulse browser close --session NAME
pulse browser check
```

## Правила

- **Секретность.** Пароли, коды 2FA, cookies наружу — не проси и не печатай. Нужен вход — отправь владельца
  на [Браузер](/p/browser/main).
- **read-only по умолчанию.** Клики/ввод — только в сессиях с `mode=write` (владелец включает сам).
- **Деньги / checkout / Pay.** Если команда вернула `confirm_id` — **остановись**, спроси владельца.
  Только после явного «да» в этом чате: `pulse browser confirm --session … --id …`. Не обходи confirm кликами
  «рядом с кнопкой оплаты».
- **Один writer.** Если «сессия занята» — подожди или сделай `snapshot` (чтение).
- **Прокси.** У каждой сессии свой proxy (или без). Не смешивай РФ и EU профили.
- **Артефакты.** Скриншоты и дампы — `@browser/<session>/…`; не монтируй «весь интернет как диск».
- **eval / cookies export.** В MVP произвольный JS и выгрузка cookies наружу не для агента без прямой просьбы
  владельца.

## Типичный сценарий

1. `pulse browser sessions` — есть ли нужный профиль; running ли Chrome.
2. Если нет — попроси владельца создать сессию и войти на странице плагина (или `session create` + он войдёт).
3. `open` → `snapshot` → действуй по селекторам из страницы; после мутаций снова `snapshot`.
4. Опасный URL — жди confirm.
5. После действия, меняющего страницу, ждите `--wait_selector/--wait_idle` вместо повторных snapshot.
   Ответ с `wait.ok=false` — условие не наступило за таймаут: проверь snapshot, не считай шаг успешным.
6. Всплыла новая вкладка (`target=_blank`) — `pulse browser tabs`, затем `tab select`. Номера вкладок стабильны,
   пока вкладка жива. Live view владельца — на странице плагина; агенту он не нужен.
