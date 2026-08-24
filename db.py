"""SQLite-хранилище автопостера.

Модель данных (связь источник↔группа — многие-ко-многим через «правила»):

    users            — телеграм-пользователь
    vk_accounts      — VK-аккаунты (токены) пользователя; один пользователь — много аккаунтов
    vk_groups        — целевые VK-группы; каждая привязана к одному аккаунту
    sources          — источники видео (путь к БД парсера + фильтр по нику)
    rules            — правило = пара (источник → группа) + настройки публикации
    published        — что уже опубликовано по каждому правилу (дедуп)
    scheduled_posts  — запланированные публикации (переживают рестарт бота)
    error_logs       — журнал ошибок для /errors и админ-панели
"""

import os
import re
import sqlite3
import time

import crypto

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
                vk_token    TEXT    -- устарело; хранится только для миграции
            );

            CREATE TABLE IF NOT EXISTS vk_accounts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                name        TEXT NOT NULL DEFAULT 'Основной',
                vk_token    TEXT,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS vk_groups (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                vk_group_id INTEGER NOT NULL,
                name        TEXT NOT NULL,
                account_id  INTEGER,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                FOREIGN KEY (account_id) REFERENCES vk_accounts(id) ON DELETE SET NULL,
                UNIQUE (telegram_id, vk_group_id)
            );

            CREATE TABLE IF NOT EXISTS sources (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id   INTEGER NOT NULL,
                name          TEXT NOT NULL,
                db_path       TEXT NOT NULL,
                username      TEXT,
                kind          TEXT NOT NULL DEFAULT 'db',
                account       TEXT,
                account_id    INTEGER,
                backfill_done INTEGER NOT NULL DEFAULT 0,
                parse_start   INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                FOREIGN KEY (account_id)  REFERENCES vk_accounts(id)   ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS rules (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id    INTEGER NOT NULL,
                source_id      INTEGER NOT NULL,
                group_id       INTEGER NOT NULL,
                videos_per_day INTEGER NOT NULL DEFAULT 3,
                slots          TEXT NOT NULL DEFAULT '9,15,20',
                description    TEXT,
                min_duration   INTEGER,
                max_duration   INTEGER,
                order_dir      TEXT NOT NULL DEFAULT 'old',
                enabled        INTEGER NOT NULL DEFAULT 1,
                allow_repost   INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)  ON DELETE CASCADE,
                FOREIGN KEY (source_id)   REFERENCES sources(id)         ON DELETE CASCADE,
                FOREIGN KEY (group_id)    REFERENCES vk_groups(id)       ON DELETE CASCADE,
                UNIQUE (source_id, group_id)
            );

            CREATE TABLE IF NOT EXISTS published (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id      INTEGER NOT NULL,
                tt_video_id  TEXT NOT NULL,
                published_at INTEGER NOT NULL,
                FOREIGN KEY (rule_id) REFERENCES rules(id) ON DELETE CASCADE,
                UNIQUE (rule_id, tt_video_id)
            );

            CREATE INDEX IF NOT EXISTS idx_published_rule ON published (rule_id);

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
                account_id    INTEGER,
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

        # ── Миграции старых схем ──────────────────────────────────────────────

        rule_cols = {row[1] for row in conn.execute("PRAGMA table_info(rules)")}
        if "allow_repost" not in rule_cols:
            conn.execute("ALTER TABLE rules ADD COLUMN allow_repost INTEGER NOT NULL DEFAULT 0")

        src_cols = {row[1] for row in conn.execute("PRAGMA table_info(sources)")}
        for col, defn in [
            ("kind",         "TEXT NOT NULL DEFAULT 'db'"),
            ("account",      "TEXT"),
            ("backfill_done","INTEGER NOT NULL DEFAULT 0"),
            ("parse_start",  "INTEGER NOT NULL DEFAULT 1"),
        ]:
            if col not in src_cols:
                conn.execute(f"ALTER TABLE sources ADD COLUMN {col} {defn}")

        # Миграция: добавить account_id и снять уникальность по имени.
        # SQLite не умеет DROP CONSTRAINT, поэтому пересоздаём таблицу.
        src_cols = {row[1] for row in conn.execute("PRAGMA table_info(sources)")}
        if "account_id" not in src_cols:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("""
                CREATE TABLE sources_new (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id   INTEGER NOT NULL,
                    name          TEXT NOT NULL,
                    db_path       TEXT NOT NULL,
                    username      TEXT,
                    kind          TEXT NOT NULL DEFAULT 'db',
                    account       TEXT,
                    account_id    INTEGER,
                    backfill_done INTEGER NOT NULL DEFAULT 0,
                    parse_start   INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                    FOREIGN KEY (account_id)  REFERENCES vk_accounts(id)   ON DELETE SET NULL
                )
            """)
            conn.execute("""
                INSERT INTO sources_new
                    (id, telegram_id, name, db_path, username,
                     kind, account, backfill_done, parse_start)
                SELECT id, telegram_id, name, db_path, username,
                       COALESCE(kind, 'db'), account,
                       COALESCE(backfill_done, 0), COALESCE(parse_start, 1)
                FROM sources
            """)
            conn.execute("DROP TABLE sources")
            conn.execute("ALTER TABLE sources_new RENAME TO sources")
            conn.execute("PRAGMA foreign_keys = ON")

        grp_cols = {row[1] for row in conn.execute("PRAGMA table_info(vk_groups)")}
        if "account_id" not in grp_cols:
            conn.execute(
                "ALTER TABLE vk_groups ADD COLUMN account_id INTEGER "
                "REFERENCES vk_accounts(id) ON DELETE SET NULL"
            )

        sp_cols = {row[1] for row in conn.execute("PRAGMA table_info(scheduled_posts)")}
        if "account_id" not in sp_cols:
            conn.execute("ALTER TABLE scheduled_posts ADD COLUMN account_id INTEGER")

        # Уникальный индекс на scheduled_posts
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

        # ── Миграция users.vk_token → vk_accounts ────────────────────────────
        for row in conn.execute(
            "SELECT telegram_id, vk_token FROM users WHERE vk_token IS NOT NULL"
        ).fetchall():
            token = row["vk_token"]
            if not crypto.is_encrypted(token):
                token = crypto.encrypt(token) or token
            existing = conn.execute(
                "SELECT id FROM vk_accounts WHERE telegram_id = ?", (row["telegram_id"],)
            ).fetchone()
            if not existing:
                cur = conn.execute(
                    "INSERT INTO vk_accounts (telegram_id, name, vk_token) VALUES (?, ?, ?)",
                    (row["telegram_id"], "Основной", token),
                )
                aid = cur.lastrowid
                conn.execute(
                    "UPDATE vk_groups SET account_id = ? "
                    "WHERE telegram_id = ? AND account_id IS NULL",
                    (aid, row["telegram_id"]),
                )

        # Зашифровать незашифрованные токены в vk_accounts
        for row in conn.execute(
            "SELECT id, vk_token FROM vk_accounts WHERE vk_token IS NOT NULL"
        ).fetchall():
            if not crypto.is_encrypted(row["vk_token"]):
                enc = crypto.encrypt(row["vk_token"])
                if enc != row["vk_token"]:
                    conn.execute(
                        "UPDATE vk_accounts SET vk_token = ? WHERE id = ?",
                        (enc, row["id"]),
                    )

        # Проставить account_id в scheduled_posts (миграция существующих строк)
        conn.execute(
            """
            UPDATE scheduled_posts SET account_id = (
                SELECT g.account_id FROM vk_groups g
                WHERE g.vk_group_id = scheduled_posts.vk_group_id
                  AND g.telegram_id = scheduled_posts.telegram_id
                LIMIT 1
            ) WHERE account_id IS NULL
            """
        )


# ─── Пользователи ─────────────────────────────────────────────────────────────

def ensure_user(telegram_id: int) -> None:
    with _connect() as conn:
        conn.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (telegram_id,))


# ─── VK-аккаунты ──────────────────────────────────────────────────────────────

def get_accounts(telegram_id: int) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM vk_accounts WHERE telegram_id = ? ORDER BY id",
            (telegram_id,),
        ).fetchall()


def get_account(account_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM vk_accounts WHERE id = ?", (account_id,)
        ).fetchone()


def add_account(telegram_id: int, name: str, token: str) -> int:
    stored = crypto.encrypt(token)
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO vk_accounts (telegram_id, name, vk_token) VALUES (?, ?, ?)",
            (telegram_id, name, stored),
        )
        return cur.lastrowid


def update_account_name(account_id: int, name: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE vk_accounts SET name = ? WHERE id = ?", (name, account_id))


def update_account_token(account_id: int, token: str) -> None:
    stored = crypto.encrypt(token)
    with _connect() as conn:
        conn.execute(
            "UPDATE vk_accounts SET vk_token = ? WHERE id = ?", (stored, account_id)
        )


def delete_account(account_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM vk_accounts WHERE id = ?", (account_id,))


def get_vk_token_by_account(account_id: int) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT vk_token FROM vk_accounts WHERE id = ?", (account_id,)
        ).fetchone()
    if not row:
        return None
    return crypto.decrypt(row["vk_token"])


def count_groups_for_account(account_id: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM vk_groups WHERE account_id = ?", (account_id,)
        ).fetchone()
    return row["c"] if row else 0


# ─── Группы VK ────────────────────────────────────────────────────────────────

def get_groups(telegram_id: int, *, account_id: int | None = None) -> list[sqlite3.Row]:
    with _connect() as conn:
        if account_id is not None:
            return conn.execute(
                """
                SELECT g.*, a.name AS account_name
                FROM vk_groups g
                LEFT JOIN vk_accounts a ON a.id = g.account_id
                WHERE g.telegram_id = ? AND g.account_id = ?
                ORDER BY g.id
                """,
                (telegram_id, account_id),
            ).fetchall()
        return conn.execute(
            """
            SELECT g.*, a.name AS account_name
            FROM vk_groups g
            LEFT JOIN vk_accounts a ON a.id = g.account_id
            WHERE g.telegram_id = ?
            ORDER BY g.id
            """,
            (telegram_id,),
        ).fetchall()


def get_group(group_row_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM vk_groups WHERE id = ?", (group_row_id,)
        ).fetchone()


def add_group(telegram_id: int, vk_group_id: int, name: str, account_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO vk_groups (telegram_id, vk_group_id, name, account_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id, vk_group_id)
            DO UPDATE SET name = excluded.name, account_id = excluded.account_id
            """,
            (telegram_id, vk_group_id, name, account_id),
        )


def rename_group(group_row_id: int, name: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE vk_groups SET name = ? WHERE id = ?", (name, group_row_id))


def delete_group(group_row_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM vk_groups WHERE id = ?", (group_row_id,))


# ─── Источники ────────────────────────────────────────────────────────────────

def get_sources(telegram_id: int, *, for_account_id: int | None = None) -> list[sqlite3.Row]:
    with _connect() as conn:
        if for_account_id is not None:
            return conn.execute(
                "SELECT * FROM sources WHERE telegram_id = ? "
                "AND (account_id = ? OR account_id IS NULL) ORDER BY id",
                (telegram_id, for_account_id),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM sources WHERE telegram_id = ? ORDER BY id", (telegram_id,)
        ).fetchall()


def get_source(source_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()


def add_source(
    telegram_id: int, name: str, db_path: str, username: str | None,
    *, kind: str = "db", account: str | None = None, account_id: int | None = None,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO sources "
            "(telegram_id, name, db_path, username, kind, account, account_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (telegram_id, name, db_path, username, kind, account, account_id),
        )
        return cur.lastrowid


def set_source_db_path(source_id: int, db_path: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE sources SET db_path = ? WHERE id = ?", (db_path, source_id))


def set_source_parse_start(source_id: int, parse_start: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE sources SET parse_start = ? WHERE id = ?",
            (max(int(parse_start), 1), source_id),
        )


def get_account_sources() -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute("SELECT * FROM sources WHERE kind = 'account'").fetchall()


def set_backfill_done(source_id: int, done: bool = True) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE sources SET backfill_done = ? WHERE id = ?",
            (1 if done else 0, source_id),
        )


def delete_source(source_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))


# ─── Правила ──────────────────────────────────────────────────────────────────

def get_rules(telegram_id: int, *, account_id: int | None = None) -> list[sqlite3.Row]:
    with _connect() as conn:
        if account_id is not None:
            return conn.execute(
                """
                SELECT r.*, s.name AS source_name,
                       g.name AS group_name, g.vk_group_id AS vk_group_id,
                       g.account_id AS account_id, a.name AS account_name
                FROM rules r
                JOIN sources      s ON s.id = r.source_id
                JOIN vk_groups    g ON g.id = r.group_id
                LEFT JOIN vk_accounts a ON a.id = g.account_id
                WHERE r.telegram_id = ? AND g.account_id = ?
                ORDER BY r.id
                """,
                (telegram_id, account_id),
            ).fetchall()
        return conn.execute(
            """
            SELECT r.*, s.name AS source_name,
                   g.name AS group_name, g.vk_group_id AS vk_group_id,
                   g.account_id AS account_id, a.name AS account_name
            FROM rules r
            JOIN sources      s ON s.id = r.source_id
            JOIN vk_groups    g ON g.id = r.group_id
            LEFT JOIN vk_accounts a ON a.id = g.account_id
            WHERE r.telegram_id = ?
            ORDER BY r.id
            """,
            (telegram_id,),
        ).fetchall()


def get_enabled_rules() -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            """
            SELECT r.*, s.name AS source_name, s.db_path AS db_path, s.username AS username,
                   g.name AS group_name, g.vk_group_id AS vk_group_id,
                   g.account_id AS account_id
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
                   g.name AS group_name, g.vk_group_id AS vk_group_id,
                   g.account_id AS account_id
            FROM rules r
            JOIN sources   s ON s.id = r.source_id
            JOIN vk_groups g ON g.id = r.group_id
            WHERE r.id = ?
            """,
            (rule_id,),
        ).fetchone()


def add_rule(
    telegram_id: int, source_id: int, group_id: int,
    *, videos_per_day: int = 3, slots: str = "9,15,20",
    description: str | None = None, min_duration: int | None = None,
    max_duration: int | None = None, order_dir: str = "old",
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
    allowed = {
        "videos_per_day", "slots", "description",
        "min_duration", "max_duration", "order_dir", "enabled", "allow_repost",
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


def clear_published(rule_id: int) -> None:
    """Сбросить историю публикаций правила (для режима перезаливки)."""
    with _connect() as conn:
        conn.execute("DELETE FROM published WHERE rule_id = ?", (rule_id,))


def mark_published(rule_id: int, tt_video_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO published (rule_id, tt_video_id, published_at) VALUES (?, ?, ?)",
            (rule_id, tt_video_id, int(time.time())),
        )


def count_published(rule_id: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM published WHERE rule_id = ?", (rule_id,)
        ).fetchone()
    return row["c"] if row else 0


def count_published_between(rule_id: int, start_ts: int, end_ts: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM published "
            "WHERE rule_id = ? AND published_at >= ? AND published_at < ?",
            (rule_id, start_ts, end_ts),
        ).fetchone()
    return row["c"] if row else 0


# ─── Запланированные публикации ───────────────────────────────────────────────

def get_scheduled_ids(rule_id: int) -> set[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT tt_video_id FROM scheduled_posts WHERE rule_id = ?", (rule_id,)
        ).fetchall()
    return {r["tt_video_id"] for r in rows}


def get_scheduled_publish_times(rule_id: int) -> list[int]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT publish_at FROM scheduled_posts WHERE rule_id = ?", (rule_id,)
        ).fetchall()
    return [r["publish_at"] for r in rows]


def count_scheduled_between(rule_id: int, start_ts: int, end_ts: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM scheduled_posts "
            "WHERE rule_id = ? AND publish_at >= ? AND publish_at < ?",
            (rule_id, start_ts, end_ts),
        ).fetchone()
    return row["c"] if row else 0


def add_scheduled_post(
    *, telegram_id: int, rule_id: int, tt_video_id: str, url: str,
    title: str | None, description: str | None,
    vk_group_id: int, vk_group_name: str, publish_at: int,
    account_id: int | None = None,
) -> int | None:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO scheduled_posts
                (telegram_id, rule_id, tt_video_id, url, title, description,
                 vk_group_id, vk_group_name, publish_at, account_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (telegram_id, rule_id, tt_video_id, url, title, description,
             vk_group_id, vk_group_name, publish_at, account_id),
        )
        if cur.rowcount == 0:
            return None
        return cur.lastrowid


def update_scheduled_posts_description(rule_id: int, description: str | None) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE scheduled_posts SET description = ? WHERE rule_id = ?",
            (description, rule_id),
        )
        return cur.rowcount


def get_scheduled_posts() -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute("SELECT * FROM scheduled_posts ORDER BY publish_at").fetchall()


def delete_scheduled_post(post_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM scheduled_posts WHERE id = ?", (post_id,))


# ─── Логи ошибок ──────────────────────────────────────────────────────────────

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
    telegram_id: int, *, stage: str | None = None, platform: str | None = None,
    url: str | None = None, vk_group_id: int | None = None,
    vk_group_name: str | None = None, error_code: int | None = None,
    message: str | None = None, traceback: str | None = None,
) -> None:
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
            "SELECT * FROM error_logs WHERE telegram_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
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
