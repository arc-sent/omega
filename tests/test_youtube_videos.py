"""Полноценные ролики YouTube (platform='youtube_videos'): парсинг вкладки «Видео»
и скачивание с отдельным лимитом длительности, без перекодирования.

В сеть не ходим: yt-dlp подменяется заглушкой.
"""

import os

import pytest

import account_source
import db
import downloader
import parser_youtube as yt
from source_reader import fetch_candidates

TID = 4001


# ─── Парсинг ──────────────────────────────────────────────────────────────────

def test_normalize_channel_videos_tab():
    assert yt.normalize_channel("@smexoshorts", "videos") == (
        "smexoshorts", "https://www.youtube.com/@smexoshorts/videos")
    assert account_source.normalize("@smexoshorts", "youtube_videos")[1].endswith("/videos")
    assert account_source.normalize("@smexoshorts", "youtube")[1].endswith("/shorts")


class FakeTabYDL:
    """Вкладка «Видео» отдаёт длительность сразу; запоминает запрошенные ссылки."""

    videos = [{"id": "vid00000001", "title": "Длинный", "duration": 900},
              {"id": "vid00000002", "title": "Короткий", "duration": 240}]
    calls: list = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False, process=True):
        FakeTabYDL.calls.append(url)
        return {"channel_id": "UC-mn86lSH3M-X_DckQuhZ4A",
                "entries": [dict(e) for e in self.videos]}


def test_refresh_youtube_videos_source(tmp_path, monkeypatch):
    FakeTabYDL.calls = []
    monkeypatch.setattr(yt, "YoutubeDL", FakeTabYDL)
    path = str(tmp_path / "ytv.db")
    res = account_source.refresh_account("@smexoshorts", path, platform="youtube_videos")
    assert res["added"] == 2
    # Берётся вкладка «Видео»; длительности уже есть — плейлист загрузок не нужен.
    assert FakeTabYDL.calls == ["https://www.youtube.com/@smexoshorts/videos"]
    rows = {r["tt_video_id"]: r for r in fetch_candidates(path, limit=10)}
    assert rows["vid00000001"]["url"] == "https://www.youtube.com/watch?v=vid00000001"
    assert rows["vid00000001"]["duration"] == 900


def test_too_long_videos_are_not_saved(tmp_path, monkeypatch):
    """Длиннее YOUTUBE_VIDEO_MAX_DURATION — не в базу (иначе правило брало бы их
    каждый день и падало на скачивании). Неизвестная длительность остаётся."""
    FakeTabYDL.calls = []
    monkeypatch.setattr(yt, "YoutubeDL", FakeTabYDL)
    monkeypatch.setattr(FakeTabYDL, "videos", [
        {"id": "vid00000001", "title": "Длинный", "duration": 900},
        {"id": "vid00000002", "title": "Короткий", "duration": 240},
        {"id": "vid00000003", "title": "Неизвестно", "duration": None},
    ])
    monkeypatch.setattr(downloader, "YOUTUBE_VIDEO_MAX_DURATION", 600)
    # у третьего длительности нет и в плейлисте загрузок — заглушка там ничего не найдёт
    monkeypatch.setattr(yt, "fill_durations", lambda *a, **k: 0)
    path = str(tmp_path / "ytv.db")
    res = account_source.refresh_account("@smexoshorts", path, platform="youtube_videos")
    assert res["total"] == 3 and res["added"] == 2 and res["too_long"] == 1
    assert {r["tt_video_id"] for r in fetch_candidates(path, limit=10)} == {
        "vid00000002", "vid00000003"}


def test_shorts_are_not_filtered_by_long_video_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(yt, "YoutubeDL", FakeTabYDL)
    monkeypatch.setattr(downloader, "YOUTUBE_VIDEO_MAX_DURATION", 10)
    path = str(tmp_path / "yts.db")
    res = account_source.refresh_account("@smexoshorts", path, platform="youtube")
    assert res["too_long"] == 0 and res["added"] == res["total"]


def test_deepen_counts_exhausted_by_fetched_not_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(yt, "YoutubeDL", FakeTabYDL)
    monkeypatch.setattr(downloader, "YOUTUBE_VIDEO_MAX_DURATION", 600)
    path = str(tmp_path / "ytv.db")
    res = account_source.deepen_account("@smexoshorts", path, step=2, platform="youtube_videos")
    # получили ровно столько, сколько просили (2) — дна канала ещё нет, хотя
    # сохранили только 1 (второй длиннее лимита)
    assert res["total"] == 2 and res["added"] == 1 and res["exhausted"] is False


def test_add_source_youtube_videos_platform():
    db.ensure_user(TID)
    sid = db.add_source(TID, "YTV", "", None, kind="account", account="smexo",
                        platform="youtube_videos")
    assert db.get_source(sid)["platform"] == "youtube_videos"


# ─── Скачивание ───────────────────────────────────────────────────────────────

def test_is_youtube_long_video():
    assert downloader.is_youtube_long_video("https://www.youtube.com/watch?v=abc")
    assert downloader.is_youtube_long_video("https://youtu.be/abc")
    assert not downloader.is_youtube_long_video("https://www.youtube.com/shorts/abc")
    assert not downloader.is_youtube_long_video("https://www.tiktok.com/@a/video/1")


def test_check_duration_limits():
    downloader._check_duration(900, 1200)
    with pytest.raises(ValueError, match="Максимум 3 мин"):
        downloader._check_duration(900)
    with pytest.raises(ValueError, match="Максимум 20 мин"):
        downloader._check_duration(1500, 1200)


class FakeDownloadYDL:
    """Фаза 1 — метаданные с duration, фаза 2 — «скачивает» пустой mp4."""

    duration = 900
    download_opts: dict = {}

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        if not download:
            return {"duration": self.duration}
        FakeDownloadYDL.download_opts = self.opts
        self._path = self.opts["outtmpl"].replace("%(id)s", "vid").replace("%(ext)s", "mp4")
        with open(self._path, "wb") as f:
            f.write(b"\0")
        return {"id": "vid", "ext": "mp4", "title": "Ролик"}

    def prepare_filename(self, info):
        return self._path


@pytest.fixture
def fake_download(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", FakeDownloadYDL)
    calls = []
    monkeypatch.setattr(downloader, "_ensure_h264", lambda p: calls.append(p) or p)
    monkeypatch.setattr(downloader, "_youtube_cookies", lambda: None)
    monkeypatch.setattr(downloader, "YOUTUBE_VIDEO_MAX_DURATION", 1200)
    monkeypatch.setattr(downloader, "YOUTUBE_VIDEO_MAX_HEIGHT", 720)
    return {"dir": str(tmp_path), "ensure_calls": calls}


def test_long_video_downloads_h264_capped_without_reencode(fake_download):
    FakeDownloadYDL.duration = 900
    path, title = downloader._download_ytdlp_sync(
        "https://www.youtube.com/watch?v=vid", fake_download["dir"], "youtube", "YouTube Video")
    assert os.path.isfile(path) and title == "Ролик"
    fmt = FakeDownloadYDL.download_opts["format"]
    assert fmt.startswith("bestvideo[vcodec^=avc1][height<=720]")
    assert fake_download["ensure_calls"] == []   # длинный ролик не перекодируем


def test_long_video_over_limit_is_rejected(fake_download):
    FakeDownloadYDL.duration = 1500
    with pytest.raises(ValueError, match="слишком длинное"):
        downloader._download_ytdlp_sync(
            "https://www.youtube.com/watch?v=vid", fake_download["dir"], "youtube", "YouTube Video")


def test_shorts_keep_3_min_limit_and_h264(fake_download):
    FakeDownloadYDL.duration = 900
    with pytest.raises(ValueError, match="Максимум 3 мин"):
        downloader._download_ytdlp_sync(
            "https://www.youtube.com/shorts/vid", fake_download["dir"], "youtube", "YouTube Video")
    FakeDownloadYDL.duration = 50
    downloader._download_ytdlp_sync(
        "https://www.youtube.com/shorts/vid", fake_download["dir"], "youtube", "YouTube Video")
    assert len(fake_download["ensure_calls"]) == 1
    assert "avc1" not in FakeDownloadYDL.download_opts["format"]


# ─── Лимит только из env ──────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    (None, None), ("", None), ("0", None), ("-5", None), ("мусор", None),
    ("1800", 1800), (" 600 ", 600),
])
def test_env_limit(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("YOUTUBE_VIDEO_MAX_DURATION", raising=False)
    else:
        monkeypatch.setenv("YOUTUBE_VIDEO_MAX_DURATION", raw)
    assert downloader._env_limit("YOUTUBE_VIDEO_MAX_DURATION") == expected


def test_no_limit_downloads_any_length(fake_download, monkeypatch):
    monkeypatch.setattr(downloader, "YOUTUBE_VIDEO_MAX_DURATION", None)
    FakeDownloadYDL.duration = 3 * 3600
    path, _ = downloader._download_ytdlp_sync(
        "https://www.youtube.com/watch?v=vid", fake_download["dir"], "youtube", "YouTube Video")
    assert os.path.isfile(path)


def test_no_limit_keeps_all_in_source(tmp_path, monkeypatch):
    monkeypatch.setattr(yt, "YoutubeDL", FakeTabYDL)
    monkeypatch.setattr(downloader, "YOUTUBE_VIDEO_MAX_DURATION", None)
    res = account_source.refresh_account("@smexoshorts", str(tmp_path / "ytv.db"),
                                         platform="youtube_videos")
    assert res["too_long"] == 0 and res["added"] == res["total"]


def test_shorts_limit_stays_3_min_without_env(fake_download, monkeypatch):
    monkeypatch.setattr(downloader, "YOUTUBE_VIDEO_MAX_DURATION", None)
    FakeDownloadYDL.duration = 900
    with pytest.raises(ValueError, match="Максимум 3 мин"):
        downloader._download_ytdlp_sync(
            "https://www.youtube.com/shorts/vid", fake_download["dir"], "youtube", "YouTube Video")


# ─── Язык звука ───────────────────────────────────────────────────────────────

def _fmt(fid, ext, vcodec, acodec, lang=None, h=None):
    d = {"format_id": fid, "ext": ext, "vcodec": vcodec, "acodec": acodec, "url": "u",
         "protocol": "https"}
    if lang:
        d["language"] = lang
    if h:
        d["height"], d["width"] = h, h * 16 // 9
    return d


def _chosen(selector, formats):
    import yt_dlp
    with yt_dlp.YoutubeDL({"quiet": True, "format": selector, "format_sort": ["vcodec:h264"]}) as y:
        res = list(y.build_format_selector(selector)(
            {"formats": [dict(x) for x in formats], "incomplete_formats": False,
             "has_merged_format": False}))
    return [c["format_id"] for c in (res[0].get("requested_formats") or [res[0]])]


SHORT_FMT = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
LONG_FMT = ("bestvideo[vcodec^=avc1][height<=1080]+bestaudio[ext=m4a]"
            "/best[vcodec^=avc1][height<=1080]/best")
VIDEO = _fmt("299", "mp4", "avc1.64002A", "none", h=1080)
EN = _fmt("140-0", "m4a", "none", "mp4a.40.2", "en-US")
RU = _fmt("140-3", "m4a", "none", "mp4a.40.2", "ru")
NOLANG = _fmt("140", "m4a", "none", "mp4a.40.2")


@pytest.mark.parametrize("base", [SHORT_FMT, LONG_FMT])
def test_prefer_audio_lang_picks_russian_track(base):
    sel = downloader._prefer_audio_lang(base, "ru")
    assert _chosen(sel, [EN, RU, VIDEO]) == ["299", "140-3"]


@pytest.mark.parametrize("base", [SHORT_FMT, LONG_FMT])
def test_prefer_audio_lang_falls_back_when_no_russian(base):
    sel = downloader._prefer_audio_lang(base, "ru")
    assert _chosen(sel, [EN, VIDEO]) == ["299", "140-0"]       # только английская
    assert _chosen(sel, [NOLANG, VIDEO]) == ["299", "140"]     # язык не указан


def test_prefer_audio_lang_disabled_or_no_audio_part():
    assert downloader._prefer_audio_lang(SHORT_FMT, "") == SHORT_FMT
    assert downloader._prefer_audio_lang("best", "ru") == "best"


def test_youtube_download_uses_audio_lang(fake_download, monkeypatch):
    monkeypatch.setattr(downloader, "YOUTUBE_AUDIO_LANG", "ru")
    FakeDownloadYDL.duration = 50
    downloader._download_ytdlp_sync(
        "https://www.youtube.com/shorts/vid", fake_download["dir"], "youtube", "YouTube Video")
    assert "language^=ru" in FakeDownloadYDL.download_opts["format"]
    # TikTok язык звука не трогает
    downloader._download_ytdlp_sync(
        "https://www.tiktok.com/@a/video/1", fake_download["dir"], "tiktok", "TikTok Video")
    assert "language^=" not in FakeDownloadYDL.download_opts["format"]
