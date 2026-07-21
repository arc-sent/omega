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
- `compute_publish_times`: раскладка N по будущим слотам сегодня, иначе слоты завтра.

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
- `sources.kind` = 'db' (внешний путь) | 'account' (бот парсит сам).
- `sources.platform` = 'tiktok' | 'youtube' (только для kind='account'). Хранится
  ник (`account`), платформа задаёт, как его разбирать при обновлении.
- Для account: `sources.account` = ник, `db_path` = data/sources/<id>.db (ведёт бот).
- `parser_tiktok.py` — копия парсера из проекта «парсер» (не редактировать по мелочи,
  синхронизировать с оригиналом при изменениях). `account_source.py` — обёртка:
  refresh_account(account, db_path, ..., platform) прогоняет yt-dlp→(embed|быстрый режим)
  и пишет в базу источника. normalize_account возвращает (username, url, platform).
- YouTube: собирается вкладка /shorts канала. Длительность есть только в «глубоком»
  режиме (extract_flat=False, заходим в каждый Shorts) — он часто требует куки, иначе
  бот-чек и откат на быстрый режим без длительности. TikTok — плоский режим, при сбое
  по нику откат на embed-страницу.
- Куки для YouTube — из локального `cookies.txt` (Netscape) в корне проекта; путь
  переопределяется env `YT_COOKIES_FILE`. Резолв: `parser_tiktok.resolve_cookiefile`
  (парсинг) и `downloader._resolve_cookiefile` (скачивание Shorts).
- До-парсинг: `scheduler.refresh_account_sources` вызывается в daily_plan_job (раз в
  сутки перед раскладкой) и в restore_and_schedule (при старте). Плюс кнопка
  «🔄 Обновить» в источнике (bot._refresh_source). Прогон парсера — в executor (блокирующий).
- PARSE_LIMIT (env, 200) — сколько последних видео проверять за прогон.

## Не сделано
- Лимит длительности в .env (сейчас захардкожен в downloader.py, как в исходном боте).
- Парсер: TikTok + YouTube Shorts. Другие платформы (Likee и т.п.) как источник-аккаунт
  не поддержаны (скачивание Likee/VK есть, но парсинга списка по аккаунту нет).
