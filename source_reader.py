"""Чтение видео из БД парсера (источника).

Парсер (проект «парсер») пишет в SQLite таблицу `videos`:
    tt_video_id TEXT PK, username TEXT, url TEXT, title TEXT,
    duration INTEGER, found_at TEXT
Здесь мы только ЧИТАЕМ эту базу (в режиме read-only) и отдаём кандидатов на
публикацию. Сам файл базы парсера бот не меняет.
"""

import os
import sqlite3


class SourceError(Exception):
    """Не удалось прочитать базу источника."""


def _connect_ro(db_path: str) -> sqlite3.Connection:
    """Открыть базу парсера только на чтение (не создавать при отсутствии)."""
    if not os.path.isfile(db_path):
        raise SourceError(f"Файл базы источника не найден: {db_path}")
    # file: URI + mode=ro — не создаём базу и не пишем в неё.
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError as e:
        raise SourceError(f"Не удалось открыть базу источника: {e}") from e


def check_source(db_path: str) -> int:
    """Проверить, что база доступна и в ней есть таблица videos. Вернуть кол-во видео."""
    conn = _connect_ro(db_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='videos'"
        ).fetchone()
        if not row:
            raise SourceError("В базе источника нет таблицы 'videos'. Это точно база парсера?")
        cnt = conn.execute("SELECT COUNT(*) AS c FROM videos").fetchone()["c"]
        return cnt
    finally:
        conn.close()


def fetch_candidates(
    db_path: str,
    *,
    username: str | None = None,
    min_duration: int | None = None,
    max_duration: int | None = None,
    order_dir: str = "old",
    exclude_ids: set[str] | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Вернуть видео-кандидаты на публикацию из базы парсера.

    Фильтрует по нику (если задан) и длительности; исключает уже
    опубликованные/запланированные (exclude_ids). Сортировка по found_at:
    'old' — сначала старые (по умолчанию), 'new' — сначала свежие.
    Видео без известной длительности фильтр по секундам НЕ отбрасывает.
    """
    exclude_ids = exclude_ids or set()
    order_sql = "ASC" if order_dir == "old" else "DESC"

    where = ["1=1"]
    params: list = []
    if username:
        where.append("username = ?")
        params.append(username)
    if min_duration is not None:
        where.append("(duration IS NULL OR duration >= ?)")
        params.append(min_duration)
    if max_duration is not None:
        where.append("(duration IS NULL OR duration <= ?)")
        params.append(max_duration)

    sql = (
        "SELECT tt_video_id, username, url, title, duration, found_at "
        f"FROM videos WHERE {' AND '.join(where)} "
        f"ORDER BY found_at {order_sql}, tt_video_id {order_sql}"
    )

    conn = _connect_ro(db_path)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    result = []
    for r in rows:
        vid = r["tt_video_id"]
        if vid in exclude_ids:
            continue
        result.append({
            "tt_video_id": vid,
            "url": r["url"],
            "title": r["title"],
            "duration": r["duration"],
        })
        if limit is not None and len(result) >= limit:
            break
    return result
