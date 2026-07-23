"""Тесты прозрачного шифрования токенов и его интеграции с db."""

import crypto
import db


def test_roundtrip():
    enc = crypto.encrypt("vk1.a.SECRET-TOKEN")
    assert crypto.is_encrypted(enc)
    assert enc != "vk1.a.SECRET-TOKEN"  # в хранимом виде токена не видно
    assert crypto.decrypt(enc) == "vk1.a.SECRET-TOKEN"


def test_legacy_plaintext_passthrough():
    # Старый незашифрованный токен (без префикса) читается как есть.
    assert crypto.decrypt("vk1.a.OLD-PLAINTEXT") == "vk1.a.OLD-PLAINTEXT"


def test_encrypt_idempotent():
    enc = crypto.encrypt("tok")
    assert crypto.encrypt(enc) == enc  # уже зашифрованное не шифруется повторно


def test_empty_values():
    assert crypto.encrypt("") == ""
    assert crypto.encrypt(None) is None
    assert crypto.decrypt(None) is None


def test_db_set_get_roundtrip():
    db.ensure_user(42)
    db.set_vk_token(42, "vk1.a.MYTOKEN")
    assert db.get_vk_token(42) == "vk1.a.MYTOKEN"  # снаружи API не изменилось


def test_db_stores_encrypted_not_plaintext():
    db.ensure_user(43)
    db.set_vk_token(43, "vk1.a.PLAINTEXT-SHOULD-NOT-APPEAR")
    # Читаем сырое значение из БД мимо get_vk_token — оно должно быть зашифровано.
    with db._connect() as conn:
        raw = conn.execute(
            "SELECT vk_token FROM users WHERE telegram_id = 43"
        ).fetchone()["vk_token"]
    assert crypto.is_encrypted(raw)
    assert "PLAINTEXT-SHOULD-NOT-APPEAR" not in raw


def test_init_db_migrates_plaintext(monkeypatch):
    # Кладём токен «как раньше» (открытым текстом), затем init_db должен его зашифровать.
    db.ensure_user(44)
    with db._connect() as conn:
        conn.execute("UPDATE users SET vk_token = ? WHERE telegram_id = 44",
                     ("vk1.a.LEGACY-PLAIN",))
    db.init_db()  # повторный вызов — идемпотентная миграция
    with db._connect() as conn:
        raw = conn.execute(
            "SELECT vk_token FROM users WHERE telegram_id = 44"
        ).fetchone()["vk_token"]
    assert crypto.is_encrypted(raw)
    assert db.get_vk_token(44) == "vk1.a.LEGACY-PLAIN"
