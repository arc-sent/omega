# Проект: Автопостер VK

Telegram-бот, который автоматически публикует видео из базы парсера в VK-группы
по расписанию. Пользователь только настраивает; руками ничего не выкладывает.
Родственные проекты на десктопе: «парсер» (наполняет базы) и «бот для парсинга
вк» (ручная публикация — отсюда переиспользован downloader.py и логика VK).

## Стек
- Python 3.10+, python-telegram-bot[job-queue]==21.6, yt-dlp, requests, pytz, python-dotenv.
- ffmpeg обязателен (перекодирование в H.264).

## Архитектура (модель данных)
Связь источник↔группа — многие-ко-многим через «правила» (rules).
- `sources` — источник = путь к SQLite базе парсера + опц. фильтр по нику.
- `vk_groups` — целевые группы.
- `rules` — пара (source_id, group_id) + настройки публикации (videos_per_day,
  slots, description, min/max_duration, order_dir, enabled). UNIQUE(source_id, group_id).
- `published` — дедуп НА ПРАВИЛО: UNIQUE(rule_id, tt_video_id).
- `scheduled_posts` — очередь публикаций, переживает рестарт (публикуем по url,
  видео качается в момент слота, а не заранее).

## Ключевые инварианты (НЕ сломать)
- **Дневная квота**: `plan_rule` планирует не N за вызов, а N в день. Остаток =
  videos_per_day − (запланировано сегодня + опубликовано сегодня). Иначе
  старт + ежедневный job задваивают. См. count_scheduled_between/count_published_between.
- **Дедуп на правило**, а не глобально: одно видео может уйти в разные группы.
- Источник читается **только на чтение** (`source_reader._connect_ro`, mode=ro) —
  файл базы парсера бот не меняет.
- Видео с неизвестной длительностью фильтр по секундам НЕ отбрасывает.
- Токены маскируются в логах ошибок (db._sanitize): access_token=*** / vk1.a.***.

## Планировщик (scheduler.py)
- `restore_and_schedule` (post_init): вернуть очередь из БД + догнать план на сегодня.
- `register_jobs`: run_daily(daily_plan_job) в DAILY_PLAN_HOUR:MINUTE МСК.
- `_publish_job`: скачать → _publish_to_vk (семафор на юзера + ретраи) → mark_published
  → удалить scheduled_post. Успех/ошибка — уведомление владельцу; ошибка ещё в /errors.
- `compute_publish_times`: равномерная раскладка N по окну `PUBLISH_WINDOW_START_HOUR..
  PUBLISH_WINDOW_END_HOUR` МСК (по умолчанию 9..22) — окно делится на videos_per_day
  интервалов, повторный прогон продолжает с индекса `already`, а не начинает заново.
  Настройка «слоты» на правиле в раскладке больше не участвует (осталась в БД/UI,
  но декоративна) — сделано по просьбе пользователя, чтобы избежать резких скачков
  нагрузки на VK (см. flood control, код ошибки 9) и распределить поток ровно.
- **Глобальная пауза публикации** (`/stop`, `/startBot`, только ADMIN_IDS):
  `scheduler.is_publishing_paused()/set_publishing_paused()` — флаг в `db.settings`
  (ключ `publishing_paused`), переживает рестарт. `_publish_job` проверяет его ПЕРВЫМ
  делом (до чтения поста из БД) и, если пауза активна, не публикует и не теряет пост —
  переставляет сам себя на повтор через PUBLISH_PAUSE_RECHECK_SECONDS (60 с). Раскладка
  плана (daily_plan_job/restore_and_schedule) паузой НЕ блокируется — копится очередь,
  публикация просто не уходит в VK, пока не пришлют /startBot.

## Переиспользованный код
- `downloader.py` — копия из «бота для парсинга вк» (TikTok/Likee/YouTube/VK + H.264,
  MAX_VIDEO_DURATION=180, ослабленный SSL для VK CDN — осознанный компромисс).
- `vk.upload_to_vk` и резолв групп — портированы оттуда же.

## Проверка после изменений
```bash
python -m py_compile bot.py db.py vk.py scheduler.py source_reader.py downloader.py
# логика без сети: DATA_DIR=$(mktemp -d), init_db, plan_rule с фейковым job_queue
```
Реальную публикацию/Telegram локально не гоняем (нужны токены). bot.py импортируется
без запуска polling — этого достаточно для smoke-теста хендлеров.

## Источники-аккаунты (парсер встроен — Фаза 3 сделана)
- `sources.kind` = 'db' (внешний путь) | 'account' (TikTok, бот парсит сам).
- Для account: `sources.account` = ник, `db_path` = data/sources/<id>.db (ведёт бот).
- `parser_tiktok.py` — копия парсера из проекта «парсер» (не редактировать по мелочи,
  синхронизировать с оригиналом при изменениях). `account_source.py` — обёртка:
  refresh_account(account, db_path) прогоняет yt-dlp→embed и пишет в базу источника.
- До-парсинг: `scheduler.refresh_account_sources` вызывается в daily_plan_job (раз в
  сутки перед раскладкой) и в restore_and_schedule (при старте). Плюс кнопка
  «🔄 Обновить» в источнике (bot._refresh_source). Прогон парсера — в executor (блокирующий).
- PARSE_LIMIT (env, 200) — сколько последних видео проверять за прогон.

## Обработка видео (video_processor.py)
- Режим хранится в `db.settings` (kv-таблица): `video_process_mode` (off/ultrafast/
  superfast/veryfast/faster) и `video_downscale` (0/1). Читает планировщик, поэтому
  БД, а не PicklePersistence. Меню «⚙️ Обработка» / `/process` — только ADMIN_IDS.
- Встроено в `scheduler._process`, вызывается в `_publish_job` между `_download` и
  `_publish_to_vk`. Executor + общий семафор VIDEO_PROCESS_CONCURRENCY (на 1 ядре — 1)
  + `nice`, чтобы бот отвечал во время перекода.
- **Инвариант**: обработка НИКОГДА не срывает публикацию. `process_video` не бросает
  исключений, возвращает (путь, статус, деталь); при `failed` публикуется оригинал,
  ошибка идёт в /errors, в «✅ Опубликовано» добавляется пометка.
- Ролики длиннее VIDEO_PROCESS_MAX_DURATION (60 с) не обрабатываются (статус
  `skipped`); неизвестная длительность обработку не отменяет.
- Метаданные не стираются в пустоту: `-map_metadata -1` + правдоподобный набор
  «снято на телефон» (creation_time, make/model/com.android.*, аппаратный encoder).
  `-fflags/-flags +bitexact` глушит Lavf/Lavc; тег encoder потока перекрывается явно.

## Не сделано
- Лимит длительности в .env (сейчас захардкожен в downloader.py, как в исходном боте).
- Парсер только TikTok (наследие исходного проекта).
