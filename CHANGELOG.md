# CHANGELOG

Журнал доработок по бэклогу `UCHET-BOT-BACKLOG.md`. Формат записи:
`<ID>` — что изменено; нужна ли миграция; нужны ли изменения переменных
окружения при деплое.

## [Не выпущено]

### SEC-1 — боевой токен бота в репозитории
- `.env.example`: реальный `TELEGRAM_BOT_TOKEN` заменён на пустой плейсхолдер;
  `REQUIRED_SUBSCRIPTIONS` — пример-плейсхолдер `@channel_username,@chat_username`.
- `bot.py`: добавлен `require_env()` — fail-fast на старте при пустых
  `TELEGRAM_BOT_TOKEN` / `DB_HOST` / `DB_NAME` / `DB_USER` / `REQUIRED_SUBSCRIPTIONS`.
  Вызывается первой строкой `main()`.
- `.pre-commit-config.yaml`: добавлен хук `gitleaks` — не даёт закоммитить секрет.
- `README_RUN.md`: раздел «Секреты» — секреты только в Environment Variables Render.
- **Миграция:** не требуется.
- **Переменные окружения:** не добавлены; изменена трактовка — пустые
  обязательные переменные теперь обрывают старт.
- **Действия владельца (вне кода):**
  - [ ] отозвать текущий токен через @BotFather (`/revoke`) — **сделать сегодня**;
  - [ ] новый токен положить только в Environment Variables на Render;
  - [ ] очистка утёкшего токена из истории git (`git filter-repo` + force-push) —
        согласовать отдельно, переписывает историю общего репозитория.

### BUG-1 — кнопки экрана подписки не отрисовывались (@username в url кнопки)
- `bot.py`: `normalize_link()` — `@username → https://t.me/username`, полный URL
  и `tg://` пропускаются как есть, голый домен получает `https://`.
- `build_keyboard()`: ссылка нормализуется; при отсутствии — кнопка скрывается,
  экран не рушится (лог о скрытой кнопке).
- `render()`: при провале и `editMessageText`, и `sendMessage` — лог с `screen_id`,
  ответом Telegram и клавиатурой (деградация вместо тишины).
- **Миграция 006_fix_channel_links.sql:** `channel_news`/`chat_accountants` →
  полные `https://t.me/...` URL. Требует применения на боевой БД.
- **Ожидание заказчика:** подтвердить точные адреса каналов (`uchet_kz`, `ConfUchet`).

### BUG-2 — проверка подписки: тихое отключение и неотличимые ошибки
- `bot.py`: `check_subscription()` возвращает `(SUB_OK|SUB_NOT_MEMBER|SUB_API_ERROR, detail)`.
  Пустой список каналов больше не даёт «всех пропускать» (fail-fast в require_env, SEC-1).
- Роутер `check_subscription`: не подписан → подсказка; ошибка API → техническое
  сообщение пользователю + `notify_admin` (зовёт проверить права бота в канале).
- `notify_admin()` — минимальная версия: пишет в лог, если `ADMIN_CHAT_ID` не задан.
- `check_channels_access()` — самодиагностика каналов при старте (getChat в лог).
- Локали: ключ `subscription_tech_error` в `ru.json` и `kz.json`.
  ⚠️ Казахский текст машинный — требует вычитки носителем (см. I18N-1).
- **Переменные окружения:** добавлен необязательный `ADMIN_CHAT_ID`.
- **Миграция:** не требуется.

### QR-1 — QR/памятку нельзя было заменить без разработчика
- `bot.py`: единая `send_media(chat_id, media_key, method, local_path, caption)`;
  источники по порядку: `telegram_file_id` → `source_url` из БД → локальный файл.
  Добавлены `get_media()`, `extract_file_id()`. Удалены `send_photo()`,
  `send_document()`, `get_cached_file_id()`, константа `CPE_HOURS_DOC_URL`.
- **Миграция 007_media_source_url.sql:** `bot_media.source_url`, снятие `NOT NULL`
  с `telegram_file_id`, seed `cpe_hours_doc`. Требует применения на боевой БД.
- `admin.md`: раздел «Как заменить QR-код или памятку» — одним SQL, без деплоя.
- **Ожидание заказчика:** боевой URL Kaspi QR (до него — локальный файл-фолбэк);
  реквизиты для `topic_payment` (сейчас демонстрационный текст).

### OPS-3 — логирование вместо print()
- `bot.py`: настроен `logging` (`log = getLogger("uchet-bot")`, формат с временем и
  уровнем, уровень из `LOG_LEVEL`). Все ~31 `print()` заменены на `log.*`:
  исключения → `log.error(..., exc_info=True)`, транзиентные сбои с фолбэком →
  `log.warning`, старт/статусы → `log.info`.
- Исправлено вводящее в заблуждение сообщение про пул БД: было «min=15, max=20»,
  стало фактическое `minconn=2, maxconn=MAX_WORKERS+4`.
- Пользовательские тексты и ПДн в логи не пишутся.
- **Переменные окружения:** добавлен необязательный `LOG_LEVEL` (по умолчанию `INFO`).

### OPS-1 — /health отражает реальное состояние
- `bot.py`: `health_check()` проверяет возраст последнего успешного `getUpdates`
  (`LAST_POLL_OK` / `POLL_STALE_SECONDS=120`) и доступность БД; при простое или
  недоступности БД отдаёт `503 degraded`.
- Главный цикл обновляет `LAST_POLL_OK` после успешного `getUpdates`.
- `409 Conflict` (второй инстанс с тем же токеном) логируется отдельным `CRITICAL`.
- **Миграция / переменные окружения:** не требуются.
