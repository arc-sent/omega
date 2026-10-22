"""Метрика групп: сбор снимка (VK замокан), прирост, отчёт, дневные суммы."""

import re

import pytest

import db
import metrics
from vk import VKError

TOKEN = "vk1.a.fake_token_for_tests_x" + "a" * 30
DAY = 86400
NOW = 1_760_000_000


def _user(tid, groups):
    db.ensure_user(tid)
    aid = db.add_account(tid, "Акк", TOKEN)
    for gid, name in groups:
        db.add_group(tid, vk_group_id=gid, name=name, account_id=aid)
    return aid


def test_collect_snapshot_saves_counts(monkeypatch):
    _user(1, [(10, "A"), (20, "B")])
    calls = []

    def fake(token, ids):
        calls.append(sorted(ids))
        return {10: (100, "A vk"), 20: (None, "B vk")}  # у B VK не отдал число

    monkeypatch.setattr(metrics, "fetch_groups_members", fake)
    res = metrics.collect_snapshot()
    assert res == {"saved": 1, "total": 2, "failed": [20]}
    assert calls == [[10, 20]]
    assert [r["members_count"] for r in db.get_group_metrics(10)] == [100]
    assert db.get_group_metrics(20) == []


def test_shared_group_queried_once_and_falls_back_to_other_token(monkeypatch):
    """Группа у двух юзеров: запрашивается один раз; упал токен — пробуем второй."""
    _user(1, [(10, "A")])
    _user(2, [(10, "A")])
    used = []

    def fake(token, ids):
        used.append(token)
        if len(used) == 1:
            raise VKError(5, "invalid token")
        return {10: (55, "A")}

    monkeypatch.setattr(metrics, "fetch_groups_members", fake)
    res = metrics.collect_snapshot()
    assert res["saved"] == 1 and res["total"] == 1
    assert len(used) == 2
    assert len(db.get_group_metrics(10)) == 1


def test_delta_uses_snapshot_before_cutoff():
    _user(1, [(10, "A")])
    db.add_group_metrics([(10, 100, "A")], captured_at=NOW - 8 * DAY)
    db.add_group_metrics([(10, 130, "A")], captured_at=NOW - DAY - 60)
    db.add_group_metrics([(10, 150, "A")], captured_at=NOW)
    assert metrics._delta(10, 150, NOW, 1) == (20, False)
    assert metrics._delta(10, 150, NOW, 7) == (50, False)
    # 30 дней назад снимков нет — от первого, помечено как приблизительное
    assert metrics._delta(10, 150, NOW, 30) == (50, True)


def test_delta_none_when_tracking_just_started():
    _user(1, [(10, "A")])
    db.add_group_metrics([(10, 100, "A")], captured_at=NOW - 60)
    assert metrics._delta(10, 100, NOW, 1) == (None, False)


def test_report_lists_groups_sorted_and_missing():
    _user(1, [(10, "Маленькая"), (20, "Большая"), (30, "Без данных")])
    db.add_group_metrics([(10, 5, "M"), (20, 900, "B")], captured_at=NOW)
    text = "\n".join(metrics.build_report(NOW))
    assert "подписчиков всего: 905" in text
    assert text.index("Большая") < text.index("Маленькая")
    assert "Нет данных" in text and "Без данных" in text


def test_report_without_groups():
    assert metrics.build_report(NOW) == ["Пока ни у одного пользователя нет групп."]


def test_daily_totals_forward_fill():
    _user(1, [(10, "A"), (20, "B")])
    db.add_group_metrics([(10, 100, "A"), (20, 10, "B")], captured_at=NOW - 2 * DAY)
    db.add_group_metrics([(10, 120, "A")], captured_at=NOW)  # у B свежего снимка нет
    totals = [v for _, v in metrics.daily_totals(3, NOW)]
    assert totals == [110, 110, 130]


def test_split_messages_respects_limit():
    parts = metrics.split_messages(["x" * 30] * 10, limit=100)
    assert all(len(p) <= 100 for p in parts)
    assert sum(p.count("x") for p in parts) == 300


@pytest.mark.parametrize("payload, expected", [
    ({"response": {"groups": [{"id": 1, "members_count": 7, "name": "G"}]}}, {1: (7, "G")}),
    ({"response": [{"id": 1, "members_count": 7, "name": "G"}]}, {1: (7, "G")}),
])
def test_fetch_groups_members_parses_both_formats(monkeypatch, payload, expected):
    import vk

    class R:
        def json(self):
            return payload

    monkeypatch.setattr(vk.requests, "post", lambda *a, **k: R())
    assert vk.fetch_groups_members("t", [-1]) == expected


def test_fetch_groups_members_raises_on_api_error(monkeypatch):
    import vk

    class R:
        def json(self):
            return {"error": {"error_code": 5, "error_msg": "auth"}}

    monkeypatch.setattr(vk.requests, "post", lambda *a, **k: R())
    with pytest.raises(VKError):
        vk.fetch_groups_members("t", [1])


def test_daily_series_growth_ignores_newly_added_groups():
    """Новая группа поднимает сумму, но не попадает в прирост за день."""
    _user(1, [(10, "A"), (20, "B")])
    db.add_group_metrics([(10, 100, "A")], captured_at=NOW - DAY)
    db.add_group_metrics([(10, 105, "A"), (20, 5000, "B")], captured_at=NOW)
    series = metrics.daily_series(2, NOW)
    assert [(t, g) for _, t, g in series] == [(100, 0), (5105, 5)]


def test_build_pdf_renders_all_pages():
    pytest.importorskip("matplotlib")
    _user(1, [(10 + i, f"Группа {i}") for i in range(12)])
    for d in range(5, -1, -1):
        db.add_group_metrics([(10 + i, 100 * i + d, "") for i in range(12)],
                             captured_at=NOW - d * DAY)
    pdf = metrics.build_pdf(NOW)
    assert pdf.startswith(b"%PDF")
    # титул + 1 страница таблицы + 2 страницы мини-графиков (12 групп по 9)
    assert len(re.findall(rb"/Type\s*/Page(?!s)", pdf)) == 4


def test_build_pdf_none_without_groups():
    assert metrics.build_pdf(NOW) is None


def test_fetch_groups_members_keeps_token_out_of_url(monkeypatch):
    """Токен — в теле POST, не в URL: иначе при сетевой ошибке он попал бы в логи."""
    import vk

    seen = {}

    class R:
        def json(self):
            return {"response": {"groups": [{"id": 1, "members_count": 5, "name": "g"}]}}

    def fake_post(url, data=None, params=None, **kw):
        seen.update(url=url, data=data, params=params)
        return R()

    monkeypatch.setattr(vk.requests, "post", fake_post)
    assert vk.fetch_groups_members("vk1.a.secret", [1]) == {1: (5, "g")}
    assert seen["data"]["access_token"] == "vk1.a.secret"
    assert "secret" not in seen["url"] and not seen["params"]
