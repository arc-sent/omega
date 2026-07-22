"""SQLite-хранилище автопостера.

Модель данных (связь источник↔группа — многие-ко-многим через «правила»):

    users            — телеграм-пользователь и его VK-токен
    vk_groups        — целевые VK-группы пользователя
    sources          — источники видео (путь к БД парсера + фильтр по нику)
    rules            — правило = пара (источник → группа) + настройки публикации
                       (сколько видео/день, слоты, описание, фильтр длительности)
    published        — что уже опубликовано по каждому правилу (дедуп)
    scheduled_posts  — запланированные публикации (переживают рестарт бота)
    error_logs       — журнал ошибок для /errors и админ-панели
"""

import os
import re
import sqlite3
import time

DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "autopost.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                vk_token    TEXT
            );

            CREATE TABLE IF NOT EXISTS vk_groups (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                vk_group_id INTEGER NOT NULL,
                name        TEXT NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                UNIQUE (telegram_id, vk_group_id)
            );

            CREATE TABLE IF NOT EXISTS sources (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                name        TEXT NOT NULL,       -- как показывать пользователю
                db_path     TEXT NOT NULL,       -- путь к SQLite-базе с видео
                username    TEXT,                -- фильтр по нику внутри базы (опц.)
                kind        TEXT NOT NULL DEFAULT 'db',  -- 'db' | 'account'
                account     TEXT,                -- ник/ссылка TikTok (для kind='account')
                backfill_done INTEGER NOT NULL DEFAULT 0,  -- 1 = вся история аккаунта уже собрана
                parse_start INTEGER NOT NULL DEFAULT 1,    -- с какого ролика парсить (1-based; kind='account')
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                UNIQUE (telegram_id, name)
            );

            -- Правило = связь «источник → группа» + настройки публикации.
            -- Многие-ко-многим: у источника может быть несколько правил (в разные
            -- группы), у группы — несколько правил (из разных источников).
            CREATE TABLE IF NOT EXISTS rules (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id    INTEGER NOT NULL,
                source_id      INTEGER NOT NULL,
                group_id       INTEGER NOT NULL,        -- ссылается на vk_groups.id
                videos_per_day INTEGER NOT NULL DEFAULT 3,
                slots          TEXT NOT NULL DEFAULT '9,15,20',  -- часы МСК через запятую
                description    TEXT,                    -- описание к записи (опц.)
                min_duration   INTEGER,                 -- сек, нижняя граница (опц.)
                max_duration   INTEGER,                 -- сек, верхняя граница (опц.)
                order_dir      TEXT NOT NULL DEFAULT 'old',  -- 'old' | 'new'
                enabled        INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)  ON DELETE CASCADE,
                FOREIGN KEY (source_id)   REFERENCES sources(id)         ON DELETE CASCADE,
                FOREIGN KEY (group_id)    REFERENCES vk_groups(id)       ON DELETE CASCADE,
                UNIQUE (source_id, group_id)
            );

            -- Дедуп: какое видео уже ушло по какому правилу. Одно видео может
            -- уйти в РАЗНЫЕ группы (разные правила), но в одну группу из одного
            -- источника — ровно один раз.
            CREATE TABLE IF NOT EXISTS published (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id      INTEGER NOT NULL,
                tt_video_id  TEXT NOT NULL,
                published_at INTEGER NOT NULL,
                FOREIGN KEY (rule_id) REFERENCES rules(id) ON DELETE CASCADE,
                UNIQUE (rule_id, tt_video_id)
            );

            CREATE INDEX IF NOT EXISTS idx_published_rule ON published (rule_id);

            -- Запланированные публикации: строка живёт от планирования до момента
            -- публикации. Нужна, чтобы расписание пережило рестарт (job_queue в
            -- памяти). Видео скачивается по url в момент публикации, а не заранее.
            CREATE TABLE IF NOT EXISTS scheduled_posts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id   INTEGER NOT NULL,
                rule_id       INTEGER NOT NULL,
                tt_video_id   TEXT NOT NULL,
                url           TEXT NOT NULL,
                title         TEXT,
                description   TEXT,
                vk_group_id   INTEGER NOT NULL,
                vk_group_name TEXT NOT NULL,
                publish_at    INTEGER NOT NULL,
                FOREIGN KEY (rule_id) REFERENCES rules(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_sched_rule ON scheduled_posts (rule_id);

            CREATE TABLE IF NOT EXISTS error_logs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id   INTEGER NOT NULL,
                created_at    INTEGER NOT NULL,
                stage         TEXT,
                platform      TEXT,
                url           TEXT,
                vk_group_id   INTEGER,
                vk_group_name TEXT,
                error_code    INTEGER,
                message       TEXT,
                traceback     TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_error_logs_user
                ON error_logs (telegram_id, created_at DESC);
            """
        )
        # Миграция старых баз, где в sources ещё не было kind/account.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(sources)")}
        if "kind" not in cols:
            conn.execute("ALTER TABLE sources ADD COLUMN kind TEXT NOT NULL DEFAULT 'db'")
        if "account" not in cols:
            conn.execute("ALTER TABLE sources ADD COLUMN account TEXT")
        if "backfill_done" not in cols:
            conn.execute(
                "ALTER TABLE sources ADD COLUMN backfill_done INTEGER NOT NULL DEFAULT 0"
            )
        if "parse_start" not in cols:
            conn.execute(
                "ALTER TABLE sources ADD COLUMN parse_start INTEGER NOT NULL DEFAULT 1"
            )

        # Одно видео не может стоять в очереди по одному правилу дважды. Сначала
        # чистим уже накопленные дубли (оставляем самую раннюю строку), затем
        # вешаем уникальный индекс — дальше INSERT OR IGNORE не даст задвоить.
        conn.execute(
            """
            DELETE FROM scheduled_posts
            WHERE id NOT IN (
                SELECT MIN(id) FROM scheduled_posts GROUP BY rule_id, tt_video_id
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sched_unique "
            "ON scheduled_posts (rule_id, tt_video_id)"
        )


# ─── Пользователи ─────────────────────────────────────────────────────────────

def ensure_user(telegram_id: int) -> None:
    with _connect() as conn:
        conn.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (telegram_id,))


def get_vk_token(telegram_id: int) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT vk_token FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    return row["vk_token"] if row else None


def set_vk_token(telegram_id: int, token: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO users (telegram_id, vk_token) VALUES (?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET vk_token = excluded.vk_token
            """,
            (telegram_id, token),
        )


def clear_vk_token(telegram_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE users SET vk_token = NULL WHERE telegram_id = ?", (telegram_id,))


# ─── Группы VK ────────────────────────────────────────────────────────────────

def get_groups(telegram_id: int) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM vk_groups WHERE telegram_id = ? ORDER BY id", (telegram_id,)
        ).fetchall()


def get_group(group_row_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM vk_groups WHERE id = ?", (group_row_id,)).fetchone()


def add_group(telegram_id: int, vk_group_id: int, name: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO vk_groups (telegram_id, vk_group_id, name) VALUES (?, ?, ?)
            ON CONFLICT(telegram_id, vk_group_id) DO UPDATE SET name = excluded.name
            """,
            (telegram_id, vk_group_id, name),
        )


def rename_group(group_row_id: int, name: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE vk_groups SET name = ? WHERE id = ?", (name, group_row_id))


def delete_group(group_row_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM vk_groups WHERE id = ?", (group_row_id,))


# ─── Источники ────────────────────────────────────────────────────────────────

def get_sources(telegram_id: int) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM sources WHERE telegram_id = ? ORDER BY id", (telegram_id,)
        ).fetchall()


def get_source(source_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()


def add_source(
    telegram_id: int, name: str, db_path: str, username: str | None,
    *, kind: str = "db", account: str | None = None,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO sources (telegram_id, name, db_path, username, kind, account) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (telegram_id, name, db_path, username, kind, account),
        )
        return cur.lastrowid


def set_source_db_path(source_id: int, db_path: str) -> None:
    """Проставить путь к базе (для источника-аккаунта — после получения id)."""
    with _connect() as conn:
        conn.execute("UPDATE sources SET db_path = ? WHERE id = ?", (db_path, source_id))


def set_source_parse_start(source_id: int, parse_start: int) -> None:
    """С какого ролика (1-based) парсить источник-аккаунт при обновлении."""
    with _connect() as conn:
        conn.execute(
            "UPDATE sources SET parse_start = ? WHERE id = ?",
            (max(int(parse_start), 1), source_id),
        )


def get_account_sources() -> list[sqlite3.Row]:
    """Все источники-аккаунты всех пользователей (для планового до-парсинга)."""
    with _connect() as conn:
        return conn.execute("SELECT * FROM sources WHERE kind = 'account'").fetchall()


def set_backfill_done(source_id: int, done: bool = True) -> None:
    """Отметить, что вся история аккаунта собрана (или сбросить флаг)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE sources SET backfill_done = ? WHERE id = ?",
            (1 if done else 0, source_id),
        )


def delete_source(source_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))


# ─── Правила (источник → группа + настройки) ──────────────────────────────────

def get_rules(telegram_id: int) -> list[sqlite3.Row]:
    """Правила пользователя с именами источника и группы (для списков в UI)."""
    with _connect() as conn:
        return conn.execute(
            """
            SELECT r.*, s.name AS source_name,
                   g.name AS group_name, g.vk_group_id AS vk_group_id
            FROM rules r
            JOIN sources   s ON s.id = r.source_id
            JOIN vk_groups g ON g.id = r.group_id
            WHERE r.telegram_id = ?
            ORDER BY r.id
            """,
            (telegram_id,),
        ).fetchall()


def get_enabled_rules() -> list[sqlite3.Row]:
    """Все включённые правила всех пользователей (для планировщика)."""
    with _connect() as conn:
        return conn.execute(
            """
            SELECT r.*, s.name AS source_name, s.db_path AS db_path, s.username AS username,
                   g.name AS group_name, g.vk_group_id AS vk_group_id
            FROM rules r
            JOIN sources   s ON s.id = r.source_id
            JOIN vk_groups g ON g.id = r.group_id
            WHERE r.enabled = 1
            ORDER BY r.telegram_id, r.id
            """
        ).fetchall()


def get_rule(rule_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            """
            SELECT r.*, s.name AS source_name, s.db_path AS db_path, s.username AS username,
                   g.name AS group_name, g.vk_group_id AS vk_group_id
            FROM rules r
            JOIN sources   s ON s.id = r.source_id
            JOIN vk_groups g ON g.id = r.group_id
            WHERE r.id = ?
            """,
            (rule_id,),
        ).fetchone()


def add_rule(
    telegram_id: int,
    source_id: int,
    group_id: int,
    *,
    videos_per_day: int = 3,
    slots: str = "9,15,20",
    description: str | None = None,
    min_duration: int | None = None,
    max_duration: int | None = None,
    order_dir: str = "old",
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO rules
                (telegram_id, source_id, group_id, videos_per_day, slots,
                 description, min_duration, max_duration, order_dir, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (telegram_id, source_id, group_id, videos_per_day, slots,
             description, min_duration, max_duration, order_dir),
        )
        return cur.lastrowid


def update_rule(rule_id: int, **fields) -> None:
    """Обновляет заданные поля правила. Разрешён только белый список колонок."""
    allowed = {
        "videos_per_day", "slots", "description",
        "min_duration", "max_duration", "order_dir", "enabled",
    }
    sets, values = [], []
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f"Недопустимое поле правила: {key}")
        sets.append(f"{key} = ?")
        values.append(value)
    if not sets:
        return
    values.append(rule_id)
    with _connect() as conn:
        conn.execute(f"UPDATE rules SET {', '.join(sets)} WHERE id = ?", values)


def delete_rule(rule_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))


# ─── Дедуп публикаций ─────────────────────────────────────────────────────────

def get_published_ids(rule_id: int) -> set[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT tt_video_id FROM published WHERE rule_id = ?", (rule_id,)
        ).fetchall()
    return {r["tt_video_id"] for r in rows}


def mark_published(rule_id: int, tt_video_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO published (rule_id, tt_video_id, published_at)
            VALUES (?, ?, ?)
            """,
            (rule_id, tt_video_id, int(time.time())),
        )


def count_published(rule_id: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM published WHERE rule_id = ?", (rule_id,)
        ).fetchone()
    return row["c"] if row else 0


def count_published_between(rule_id: int, start_ts: int, end_ts: int) -> int:
    """Сколько опубликовано по правилу за интервал (для дневной квоты)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM published "
            "WHERE rule_id = ? AND published_at >= ? AND published_at < ?",
            (rule_id, start_ts, end_ts),
        ).fetchone()
    return row["c"] if row else 0


# ─── Запланированные публикации ───────────────────────────────────────────────

def get_scheduled_ids(rule_id: int) -> set[str]:
    """Видео правила, уже стоящие в очереди (чтобы планировщик не выбрал их дважды)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT tt_video_id FROM scheduled_posts WHERE rule_id = ?", (rule_id,)
        ).fetchall()
    return {r["tt_video_id"] for r in rows}


def get_scheduled_publish_times(rule_id: int) -> list[int]:
    """Моменты (publish_at) всех публикаций правила, уже стоящих в очереди.

    Нужно планировщику, чтобы не ставить второй ролик на уже занятый слот
    (защита от «пачки» при повторных прогонах/рестартах в пределах дня)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT publish_at FROM scheduled_posts WHERE rule_id = ?", (rule_id,)
        ).fetchall()
    return [r["publish_at"] for r in rows]


def count_scheduled_between(rule_id: int, start_ts: int, end_ts: int) -> int:
    """Сколько публикаций правила уже стоит в очереди на интервал (для дневной квоты)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM scheduled_posts "
            "WHERE rule_id = ? AND publish_at >= ? AND publish_at < ?",
            (rule_id, start_ts, end_ts),
        ).fetchone()
    return row["c"] if row else 0


def add_scheduled_post(
    *,
    telegram_id: int,
    rule_id: int,
    tt_video_id: str,
    url: str,
    title: str | None,
    description: str | None,
    vk_group_id: int,
    vk_group_name: str,
    publish_at: int,
) -> int | None:
    """Поставить публикацию в очередь. Вернуть id новой строки, либо None, если
    это видео уже стоит в очереди по данному правилу (дубль не создаётся)."""
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO scheduled_posts
                (telegram_id, rule_id, tt_video_id, url, title, description,
                 vk_group_id, vk_group_name, publish_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (telegram_id, rule_id, tt_video_id, url, title, description,
             vk_group_id, vk_group_name, publish_at),
        )
        if cur.rowcount == 0:
            return None  # уникальный индекс (rule_id, tt_video_id) отсёк дубль
        return cur.lastrowid


def get_scheduled_posts() -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute("SELECT * FROM scheduled_posts ORDER BY publish_at").fetchall()


def delete_scheduled_post(post_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM scheduled_posts WHERE id = ?", (post_id,))


# ─── Логи ошибок ──────────────────────────────────────────────────────────────

# Маскировка VK-токенов перед записью: текст ошибки/traceback может содержать
# access_token= в URL или сам токен vk1.a.* — их нельзя хранить в логах.
_TOKEN_PATTERNS = [
    (re.compile(r"access_token=[^&\s\"'}]+"), "access_token=***"),
    (re.compile(r"vk1\.a\.[A-Za-z0-9._\-]+"), "vk1.a.***"),
]


def _sanitize(text: str | None) -> str | None:
    if not text:
        return text
    for pattern, repl in _TOKEN_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def log_error(
    telegram_id: int,
    *,
    stage: str | None = None,
    platform: str | None = None,
    url: str | None = None,
    vk_group_id: int | None = None,
    vk_group_name: str | None = None,
    error_code: int | None = None,
    message: str | None = None,
    traceback: str | None = None,
) -> None:
    """Best-effort: при сбое БД не роняет обработку."""
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO error_logs
                    (telegram_id, created_at, stage, platform, url,
                     vk_group_id, vk_group_name, error_code, message, traceback)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    telegram_id, int(time.time()), stage, platform, _sanitize(url),
                    vk_group_id, vk_group_name, error_code,
                    _sanitize(message), _sanitize(traceback),
                ),
            )
    except Exception:
        pass


def get_errors(telegram_id: int, limit: int = 8, offset: int = 0) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            """
            SELECT * FROM error_logs WHERE telegram_id = ?
            ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?
            """,
            (telegram_id, limit, offset),
        ).fetchall()


def count_errors(telegram_id: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM error_logs WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    return row["c"] if row else 0


def get_error(error_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM error_logs WHERE id = ?", (error_id,)).fetchone()


def get_users_with_errors(limit: int = 20, offset: int = 0) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            """
            SELECT telegram_id, COUNT(*) AS cnt, MAX(created_at) AS last_at
            FROM error_logs
            GROUP BY telegram_id
            ORDER BY last_at DESC LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()


def count_users_with_errors() -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT telegram_id) AS c FROM error_logs"
        ).fetchone()
    return row["c"] if row else 0


def cleanup_old_errors(days: int) -> int:
    cutoff = int(time.time()) - days * 86400
    with _connect() as conn:
        cur = conn.execute("DELETE FROM error_logs WHERE created_at < ?", (cutoff,))
        return cur.rowcount
