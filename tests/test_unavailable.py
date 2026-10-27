"""Недоступные ролики (удалены/скрыты): помечаются после сбоя и больше не берутся в план."""

import asyncio
import os

import pytest

import db
import downloader
import scheduler

from conftest import FakeJobQueue
from test_plan_rule import _make_rule_with_source

BLOCKED = ("ERROR: [TikTok] 7520139243537141010: "
           "Your IP address is blocked from accessing this post")


@pytest.mark.parametrize("message, expected", [
    ("ERROR: [youtube] abc: Video unavailable. This video has been removed", "Video unavailable"),
    ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access", "Private video"),
    ("[TikTok] 1: You do not have permission to view this post. Log into an account",
     "You do not have permission to view this post"),
    ("Unable to download webpage: Read timed out", None),       # сеть — не навсегда
    ("Видео слишком длинное — 5:00. Максимум 3:00.", None),     # лимит могут поднять
])
def test_unavailable_reason(message, expected):
    assert downloader.unavailable_reason(RuntimeError(message)) == expected


def test_tiktok_ip_block_is_final_only_after_proxy(monkeypatch):
    monkeypatch.setattr(downloader, "DOWNLOAD_PROXY", None)
    assert downloader.unavailable_reason(RuntimeError(BLOCKED)) is None   # прокси мог бы помочь
    monkeypatch.setattr(downloader, "DOWNLOAD_PROXY", "socks5h://xray:1080")
    assert downloader.unavailable_reason(RuntimeError(BLOCKED))


def test_plan_rule_skips_unavailable_videos(user_group_source, tmp_path):
    rid = _make_rule_with_source(user_group_source, str(tmp_path), n_videos=3, videos_per_day=2)
    db.mark_unavailable("v1", "удалён")   # самый старый — правило взяло бы его первым
    scheduler.plan_rule(FakeJobQueue(), db.get_rule(rid))
    assert {p["tt_video_id"] for p in db.get_scheduled_posts()} == {"v2", "v3"}


def test_unavailable_is_shared_between_rules_and_idempotent():
    db.mark_unavailable("v9", "удалён")
    db.mark_unavailable("v9", "скрыт")
    assert db.get_unavailable_ids() == {"v9"}


# ─── Замена недоступного ролика следующим ─────────────────────────────────────

class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)


class _Ctx:
    def __init__(self, post_id):
        self.job = type("J", (), {"data": {"scheduled_post_id": post_id}})()
        self.job_queue = FakeJobQueue()
        self.bot = _Bot()


def test_replacement_takes_next_video(user_group_source, tmp_path):
    rid = _make_rule_with_source(user_group_source, str(tmp_path), n_videos=3)
    db.mark_unavailable("v1", "удалён")
    jq = FakeJobQueue()
    assert scheduler.schedule_replacement(jq, rid) == "https://tiktok/v2"
    assert [p["tt_video_id"] for p in db.get_scheduled_posts()] == ["v2"]
    assert len(jq.jobs) == 1


def test_no_replacement_for_disabled_rule(user_group_source, tmp_path):
    rid = _make_rule_with_source(user_group_source, str(tmp_path), n_videos=3)
    db.update_rule(rid, enabled=0)
    assert scheduler.schedule_replacement(FakeJobQueue(), rid) is None


def _queue_first_video(rid):
    rule = db.get_rule(rid)
    return db.add_scheduled_post(
        telegram_id=rule["telegram_id"], rule_id=rid, tt_video_id="v1",
        url="https://tiktok/v1", title="t", description=None,
        vk_group_id=rule["vk_group_id"], vk_group_name=rule["group_name"],
        publish_at=0, account_id=rule["account_id"],
    )


def test_dead_video_is_replaced_by_next(user_group_source, tmp_path, monkeypatch):
    rid = _make_rule_with_source(user_group_source, str(tmp_path), n_videos=3)
    post_id = _queue_first_video(rid)
    monkeypatch.setattr(downloader, "DOWNLOAD_PROXY", "socks5h://xray:1080")

    async def dead(url, token):
        raise RuntimeError(BLOCKED)

    monkeypatch.setattr(scheduler, "_download", dead)
    ctx = _Ctx(post_id)
    asyncio.run(scheduler._publish_job(ctx))

    assert db.get_unavailable_ids() == {"v1"}
    assert [p["tt_video_id"] for p in db.get_scheduled_posts()] == ["v2"]   # v1 убран, v2 встал
    assert len(ctx.job_queue.jobs) == 1
    assert "https://tiktok/v2" in ctx.bot.sent[-1]


def test_network_error_is_not_replaced(user_group_source, tmp_path, monkeypatch):
    rid = _make_rule_with_source(user_group_source, str(tmp_path), n_videos=3)
    post_id = _queue_first_video(rid)

    async def flaky(url, token):
        raise RuntimeError("Unable to download webpage: Read timed out")

    monkeypatch.setattr(scheduler, "_download", flaky)
    ctx = _Ctx(post_id)
    asyncio.run(scheduler._publish_job(ctx))

    assert not db.get_unavailable_ids()
    assert not db.get_scheduled_posts() and not ctx.job_queue.jobs
