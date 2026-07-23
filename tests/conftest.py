"""Общая настройка тестов.

Тесты не ходят в сеть и не трогают реальную БД: DATA_DIR перенаправляется во
временную папку ДО первого импорта db (иначе db.DB_PATH зафиксируется на боевом
пути). Перед каждым тестом база пересоздаётся с нуля.
"""

import os
import tempfile
import pathlib

# Должно выполниться раньше, чем любой тест сделает `import db`.
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="autopost_tests_")
os.environ["DATA_DIR"] = _TEST_DATA_DIR

import pytest

import db  # noqa: E402  (импорт после установки DATA_DIR — намеренно)


@pytest.fixture(autouse=True)
def fresh_db():
    """Чистая база перед каждым тестом."""
    for f in pathlib.Path(_TEST_DATA_DIR).glob("autopost.db*"):
        f.unlink()
    db.init_db()
    yield


@pytest.fixture
def user_group_source():
    """Создать пользователя, группу и источник-заглушку. Вернуть их идентификаторы."""
    telegram_id = 1001
    db.ensure_user(telegram_id)
    db.add_group(telegram_id, vk_group_id=555, name="Тестовая группа")
    group_id = db.get_groups(telegram_id)[0]["id"]
    source_id = db.add_source(telegram_id, "Источник", "/nonexistent.db", None)
    return {"telegram_id": telegram_id, "group_id": group_id, "source_id": source_id}


def make_source_db(path, videos):
    """Создать SQLite-базу парсера с таблицей videos.

    videos: список кортежей (tt_video_id, username, url, title, duration, found_at).
    """
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE videos (tt_video_id TEXT PRIMARY KEY, username TEXT, url TEXT, "
        "title TEXT, duration INTEGER, found_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO videos (tt_video_id, username, url, title, duration, found_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        videos,
    )
    conn.commit()
    conn.close()


class FakeJobQueue:
    """Заглушка job_queue: запоминает поставленные джобы, ничего не выполняет."""

    def __init__(self):
        self.jobs = []

    def run_once(self, callback, when=None, data=None, name=None, **kwargs):
        self.jobs.append({"callback": callback, "when": when, "data": data, "name": name})
        return object()
