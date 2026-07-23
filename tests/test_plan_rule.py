"""Сквозной тест реального бага: описание, заданное после планирования.

Воспроизводит сценарий из UI (bot.py): правило создаётся БЕЗ описания и сразу
планируется на сегодня -> посты в очереди с пустым описанием. Затем пользователь
задаёт описание. Проверяем, что синхронизация применяет его к очереди.
"""

import os
from datetime import datetime

import pytest
import pytz

import db
import scheduler

from conftest import make_source_db, FakeJobQueue

MOSCOW_TZ = pytz.timezone("Europe/Moscow")


class _FixedDatetime(datetime):
    """datetime с зафиксированным now() — чтобы слоты гарантированно были сегодня."""

    @classmethod
    def now(cls, tz=None):
        return MOSCOW_TZ.localize(datetime(2026, 7, 23, 0, 30))


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    # 00:30 МСК: все слоты 1,5,9,13,17,21 — ещё впереди сегодня (детерминизм).
    monkeypatch.setattr(scheduler, "datetime", _FixedDatetime)


def _make_rule_with_source(uge, tmp_path, n_videos=5, videos_per_day=2):
    src_db = os.path.join(tmp_path, "source.db")
    make_source_db(src_db, [
        (f"v{i}", "nick", f"https://tiktok/v{i}", f"title {i}", 30, f"2026-01-0{i}")
        for i in range(1, n_videos + 1)
    ])
    source_id = db.add_source(uge["telegram_id"], "Src", src_db, None)
    rid = db.add_rule(
        uge["telegram_id"], source_id, uge["group_id"],
        videos_per_day=videos_per_day, slots="1,5,9,13,17,21",  # ничего не задаём в description
    )
    return rid


def test_plan_then_set_description_syncs_queue(user_group_source, tmp_path):
    rid = _make_rule_with_source(user_group_source, str(tmp_path))
    jq = FakeJobQueue()

    scheduled = scheduler.plan_rule(jq, db.get_rule(rid))
    assert scheduled >= 1
    assert len(jq.jobs) == scheduled  # каждая публикация поставлена в job_queue

    # Баг: посты уже в очереди с пустым описанием.
    posts = db.get_scheduled_posts()
    assert posts, "должны быть запланированные посты"
    assert all(p["description"] is None for p in posts)

    # Пользователь задаёт описание уже ПОСЛЕ планирования (как в UI).
    db.update_rule(rid, description="итоговое описание")
    synced = db.update_scheduled_posts_description(rid, "итоговое описание")

    assert synced == len(posts)
    posts = db.get_scheduled_posts()
    assert all(p["description"] == "итоговое описание" for p in posts)


def test_plan_rule_respects_daily_quota(user_group_source, tmp_path):
    """Повторный вызов plan_rule не задваивает дневную квоту."""
    rid = _make_rule_with_source(user_group_source, str(tmp_path), n_videos=10, videos_per_day=2)
    jq = FakeJobQueue()

    first = scheduler.plan_rule(jq, db.get_rule(rid))
    second = scheduler.plan_rule(jq, db.get_rule(rid))
    # Второй прогон в тот же день не добирает сверх уже поставленного на сегодня.
    assert first >= 1
    assert first + second <= 2  # videos_per_day
