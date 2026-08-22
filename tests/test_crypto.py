"""Тесты прозрачного шифрования токенов и его интеграции с db."""

import crypto
import db


def test_roundtrip():
    enc = crypto.encrypt("vk1.a.SECRET-TOKEN")
    assert crypto.is_encrypted(enc)
    assert enc != "vk1.a.SECRET-TOKEN"
    assert crypto.decrypt(enc) == "vk1.a.SECRET-TOKEN"


def test_legacy_plaintext_passthrough():
    assert crypto.decrypt("vk1.a.OLD-PLAINTEXT") == "vk1.a.OLD-PLAINTEXT"


def test_encrypt_idempotent():
    enc = crypto.encrypt("tok")
    assert crypto.encrypt(enc) == enc


def test_validate_key_rejects_garbage():
    import pytest
    with pytest.raises(RuntimeError):
        crypto._validate_key("not-a-real-fernet-key")


def test_validate_key_accepts_generated():
    from cryptography.fernet import Fernet
    crypto._validate_key(Fernet.generate_key().decode())


def test_empty_values():
    assert crypto.encrypt("") == ""
    assert crypto.encrypt(None) is None
    assert crypto.decrypt(None) is None


def test_db_set_get_roundtrip():
    """Токен, сохранённый через add_account, читается через get_vk_token_by_account."""
    db.ensure_user(42)
    aid = db.add_account(42, "Test", "vk1.a.MYTOKEN" + "x" * 30)
    assert db.get_vk_token_by_account(aid) == "vk1.a.MYTOKEN" + "x" * 30


def test_db_stores_encrypted_not_plaintext():
    """В vk_accounts токен хранится в зашифрованном виде."""
    db.ensure_user(43)
    token = "vk1.a.PLAINTEXT-SHOULD-NOT-APPEAR" + "y" * 10
    aid = db.add_account(43, "Test", token)
    with db._connect() as conn:
        raw = conn.execute(
            "SELECT vk_token FROM vk_accounts WHERE id = ?", (aid,)
        ).fetchone()["vk_token"]
    assert crypto.is_encrypted(raw)
    assert "PLAINTEXT-SHOULD-NOT-APPEAR" not in raw


def test_init_db_migrates_plaintext():
    """init_db переносит незашифрованный users.vk_token в vk_accounts и шифрует."""
    db.ensure_user(44)
    with db._connect() as conn:
        conn.execute("UPDATE users SET vk_token = ? WHERE telegram_id = 44",
                     ("vk1.a.LEGACY-PLAIN" + "z" * 20,))
    db.init_db()
    accounts = db.get_accounts(44)
    assert accounts
    assert db.get_vk_token_by_account(accounts[0]["id"]) == "vk1.a.LEGACY-PLAIN" + "z" * 20
    with db._connect() as conn:
        raw = conn.execute(
            "SELECT vk_token FROM vk_accounts WHERE id = ?", (accounts[0]["id"],)
        ).fetchone()["vk_token"]
    assert crypto.is_encrypted(raw)
    assert "LEGACY-PLAIN" not in raw
