"""Парсер YouTube-канала для источников-аккаунтов: шортсы (platform='youtube')
или полноценные ролики (platform='youtube_videos').

Перенесён из тестового скрипта youtube_shorts.py (проект «adrian»). Собирает
список роликов вкладки Shorts или «Видео» через yt-dlp в режиме extract_flat —
сами видео НЕ скачиваются. Шортсы пишутся ссылками youtube.com/shorts/<id>,
обычные ролики — youtube.com/watch?v=<id>: по форме ссылки downloader решает,
какой лимит длительности применять и нужен ли H.264 для «Клипов».
Пишет в базу того же формата, что parser_tiktok (таблица videos; в колонке
tt_video_id лежит id шортса), поэтому source_reader и правила работают без правок.

Длительность: вкладка Shorts в плоском режиме её не отдаёт, а запрос по каждому
ролику упирается в «Sign in to confirm you're not a bot». Плейлист всех загрузок
канала (UU…) отдаёт длительность в том же плоском режиме — берём её оттуда.

Из России YouTube заблокирован: YOUTUBE_PROXY (или DOWNLOAD_PROXY) — прокси для
парсинга, YOUTUBE_COOKIES — путь к cookies.txt, если YouTube просит вход.
"""

import os
import re

from yt_dlp import YoutubeDL

# Сколько роликов плейлиста загрузок просмотреть ради длительностей: там вперемешку
# и обычные видео, поэтому окно шире, чем число шортсов.
DURATION_SCAN = int(os.getenv("YOUTUBE_DURATION_SCAN", "1000"))

_NETWORK_MARKERS = ("ssl", "eof occurred", "timed out", "timeout", "connection reset",
                    "connection refused", "connection aborted", "unable to download api page",
                    "failed to establish", "name resolution", "getaddrinfo")


class ExtractionError(Exception):
    """yt-dlp не смог прочитать канал (а не «шортсов нет»)."""


def _proxy() -> str | None:
    return os.getenv("YOUTUBE_PROXY") or os.getenv("DOWNLOAD_PROXY") or None


def default_cookies_path() -> str:
    """Куда класть cookies.txt, чтобы бот подхватил его без настройки: data/ рядом
    с базой бота (DATA_DIR). Папка в .gitignore — куки не уйдут в репозиторий."""
    data_dir = os.getenv("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
    return os.path.join(data_dir, "youtube_cookies.txt")


def cookies_path() -> str | None:
    """YOUTUBE_COOKIES, иначе data/youtube_cookies.txt — если файл существует."""
    path = os.getenv("YOUTUBE_COOKIES") or default_cookies_path()
    return path if os.path.isfile(path) else None


def _cookies() -> str | None:
    return cookies_path()


# Вкладка канала → как строить ссылку на ролик.
TABS = {
    "shorts": "https://www.youtube.com/shorts/{id}",
    "videos": "https://www.youtube.com/watch?v={id}",
}


def normalize_channel(raw: str, tab: str = "shorts") -> tuple[str, str]:
    """Ссылка/@handle/имя/UC-id → (имя канала, ссылка на вкладку tab).

    tab — 'shorts' или 'videos'. Бросает ValueError, если строка не похожа на канал.
    """
    s = (raw or "").strip().rstrip("/")
    m = re.search(r"youtube\.com/(@[\w.\-]+|channel/UC[\w\-]{22}|c/[\w.\-]+|user/[\w.\-]+)", s)
    if m:
        path = m.group(1)
    elif re.fullmatch(r"UC[\w\-]{22}", s):
        path = f"channel/{s}"
    elif re.fullmatch(r"@?[\w.\-]{3,100}", s):
        path = s if s.startswith("@") else f"@{s}"
    else:
        raise ValueError("Не похоже на YouTube-канал. Пришли ссылку на канал или @handle.")
    name = path.split("/")[-1].lstrip("@")
    return name, f"https://www.youtube.com/{path}/{tab}"


def _opts(logger) -> dict:
    opts = {
        "extract_flat": True,
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "logger": logger,
        "socket_timeout": 30,   # без него при блокировке YouTube висит минутами
        "retries": 2,
        "extractor_retries": 1,
    }
    proxy, cookies = _proxy(), _cookies()
    if proxy:
        opts["proxy"] = proxy
    if cookies:
        opts["cookiefile"] = cookies
    return opts


class _Logger:
    def __init__(self):
        self.errors: list[str] = []

    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass

    def error(self, msg):
        self.errors.append(re.sub(r"^ERROR:\s*", "", str(msg)))


def fetch_shorts(shorts_url: str, limit: int, start: int = 1) -> tuple[list, str | None]:
    """(шортсы новые → старые, channel_id) без скачивания."""
    return fetch_tab(shorts_url, limit, start, "shorts")


def fetch_tab(tab_url: str, limit: int, start: int = 1,
              tab: str = "shorts") -> tuple[list, str | None]:
    """(ролики вкладки новые → старые, channel_id) без скачивания.

    start (1-based) и limit — окно, как у parser_tiktok.fetch_videos.
    У каждой записи проставлен url по TABS[tab] (shorts/<id> или watch?v=<id>).
    """
    start = max(start, 1)
    log = _Logger()
    opts = _opts(log)
    opts["playliststart"] = start
    opts["playlistend"] = start + limit - 1
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(tab_url, download=False)
    entries = [e for e in ((info or {}).get("entries") or []) if e and e.get("id")]
    if not entries and log.errors:
        raise ExtractionError(describe_error(log.errors[-1]))
    for e in entries:
        e["url"] = TABS[tab].format(id=e["id"])
    return entries, (info or {}).get("channel_id")


def fill_durations(channel_id: str | None, entries: list, max_scan: int = DURATION_SCAN) -> int:
    """Дозаполнить duration из плейлиста загрузок канала. Вернуть, скольким нашли.

    Сбой здесь не критичен: без длительности видео всё равно попадают в базу, а
    фильтр правил по секундам видео с неизвестной длительностью не отбрасывает.
    """
    need = {e["id"]: e for e in entries if e.get("duration") is None}
    if not need or not channel_id or not channel_id.startswith("UC"):
        return 0
    opts = _opts(_Logger())
    opts["playlistend"] = max_scan
    found = 0
    with YoutubeDL(opts) as ydl:
        # process=False — страницы плейлиста подгружаются лениво, можно выйти раньше.
        info = ydl.extract_info(f"https://www.youtube.com/playlist?list=UU{channel_id[2:]}",
                                download=False, process=False) or {}
        for i, item in enumerate(info.get("entries") or []):
            if i >= max_scan:
                break
            e = need.pop((item or {}).get("id"), None)
            if e is not None and item.get("duration") is not None:
                e["duration"] = int(item["duration"])
                found += 1
            if not need:
                break
    return found


def describe_error(msg: str) -> str:
    """Понятное сообщение по ошибке yt-dlp (исходный текст — в конце)."""
    text = msg.lower()
    if "not a bot" in text or "sign in to confirm" in text:
        hint = ("YouTube требует подтверждения «вы не бот» — задай YOUTUBE_COOKIES "
                "(cookies.txt из браузера) или прокси.")
    elif any(m in text for m in _NETWORK_MARKERS):
        hint = ("YouTube недоступен по сети (из России — блокировка). "
                "Задай YOUTUBE_PROXY или DOWNLOAD_PROXY с зарубежным прокси.")
    elif "does not have a shorts tab" in text:
        hint = "У канала нет вкладки Shorts."
    elif "does not have a videos tab" in text:
        hint = "У канала нет вкладки «Видео»."
    elif "does not exist" in text or "404" in text:
        hint = "Канал не найден — проверь ссылку или @handle."
    else:
        hint = "Не удалось получить шортсы."
    return f"{hint}\nyt-dlp: {msg}"
