"""Планировщик автопубликации.

Раз в сутки (DAILY_PLAN_HOUR:MINUTE МСК) и при старте бот раскладывает план:
для каждого включённого правила берёт N самых подходящих неопубликованных видео
из базы источника и ставит их на слоты времени (с джиттером). В момент слота
видео скачивается по ссылке и публикуется в VK; после успеха помечается
опубликованным (дедуп). Расписание хранится в БД и переживает рестарт.
"""

import asyncio
import logging
import os
import random
import traceback as tb_module
from datetime import datetime, time as dtime, timedelta

import pytz

import db
from downloader import (
    detect_platform, download_tiktok, download_likee, download_youtube, download_vk,
)
from vk import VKError, VK_RETRYABLE_ERROR_CODES, upload_to_vk

logger = logging.getLogger(__name__)
MOSCOW_TZ = pytz.timezone("Europe/Moscow")

# ─── Настройки из окружения ───────────────────────────────────────────────────
VK_PUBLISH_CONCURRENCY = int(os.getenv("VK_PUBLISH_CONCURRENCY", "1"))
VK_PUBLISH_RETRIES = int(os.getenv("VK_PUBLISH_RETRIES", "3"))
VK_RETRY_BASE_DELAY = float(os.getenv("VK_RETRY_BASE_DELAY", "3"))
PUBLISH_JITTER_SECONDS = int(os.getenv("PUBLISH_JITTER_SECONDS", "300"))
DAILY_PLAN_HOUR = int(os.getenv("DAILY_PLAN_HOUR", "0"))
DAILY_PLAN_MINUTE = int(os.getenv("DAILY_PLAN_MINUTE", "5"))
# Автодопарсинг истории: держим запас невыложенных кандидатов на столько дневных
# норм вперёд; если меньше — углубляемся в историю (не больше N проходов за раз).
BACKLOG_DAYS = int(os.getenv("BACKLOG_DAYS", "3"))
DEEPEN_MAX_PASSES = int(os.getenv("DEEPEN_MAX_PASSES", "3"))

PLATFORM_LABELS = {"tiktok": "TikTok", "likee": "Likee", "youtube": "YouTube Shorts", "vk": "VK"}

# Семафор публикации на пользователя: лимиты VK считаются по токену, поэтому
# запросы одного юзера не идут лавиной, а разные юзеры друг друга не ждут.
_vk_publish_semaphores: dict[int, asyncio.Semaphore] = {}


def _user_semaphore(telegram_id: int) -> asyncio.Semaphore:
    sem = _vk_publish_semaphores.get(telegram_id)
    if sem is None:
        sem = asyncio.Semaphore(VK_PUBLISH_CONCURRENCY)
        _vk_publish_semaphores[telegram_id] = sem
    return sem


# ─── Скачивание видео по ссылке ───────────────────────────────────────────────

async def _download(url: str, vk_token: str | None) -> tuple[str, str]:
    platform = detect_platform(url)
    if platform == "tiktok":
        return await download_tiktok(url, None)
    if platform == "likee":
        return await download_likee(url)
    if platform == "youtube":
        return await download_youtube(url, None)
    if platform == "vk":
        return await download_vk(url, vk_token)
    raise ValueError(f"Не удалось определить платформу по ссылке: {url}")


# ─── Публикация с семафором и ретраями ────────────────────────────────────────

async def _publish_to_vk(telegram_id, vk_token, vk_group_id, file_path, title, description) -> None:
    loop = asyncio.get_running_loop()
    semaphore = _user_semaphore(telegram_id)
    for attempt in range(1, VK_PUBLISH_RETRIES + 1):
        async with semaphore:
            try:
                await loop.run_in_executor(
                    None,
                    lambda: upload_to_vk(vk_token, vk_group_id, file_path, title, description),
                )
                return
            except VKError as exc:
                retryable = (exc.network or exc.code in VK_RETRYABLE_ERROR_CODES) and not exc.no_retry
                if not retryable or attempt == VK_PUBLISH_RETRIES:
                    raise
                last_exc = exc
        # Пауза вне семафора — другие ролики этого юзера могут публиковаться.
        delay = VK_RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 2)
        logger.warning("Публикация не удалась (попытка %s/%s): %s. Повтор через %.1f c",
                       attempt, VK_PUBLISH_RETRIES, last_exc, delay)
        await asyncio.sleep(delay)


def _record_error(telegram_id, exc, *, stage=None, platform=None, url=None,
                  vk_group_id=None, vk_group_name=None) -> None:
    code = exc.code if isinstance(exc, VKError) else None
    eff_stage = stage or (exc.stage if isinstance(exc, VKError) else None)
    db.log_error(
        telegram_id, stage=eff_stage, platform=platform, url=url,
        vk_group_id=vk_group_id, vk_group_name=vk_group_name, error_code=code,
        message=str(exc),
        traceback="".join(tb_module.format_exception(type(exc), exc, exc.__traceback__)),
    )


# ─── Раскладка времени по слотам ──────────────────────────────────────────────

def parse_slots(slots_csv: str) -> list[int]:
    """'9,15,20' -> [9, 15, 20]. Отбрасывает мусор и часы вне 0..23."""
    hours = []
    for part in (slots_csv or "").split(","):
        part = part.strip()
        if part.isdigit():
            h = int(part)
            if 0 <= h <= 23 and h not in hours:
                hours.append(h)
    return sorted(hours)


def _base_slot_times(slots: list[int], now: datetime) -> list[datetime]:
    """Слоты ближайшей раскладки: будущие слоты сегодня, иначе — все слоты завтра."""
    def slot_dt(date, h):
        return MOSCOW_TZ.localize(datetime(date.year, date.month, date.day, h))

    today = now.date()
    base = [slot_dt(today, h) for h in slots if slot_dt(today, h) > now + timedelta(minutes=2)]
    if not base:
        tomorrow = today + timedelta(days=1)
        base = [slot_dt(tomorrow, h) for h in slots]
    return base


def compute_publish_times(slots: list[int], n: int, now: datetime) -> list[datetime]:
    """Вернуть n моментов публикации, распределяя видео по слотам.

    Используются только будущие слоты сегодня; если на сегодня их не осталось —
    берутся слоты завтрашнего дня. Если видео больше, чем слотов, они идут по
    кругу (несколько на слот — джиттер их разнесёт).
    """
    if n <= 0 or not slots:
        return []
    base = _base_slot_times(slots, now)
    return [base[i % len(base)] for i in range(n)]


# ─── Планирование правила ─────────────────────────────────────────────────────

def _schedule_post_job(job_queue, post_row) -> None:
    """Поставить одну запланированную публикацию в job_queue."""
    when = datetime.fromtimestamp(post_row["publish_at"], tz=MOSCOW_TZ)
    now = datetime.now(MOSCOW_TZ)
    if when < now:
        when = now + timedelta(seconds=5)
    job_queue.run_once(
        _publish_job,
        when=when,
        data={"scheduled_post_id": post_row["id"]},
        name=f"post_{post_row['id']}",
    )


def plan_rule(job_queue, rule) -> int:
    """Разложить план по одному правилу. Вернуть кол-во поставленных публикаций."""
    from source_reader import fetch_candidates, SourceError

    rule_id = rule["id"]
    slots = parse_slots(rule["slots"])
    now = datetime.now(MOSCOW_TZ)

    # Дневная квота: N видео В ДЕНЬ, а не за вызов. Считаем, сколько по этому
    # правилу уже опубликовано и уже стоит в очереди на СЕГОДНЯ, и добираем
    # только остаток. Иначе повторный прогон (старт + ежедневный job) задвоил бы.
    day_start = MOSCOW_TZ.localize(datetime(now.year, now.month, now.day))
    day_end = day_start + timedelta(days=1)
    start_ts, end_ts = int(day_start.timestamp()), int(day_end.timestamp())
    already = (db.count_scheduled_between(rule_id, start_ts, end_ts)
               + db.count_published_between(rule_id, start_ts, end_ts))
    remaining = rule["videos_per_day"] - already
    if remaining <= 0:
        return 0

    # Пропущенное за сегодня НЕ «догоняем»: ставим максимум по одному ролику на
    # слот и только на СВОБОДНЫЕ будущие слоты. Слот считаем занятым, если по
    # этому правилу в его пределах (в течение часа) уже стоит публикация — так
    # повторные прогоны/рестарты в пределах дня не сваливают пачку в один слот.
    base_times = _base_slot_times(slots, now) if slots else []
    taken = db.get_scheduled_publish_times(rule_id)
    free_times = [
        t for t in base_times
        if not any(int(t.timestamp()) <= at < int(t.timestamp()) + 3600 for at in taken)
    ]
    remaining = min(remaining, len(free_times))
    if remaining <= 0:
        return 0

    # Исключаем уже опубликованные И уже стоящие в очереди — чтобы не задвоить.
    exclude = db.get_published_ids(rule_id) | db.get_scheduled_ids(rule_id)

    try:
        candidates = fetch_candidates(
            rule["db_path"],
            username=rule["username"],
            min_duration=rule["min_duration"],
            max_duration=rule["max_duration"],
            order_dir=rule["order_dir"],
            exclude_ids=exclude,
            limit=remaining,
        )
    except SourceError as e:
        logger.warning("Правило %s: источник недоступен: %s", rule_id, e)
        _record_error(rule["telegram_id"], e, stage="чтение источника",
                      vk_group_id=rule["vk_group_id"], vk_group_name=rule["group_name"])
        return 0

    if not candidates:
        return 0

    # По одному ролику на свободный слот (кандидатов не больше, чем свободных слотов).
    times = free_times[:len(candidates)]

    scheduled = 0
    for video, base_time in zip(candidates, times):
        jitter = random.randint(0, PUBLISH_JITTER_SECONDS)
        publish_at = int(base_time.timestamp()) + jitter
        post_id = db.add_scheduled_post(
            telegram_id=rule["telegram_id"],
            rule_id=rule_id,
            tt_video_id=video["tt_video_id"],
            url=video["url"],
            title=video["title"],
            description=rule["description"],
            vk_group_id=rule["vk_group_id"],
            vk_group_name=rule["group_name"],
            publish_at=publish_at,
        )
        if post_id is None:
            continue  # видео уже в очереди (гонка/второй экземпляр) — не задваиваем
        _schedule_post_job(job_queue, {"id": post_id, "publish_at": publish_at})
        scheduled += 1
    logger.info("Правило %s (%s → %s): запланировано %s видео",
                rule_id, rule["source_name"], rule["group_name"], scheduled)
    return scheduled


def schedule_test_now(job_queue, rule_id: int) -> tuple[bool, str]:
    """Поставить ОДНО новое видео правила на публикацию прямо сейчас (тест).

    Игнорирует дневную квоту (это ручная проверка настроек), но соблюдает дедуп.
    """
    from source_reader import fetch_candidates, SourceError

    rule = db.get_rule(rule_id)
    if not rule:
        return False, "Правило не найдено."
    if not db.get_vk_token(rule["telegram_id"]):
        return False, "Сначала задай VK токен."

    exclude = db.get_published_ids(rule_id) | db.get_scheduled_ids(rule_id)
    try:
        cands = fetch_candidates(
            rule["db_path"], username=rule["username"],
            min_duration=rule["min_duration"], max_duration=rule["max_duration"],
            order_dir=rule["order_dir"], exclude_ids=exclude, limit=1,
        )
    except SourceError as e:
        return False, f"Источник недоступен: {e}"
    if not cands:
        return False, "Нет новых видео (все уже опубликованы или в очереди)."

    v = cands[0]
    publish_at = int(datetime.now(MOSCOW_TZ).timestamp()) + 5
    post_id = db.add_scheduled_post(
        telegram_id=rule["telegram_id"], rule_id=rule_id,
        tt_video_id=v["tt_video_id"], url=v["url"], title=v["title"],
        description=rule["description"], vk_group_id=rule["vk_group_id"],
        vk_group_name=rule["group_name"], publish_at=publish_at,
    )
    if post_id is None:
        return False, "Это видео уже стоит в очереди на публикацию."
    _schedule_post_job(job_queue, {"id": post_id, "publish_at": publish_at})
    return True, f"▶️ Публикую тестовое видео (~через 5 сек):\n{v['url']}"


# ─── Джобы ────────────────────────────────────────────────────────────────────

async def refresh_account_sources(context_or_app) -> int:
    """До-парсить все источники-аккаунты (собрать свежие ссылки в их базы).

    Каждый прогон парсера — блокирующий (сеть/yt-dlp), поэтому уводим в executor.
    Ошибка одного аккаунта не мешает остальным и логируется в /errors владельца.
    """
    import account_source

    sources = db.get_account_sources()
    if not sources:
        return 0
    loop = asyncio.get_running_loop()
    refreshed = 0
    for s in sources:
        db_path = s["db_path"] or account_source.source_db_path(s["id"])
        start = s["parse_start"] if "parse_start" in s.keys() else 1
        platform = s["platform"] if "platform" in s.keys() else "tiktok"
        try:
            res = await loop.run_in_executor(
                None, account_source.refresh_account, s["account"], db_path,
                account_source.PARSE_LIMIT, start, platform,
            )
            refreshed += 1
            logger.info("Источник %s (@%s): +%s новых", s["id"], res["username"], res["added"])
        except Exception as exc:
            logger.exception("Не удалось спарсить источник %s (%s)", s["id"], s["account"])
            _record_error(s["telegram_id"], exc, stage="парсинг аккаунта", platform=platform)
    return refreshed


def _min_remaining(src_rules, cap: int) -> int:
    """Минимальный запас невыложенных кандидатов среди правил источника.

    Считает по каждому правилу число видео в базе источника, не попавших ни в
    published, ни в очередь (с учётом фильтров правила), и берёт минимум —
    «слабое звено». cap ограничивает подсчёт: выше порога точное число не важно.
    """
    from source_reader import fetch_candidates

    worst = cap
    for r in src_rules:
        exclude = db.get_published_ids(r["id"]) | db.get_scheduled_ids(r["id"])
        cands = fetch_candidates(
            r["db_path"], username=r["username"],
            min_duration=r["min_duration"], max_duration=r["max_duration"],
            order_dir=r["order_dir"], exclude_ids=exclude, limit=cap,
        )
        worst = min(worst, len(cands))
        if worst == 0:
            break
    return worst


async def ensure_backlog(context_or_app) -> int:
    """Догрузить историю аккаунтов, у чьих правил кандидаты на исходе.

    Для каждого источника-аккаунта с активными правилами: если запас
    невыложенных кандидатов (минимум по его правилам) меньше BACKLOG_DAYS
    дневных норм — углубляемся в историю, пока запас не наберётся или пока не
    упрёмся в дно аккаунта (тогда ставим backfill_done, чтобы не долбить зря).
    Возвращает число выполненных проходов углубления.
    """
    import account_source
    from source_reader import SourceError

    account_sources = db.get_account_sources()
    if not account_sources:
        return 0
    rules = db.get_enabled_rules()
    rules_by_source: dict[int, list] = {}
    for r in rules:
        rules_by_source.setdefault(r["source_id"], []).append(r)

    loop = asyncio.get_running_loop()
    deepened = 0
    for src in account_sources:
        if src["backfill_done"]:
            continue  # вся история собрана — новые ролики ловит обычный до-парсинг
        src_rules = rules_by_source.get(src["id"])
        if not src_rules:
            continue  # нет активных правил — нечему заканчиваться
        db_path = src["db_path"] or account_source.source_db_path(src["id"])
        platform = src["platform"] if "platform" in src.keys() else "tiktok"
        # запас на столько дней по самому «прожорливому» правилу источника
        need = max(r["videos_per_day"] for r in src_rules) * BACKLOG_DAYS

        for _ in range(DEEPEN_MAX_PASSES):
            try:
                remaining = _min_remaining(src_rules, need)
            except SourceError:
                break  # базы источника ещё нет — обычный до-парсинг её создаст
            if remaining >= need:
                break  # запаса хватает
            try:
                res = await loop.run_in_executor(
                    None, account_source.deepen_account, src["account"], db_path,
                    account_source.PARSE_STEP, platform,
                )
            except Exception as exc:
                logger.exception("Не удалось углубить источник %s (%s)",
                                 src["id"], src["account"])
                _record_error(src["telegram_id"], exc,
                              stage="допарсинг истории", platform=platform)
                break
            deepened += 1
            logger.info("Источник %s (@%s): углубление +%s новых (запас был %s/%s)",
                        src["id"], res["username"], res["added"], remaining, need)
            if res["exhausted"]:
                db.set_backfill_done(src["id"])
                break  # дошли до самого старого ролика аккаунта
            if not res["can_deepen"] or res["added"] == 0:
                break  # embed не умеет глубже / новых не пришло — на этот прогон всё
    return deepened


async def daily_plan_job(context) -> None:
    """Ежедневно: до-парсить аккаунты, докачать историю при нехватке, разложить план."""
    await refresh_account_sources(context)
    await ensure_backlog(context)

    rules = db.get_enabled_rules()
    total = 0
    for rule in rules:
        total += plan_rule(context.job_queue, rule)
    if total:
        logger.info("Ежедневный план: поставлено публикаций: %s (правил: %s)", total, len(rules))


async def _publish_job(context) -> None:
    """Публикация одного запланированного видео в момент слота."""
    post_id = context.job.data["scheduled_post_id"]

    # Актуальная строка из БД (правило могли удалить/выключить, токен сменить).
    posts = {p["id"]: p for p in db.get_scheduled_posts()}
    post = posts.get(post_id)
    if not post:
        return  # уже удалена (например, правило снесли)

    telegram_id = post["telegram_id"]
    rule_id = post["rule_id"]
    url = post["url"]

    # Страховка от повторной публикации: если по этому правилу видео уже отмечено
    # опубликованным (задвоенный job, гонка, повторный запуск) — ничего не постим,
    # только убираем строку из очереди.
    if post["tt_video_id"] in db.get_published_ids(rule_id):
        logger.info("Пропуск публикации rule=%s video=%s: уже опубликовано",
                    rule_id, post["tt_video_id"])
        db.delete_scheduled_post(post_id)
        return
    group_name = post["vk_group_name"]
    vk_group_id = post["vk_group_id"]
    platform = detect_platform(url)
    file_path = None

    try:
        vk_token = db.get_vk_token(telegram_id)
        if not vk_token:
            raise VKError(None, "VK токен не задан — публиковать нечем", stage="проверка токена")

        file_path, title = await _download(url, vk_token)
        title = post["title"] or title
        await _publish_to_vk(telegram_id, vk_token, vk_group_id, file_path,
                             title, post["description"] or "")

        db.mark_published(rule_id, post["tt_video_id"])
        try:
            await context.bot.send_message(
                telegram_id, f"✅ Опубликовано в «{group_name}»: {url}"
            )
        except Exception:
            pass
        logger.info("Опубликовано rule=%s video=%s в «%s»", rule_id, post["tt_video_id"], group_name)

    except Exception as exc:
        logger.exception("Ошибка автопубликации url=%s", url)
        stage = exc.stage if isinstance(exc, VKError) else "публикация"
        _record_error(telegram_id, exc, stage=stage, platform=platform, url=url,
                      vk_group_id=vk_group_id, vk_group_name=group_name)
        try:
            await context.bot.send_message(
                telegram_id,
                f"❌ Не удалось опубликовать в «{group_name}»:\n{exc}\n\n"
                f"🔗 {url}\nℹ️ Подробности — в /errors",
            )
        except Exception:
            pass
    finally:
        # Публикация завершена (успех или финальная ошибка) — убираем из очереди.
        db.delete_scheduled_post(post_id)
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass


# ─── Регистрация и восстановление ─────────────────────────────────────────────

async def restore_and_schedule(app) -> None:
    """post_init: восстановить очередь из БД и разложить план на сегодня.

    Сначала возвращаем в job_queue уже запланированные публикации (пережившие
    рестарт), затем догоняем план на сегодня для правил, у которых на сегодня
    ещё ничего не стоит.
    """
    restored = 0
    for post in db.get_scheduled_posts():
        _schedule_post_job(app.job_queue, post)
        restored += 1
    if restored:
        logger.info("Восстановлено запланированных публикаций: %s", restored)

    # Свежий старт: до-парсим аккаунты, чтобы в базах были актуальные ссылки,
    # и докачиваем историю, если запас невыложенных кандидатов уже на исходе.
    try:
        await refresh_account_sources(app)
        await ensure_backlog(app)
    except Exception:
        logger.exception("Ошибка стартового до-парсинга аккаунтов")

    # Догоняем сегодняшний план (plan_rule сам исключит уже стоящие в очереди).
    total = 0
    for rule in db.get_enabled_rules():
        total += plan_rule(app.job_queue, rule)
    if total:
        logger.info("Стартовый план: поставлено публикаций: %s", total)


def register_jobs(app) -> None:
    """Зарегистрировать ежедневную раскладку плана."""
    app.job_queue.run_daily(
        daily_plan_job,
        time=dtime(hour=DAILY_PLAN_HOUR, minute=DAILY_PLAN_MINUTE, tzinfo=MOSCOW_TZ),
        name="daily_plan",
    )
