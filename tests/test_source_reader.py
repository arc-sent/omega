"""Тесты source_reader: fetch_candidates — фильтрация, сортировка, ошибки."""

import os
import sqlite3

import pytest

from source_reader import fetch_candidates, SourceError
from conftest import make_source_db


# ─── Вспомогательные данные ───────────────────────────────────────────────────

VIDEOS = [
    ("v1", "alice", "https://tt/v1", "Title 1", 10,  "2026-01-01"),
    ("v2", "bob",   "https://tt/v2", "Title 2", 30,  "2026-01-02"),
    ("v3", "alice", "https://tt/v3", "Title 3", 60,  "2026-01-03"),
    ("v4", "bob",   "https://tt/v4", "Title 4", 90,  "2026-01-04"),
    ("v5", "alice", "https://tt/v5", "Title 5", 120, "2026-01-05"),
]


@pytest.fixture
def src(tmp_path):
    path = str(tmp_path / "source.db")
    make_source_db(path, VIDEOS)
    return path


# ─── Базовые тесты ────────────────────────────────────────────────────────────

def test_fetch_all(src):
    result = fetch_candidates(src)
    assert len(result) == 5
    assert result[0]["tt_video_id"] == "v1"  # order_dir='old' по умолчанию


def test_fetch_returns_expected_fields(src):
    result = fetch_candidates(src, limit=1)
    r = result[0]
    assert "tt_video_id" in r
    assert "url" in r
    assert "title" in r
    assert "duration" in r


def test_fetch_limit(src):
    result = fetch_candidates(src, limit=2)
    assert len(result) == 2


def test_fetch_limit_zero(src):
    result = fetch_candidates(src, limit=0)
    assert result == []


# ─── Фильтр по username ───────────────────────────────────────────────────────

def test_fetch_username_filter(src):
    result = fetch_candidates(src, username="alice")
    ids = [r["tt_video_id"] for r in result]
    assert ids == ["v1", "v3", "v5"]


def test_fetch_username_no_match(src):
    result = fetch_candidates(src, username="charlie")
    assert result == []


# ─── Фильтр по длительности ───────────────────────────────────────────────────

def test_fetch_min_duration(src):
    result = fetch_candidates(src, min_duration=60)
    durations = [r["duration"] for r in result]
    assert all(d >= 60 for d in durations)
    assert len(result) == 3  # v3=60, v4=90, v5=120


def test_fetch_max_duration(src):
    result = fetch_candidates(src, max_duration=30)
    durations = [r["duration"] for r in result]
    assert all(d <= 30 for d in durations)
    assert len(result) == 2  # v1=10, v2=30


def test_fetch_min_max_duration(src):
    result = fetch_candidates(src, min_duration=30, max_duration=90)
    ids = [r["tt_video_id"] for r in result]
    assert ids == ["v2", "v3", "v4"]


def test_fetch_null_duration_passes_filter(tmp_path):
    """Видео без known duration не отбрасывается фильтром по секундам."""
    path = str(tmp_path / "src.db")
    make_source_db(path, [
        ("null_dur", "u", "https://x", "t", None, "2026-01-01"),
    ])
    result = fetch_candidates(path, min_duration=30, max_duration=120)
    assert len(result) == 1
    assert result[0]["tt_video_id"] == "null_dur"


# ─── Порядок сортировки ───────────────────────────────────────────────────────

def test_fetch_order_old(src):
    result = fetch_candidates(src, order_dir="old")
    assert result[0]["tt_video_id"] == "v1"
    assert result[-1]["tt_video_id"] == "v5"


def test_fetch_order_new(src):
    result = fetch_candidates(src, order_dir="new")
    assert result[0]["tt_video_id"] == "v5"
    assert result[-1]["tt_video_id"] == "v1"


# ─── Исключение уже опубликованных ───────────────────────────────────────────

def test_fetch_exclude_ids(src):
    result = fetch_candidates(src, exclude_ids={"v1", "v3", "v5"})
    ids = [r["tt_video_id"] for r in result]
    assert ids == ["v2", "v4"]


def test_fetch_exclude_all(src):
    all_ids = {v[0] for v in VIDEOS}
    result = fetch_candidates(src, exclude_ids=all_ids)
    assert result == []


def test_fetch_exclude_plus_limit(src):
    """exclude_ids + limit: возвращаем ровно limit кандидатов после исключений."""
    result = fetch_candidates(src, exclude_ids={"v1", "v2"}, limit=2)
    ids = [r["tt_video_id"] for r in result]
    assert ids == ["v3", "v4"]


# ─── SQL LIMIT — защита от полного скана большой базы ────────────────────────

def test_fetch_sql_limit_does_not_break_results(tmp_path):
    """При large exclude_ids и небольшом limit возвращаем правильные кандидаты."""
    path = str(tmp_path / "big.db")
    # 200 видео; первые 190 будут в exclude_ids, нужно вернуть 3 из оставшихся
    videos = [
        (f"v{i:03d}", "u", f"https://x/{i}", f"t{i}", 30, f"2026-01-01T{i:05d}")
        for i in range(200)
    ]
    make_source_db(path, videos)
    exclude = {f"v{i:03d}" for i in range(190)}
    result = fetch_candidates(path, exclude_ids=exclude, limit=3, order_dir="old")
    assert len(result) == 3
    # Ожидаем v190, v191, v192 (первые не исключённые в порядке ASC)
    assert result[0]["tt_video_id"] == "v190"
    assert result[1]["tt_video_id"] == "v191"


# ─── Ошибки ───────────────────────────────────────────────────────────────────

def test_fetch_missing_file_raises():
    with pytest.raises(SourceError, match="не найден"):
        fetch_candidates("/nonexistent/path.db")


def test_fetch_no_videos_table_raises(tmp_path):
    """База без таблицы videos → SourceError (OperationalError обёрнут)."""
    path = str(tmp_path / "empty.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE other (id INTEGER)")
    conn.close()
    with pytest.raises(SourceError):
        fetch_candidates(path)


def test_fetch_wrong_schema_raises(tmp_path):
    """База с таблицей videos, но без поля found_at → SourceError."""
    path = str(tmp_path / "bad_schema.db")
    conn = sqlite3.connect(path)
    # Схема без found_at (более старая версия парсера)
    conn.execute(
        "CREATE TABLE videos (tt_video_id TEXT PRIMARY KEY, username TEXT, "
        "url TEXT, title TEXT, duration INTEGER)"
    )
    conn.execute("INSERT INTO videos VALUES ('v1', 'u', 'https://x', 't', 30)")
    conn.commit()
    conn.close()
    with pytest.raises(SourceError):
        fetch_candidates(path)


def test_fetch_empty_table(tmp_path):
    """Пустая таблица videos — возвращает пустой список, не ошибку."""
    path = str(tmp_path / "empty_videos.db")
    make_source_db(path, [])
    result = fetch_candidates(path)
    assert result == []
