"""Тесты модели vk_accounts: CRUD, токены, привязка групп, миграция."""

import sqlite3

import pytest

import db


TID = 2001
TOKEN = "vk1.a." + "x" * 40  # валидный по длине фейковый токен


def _setup_user():
    db.ensure_user(TID)


# ─── CRUD аккаунтов ───────────────────────────────────────────────────────────

def test_add_and_list_accounts():
    _setup_user()
    aid1 = db.add_account(TID, "Основной", TOKEN)
    aid2 = db.add_account(TID, "Второй", TOKEN)
    accounts = db.get_accounts(TID)
    assert len(accounts) == 2
    assert accounts[0]["id"] == aid1
    assert accounts[1]["id"] == aid2
    assert accounts[0]["name"] == "Основной"


def test_get_account():
    _setup_user()
    aid = db.add_account(TID, "Один", TOKEN)
    a = db.get_account(aid)
    assert a is not None
    assert a["name"] == "Один"
    assert a["telegram_id"] == TID


def test_get_account_not_found():
    assert db.get_account(99999) is None


def test_update_account_name():
    _setup_user()
    aid = db.add_account(TID, "Старое", TOKEN)
    db.update_account_name(aid, "Новое")
    assert db.get_account(aid)["name"] == "Новое"


def test_update_account_token_roundtrip():
    _setup_user()
    aid = db.add_account(TID, "Акк", TOKEN)
    new_token = "vk1.a." + "y" * 40
    db.update_account_token(aid, new_token)
    assert db.get_vk_token_by_account(aid) == new_token


def test_get_vk_token_roundtrip():
    _setup_user()
    aid = db.add_account(TID, "Акк", TOKEN)
    assert db.get_vk_token_by_account(aid) == TOKEN


def test_get_vk_token_missing_account():
    assert db.get_vk_token_by_account(99999) is None


def test_delete_account_no_groups():
    _setup_user()
    aid = db.add_account(TID, "Удаляемый", TOKEN)
    assert db.get_account(aid) is not None
    db.delete_account(aid)
    assert db.get_account(aid) is None


def test_delete_account_sets_groups_null():
    """ON DELETE SET NULL: группа остаётся, но account_id становится NULL."""
    _setup_user()
    aid = db.add_account(TID, "Акк", TOKEN)
    db.add_group(TID, vk_group_id=100, name="Группа", account_id=aid)
    gid = db.get_groups(TID)[0]["id"]

    db.delete_account(aid)

    # Группа существует, account_id = NULL
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT account_id FROM vk_groups WHERE id = ?", (gid,)).fetchone()
    assert row is not None
    assert row["account_id"] is None


def test_accounts_isolated_between_users():
    tid2 = TID + 1
    db.ensure_user(TID)
    db.ensure_user(tid2)
    db.add_account(TID, "A", TOKEN)
    db.add_account(tid2, "B", TOKEN)
    assert len(db.get_accounts(TID)) == 1
    assert len(db.get_accounts(tid2)) == 1


# ─── Привязка групп к аккаунту ────────────────────────────────────────────────

def test_count_groups_for_account():
    _setup_user()
    aid = db.add_account(TID, "Акк", TOKEN)
    assert db.count_groups_for_account(aid) == 0
    db.add_group(TID, vk_group_id=1, name="G1", account_id=aid)
    db.add_group(TID, vk_group_id=2, name="G2", account_id=aid)
    assert db.count_groups_for_account(aid) == 2


def test_get_groups_shows_account_name():
    _setup_user()
    aid = db.add_account(TID, "МойАккаунт", TOKEN)
    db.add_group(TID, vk_group_id=1, name="Группа", account_id=aid)
    groups = db.get_groups(TID)
    assert groups[0]["account_name"] == "МойАккаунт"


def test_get_groups_no_account_returns_null_name():
    _setup_user()
    aid = db.add_account(TID, "Акк", TOKEN)
    db.add_group(TID, vk_group_id=1, name="Группа", account_id=aid)
    db.delete_account(aid)  # SET NULL
    groups = db.get_groups(TID)
    assert groups[0]["account_name"] is None


def test_add_group_upsert_changes_account():
    """Повторный add_group меняет аккаунт у существующей группы."""
    _setup_user()
    aid1 = db.add_account(TID, "A1", TOKEN)
    aid2 = db.add_account(TID, "A2", TOKEN)
    db.add_group(TID, vk_group_id=1, name="G", account_id=aid1)
    db.add_group(TID, vk_group_id=1, name="G", account_id=aid2)  # upsert
    groups = db.get_groups(TID)
    assert len(groups) == 1
    assert groups[0]["account_id"] == aid2


# ─── account_id в правилах и очереди ─────────────────────────────────────────

def test_rule_carries_account_id(user_group_source):
    uge = user_group_source
    rid = db.add_rule(uge["telegram_id"], uge["source_id"], uge["group_id"])
    r = db.get_rule(rid)
    assert r["account_id"] == uge["account_id"]


def test_enabled_rules_carry_account_id(user_group_source):
    uge = user_group_source
    rid = db.add_rule(uge["telegram_id"], uge["source_id"], uge["group_id"])
    rules = {r["id"]: r for r in db.get_enabled_rules()}
    assert rules[rid]["account_id"] == uge["account_id"]


def test_scheduled_post_stores_account_id(user_group_source):
    uge = user_group_source
    rid = db.add_rule(uge["telegram_id"], uge["source_id"], uge["group_id"])
    post_id = db.add_scheduled_post(
        telegram_id=uge["telegram_id"], rule_id=rid, tt_video_id="v1",
        url="https://x/v1", title="t", description=None,
        vk_group_id=555, vk_group_name="g", publish_at=1000,
        account_id=uge["account_id"],
    )
    posts = {p["id"]: p for p in db.get_scheduled_posts()}
    assert posts[post_id]["account_id"] == uge["account_id"]


# ─── Миграция: users.vk_token → vk_accounts ──────────────────────────────────

def test_migration_creates_account_from_users_token():
    """Если в users есть vk_token, init_db должен перенести его в vk_accounts."""
    legacy_tid = 9001
    legacy_token = "vk1.a." + "m" * 40

    # Напрямую вставляем в users как это было в старой схеме
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO users (telegram_id, vk_token) VALUES (?, ?)",
                     (legacy_tid, legacy_token))

    # Повторный вызов init_db запускает миграцию
    db.init_db()

    accounts = db.get_accounts(legacy_tid)
    assert len(accounts) == 1
    assert accounts[0]["name"] == "Основной"
    assert db.get_vk_token_by_account(accounts[0]["id"]) == legacy_token


def test_migration_links_existing_groups_to_new_account():
    """Группы пользователя привязываются к мигрированному аккаунту."""
    legacy_tid = 9002
    legacy_token = "vk1.a." + "g" * 40

    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO users (telegram_id, vk_token) VALUES (?, ?)",
                     (legacy_tid, legacy_token))
        conn.execute(
            "INSERT INTO vk_groups (telegram_id, vk_group_id, name) VALUES (?, ?, ?)",
            (legacy_tid, 321, "Старая группа"),
        )

    db.init_db()

    accounts = db.get_accounts(legacy_tid)
    assert accounts
    aid = accounts[0]["id"]
    groups = db.get_groups(legacy_tid)
    assert groups[0]["account_id"] == aid


def test_migration_idempotent():
    """Повторный вызов init_db не создаёт дублирующих аккаунтов."""
    legacy_tid = 9003
    legacy_token = "vk1.a." + "i" * 40

    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO users (telegram_id, vk_token) VALUES (?, ?)",
                     (legacy_tid, legacy_token))

    db.init_db()
    db.init_db()  # второй вызов

    assert len(db.get_accounts(legacy_tid)) == 1


def test_migration_scheduled_posts_get_account_id(user_group_source):
    """Существующие scheduled_posts без account_id получают его при миграции."""
    uge = user_group_source
    rid = db.add_rule(uge["telegram_id"], uge["source_id"], uge["group_id"])

    # Вставляем пост без account_id (как это было до рефакторинга)
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO scheduled_posts "
            "(telegram_id, rule_id, tt_video_id, url, title, description, "
            " vk_group_id, vk_group_name, publish_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (uge["telegram_id"], rid, "legacy_v", "https://x", "t", None,
             555, "g", 1000),
        )

    db.init_db()

    posts = db.get_scheduled_posts()
    assert posts[0]["account_id"] == uge["account_id"]
