"""Источник-аккаунт: бот сам парсит TikTok-аккаунт в свою базу.

Обёртка над parser_tiktok (перенесённый парсер): по нику/ссылке собирает ссылки
на видео и складывает их в отдельную SQLite-базу этого источника (в data/sources/).
Дальше правила читают эту базу так же, как внешнюю базу парсера.
"""

import logging
import os
import sqlite3

import db
import parser_tiktok as pt

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


def normalize(account: str, platform: str | None = None) -> tuple[str, str, str]:
    """(username, profile_url, platform) из ссылки/ника.

    platform — подсказка для голого ника без ссылки ('tiktok' | 'youtube').
    Бросает ValueError при мусоре.
    """
    return pt.normalize_account(account, platform)


def _fetch(account: str, limit: int, start: int = 1,
           platform: str | None = None) -> tuple[str, list, str, str]:
    """Собрать `limit` роликов начиная со `start`-го (1-based, новые→старые).

    start=1 — с самого свежего; start=20 — пропустить 19 свежих. Вернуть
    (username, entries, source, platform).

    Для YouTube идём в глубоком режиме (за длительностью Shorts), с куки из
    локального cookies.txt; при бот-чеке откатываемся на быстрый режим без
    длительности. Для TikTok по нику при сбое yt-dlp пробуем embed-страницу.
    """
    username, profile_url, plat = pt.normalize_account(account, platform)
    is_username_form = profile_url.startswith("https://")
    cookiefile = pt.resolve_cookiefile()

    source = "yt-dlp"
    deep = plat == "youtube"
    try:
        entries = pt.fetch_videos(profile_url, limit, start,
                                  extract_flat=not deep, cookiefile=cookiefile)
    except (pt.ExtractionError, pt.DownloadError, pt.ExtractorError):
        if plat == "youtube" and deep:
            # Глубокий режим заблокирован бот-чеком — берём быстрый плоский
            # список (Shorts соберутся, но без длительности).
            entries = pt.fetch_videos(profile_url, limit, start,
                                      extract_flat=True, cookiefile=cookiefile)
        elif plat == "tiktok" and is_username_form:
            entries = pt.fetch_videos_embed(username, limit, start)
            source = "embed"
        else:
            raise
    return username, entries, source, plat


def _save(username: str, db_path: str, entries: list, platform: str = "tiktok") -> int:
    conn = pt.init_db(db_path)
    try:
        return pt.save_videos(conn, username, entries, platform=platform)
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
                    start: int = 1, platform: str | None = None) -> dict:
    """Спарсить аккаунт и дописать новые видео в базу источника.

    start (1-based) — с какого ролика начинать (1 = с самого свежего). Позволяет
    пропустить N свежих роликов и парсить окно старее. platform ('tiktok' |
    'youtube') — подсказка для голого ника. Возвращает
    {username, added, total, source, platform}. Бросает исключения парсера, если
    ни yt-dlp, ни embed не смогли собрать видео.
    """
    username, entries, source, plat = _fetch(account, limit, start, platform)
    added = _save(username, db_path, entries, plat)
    logger.info("Парсинг @%s [%s]: старт %s, всего %s, новых %s (источник %s)",
                username, plat, start, len(entries), added, source)
    return {"username": username, "added": added, "total": len(entries),
            "source": source, "platform": plat}


def deepen_account(account: str, db_path: str, step: int = PARSE_STEP,
                   platform: str | None = None) -> dict:
    """Углубиться в историю: запросить (текущее число + step) роликов от верха.

    Список TikTok идёт новыми→старыми, поэтому берём от верха окно шире того, что
    уже собрано: верхушку yt-dlp/embed отсеют по tt_video_id (INSERT OR IGNORE),
    а низ окна — это следующие ещё не собранные старые ролики. Новые ролики,
    появившиеся сверху за время выкладки, растворяются автоматически.

    Возвращает refresh-словарь + exhausted (yt-dlp дошёл до дна аккаунта) и
    can_deepen (источник умеет уходить глубже — только yt-dlp, не embed).
    """
    limit = count_videos(db_path) + step
    username, entries, source, plat = _fetch(account, limit, platform=platform)
    added = _save(username, db_path, entries, plat)
    exhausted = source == "yt-dlp" and len(entries) < limit
    logger.info("Углубление @%s [%s]: просили %s, получили %s, новых %s%s",
                username, plat, limit, len(entries), added,
                " (дно аккаунта)" if exhausted else "")
    return {
        "username": username, "added": added, "total": len(entries),
        "source": source, "platform": plat,
        "exhausted": exhausted, "can_deepen": source == "yt-dlp",
    }
