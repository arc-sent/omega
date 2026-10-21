"""Источники-аккаунты YouTube: разбор канала, платформа в БД, парсинг в базу.

В сеть не ходим: yt-dlp подменяется заглушкой.
"""

import sqlite3

import pytest

import account_source
import db
import parser_youtube as yt
from source_reader import check_source, fetch_candidates

TID = 3001


# ─── Разбор канала ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, name, url", [
    ("@smexoshorts", "smexoshorts", "https://www.youtube.com/@smexoshorts/shorts"),
    ("smexoshorts", "smexoshorts", "https://www.youtube.com/@smexoshorts/shorts"),
    ("https://www.youtube.com/@smexoshorts", "smexoshorts",
     "https://www.youtube.com/@smexoshorts/shorts"),
    ("https://www.youtube.com/@smexoshorts/shorts/", "smexoshorts",
     "https://www.youtube.com/@smexoshorts/shorts"),
    ("https://youtube.com/channel/UC-mn86lSH3M-X_DckQuhZ4A", "UC-mn86lSH3M-X_DckQuhZ4A",
     "https://www.youtube.com/channel/UC-mn86lSH3M-X_DckQuhZ4A/shorts"),
    ("UC-mn86lSH3M-X_DckQuhZ4A", "UC-mn86lSH3M-X_DckQuhZ4A",
     "https://www.youtube.com/channel/UC-mn86lSH3M-X_DckQuhZ4A/shorts"),
])
def test_normalize_channel(raw, name, url):
    assert yt.normalize_channel(raw) == (name, url)


@pytest.mark.parametrize("raw", ["", "  ", "какой-то текст с пробелами", "ab"])
def test_normalize_channel_rejects_garbage(raw):
    with pytest.raises(ValueError):
        yt.normalize_channel(raw)


def test_account_source_normalize_dispatches_by_platform():
    assert account_source.normalize("@smexoshorts", "youtube")[1].endswith("/shorts")
    assert "tiktok.com" in account_source.normalize("@batek.official")[1]


# ─── Платформа в sources ──────────────────────────────────────────────────────

def test_add_source_platform_default_and_youtube():
    db.ensure_user(TID)
    tt = db.add_source(TID, "TT", "", None, kind="account", account="batek")
    ytid = db.add_source(TID, "YT", "", None, kind="account", account="smexo",
                         platform="youtube")
    assert db.get_source(tt)["platform"] == "tiktok"
    assert db.get_source(ytid)["platform"] == "youtube"
    assert {s["platform"] for s in db.get_account_sources()} == {"tiktok", "youtube"}


def test_migration_adds_platform_as_tiktok():
    """Старая база без колонки platform: init_db добавляет её, источники — TikTok."""
    db.ensure_user(TID)
    sid = db.add_source(TID, "Старый", "", None, kind="account", account="batek")
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("ALTER TABLE sources DROP COLUMN platform")
    db.init_db()
    assert db.get_source(sid)["platform"] == "tiktok"


# ─── Парсинг в базу (yt-dlp подменён) ─────────────────────────────────────────

class FakeYDL:
    """Заглушка YoutubeDL: вкладка Shorts без длительностей, плейлист UU — с ними."""

    shorts = [{"id": "aaaaaaaaaa1", "title": "Первый", "duration": None},
              {"id": "bbbbbbbbbb2", "title": "Второй", "duration": None},
              {"id": "ccccccccccc", "title": "Без длительности", "duration": None}]
    uploads = [{"id": "longvideo01", "duration": 900},       # обычное видео канала
               {"id": "aaaaaaaaaa1", "duration": 41},
               {"id": "bbbbbbbbbb2", "duration": 59}]
    calls: list = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False, process=True):
        FakeYDL.calls.append(url)
        if "list=UU" in url:
            return {"entries": iter([dict(e) for e in self.uploads])}
        start = self.opts.get("playliststart", 1)
        end = self.opts.get("playlistend", len(self.shorts))
        return {"channel_id": "UC-mn86lSH3M-X_DckQuhZ4A",
                "entries": [dict(e) for e in self.shorts[start - 1:end]]}


@pytest.fixture
def fake_ytdlp(monkeypatch):
    FakeYDL.calls = []
    monkeypatch.setattr(yt, "YoutubeDL", FakeYDL)
    return FakeYDL


def test_refresh_youtube_account_writes_source_db(tmp_path, fake_ytdlp):
    path = str(tmp_path / "yt.db")
    res = account_source.refresh_account("@smexoshorts", path, platform="youtube")
    assert res == {"username": "smexoshorts", "added": 3, "total": 3, "source": "yt-dlp"}
    assert check_source(path) == 3

    rows = {r["tt_video_id"]: r for r in fetch_candidates(path, limit=10)}
    assert rows["aaaaaaaaaa1"]["url"] == "https://www.youtube.com/shorts/aaaaaaaaaa1"
    assert rows["aaaaaaaaaa1"]["duration"] == 41
    assert rows["bbbbbbbbbb2"]["duration"] == 59
    # Нет в плейлисте загрузок — длительность неизвестна, но видео в базе.
    assert rows["ccccccccccc"]["duration"] is None
    # Фильтр правила по секундам не отбрасывает видео с неизвестной длительностью.
    kept = {r["tt_video_id"] for r in fetch_candidates(path, max_duration=50, limit=10)}
    assert kept == {"aaaaaaaaaa1", "ccccccccccc"}


def test_refresh_youtube_respects_start_window(tmp_path, fake_ytdlp):
    path = str(tmp_path / "yt.db")
    res = account_source.refresh_account("@smexoshorts", path, limit=1, start=2,
                                         platform="youtube")
    assert res["total"] == 1
    assert [r["tt_video_id"] for r in fetch_candidates(path, limit=10)] == ["bbbbbbbbbb2"]


def test_deepen_youtube_account_detects_bottom(tmp_path, fake_ytdlp):
    path = str(tmp_path / "yt.db")
    account_source.refresh_account("@smexoshorts", path, limit=2, platform="youtube")
    res = account_source.deepen_account("@smexoshorts", path, step=5, platform="youtube")
    assert res["added"] == 1           # третий шортс
    assert res["exhausted"] is True    # просили 7, у канала всего 3
    assert res["can_deepen"] is True


def test_duration_lookup_failure_does_not_break_refresh(tmp_path, fake_ytdlp, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("плейлист недоступен")
    monkeypatch.setattr(yt, "fill_durations", boom)
    path = str(tmp_path / "yt.db")
    res = account_source.refresh_account("@smexoshorts", path, platform="youtube")
    assert res["added"] == 3
    assert all(r["duration"] is None for r in fetch_candidates(path, limit=10))


def test_fill_durations_stops_when_all_found(fake_ytdlp):
    entries = [{"id": "aaaaaaaaaa1", "duration": None}]
    assert yt.fill_durations("UC-mn86lSH3M-X_DckQuhZ4A", entries) == 1
    assert entries[0]["duration"] == 41


def test_fill_durations_needs_channel_id(fake_ytdlp):
    entries = [{"id": "aaaaaaaaaa1", "duration": None}]
    assert yt.fill_durations(None, entries) == 0
    assert fake_ytdlp.calls == []


# ─── Куки ─────────────────────────────────────────────────────────────────────

def test_cookies_default_path_in_data_dir(tmp_path, monkeypatch):
    """Без YOUTUBE_COOKIES куки берутся из data/youtube_cookies.txt, если файл есть."""
    monkeypatch.delenv("YOUTUBE_COOKIES", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert yt.cookies_path() is None
    (tmp_path / "youtube_cookies.txt").write_text("# Netscape HTTP Cookie File\n")
    assert yt.cookies_path() == str(tmp_path / "youtube_cookies.txt")


def test_cookies_env_overrides_default(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "youtube_cookies.txt").write_text("#")
    custom = tmp_path / "other.txt"
    custom.write_text("#")
    monkeypatch.setenv("YOUTUBE_COOKIES", str(custom))
    assert yt.cookies_path() == str(custom)
    monkeypatch.setenv("YOUTUBE_COOKIES", str(tmp_path / "missing.txt"))
    assert yt.cookies_path() is None
