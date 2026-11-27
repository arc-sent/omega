"""Метрика групп: сбор снимка (VK замокан), прирост, отчёт, дневные суммы."""

import re

import pytest

import db
import metrics
from vk import VKError

TOKEN = "vk1.a.fake_token_for_tests_x" + "a" * 30
DAY = 86400
NOW = 1_760_000_000


@pytest.fixture(autouse=True)
def _no_managed_groups(monkeypatch):
    """По умолчанию groups.get ничего не отдаёт — тесты не ходят в сеть."""
    monkeypatch.setattr(metrics, "fetch_managed_groups", lambda token: {})


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
    assert res == {"saved": 1, "total": 2, "failed": [20], "accounts_failed": []}
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
    # титул + сводка по аккаунтам + 1 страница таблицы + 2 страницы мини-графиков (12 по 9)
    assert len(re.findall(rb"/Type\s*/Page(?!s)", pdf)) == 5


def _account(tid, name, groups):
    aid = db.add_account(tid, name, TOKEN)
    for gid, gname in groups:
        db.add_group(tid, vk_group_id=gid, name=gname, account_id=aid)
    return aid


def test_report_split_by_accounts():
    """Несколько аккаунтов (у одного и у разных юзеров) — отдельные разделы с итогами."""
    db.ensure_user(1)
    db.ensure_user(2)
    _account(1, "Первый", [(10, "A"), (20, "B")])
    _account(1, "Второй", [(30, "C")])
    _account(2, "Чужой", [(40, "D")])
    db.add_group_metrics([(10, 100, ""), (20, 50, ""), (30, 7, ""), (40, 1000, "")],
                         captured_at=NOW - 2 * DAY)
    db.add_group_metrics([(10, 110, ""), (20, 55, ""), (30, 9, ""), (40, 1000, "")],
                         captured_at=NOW)
    data = metrics.report_data(NOW)
    accs = {a["name"]: a for a in data["accounts"]}
    assert set(accs) == {"Первый · tg 1", "Второй · tg 1", "Чужой · tg 2"}
    assert accs["Первый · tg 1"]["total"] == 165
    assert accs["Первый · tg 1"]["totals"][1] == (15, False)
    assert [r["gid"] for r in accs["Первый · tg 1"]["rows"]] == [10, 20]
    assert accs["Второй · tg 1"]["total"] == 9
    assert data["total"] == 1174
    text = "\n".join(metrics.build_report(NOW))
    assert "Аккаунтов: 3" in text
    assert text.index("Первый") < text.index("Второй") < text.index("Чужой")


def test_build_pdf_has_pages_per_account():
    pytest.importorskip("matplotlib")
    db.ensure_user(1)
    _account(1, "Первый", [(10, "A")])
    _account(1, "Второй", [(20, "B")])
    for d in (1, 0):
        db.add_group_metrics([(10, 100 + d, ""), (20, 200 + d, "")], captured_at=NOW - d * DAY)
    pdf = metrics.build_pdf(NOW)
    # титул + сводка + (таблица + мини-графики) × 2 аккаунта
    assert len(re.findall(rb"/Type\s*/Page(?!s)", pdf)) == 6


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


# ─── Все группы под управлением аккаунта (groups.get) ─────────────────────────

def test_fetch_managed_groups_pages_and_parses(monkeypatch):
    import vk

    pages = [
        {"response": {"count": 3, "items": [{"id": 1, "members_count": 10, "name": "a"},
                                            {"id": 2, "members_count": 20, "name": "b"}]}},
        {"response": {"count": 3, "items": [{"id": 3, "name": "c"}]}},  # без members_count
    ]
    seen = []

    class R:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def fake_post(url, data=None, **kw):
        seen.append((url, dict(data)))
        return R(pages[len(seen) - 1])

    monkeypatch.setattr(vk.requests, "post", fake_post)
    assert vk.fetch_managed_groups("vk1.a.secret") == {1: (10, "a"), 2: (20, "b"), 3: (None, "c")}
    assert [d["offset"] for _, d in seen] == [0, 2]
    assert seen[0][0].endswith("/groups.get") and seen[0][1]["filter"] == "admin,editor,moder"
    assert all("secret" not in url for url, _ in seen)


def test_snapshot_includes_managed_groups(monkeypatch):
    """groups.get даёт и список, и подписчиков: группы из бота, уже пришедшие так,
    повторно через getById не запрашиваются."""
    aid = _user(1, [(10, "A")])
    monkeypatch.setattr(metrics, "fetch_managed_groups",
                        lambda token: {10: (100, "A vk"), 77: (7000, "Чужая")})
    calls = []
    monkeypatch.setattr(metrics, "fetch_groups_members",
                        lambda token, ids: calls.append(ids) or {})
    res = metrics.collect_snapshot()
    assert res["saved"] == 2 and res["total"] == 2 and res["failed"] == []
    assert calls == []
    assert [r["vk_group_id"] for r in db.get_managed_groups_for_metrics()] == [10, 77]
    assert db.get_managed_groups_for_metrics()[0]["account_id"] == aid
    assert [r["members_count"] for r in db.get_group_metrics(77)] == [7000]


def test_managed_groups_failure_keeps_old_list(monkeypatch):
    aid = _user(1, [(10, "A")])
    db.set_managed_groups(aid, [(77, "Чужая")])

    def boom(token):
        raise VKError(15, "Access denied: no access to call this method")

    monkeypatch.setattr(metrics, "fetch_managed_groups", boom)
    monkeypatch.setattr(metrics, "fetch_groups_members", lambda token, ids: {g: (5, "") for g in ids})
    res = metrics.collect_snapshot()
    assert res["accounts_failed"] == [aid]
    assert [r["vk_group_id"] for r in db.get_managed_groups_for_metrics()] == [77]
    assert res["saved"] == 2  # группы бота и прежнего списка сняты через getById


def test_report_scope_bot_vs_all():
    aid = _user(1, [(10, "Из бота")])
    db.set_managed_groups(aid, [(10, "Из бота vk"), (77, "Только под управлением")])
    db.add_group_metrics([(10, 100, ""), (77, 5000, "")], captured_at=NOW)

    bot_data = metrics.report_data(NOW, metrics.SCOPE_BOT)
    assert [r["gid"] for r in bot_data["rows"]] == [10] and bot_data["total"] == 100

    all_data = metrics.report_data(NOW, metrics.SCOPE_ALL)
    assert [r["gid"] for r in all_data["rows"]] == [77, 10] and all_data["total"] == 5100
    assert {r["gid"]: r["in_bot"] for r in all_data["rows"]} == {77: False, 10: True}
    (acc,) = all_data["accounts"]
    assert acc["groups"] == 2 and acc["total"] == 5100
    assert all_data["names"][10] == "Из бота"  # имя из бота приоритетнее имени из VK

    text = "\n".join(metrics.build_report(NOW, metrics.SCOPE_ALL))
    assert "все группы аккаунтов" in text and "Только под управлением (id 77) · вне бота" in text
    assert "вне бота" not in "\n".join(metrics.build_report(NOW, metrics.SCOPE_BOT))


def test_build_pdf_all_scope():
    pytest.importorskip("matplotlib")
    aid = _user(1, [(10, "A")])
    db.set_managed_groups(aid, [(77, "B")])
    for d in (1, 0):
        db.add_group_metrics([(10, 100 + d, ""), (77, 200 + d, "")], captured_at=NOW - d * DAY)
    pdf = metrics.build_pdf(NOW, scope=metrics.SCOPE_ALL)
    assert pdf.startswith(b"%PDF")
    assert metrics.pdf_filename(NOW, metrics.SCOPE_ALL).startswith("metrics_all_")
