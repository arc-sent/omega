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


def test_publish_window_within_today():
    now = MOSCOW_TZ.localize(datetime(2026, 7, 23, 10, 0))
    start, end = scheduler._publish_window(now)
    assert start.date() == now.date() and end.date() == now.date()
    assert start.hour == 10 and start.minute == 2  # окно уже началось -> от текущего момента
    assert end.hour == scheduler.PUBLISH_WINDOW_END_HOUR


def test_publish_window_before_start_uses_window_start():
    now = MOSCOW_TZ.localize(datetime(2026, 7, 23, 3, 0))
    start, end = scheduler._publish_window(now)
    assert start.hour == scheduler.PUBLISH_WINDOW_START_HOUR
    assert start.date() == now.date()


def test_publish_window_rolls_to_tomorrow():
    now = MOSCOW_TZ.localize(datetime(2026, 7, 23, 22, 0))
    start, end = scheduler._publish_window(now)
    # окно сегодня уже кончилось -> целиком завтрашнее
    assert start.date() == now.date() + timedelta(days=1)
    assert start.hour == scheduler.PUBLISH_WINDOW_START_HOUR
    assert end.hour == scheduler.PUBLISH_WINDOW_END_HOUR


def test_compute_publish_times_spread_evenly_in_window():
    now = MOSCOW_TZ.localize(datetime(2026, 7, 23, 0, 0))
    times = scheduler.compute_publish_times(4, 0, 4, now)
    assert len(times) == 4
    # монотонно возрастают и не выходят за пределы окна
    start, end = scheduler._publish_window(now)
    assert all(start <= t < end for t in times)
    assert times == sorted(times)
    # ни одна пара не кучкуется в одной узкой точке — раскиданы по четвертям окна
    span = (end - start).total_seconds()
    step = span / 4
    for i, t in enumerate(times):
        offset = (t - start).total_seconds()
        assert i * step <= offset < (i + 1) * step


def test_compute_publish_times_continues_from_already():
    """Повторный прогон (already>0) продолжает график, а не начинает заново."""
    now = MOSCOW_TZ.localize(datetime(2026, 7, 23, 0, 0))
    first = scheduler.compute_publish_times(4, 0, 2, now)
    second = scheduler.compute_publish_times(4, 2, 2, now)
    assert max(first) < min(second)


def test_compute_publish_times_empty():
    now = MOSCOW_TZ.localize(datetime(2026, 7, 23, 0, 0))
    assert scheduler.compute_publish_times(0, 0, 3, now) == []
    assert scheduler.compute_publish_times(4, 0, 0, now) == []
