"""PARSE_LIMIT: по умолчанию парсится весь аккаунт."""

import pytest

import account_source


@pytest.mark.parametrize("value, expected", [
    (None, account_source.UNLIMITED),
    ("", account_source.UNLIMITED),
    ("0", account_source.UNLIMITED),
    ("350", 350),
])
def test_parse_limit_from_env(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("PARSE_LIMIT", raising=False)
    else:
        monkeypatch.setenv("PARSE_LIMIT", value)
    assert account_source._parse_limit_from_env() == expected
