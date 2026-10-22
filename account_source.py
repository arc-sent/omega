"""Источник-аккаунт: бот сам парсит аккаунт в свою базу.

Платформа источника (sources.platform) выбирает парсер: 'tiktok' — parser_tiktok
(перенесённый парсер), 'youtube' — parser_youtube (шортсы канала), 'youtube_videos'
— parser_youtube (полноценные ролики канала, вкладка «Видео»). Все пишут в
базу одного формата (таблица videos) — отдельную SQLite-базу источника в
data/sources/. Дальше правила читают её так же, как внешнюю базу парсера.
"""

import logging
import os
import sqlite3

import db
import parser_tiktok as pt
import parser_youtube as yt

# Платформа YouTube-источника → вкладка канала.
YOUTUBE_TABS = {"youtube": "shorts", "youtube_videos": "videos"}

logger = logging.getLogger(__name__)

SOURCES_DIR = os.path.join(db.DATA_DIR, "sources")
os.makedirs(SOURCES_DIR, exist_ok=True)

# Сколько последних видео проверять за один прогон парсера.
PARSE_LIMIT = int(os.getenv("PARSE_LIMIT", "200"))

# На сколько роликов углубляться в историю за один проход допарсинга.
PARSE_STEP = int(os.getenv("PARSE_STEP", "200"))


def source_db_path(source_id: int) -> str:
    """Путь к базе, которую бот ведёт под источник-аккаунт."""
    return os.path.join(SOURCES_DIR, f"{source_id}.db")


def normalize(account: str, platform: str = "tiktok") -> tuple[str, str]:
    """(username, profile_url) из ссылки/ника. Бросает ValueError при мусоре."""
    if platform in YOUTUBE_TABS:
        return yt.normalize_channel(account, YOUTUBE_TABS[platform])
    return pt.normalize_account(account)


def _fetch_youtube(account: str, limit: int, start: int = 1,
                   tab: str = "shorts") -> tuple[str, list, str]:
    """Ролики вкладки канала окном [start, start+limit) + длительности из плейлиста
    загрузок (у шортсов в плоском режиме их нет; у «Видео» обычно есть сразу)."""
    name, tab_url = yt.normalize_channel(account, tab)
    entries, channel_id = yt.fetch_tab(tab_url, limit, start, tab)
    try:
        yt.fill_durations(channel_id, entries)
    except Exception:
        # Длительность — бонус: без неё видео всё равно годятся (фильтр их не режет).
        logger.warning("YouTube %s: не удалось узнать длительности", name, exc_info=True)
    return name, entries, "yt-dlp"


def _fetch(account: str, limit: int, start: int = 1,
           platform: str = "tiktok") -> tuple[str, list, str]:
    """Собрать `limit` роликов начиная со `start`-го (1-based, новые→старые).

    start=1 — с самого свежего; start=20 — пропустить 19 свежих. Вернуть
    (username, entries, source).
    """
    if platform in YOUTUBE_TABS:
        return _fetch_youtube(account, limit, start, YOUTUBE_TABS[platform])
    username, profile_url = pt.normalize_account(account)
    is_username_form = profile_url.startswith("https://")

    source = "yt-dlp"
    try:
        entries = pt.fetch_videos(profile_url, limit, start)
    except (pt.ExtractionError, pt.DownloadError, pt.ExtractorError):
        # yt-dlp не справился — для запуска по нику пробуем embed-страницу.
        if not is_username_form:
            raise
        entries = pt.fetch_videos_embed(username, limit, start)
        source = "embed"
    return username, entries, source


def _drop_too_long(entries: list, platform: str) -> tuple[list, int]:
    """Для полноценных роликов YouTube — не пускать в базу то, что не скачается.

    Работает, только если YOUTUBE_VIDEO_MAX_DURATION задан в env (по умолчанию
    лимита нет). Ролик длиннее лимита downloader отвергнет, а упавший пост
    не попадает в published — правило брало бы его снова каждый день. Поэтому
    такие ролики в базу источника не пишем; поднимут лимит — ближайший до-парсинг
    их добавит. Неизвестная длительность не отбрасывается (как и в фильтре правил).
    Возвращает (что сохранять, сколько отброшено).
    """
    from downloader import YOUTUBE_VIDEO_MAX_DURATION as limit
    if platform != "youtube_videos" or not limit:
        return entries, 0  # лимит не задан в env — длительность не ограничена
    kept = [e for e in entries if e.get("duration") is None or e["duration"] <= limit]
    return kept, len(entries) - len(kept)


def _save(username: str, db_path: str, entries: list) -> int:
    conn = pt.init_db(db_path)
    try:
        return pt.save_videos(conn, username, entries)
    finally:
        conn.close()


def count_videos(db_path: str) -> int:
    """Сколько роликов уже в базе источника (0, если базы/таблицы ещё нет)."""
    if not os.path.isfile(db_path):
        return 0
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM videos").fetchone()
        return row[0] if row else 0
    except sqlite3.OperationalError:
        return 0  # таблицы videos ещё нет
    finally:
        conn.close()


def refresh_account(account: str, db_path: str, limit: int = PARSE_LIMIT,
                    start: int = 1, platform: str = "tiktok") -> dict:
    """Спарсить аккаунт и дописать новые видео в базу источника.

    start (1-based) — с какого ролика начинать (1 = с самого свежего). Позволяет
    пропустить N свежих роликов и парсить окно старее. Возвращает
    {username, added, total, source}. Бросает исключения парсера, если ни
    yt-dlp, ни embed не смогли собрать видео.
    """
    username, entries, source = _fetch(account, limit, start, platform)
    to_save, too_long = _drop_too_long(entries, platform)
    added = _save(username, db_path, to_save)
    logger.info("Парсинг %s @%s: старт %s, всего %s, новых %s, длиннее лимита %s (источник %s)",
                platform, username, start, len(entries), added, too_long, source)
    return {"username": username, "added": added, "total": len(entries), "source": source,
            "too_long": too_long}


def deepen_account(account: str, db_path: str, step: int = PARSE_STEP,
                   platform: str = "tiktok") -> dict:
    """Углубиться в историю: запросить (текущее число + step) роликов от верха.

    Список TikTok идёт новыми→старыми, поэтому берём от верха окно шире того, что
    уже собрано: верхушку yt-dlp/embed отсеют по tt_video_id (INSERT OR IGNORE),
    а низ окна — это следующие ещё не собранные старые ролики. Новые ролики,
    появившиеся сверху за время выкладки, растворяются автоматически.

    Возвращает refresh-словарь + exhausted (yt-dlp дошёл до дна аккаунта) и
    can_deepen (источник умеет уходить глубже — только yt-dlp, не embed).

    У youtube_videos слишком длинные ролики в базу не пишутся (_drop_too_long),
    поэтому окно растёт только за счёт подходящих: если в очередных step роликах
    канала нет ни одного подходящего, проход ничего не добавит. exhausted считается
    по всем полученным роликам, а не по сохранённым.
    """
    limit = count_videos(db_path) + step
    username, entries, source = _fetch(account, limit, platform=platform)
    to_save, too_long = _drop_too_long(entries, platform)
    added = _save(username, db_path, to_save)
    exhausted = source == "yt-dlp" and len(entries) < limit
    logger.info("Углубление @%s: просили %s, получили %s, новых %s%s",
                username, limit, len(entries), added,
                " (дно аккаунта)" if exhausted else "")
    return {
        "username": username, "added": added, "total": len(entries),
        "source": source, "exhausted": exhausted, "can_deepen": source == "yt-dlp",
        "too_long": too_long,
    }
