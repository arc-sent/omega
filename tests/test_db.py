"""Тесты слоя БД: правила, дедуп, очередь, дневная квота и синхронизация описания."""

import time

import db


def _add_rule(uge, **kw):
    return db.add_rule(uge["telegram_id"], uge["source_id"], uge["group_id"], **kw)


def test_add_and_get_rule_carries_description(user_group_source):
    rid = _add_rule(user_group_source, description="привет", videos_per_day=5, slots="9,15")
    r = db.get_rule(rid)
    assert r["description"] == "привет"
    assert r["videos_per_day"] == 5
    assert r["slots"] == "9,15"
    # get_enabled_rules тоже должен отдавать описание (его читает планировщик).
    enabled = {row["id"]: row for row in db.get_enabled_rules()}
    assert enabled[rid]["description"] == "привет"


def test_update_rule_whitelist(user_group_source):
    rid = _add_rule(user_group_source)
    db.update_rule(rid, description="x", videos_per_day=7)
    r = db.get_rule(rid)
    assert r["description"] == "x"
    assert r["videos_per_day"] == 7
    import pytest

    with pytest.raises(ValueError):
        db.update_rule(rid, telegram_id=999)  # не в белом списке


def _add_post(uge, rid, video_id, *, description, publish_at):
    return db.add_scheduled_post(
        telegram_id=uge["telegram_id"], rule_id=rid, tt_video_id=video_id,
        url=f"https://x/{video_id}", title="t", description=description,
        vk_group_id=555, vk_group_name="g", publish_at=publish_at,
    )


def test_scheduled_post_dedup(user_group_source):
    rid = _add_rule(user_group_source)
    first = _add_post(user_group_source, rid, "v1", description=None, publish_at=100)
    dup = _add_post(user_group_source, rid, "v1", description=None, publish_at=200)
    assert first is not None
    assert dup is None  # (rule_id, tt_video_id) уникальны — дубль не создаётся
    assert db.get_scheduled_ids(rid) == {"v1"}


def test_update_scheduled_posts_description_is_the_fix(user_group_source):
    """Регрессия: описание, заданное ПОСЛЕ планирования, применяется к очереди."""
    rid = _add_rule(user_group_source)  # правило без описания
    _add_post(user_group_source, rid, "v1", description=None, publish_at=100)
    _add_post(user_group_source, rid, "v2", description=None, publish_at=200)

    # До фикса эти посты ушли бы в VK с пустым описанием.
    posts = {p["tt_video_id"]: p for p in db.get_scheduled_posts()}
    assert posts["v1"]["description"] is None

    updated = db.update_scheduled_posts_description(rid, "новое описание")
    assert updated == 2
    posts = {p["tt_video_id"]: p for p in db.get_scheduled_posts()}
    assert posts["v1"]["description"] == "новое описание"
    assert posts["v2"]["description"] == "новое описание"


def test_update_scheduled_posts_description_only_target_rule(user_group_source):
    rid1 = _add_rule(user_group_source)
    # второе правило (другая группа), чтобы проверить изоляцию
    db.add_group(user_group_source["telegram_id"], vk_group_id=777, name="Группа 2")
    gid2 = [g for g in db.get_groups(user_group_source["telegram_id"]) if g["vk_group_id"] == 777][0]["id"]
    rid2 = db.add_rule(user_group_source["telegram_id"], user_group_source["source_id"], gid2)

    _add_post(user_group_source, rid1, "v1", description=None, publish_at=100)
    _add_post(user_group_source, rid2, "v1", description="чужое", publish_at=100)

    db.update_scheduled_posts_description(rid1, "мое")
    posts = {(p["rule_id"], p["tt_video_id"]): p for p in db.get_scheduled_posts()}
    assert posts[(rid1, "v1")]["description"] == "мое"
    assert posts[(rid2, "v1")]["description"] == "чужое"  # не задет


def test_daily_quota_counts(user_group_source):
    rid = _add_rule(user_group_source)
    day_start = 1_000_000
    day_end = day_start + 86400
    _add_post(user_group_source, rid, "v1", description=None, publish_at=day_start + 10)
    _add_post(user_group_source, rid, "v2", description=None, publish_at=day_end + 10)  # завтра
    assert db.count_scheduled_between(rid, day_start, day_end) == 1

    db.mark_published(rid, "vp")
    # published_at = now(); проверим широкий интервал
    now = int(time.time())
    assert db.count_published_between(rid, now - 100, now + 100) == 1
    assert db.count_published(rid) == 1


def test_publish_dedup(user_group_source):
    rid = _add_rule(user_group_source)
    db.mark_published(rid, "v1")
    db.mark_published(rid, "v1")  # повтор игнорируется
    assert db.get_published_ids(rid) == {"v1"}
    assert db.count_published(rid) == 1


def test_delete_rule_cascades_scheduled(user_group_source):
    rid = _add_rule(user_group_source)
    _add_post(user_group_source, rid, "v1", description=None, publish_at=100)
    db.delete_rule(rid)
    assert db.get_scheduled_posts() == []
