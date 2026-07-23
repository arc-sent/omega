"""Тесты планировщика: раскладка по слотам и plan_rule (без сети)."""

from datetime import datetime, timedelta

import pytz

import scheduler

MOSCOW_TZ = pytz.timezone("Europe/Moscow")


def test_parse_slots():
    assert scheduler.parse_slots("9,15,20") == [9, 15, 20]
    assert scheduler.parse_slots("20,9,9,15") == [9, 15, 20]  # сорт + дедуп
    assert scheduler.parse_slots("25, -1, abc, 12") == [12]  # мусор и вне 0..23 отброшены
    assert scheduler.parse_slots("") == []


def test_base_slot_times_future_today():
    now = MOSCOW_TZ.localize(datetime(2026, 7, 23, 10, 0))
    times = scheduler._base_slot_times([9, 15, 20], now)
    # 9 уже прошёл, остаются 15 и 20 сегодня
    assert [t.hour for t in times] == [15, 20]
    assert all(t.date() == now.date() for t in times)


def test_base_slot_times_rolls_to_tomorrow():
    now = MOSCOW_TZ.localize(datetime(2026, 7, 23, 22, 0))
    times = scheduler._base_slot_times([9, 15, 20], now)
    # все сегодняшние слоты прошли -> завтрашние
    assert [t.hour for t in times] == [9, 15, 20]
    assert all(t.date() == now.date() + timedelta(days=1) for t in times)


def test_compute_publish_times_wraps_over_slots():
    now = MOSCOW_TZ.localize(datetime(2026, 7, 23, 0, 0))
    times = scheduler.compute_publish_times([9, 15], 3, now)
    assert len(times) == 3
    assert [t.hour for t in times] == [9, 15, 9]  # по кругу


def test_compute_publish_times_empty():
    now = MOSCOW_TZ.localize(datetime(2026, 7, 23, 0, 0))
    assert scheduler.compute_publish_times([], 3, now) == []
    assert scheduler.compute_publish_times([9], 0, now) == []
