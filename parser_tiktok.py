#!/usr/bin/env python3
"""
Сбор ссылок на видео TikTok-аккаунта в SQLite-базу.

Использует yt-dlp в режиме extract_flat=True — сами видео НЕ скачиваются,
собираются только id и ссылки. За один прогон: получить список видео и
записать новые в базу (без дублей).

Пример:
    python tiktok_scraper.py https://www.tiktok.com/@username
    python tiktok_scraper.py username --limit 100 --db my.db
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

try:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError, ExtractorError
except ImportError:
    print("Не установлен yt-dlp. Установите: pip install -U yt-dlp", file=sys.stderr)
    sys.exit(1)


def _parse_youtube(raw: str):
    """Из ссылки/ника YouTube получить (username, shorts_url, 'youtube').

    Собираем именно вкладку Shorts канала (.../shorts). Поддерживаются:
      - https://www.youtube.com/@handle[/...]
      - https://www.youtube.com/channel/UCxxxx | /c/Name | /user/Name
      - @handle / handle   (голый ник)
    """
    # Полная ссылка с @handle
    m = re.search(r"youtube\.com/@([\w.\-]+)", raw, re.IGNORECASE)
    if m:
        handle = m.group(1)
        return handle, f"https://www.youtube.com/@{handle}/shorts", "youtube"

    # Legacy-форматы канала: /channel/UC..., /c/Name, /user/Name
    m = re.search(r"youtube\.com/(channel|c|user)/([\w.\-]+)", raw, re.IGNORECASE)
    if m:
        seg, name = m.group(1), m.group(2)
        return name, f"https://www.youtube.com/{seg}/{name}/shorts", "youtube"

    # Голый ник (со @ или без)
    handle = raw.lstrip("@")
    if not handle or "/" in handle:
        raise ValueError(f"Не удалось определить YouTube-канал из: {raw!r}")
    return handle, f"https://www.youtube.com/@{handle}/shorts", "youtube"


def normalize_account(raw: str, platform: str | None = None):
    """Из ссылки/username/channel_id получить (username, input_url, platform).

    Платформа определяется автоматически по ссылке/префиксу. Для голого ника
    без ссылки используется значение `platform` (по умолчанию 'tiktok').

    Поддерживаются форматы:
      TikTok:
        - https://www.tiktok.com/@username
        - @username / username
        - tiktokuser:1234567890  (обход ошибки "Unable to extract secondary user ID")
        - 1234567890             (голый числовой channel_id)
      YouTube (только вкладка Shorts):
        - https://www.youtube.com/@handle[/shorts]
        - https://www.youtube.com/channel|c|user/<name>
        - youtube:@handle / yt:@handle  (явный префикс для голого ника)
    """
    raw = raw.strip()

    # Явный обход TikTok через channel_id, как советует yt-dlp
    m = re.match(r"tiktokuser:(\d+)$", raw, re.IGNORECASE)
    if m:
        channel_id = m.group(1)
        return channel_id, f"tiktokuser:{channel_id}", "tiktok"

    # Явный префикс YouTube для голого ника: youtube:@handle / yt:@handle
    m = re.match(r"(?:youtube|yt):(.+)$", raw, re.IGNORECASE)
    if m:
        return _parse_youtube(m.group(1).strip())

    # Ссылка на YouTube
    if re.search(r"(youtube\.com|youtu\.be)", raw, re.IGNORECASE):
        return _parse_youtube(raw)

    # Ссылка на TikTok
    if re.search(r"tiktok\.com", raw, re.IGNORECASE):
        m = re.search(r"tiktok\.com/@([\w.\-]+)", raw, re.IGNORECASE)
        if not m:
            raise ValueError(f"Не удалось определить username из: {raw!r}")
        username = m.group(1)
        return username, f"https://www.tiktok.com/@{username}", "tiktok"

    # Голый числовой channel_id -> TikTok
    if raw.isdigit():
        return raw, f"tiktokuser:{raw}", "tiktok"

    # Голый ник без ссылки — платформа берётся из аргумента (по умолчанию TikTok)
    plat = (platform or "tiktok").lower()
    if plat == "youtube":
        return _parse_youtube(raw)

    username = raw.lstrip("@")
    if not username or "/" in username:
        raise ValueError(f"Не удалось определить username из: {raw!r}")
    return username, f"https://www.tiktok.com/@{username}", "tiktok"


# --- Собственный парсер по нику (через embed-страницу TikTok) -----------------
#
# Страница профиля https://www.tiktok.com/@name отдаёт «пустую оболочку» без
# данных пользователя, если запрос без JS/куки (анти-бот). А публичная
# embed-страница https://www.tiktok.com/embed/@name предназначена для
# встраивания и НЕ блокируется — в её JSON (__FRONTITY_CONNECT_STATE__)
# лежат и id пользователя, и список последних видео. Это и есть наш
# собственный парсер, независимый от yt-dlp.

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_FRONTITY_RE = re.compile(
    r'<script id="__FRONTITY_CONNECT_STATE__"[^>]*>(.*?)</script>',
    re.DOTALL,
)


class ResolveError(Exception):
    """Не удалось разобрать профиль по нику."""


def _fetch_embed(username: str) -> dict:
    """Скачать и распарсить embed-страницу; вернуть узел с userInfo и videoList."""
    url = f"https://www.tiktok.com/embed/@{username}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            html = resp.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ResolveError(f"Аккаунт @{username} не найден (HTTP 404).") from e
        raise ResolveError(f"HTTP {e.code} при запросе embed @{username}.") from e
    except urllib.error.URLError as e:
        raise ResolveError(f"Сетевая ошибка при запросе @{username}: {e.reason}") from e

    m = _FRONTITY_RE.search(html)
    if not m:
        raise ResolveError(
            f"Не удалось найти данные в embed-странице @{username} "
            "(TikTok мог изменить разметку)."
        )
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise ResolveError(f"Не удалось разобрать JSON embed @{username}: {e}") from e

    source = data.get("source", {}).get("data", {})
    # Ключ вида "/embed/@username" (иногда с query) — берём первый подходящий
    node = None
    for key, value in source.items():
        if isinstance(value, dict) and "userInfo" in value:
            node = value
            break
    if not node or not node.get("userInfo"):
        raise ResolveError(
            f"В embed-странице @{username} нет данных пользователя "
            "(аккаунт не существует или скрыт)."
        )
    return node


def resolve_channel_id(username: str) -> str:
    """Собственный парсер: вернуть числовой id пользователя по нику."""
    node = _fetch_embed(username)
    user_id = node.get("userInfo", {}).get("id")
    if not user_id:
        raise ResolveError(f"Не удалось определить id пользователя @{username}.")
    return str(user_id)


def fetch_videos_embed(username: str, limit: int, start: int = 1) -> list:
    """Собственный сбор видео из embed-страницы (без yt-dlp).

    Возвращает entries в том же формате, что и yt-dlp: dict с ключами
    id / url / title — чтобы save_videos работал без изменений.
    Ограничение: embed отдаёт только последние ~10-30 видео (без пагинации),
    поэтому start здесь — лишь смещение внутри этого короткого списка.
    """
    node = _fetch_embed(username)
    author = node.get("userInfo", {}).get("uniqueId") or username
    video_list = node.get("videoList") or []

    offset = max(start - 1, 0)  # start 1-based → индекс с 0
    entries = []
    for v in video_list[offset:offset + limit]:
        vid = v.get("id")
        if not vid:
            continue
        entries.append({
            "id": str(vid),
            "url": f"https://www.tiktok.com/@{author}/video/{vid}",
            "title": v.get("desc") or None,
        })
    return entries


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS videos (
            tt_video_id TEXT PRIMARY KEY,
            username    TEXT NOT NULL,
            url         TEXT NOT NULL,
            title       TEXT,
            duration    INTEGER,
            platform    TEXT,
            found_at    TEXT NOT NULL
        )
        """
    )
    # Миграция старых баз, созданных без колонок duration / platform
    cols = {row[1] for row in conn.execute("PRAGMA table_info(videos)")}
    if "duration" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN duration INTEGER")
    if "platform" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN platform TEXT")
    conn.commit()
    return conn


def format_duration(seconds) -> str:
    """Секунды -> 'M:SS' (или 'H:MM:SS'). Пусто, если длительность неизвестна."""
    if seconds is None:
        return ""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return ""
    if s < 0:
        return ""
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


class ExtractionError(Exception):
    """Не удалось получить список видео (yt-dlp вернул ошибку экстрактора)."""


def resolve_cookiefile() -> str | None:
    """Путь к cookies.txt (Netscape) для yt-dlp или None, если файла нет.

    Куки берутся из локальной папки проекта — нужны для YouTube, чтобы обойти
    бот-чек и получить длительность Shorts. Путь можно переопределить через
    переменную окружения YT_COOKIES_FILE; иначе ищем cookies.txt рядом со
    скриптом (в корне проекта).
    """
    raw = os.environ.get("YT_COOKIES_FILE")
    if raw and raw.strip():
        path = raw.strip()
        return path if os.path.isfile(path) else None
    default = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
    return default if os.path.isfile(default) else None


def fetch_videos(profile_url: str, limit: int, start: int = 1,
                 extract_flat: bool = True, cookiefile: str | None = None):
    """Вернуть список entries (dict) без скачивания видео.

    start (1-based) — с какого ролика списка (новые→старые) начинать: 1 = с
    самого свежего, 20 = пропустить 19 свежих и взять окно начиная с 20-го.
    limit — размер окна (сколько роликов взять начиная со start).

    extract_flat=True — быстрый режим (только список; у TikTok сразу есть
    duration). extract_flat=False — заходить в каждое видео за метаданными
    (нужно для YouTube, где duration нет в плоском списке); медленнее.
    cookiefile — путь к cookies.txt (Netscape) для обхода бот-чека YouTube.

    Отличает «профиль не распарсился» (ExtractionError) от «профиль пустой»
    (пустой список без ошибок). Ошибки yt-dlp перехватываются через logger,
    чтобы не смешивать их с легитимно пустым профилем.
    """
    start = max(start, 1)
    collected_errors: list[str] = []

    class _Logger:
        def debug(self, msg):
            pass

        def info(self, msg):
            pass

        def warning(self, msg):
            pass

        def error(self, msg):
            collected_errors.append(str(msg))

    ydl_opts = {
        "extract_flat": extract_flat,  # True — не заходить в каждое видео
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,   # не бросать исключение, а звать logger.error
        "playliststart": start,             # с какого ролика (1-based, включительно)
        "playlistend": start + limit - 1,   # по какой (включительно) — окно из limit штук
        "logger": _Logger(),
    }
    if cookiefile:
        ydl_opts["cookiefile"] = cookiefile  # Netscape cookies.txt — обход бот-чека YouTube
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(profile_url, download=False)

    if not info:
        # Ничего не извлеклось И были ошибки — это сбой парсинга, а не пустой профиль
        if collected_errors:
            raise ExtractionError(_clean(collected_errors[-1]))
        return []

    entries = info.get("entries") or []
    entries = [e for e in entries if e]  # ignoreerrors может подсунуть None

    # Профиль открылся, но видео 0 и при этом были ошибки → тоже сбой
    if not entries and collected_errors:
        raise ExtractionError(_clean(collected_errors[-1]))

    return entries


def _clean(msg: str) -> str:
    """Убрать префикс 'ERROR: ' из сообщения yt-dlp."""
    return re.sub(r"^ERROR:\s*", "", msg).strip()


def load_dotenv(path: str = ".env") -> None:
    """Подтянуть переменные из .env в окружение (без внешних зависимостей).

    Простой парсер формата KEY=VALUE:
      - пустые строки и строки-комментарии (#) игнорируются;
      - поддерживается необязательный префикс 'export ';
      - кавычки вокруг значения снимаются;
      - уже заданные переменные окружения НЕ перезаписываются
        (реальное окружение имеет приоритет над .env).
    """
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass  # нет доступа к .env — не критично, работаем без него


def resolve_txt_path(db_path: str) -> str | None:
    """Определить путь txt-дампа по переменной окружения TIKTOK_TXT.

    Возвращает None, если запись в txt выключена. Иначе:
      - если TIKTOK_TXT — путь (содержит разделитель или .txt), берём его;
      - если это просто флаг (1/true/yes/on), имя файла берём от БД:
        <db без расширения>.txt.
    """
    raw = os.environ.get("TIKTOK_TXT")
    if raw is None:
        return None
    raw = raw.strip()
    if raw == "" or raw.lower() in ("0", "false", "no", "off"):
        return None
    if raw.lower() in ("1", "true", "yes", "on"):
        base = db_path[:-3] if db_path.lower().endswith(".db") else db_path
        return base + ".txt"
    return raw  # задан явный путь


def _read_int_env(name: str) -> int | None:
    """Прочитать неотрицательное целое из переменной окружения (или None)."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        print(f"Предупреждение: {name}={raw!r} — не целое число, игнорирую.",
              file=sys.stderr)
        return None
    if value < 0:
        print(f"Предупреждение: {name}={value} — отрицательное, игнорирую.",
              file=sys.stderr)
        return None
    return value


def resolve_duration_filter() -> tuple[int | None, int | None]:
    """Границы фильтра по длительности (сек) из env: (min, max).

    TIKTOK_MIN_DURATION / TIKTOK_MAX_DURATION. Любую границу можно не задавать.
    """
    return _read_int_env("TIKTOK_MIN_DURATION"), _read_int_env("TIKTOK_MAX_DURATION")


def filter_by_duration(entries: list, min_sec: int | None, max_sec: int | None) -> list:
    """Оставить видео, длительность которых попадает в [min_sec, max_sec].

    Видео без известной длительности (None — например, из embed-парсера)
    пропускаются в результат: отфильтровать их по длительности невозможно.
    """
    if min_sec is None and max_sec is None:
        return entries

    result = []
    for e in entries:
        dur = e.get("duration")
        if dur is None:
            result.append(e)  # длительность неизвестна — не отбрасываем
            continue
        if min_sec is not None and dur < min_sec:
            continue
        if max_sec is not None and dur > max_sec:
            continue
        result.append(e)
    return result


def save_videos(
    conn: sqlite3.Connection,
    username: str,
    entries: list,
    txt_path: str | None = None,
    platform: str = "tiktok",
) -> int:
    """INSERT OR IGNORE; вернуть количество реально добавленных строк.

    Если задан txt_path, ВСЕ найденные за прогон видео перезаписываются в
    текстовый файл (полный снимок), по одной строке на видео. Файл создаётся
    всегда, даже если новых для БД видео нет.
    """
    found_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    added = 0
    all_rows: list[tuple[str, str, str | None, object]] = []

    for e in entries:
        video_id = e.get("id")
        if not video_id:
            continue

        if platform == "youtube":
            # В глубоком режиме e["url"] — прямая медиа-ссылка; берём канонический
            # адрес Shorts по id, чтобы в БД лежала нормальная веб-ссылка.
            url = f"https://www.youtube.com/shorts/{video_id}"
        else:
            url = e.get("url") or e.get("webpage_url")
            if not url:
                url = f"https://www.tiktok.com/@{username}/video/{video_id}"

        title = e.get("title") or e.get("description")
        duration = e.get("duration")  # секунды; None у embed-парсера

        cur = conn.execute(
            """
            INSERT OR IGNORE INTO videos
                (tt_video_id, username, url, title, duration, platform, found_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (str(video_id), username, url, title, duration, platform, found_at),
        )
        added += cur.rowcount  # 1 если вставлено, 0 если UNIQUE-конфликт

        # бэкфилл: если строка уже была без длительности — дозаполнить
        if not cur.rowcount and duration is not None:
            conn.execute(
                "UPDATE videos SET duration = ? "
                "WHERE tt_video_id = ? AND duration IS NULL",
                (duration, str(video_id)),
            )

        if txt_path:
            all_rows.append((str(video_id), url, title, duration))

    conn.commit()

    if txt_path:
        # полный снимок текущего прогона (перезапись), файл создаётся всегда
        with open(txt_path, "w", encoding="utf-8") as f:
            for video_id, url, title, duration in all_rows:
                f.write(
                    f"{found_at}\t@{username}\t{video_id}\t"
                    f"{format_duration(duration)}\t{url}\t{title or ''}\n"
                )

    return added


def describe_error(err: Exception) -> str:
    """Понятное сообщение по типовым ошибкам."""
    text = str(err).lower()
    if "private" in text:
        return "Аккаунт приватный — список видео недоступен."
    if "secondary user id" in text or "channel_id" in text:
        return (
            "yt-dlp не смог прочитать профиль (не извлёк channel_id).\n"
            "  Это частая проблема TikTok-экстрактора, а не пустой аккаунт.\n"
            "  Попробуйте:\n"
            "    1) обновить yt-dlp:  python -m pip install -U --pre yt-dlp\n"
            "    2) запустить по channel_id:  python tiktok_scraper.py tiktokuser:<channel_id>\n"
            "       (channel_id — длинное число из исходника страницы профиля, поле \"id\"/\"secUid\")"
        )
    if any(w in text for w in ("not found", "404", "doesn't exist", "unable to find")):
        return "Аккаунт не найден. Проверьте username/ссылку."
    if "empty" in text or "no video" in text:
        return "Профиль пустой — видео не найдено."
    return f"Не удалось получить видео: {err}"


def main() -> int:
    load_dotenv()  # подтянуть флаги из .env (например, TIKTOK_TXT)

    parser = argparse.ArgumentParser(
        description="Сбор ссылок на видео TikTok-аккаунта в SQLite (без скачивания)."
    )
    parser.add_argument("account", help="Ссылка на TikTok-аккаунт или username")
    parser.add_argument(
        "--limit", type=int, default=50,
        help="Сколько видео взять начиная со --start (по умолчанию 50)",
    )
    parser.add_argument(
        "--start", type=int, default=1,
        help="С какого ролика начинать (1-based, 1 = самый свежий; по умолчанию 1)",
    )
    parser.add_argument(
        "--db", default=None,
        help="Путь к файлу SQLite (по умолчанию <username>.db)",
    )
    parser.add_argument(
        "--resolve-only", action="store_true",
        help="Только определить channel_id по нику и выйти (без сбора видео)",
    )
    parser.add_argument(
        "--platform", choices=["tiktok", "youtube"], default=None,
        help="Платформа для голого ника без ссылки (по умолчанию tiktok). "
             "Для ссылок платформа определяется автоматически.",
    )
    parser.add_argument(
        "--cookies-file", default=None,
        help="Путь к cookies.txt (Netscape). Нужен для YouTube, чтобы обойти "
             "бот-чек и получить длительность Shorts. Можно задать через env "
             "YT_COOKIES_FILE; по умолчанию берётся cookies.txt рядом со скриптом.",
    )
    args = parser.parse_args()

    cookiefile = args.cookies_file or resolve_cookiefile()

    try:
        username, profile_url, platform = normalize_account(args.account, args.platform)
    except ValueError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1

    is_username_form = profile_url.startswith("https://")

    # Режим "только резолв" (channel_id) поддержан только для TikTok
    if args.resolve_only:
        if platform == "youtube":
            print("Режим --resolve-only поддержан только для TikTok "
                  "(YouTube-канал yt-dlp читает напрямую).", file=sys.stderr)
            return 1
        if not is_username_form:
            print(f"channel_id: {username}")
            return 0
        try:
            channel_id = resolve_channel_id(username)
        except ResolveError as e:
            print(f"Ошибка: {e}", file=sys.stderr)
            return 1
        print(f"@{username} -> channel_id: {channel_id}")
        print(f"Запуск сбора:  python tiktok_scraper.py tiktokuser:{channel_id} --db {username}.db")
        return 0

    db_path = args.db or f"{username}.db"
    source = "yt-dlp"

    # Получение списка видео. Для YouTube — глубокий режим (заходим в каждый
    # Shorts за длительностью), т.к. в плоском списке duration отсутствует.
    deep = platform == "youtube"
    if deep:
        print(f"YouTube: собираю длительность по каждому видео "
              f"(до {args.limit} шт.), это медленнее...", file=sys.stderr)
    try:
        entries = fetch_videos(profile_url, args.limit, args.start,
                               extract_flat=not deep, cookiefile=cookiefile)
    except (ExtractionError, DownloadError, ExtractorError) as e:
        # YouTube: глубокий сбор часто блокируется бот-чеком без кук. Откатываемся
        # на быстрый плоский список (без длительности), чтобы Shorts всё же собрать.
        if platform == "youtube" and deep:
            print(f"YouTube не отдал длительность в глубоком режиме "
                  f"({_clean(str(e))[:120]}).", file=sys.stderr)
            if not cookiefile:
                print("Совет: для длительности положите cookies.txt рядом со "
                      "скриптом или задайте --cookies-file.", file=sys.stderr)
            print("Откат на быстрый режим (без длительности)...", file=sys.stderr)
            try:
                entries = fetch_videos(profile_url, args.limit, args.start,
                                       extract_flat=True, cookiefile=cookiefile)
                deep = False
            except (ExtractionError, DownloadError, ExtractorError) as e2:
                print(f"Ошибка: {describe_error(e2)}", file=sys.stderr)
                return 1
        # TikTok: если запуск был по нику, переключаемся на собственный парсер
        # (embed-страница), который тут работает без куки.
        elif platform == "tiktok" and is_username_form:
            print(f"yt-dlp не смог собрать видео ({_clean(str(e))[:120]}).",
                  file=sys.stderr)
            print(f"Переключаюсь на собственный парсер (embed) для @{username}...",
                  file=sys.stderr)
            try:
                entries = fetch_videos_embed(username, args.limit, args.start)
                source = "embed"
            except ResolveError as re_err:
                print(f"Ошибка: {re_err}", file=sys.stderr)
                return 1
        else:
            print(f"Ошибка: {describe_error(e)}", file=sys.stderr)
            return 1
    except Exception as e:  # сеть, куки и прочее — не роняем скрипт
        if platform == "youtube" and deep:
            print(f"Глубокий режим YouTube не сработал ({str(e)[:120]}).",
                  file=sys.stderr)
            print("Откат на быстрый режим (без длительности)...", file=sys.stderr)
            try:
                entries = fetch_videos(profile_url, args.limit, args.start,
                                       extract_flat=True, cookiefile=cookiefile)
                deep = False
            except Exception as e2:
                print(f"Непредвиденная ошибка при запросе: {e2}", file=sys.stderr)
                return 1
        else:
            print(f"Непредвиденная ошибка при запросе: {e}", file=sys.stderr)
            return 1

    if not entries:
        print(f"Профиль @{username} открыт, но видео не найдено (пустой или приватный аккаунт).")
        print(f"База: {db_path}")
        return 0

    # Фильтр по длительности (env: TIKTOK_MIN_DURATION / TIKTOK_MAX_DURATION)
    min_sec, max_sec = resolve_duration_filter()
    total_found = len(entries)
    if min_sec is not None or max_sec is not None:
        if source == "embed":
            print("Прим.: embed не отдаёт длительность — фильтр по секундам к нему "
                  "не применяется (такие видео проходят как есть).", file=sys.stderr)
        elif platform == "youtube" and not deep:
            print("Прим.: длительность YouTube получить не удалось (нужны куки) — "
                  "фильтр по секундам не применяется, видео проходят как есть.",
                  file=sys.stderr)
        entries = filter_by_duration(entries, min_sec, max_sec)

    if not entries:
        bounds = f"{min_sec or 0}..{max_sec if max_sec is not None else '∞'} сек"
        print(f"После фильтра по длительности ({bounds}) не осталось видео "
              f"(из {total_found} найденных).")
        print(f"База: {db_path}")
        return 0

    # Сохранение
    txt_path = resolve_txt_path(db_path)
    conn = init_db(db_path)
    try:
        added = save_videos(conn, username, entries, txt_path, platform)
    finally:
        conn.close()

    print(f"Аккаунт:         @{username}")
    print(f"Платформа:       {platform}")
    print(f"Источник:        {source}")
    print(f"Найдено видео:   {total_found}")
    if min_sec is not None or max_sec is not None:
        bounds = f"{min_sec or 0}..{max_sec if max_sec is not None else '∞'} сек"
        print(f"После фильтра:    {len(entries)}  (фильтр {bounds})")
    print(f"Новых добавлено: {added}")
    print(f"База:            {db_path}")
    if txt_path:
        print(f"TXT-дамп:        {txt_path}")
    if source == "embed":
        print("Прим.: embed отдаёт только последние ~10-30 видео (без пагинации).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
