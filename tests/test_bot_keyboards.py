"""Клавиатуры списков: страницы вместо упора в лимит Telegram (100 кнопок) и покрытие
callback_data шаблонами обработчиков (кнопка, которой нет в шаблоне, молча не работает).
"""

import re
import sys
import types
from pathlib import Path

import pytest

import db

_STUBBED = []
try:
    import matplotlib  # noqa: F401
except ImportError:
    # bot.py импортирует matplotlib ради графиков; для клавиатур он не нужен.
    for _name in ("matplotlib", "matplotlib.pyplot", "matplotlib.dates", "matplotlib.ticker"):
        sys.modules[_name] = types.ModuleType(_name)
        _STUBBED.append(_name)
    sys.modules["matplotlib"].use = lambda *a, **k: None
    sys.modules["matplotlib"].pyplot = sys.modules["matplotlib.pyplot"]

import bot  # noqa: E402

# Заглушки нужны только на время импорта: bot уже держит на них ссылки, а в sys.modules
# они мешали бы тестам, которые пропускаются через importorskip("matplotlib").
for _name in _STUBBED:
    sys.modules.pop(_name, None)

TID = 7001
TG_BUTTON_LIMIT = 100


def _count(kb) -> int:
    return sum(len(row) for row in kb.inline_keyboard)


def _callbacks(kb) -> list[str]:
    return [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]


@pytest.fixture
def big():
    """Пользователь с 40 аккаунтами, 12 группами, 40 источниками и 144 правилами."""
    db.ensure_user(TID)
    acc_ids = [db.add_account(TID, f"Акк {i}", "vk1.a." + "x" * 40) for i in range(40)]
    for i in range(12):
        db.add_group(TID, 1000 + i, f"Группа {i}", acc_ids[0])
    group_ids = [g["id"] for g in db.get_groups(TID, account_id=acc_ids[0])]
    src_ids = [db.add_source(TID, f"Источник {i}", f"/s{i}.db", None, account_id=acc_ids[0])
               for i in range(40)]
    for sid in src_ids[:12]:
        for gid in group_ids:
            db.add_rule(TID, sid, gid)
    return {"accounts": acc_ids, "account": acc_ids[0]}


# ─── Лимит кнопок и листание ──────────────────────────────────────────────────

def test_accounts_kb_fits_telegram_limit_and_pages(big):
    accs = big["accounts"]
    kb = bot.accounts_kb(TID, accs[0], 0)
    assert _count(kb) < TG_BUTTON_LIMIT
    assert "acc_page_1" in _callbacks(kb) and "acc_page_-1" not in _callbacks(kb)
    last = (len(accs) - 1) // bot.ACCOUNTS_PAGE_SIZE
    assert _count(bot.accounts_kb(TID, accs[0], last)) < TG_BUTTON_LIMIT
    # страница вне диапазона зажимается, а не падает
    assert _count(bot.accounts_kb(TID, accs[0], 999)) < TG_BUTTON_LIMIT


def test_accounts_kb_opens_on_page_of_current_account(big):
    accs = big["accounts"]
    current = accs[30]
    kb = bot.accounts_kb(TID, current)   # page=None → страница с текущим
    switch = [c for c in _callbacks(kb) if c.startswith("acc_switch_")]
    assert f"acc_switch_{current}" not in switch   # у активного кнопки «Перейти» нет…
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert any(t.startswith("✅") and "Акк 30" in t for t in texts)  # …но он виден с ✅


def test_sources_kb_fits_limit_and_pages(big):
    kb = bot.sources_kb(TID, big["account"])
    assert _count(kb) < TG_BUTTON_LIMIT
    assert "src_page_1" in _callbacks(kb)
    assert _count(bot.sources_kb(TID, big["account"], 4)) < TG_BUTTON_LIMIT


def test_rules_kb_fits_limit_and_pages(big):
    kb = bot.rules_kb(TID, big["account"])
    assert _count(kb) < TG_BUTTON_LIMIT
    assert "rule_page_1" in _callbacks(kb)
    pages = -(-144 // bot.RULES_PAGE_SIZE)
    assert _count(bot.rules_kb(TID, big["account"], pages - 1)) < TG_BUTTON_LIMIT


def test_rule_source_picker_pages(big):
    sources = db.get_sources(TID, for_account_id=big["account"])
    kb = bot._rule_source_picker_kb(sources)
    assert _count(kb) < TG_BUTTON_LIMIT
    assert "rule_spg_1" in _callbacks(kb)
    picked = [c for c in _callbacks(bot._rule_source_picker_kb(sources, 1))
              if c.startswith("rule_src_")]
    assert len(picked) == bot.RULES_PAGE_SIZE


def test_single_page_has_no_pager(big):
    db.ensure_user(TID + 1)
    db.add_account(TID + 1, "Один", "vk1.a." + "y" * 40)
    kb = bot.accounts_kb(TID + 1)
    assert not [c for c in _callbacks(kb) if c.startswith("acc_page_")]


# ─── Каждая кнопка листания должна попадать в шаблон обработчика ──────────────

def _registered_patterns() -> list[re.Pattern]:
    src = (Path(bot.__file__)).read_text(encoding="utf-8")
    result = []
    for m in re.findall(r'pattern=r"([^"]+)"', src):
        try:
            result.append(re.compile(m))
        except re.error:
            pass  # шаблон из нескольких склеенных литералов — выборка его режет; не наш случай
    return result


@pytest.mark.parametrize("callback", [
    "acc_page_1", "src_page_1", "rule_page_1", "rule_spg_1",
    "rule_gpg_1",   # стрелки выбора группы для правила: раньше не было в шаблоне
    "g_page_1", "t_pg_1",
])
def test_pager_callbacks_are_registered(callback):
    assert any(p.match(callback) for p in _registered_patterns()), (
        f"{callback}: ни один CallbackQueryHandler не ловит эту кнопку — она не сработает")
