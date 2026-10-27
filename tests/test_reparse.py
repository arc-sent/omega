"""/reparse: прогон по всем источникам-аккаунтам с прогрессом; ошибка одного не мешает другим."""

import asyncio

import account_source
import db
import scheduler


def test_refresh_reports_every_source(user_group_source, tmp_path, monkeypatch):
    tg = user_group_source["telegram_id"]
    for nick in ("good", "bad", "good2"):
        db.add_source(tg, nick, str(tmp_path / f"{nick}.db"), None, kind="account", account=nick)

    def fake_refresh(account, db_path, limit, start, platform):
        if account == "bad":
            raise RuntimeError("профиль не открылся")
        return {"username": account, "added": 2, "total": 5, "source": "yt-dlp"}

    monkeypatch.setattr(account_source, "refresh_account", fake_refresh)
    seen = []

    async def on_result(s, i, total, res, exc):
        assert scheduler.is_refreshing_sources()   # пока идёт — второй прогон не начнут
        seen.append((s["account"], i, total, res and res["added"], exc is not None))

    ok = asyncio.run(scheduler.refresh_account_sources(None, on_result=on_result))
    assert ok == 2
    assert seen == [("good", 1, 3, 2, False), ("bad", 2, 3, None, True), ("good2", 3, 3, 2, False)]
    assert not scheduler.is_refreshing_sources()
    assert any("профиль не открылся" in (e["message"] or "") for e in db.get_errors(tg))
