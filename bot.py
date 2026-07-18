"""Автопостер: Telegram-бот настройки автоматической публикации видео из БД
парсера в VK-группы по правилам «источник → группа».

Пользователь задаёт VK-токен, добавляет группы и источники (базы парсера), а
затем связывает их правилами с настройками (сколько видео/день, слоты времени,
описание, фильтр длительности). Дальше публикует планировщик (scheduler.py) —
руками ничего выкладывать не нужно.
"""

import os
import re
import asyncio
import logging
from io import BytesIO
from datetime import datetime, timedelta

import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters,
    ContextTypes, PicklePersistence,
)
from dotenv import load_dotenv

import db
import scheduler
import account_source
from vk import resolve_screen_name, fetch_group_name
from source_reader import check_source, fetch_candidates, SourceError

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MOSCOW_TZ = pytz.timezone("Europe/Moscow")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
ERROR_RETENTION_DAYS = int(os.getenv("ERROR_RETENTION_DAYS", "5"))
ERROR_CLEANUP_INTERVAL_DAYS = int(os.getenv("ERROR_CLEANUP_INTERVAL_DAYS", "5"))
ERRORS_PAGE_SIZE = 8

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Состояния разговоров ─────────────────────────────────────────────────────
TOKEN_WAIT = 10
G_ADD_ID, G_ADD_CONFIRM, G_ADD_NAME, G_RENAME = range(20, 24)
SRC_NAME, SRC_TYPE, SRC_ACCOUNT, SRC_PATH, SRC_USER = range(30, 35)
RULE_EDIT_VALUE = 40

# ─── Постоянное меню ──────────────────────────────────────────────────────────
BTN_TOKEN = "🔑 Токен"
BTN_GROUPS = "👥 Группы"
BTN_SOURCES = "🎬 Источники"
BTN_RULES = "📋 Правила"
BTN_STATUS = "📊 Статус"
MENU_BUTTON_TEXTS = [BTN_TOKEN, BTN_GROUPS, BTN_SOURCES, BTN_RULES, BTN_STATUS]


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BTN_TOKEN, BTN_GROUPS], [BTN_SOURCES, BTN_RULES], [BTN_STATUS]],
        resize_keyboard=True,
    )


def _mask_token(token: str) -> str:
    if len(token) <= 12:
        return "•" * len(token)
    return f"{token[:6]}…{token[-4:]}"


# ─── /start ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db.ensure_user(update.effective_user.id)
    await update.message.reply_text(
        "Привет! Я сам публикую видео из базы парсера в твои VK-группы по расписанию.\n\n"
        "Как настроить:\n"
        f"1. {BTN_TOKEN} — задай VK токен.\n"
        f"2. {BTN_GROUPS} — добавь целевые группы VK.\n"
        f"3. {BTN_SOURCES} — добавь источники (базы парсера со ссылками).\n"
        f"4. {BTN_RULES} — свяжи источник с группой и настрой: сколько видео/день, "
        "во сколько, описание, фильтр длительности.\n\n"
        "Дальше публикация идёт автоматически — вручную ничего выкладывать не нужно.",
        reply_markup=main_keyboard(),
    )


# ─── Меню ─────────────────────────────────────────────────────────────────────

async def main_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    telegram_id = update.effective_user.id
    db.ensure_user(telegram_id)
    if text == BTN_TOKEN:
        await show_token_status(update, context)
    elif text == BTN_GROUPS:
        await update.message.reply_text("Твои группы VK:", reply_markup=groups_kb(telegram_id))
    elif text == BTN_SOURCES:
        await update.message.reply_text("Твои источники:", reply_markup=sources_kb(telegram_id))
    elif text == BTN_RULES:
        await update.message.reply_text("Твои правила публикации:", reply_markup=rules_kb(telegram_id))
    elif text == BTN_STATUS:
        await cmd_status(update, context)


# ─── Токен ────────────────────────────────────────────────────────────────────

def token_kb(has_token: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        "✏️ Изменить токен" if has_token else "➕ Задать токен", callback_data="settoken_change"
    )]]
    if has_token:
        rows.append([InlineKeyboardButton("🗑 Удалить токен", callback_data="settoken_delete")])
    return InlineKeyboardMarkup(rows)


async def show_token_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    token = db.get_vk_token(update.effective_user.id)
    msg = (f"🔑 Токен задан: {_mask_token(token)}\n(показан частично)"
           if token else "❌ Токен не задан.")
    await update.message.reply_text(msg, reply_markup=token_kb(bool(token)))


SETTOKEN_PROMPT = (
    "Пришли свой VK токен.\n\n"
    "Как получить через Kate Mobile:\n"
    "1. Открой в браузере:\n"
    "https://oauth.vk.com/authorize?client_id=2685278&scope=1073737727&redirect_uri=https://oauth.vk.com/blank.html&display=page&response_type=token\n"
    "2. Войди и разреши доступ.\n"
    "3. Скопируй access_token из адресной строки."
)


async def cmd_settoken(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(SETTOKEN_PROMPT)
    return TOKEN_WAIT


async def settoken_from_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(SETTOKEN_PROMPT)
    return TOKEN_WAIT


async def handle_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    token = update.message.text.strip()
    if token in MENU_BUTTON_TEXTS:
        await update.message.reply_text("Ввод токена отменён.", reply_markup=main_keyboard())
        return ConversationHandler.END
    is_new = token.startswith("vk1.a.") and len(token) >= 26
    is_old = len(token) >= 85 and not any(ch.isspace() for ch in token)
    if not (is_new or is_old):
        await update.message.reply_text(
            "❌ Это не похоже на VK токен. Пришли правильный или /cancel."
        )
        return TOKEN_WAIT
    db.set_vk_token(update.effective_user.id, token)
    await update.message.reply_text("✅ Токен сохранён.", reply_markup=main_keyboard())
    return ConversationHandler.END


async def handle_token_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Токен удалён")
    db.clear_vk_token(update.effective_user.id)
    await query.edit_message_text("🗑 Токен удалён.", reply_markup=token_kb(False))


# ─── Группы ───────────────────────────────────────────────────────────────────

_VK_HOST_RE = re.compile(r"(?:https?://)?(?:m\.|www\.)?(?:vk\.com|vkontakte\.ru)/", re.IGNORECASE)


def _extract_screen_name(text: str) -> str:
    text = _VK_HOST_RE.sub("", text.strip())
    text = text.split("?")[0].split("#")[0].strip("/")
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return text


def resolve_vk_group(vk_token: str | None, text: str) -> tuple[int | None, str | None, str | None]:
    raw = _extract_screen_name(text)
    if not raw:
        return None, None, "Пустая ссылка. Пришли ссылку на сообщество VK."
    m = re.match(r"(?:video|wall|clip|photo)-(\d+)", raw, re.IGNORECASE)
    if m:
        gid = int(m.group(1))
        return gid, fetch_group_name(vk_token, gid) if vk_token else None, None
    m = re.match(r"(?:club|public|event)(\d+)$", raw, re.IGNORECASE)
    if m:
        gid = int(m.group(1))
        return gid, fetch_group_name(vk_token, gid) if vk_token else None, None
    if re.fullmatch(r"-?\d+", raw):
        gid = abs(int(raw))
        return gid, fetch_group_name(vk_token, gid) if vk_token else None, None
    if not vk_token:
        return None, None, (
            f"Чтобы добавить группу по короткой ссылке, сначала задай VK токен ({BTN_TOKEN}). "
            "Либо пришли ссылку вида vk.com/club123."
        )
    obj = resolve_screen_name(vk_token, raw)
    if not obj:
        return None, None, "Не удалось найти сообщество по этой ссылке. Проверь её."
    if obj.get("type") not in ("group", "page"):
        return None, None, "Это не сообщество. Пришли ссылку именно на группу/паблик VK."
    gid = int(obj["object_id"])
    return gid, fetch_group_name(vk_token, gid), None


def groups_kb(telegram_id: int) -> InlineKeyboardMarkup:
    rows = []
    for g in db.get_groups(telegram_id):
        rows.append([InlineKeyboardButton(f"{g['name']} (id {g['vk_group_id']})", callback_data="noop")])
        rows.append([
            InlineKeyboardButton("✏️ Переименовать", callback_data=f"g_rename_{g['id']}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"g_del_{g['id']}"),
        ])
    rows.append([InlineKeyboardButton("➕ Добавить группу", callback_data="g_add")])
    return InlineKeyboardMarkup(rows)


async def cmd_groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db.ensure_user(update.effective_user.id)
    await update.message.reply_text("Твои группы VK:", reply_markup=groups_kb(update.effective_user.id))
    return ConversationHandler.END


async def groups_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    data = query.data
    if data == "noop":
        await query.answer()
        return ConversationHandler.END
    if data == "g_add":
        await query.answer()
        await query.edit_message_text(
            "Пришли ссылку на сообщество VK (ID определю сам):\n"
            "• vk.com/club123456\n• vk.com/public123456\n• vk.com/my_group_name"
        )
        return G_ADD_ID
    if data.startswith("g_del_"):
        await query.answer("Удалено")
        db.delete_group(int(data.rsplit("_", 1)[1]))
        await query.edit_message_text("Твои группы VK:", reply_markup=groups_kb(update.effective_user.id))
        return ConversationHandler.END
    if data.startswith("g_rename_"):
        await query.answer()
        context.user_data["rename_group_id"] = int(data.rsplit("_", 1)[1])
        await query.edit_message_text("Введи новое название группы:")
        return G_RENAME
    await query.answer()
    return ConversationHandler.END


async def groups_add_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    import asyncio
    vk_token = db.get_vk_token(update.effective_user.id)
    loop = asyncio.get_running_loop()
    group_id, name, error = await loop.run_in_executor(None, resolve_vk_group, vk_token, update.message.text)
    if error:
        await update.message.reply_text(error + "\n\nПопробуй ещё раз или /cancel.")
        return G_ADD_ID
    context.user_data["pending_group_id"] = group_id
    if name:
        context.user_data["pending_group_name"] = name
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Сохранить", callback_data="g_confirmname")],
            [InlineKeyboardButton("✏️ Задать своё имя", callback_data="g_manualname")],
        ])
        await update.message.reply_text(f"Нашёл: «{name}» (id {group_id}). Сохранить?", reply_markup=kb)
        return G_ADD_CONFIRM
    await update.message.reply_text(f"Сообщество найдено (id {group_id}). Введи название вручную:")
    return G_ADD_NAME


async def groups_add_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    if query.data == "g_confirmname":
        db.add_group(telegram_id, context.user_data["pending_group_id"],
                     context.user_data["pending_group_name"])
        await query.edit_message_text("✅ Группа добавлена.\n\nТвои группы VK:",
                                      reply_markup=groups_kb(telegram_id))
        return ConversationHandler.END
    await query.edit_message_text("Введи название группы вручную:")
    return G_ADD_NAME


async def groups_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    telegram_id = update.effective_user.id
    db.add_group(telegram_id, context.user_data["pending_group_id"], update.message.text.strip())
    await update.message.reply_text("✅ Группа добавлена.\n\nТвои группы VK:",
                                    reply_markup=groups_kb(telegram_id))
    return ConversationHandler.END


async def groups_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    telegram_id = update.effective_user.id
    db.rename_group(context.user_data["rename_group_id"], update.message.text.strip())
    await update.message.reply_text("✅ Переименовано.\n\nТвои группы VK:",
                                    reply_markup=groups_kb(telegram_id))
    return ConversationHandler.END


# ─── Источники ────────────────────────────────────────────────────────────────

def sources_kb(telegram_id: int) -> InlineKeyboardMarkup:
    rows = []
    for s in db.get_sources(telegram_id):
        if s["kind"] == "account":
            label = f"🎵 {s['name']} (@{s['account']})"
        else:
            label = f"🗄 {s['name']}" + (f" · @{s['username']}" if s["username"] else "")
        rows.append([InlineKeyboardButton(label, callback_data="noop")])
        btn_row = []
        if s["kind"] == "account":
            btn_row.append(InlineKeyboardButton("🔄 Обновить", callback_data=f"src_refresh_{s['id']}"))
        btn_row.append(InlineKeyboardButton("🗑 Удалить", callback_data=f"src_del_{s['id']}"))
        rows.append(btn_row)
    rows.append([InlineKeyboardButton("➕ Добавить источник", callback_data="src_add")])
    return InlineKeyboardMarkup(rows)


async def cmd_sources(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db.ensure_user(update.effective_user.id)
    await update.message.reply_text("Твои источники:", reply_markup=sources_kb(update.effective_user.id))
    return ConversationHandler.END


async def sources_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    data = query.data
    if data == "noop":
        await query.answer()
        return ConversationHandler.END
    if data == "src_add":
        await query.answer()
        await query.edit_message_text("Введи название источника (для себя, например «TikTok Batek»):")
        return SRC_NAME
    if data.startswith("src_del_"):
        await query.answer("Удалено")
        db.delete_source(int(data.rsplit("_", 1)[1]))
        await query.edit_message_text("Твои источники:", reply_markup=sources_kb(update.effective_user.id))
        return ConversationHandler.END
    if data.startswith("src_refresh_"):
        await query.answer("Обновляю…")
        await _refresh_source(query, int(data.rsplit("_", 1)[1]))
        return ConversationHandler.END
    await query.answer()
    return ConversationHandler.END


async def _refresh_source(query, source_id: int) -> None:
    """Прогнать парсер по источнику-аккаунту прямо сейчас и показать результат."""
    s = db.get_source(source_id)
    if not s or s["kind"] != "account":
        return
    await query.edit_message_text(f"⏳ Парсю @{s['account']}…")
    path = s["db_path"] or account_source.source_db_path(source_id)
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(None, account_source.refresh_account, s["account"], path)
        note = f"✅ @{res['username']}: всего {res['total']}, новых {res['added']}."
    except Exception as exc:
        note = f"❌ Не удалось спарсить @{s['account']}:\n{exc}"
    await query.message.reply_text(note)
    await query.message.reply_text("Твои источники:", reply_markup=sources_kb(s["telegram_id"]))


async def sources_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["src_name"] = update.message.text.strip()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎵 TikTok-аккаунт (бот парсит сам)", callback_data="src_type_account")],
        [InlineKeyboardButton("🗄 Готовая база парсера (файл .db)", callback_data="src_type_db")],
    ])
    await update.message.reply_text("Какой это источник?", reply_markup=kb)
    return SRC_TYPE


async def sources_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "src_type_account":
        await query.edit_message_text(
            "Пришли ссылку или @ник TikTok-аккаунта, который парсить.\n"
            "Например: https://www.tiktok.com/@batek.official или @batek.official"
        )
        return SRC_ACCOUNT
    await query.edit_message_text(
        "Пришли путь к базе парсера (SQLite-файл со ссылками),\n"
        "например: C:\\Users\\...\\batek.official.db"
    )
    return SRC_PATH


async def sources_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    try:
        username, _ = account_source.normalize(raw)
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}\nПришли корректную ссылку/ник или /cancel.")
        return SRC_ACCOUNT

    telegram_id = update.effective_user.id
    # Заводим источник, затем проставляем путь к его базе (нужен id).
    sid = db.add_source(telegram_id, context.user_data["src_name"], "", None,
                        kind="account", account=username)
    path = account_source.source_db_path(sid)
    db.set_source_db_path(sid, path)

    await update.message.reply_text(f"⏳ Первый парсинг @{username}…")
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(None, account_source.refresh_account, username, path)
        note = f"✅ Источник добавлен. Собрано ссылок: {res['total']} (новых {res['added']})."
    except Exception as exc:
        note = (f"⚠️ Источник добавлен, но первый парсинг не удался:\n{exc}\n"
                "Можно повторить кнопкой «🔄 Обновить».")
    await update.message.reply_text(note)
    await update.message.reply_text("Твои источники:", reply_markup=sources_kb(telegram_id))
    return ConversationHandler.END


async def sources_path(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    path = update.message.text.strip().strip('"')
    try:
        count = check_source(path)
    except SourceError as e:
        await update.message.reply_text(f"❌ {e}\n\nПришли корректный путь или /cancel.")
        return SRC_PATH
    context.user_data["src_path"] = path
    await update.message.reply_text(
        f"✅ База найдена, видео в ней: {count}.\n\n"
        "Если нужно брать только один ник из базы — пришли его (например batek.official).\n"
        "Если брать все — отправь «-»."
    )
    return SRC_USER


async def sources_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip().lstrip("@")
    username = None if raw in ("-", "") else raw
    telegram_id = update.effective_user.id
    db.add_source(telegram_id, context.user_data["src_name"], context.user_data["src_path"],
                  username, kind="db")
    await update.message.reply_text("✅ Источник добавлен.\n\nТвои источники:",
                                    reply_markup=sources_kb(telegram_id))
    return ConversationHandler.END


# ─── Правила (источник → группа) ──────────────────────────────────────────────

def rules_kb(telegram_id: int) -> InlineKeyboardMarkup:
    rows = []
    for r in db.get_rules(telegram_id):
        mark = "🟢" if r["enabled"] else "⚪"
        rows.append([InlineKeyboardButton(
            f"{mark} {r['source_name']} → {r['group_name']}", callback_data=f"rule_open_{r['id']}"
        )])
    rows.append([InlineKeyboardButton("➕ Добавить правило", callback_data="rule_add")])
    return InlineKeyboardMarkup(rows)


def rule_detail_text(r) -> str:
    dur = "любая"
    if r["min_duration"] is not None or r["max_duration"] is not None:
        lo = r["min_duration"] if r["min_duration"] is not None else 0
        hi = r["max_duration"] if r["max_duration"] is not None else "∞"
        dur = f"{lo}..{hi} сек"
    order = "старые→новые" if r["order_dir"] == "old" else "новые→старые"
    return (
        f"📋 Правило #{r['id']}\n"
        f"🎬 Источник: {r['source_name']}\n"
        f"👥 Группа: {r['group_name']} (id {r['vk_group_id']})\n"
        f"▶️ Статус: {'включено' if r['enabled'] else 'выключено'}\n"
        f"🔢 Видео в день: {r['videos_per_day']}\n"
        f"🕒 Слоты (МСК): {r['slots']}\n"
        f"⏱ Длительность: {dur}\n"
        f"↕️ Порядок: {order}\n"
        f"📝 Описание: {(r['description'] or '—')[:100]}\n"
        f"✅ Опубликовано: {db.count_published(r['id'])}"
    )


def rule_detail_kb(r) -> InlineKeyboardMarkup:
    rid = r["id"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⛔ Выключить" if r["enabled"] else "✅ Включить",
                              callback_data=f"rule_toggle_{rid}")],
        [InlineKeyboardButton("🔢 Видео/день", callback_data=f"rule_nday_{rid}"),
         InlineKeyboardButton("🕒 Слоты", callback_data=f"rule_slots_{rid}")],
        [InlineKeyboardButton("⏱ Длительность", callback_data=f"rule_dur_{rid}"),
         InlineKeyboardButton("↕️ Порядок", callback_data=f"rule_order_{rid}")],
        [InlineKeyboardButton("📝 Описание", callback_data=f"rule_desc_{rid}")],
        [InlineKeyboardButton("▶️ Тест: опубликовать 1 сейчас", callback_data=f"rule_now_{rid}")],
        [InlineKeyboardButton("🗑 Удалить правило", callback_data=f"rule_del_{rid}")],
        [InlineKeyboardButton("⬅️ К списку", callback_data="rule_list")],
    ])


async def _plan_and_notify(query, context, rule_id: int) -> None:
    """Сразу разложить сегодняшнюю квоту правила (не ждать ночного планировщика)."""
    r = db.get_rule(rule_id)
    if not r or not r["enabled"]:
        return
    n = scheduler.plan_rule(context.job_queue, r)
    if n:
        try:
            await context.bot.send_message(query.message.chat_id, f"🗓 Запланировано на сегодня: {n} видео.")
        except Exception:
            pass


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db.ensure_user(update.effective_user.id)
    await update.message.reply_text("Твои правила публикации:", reply_markup=rules_kb(update.effective_user.id))
    return ConversationHandler.END


async def rules_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки правил, не требующие ввода текста (вне разговора)."""
    query = update.callback_query
    data = query.data
    telegram_id = update.effective_user.id

    if data == "rule_list":
        await query.answer()
        await query.edit_message_text("Твои правила публикации:", reply_markup=rules_kb(telegram_id))
        return

    if data == "rule_add":
        await query.answer()
        sources = db.get_sources(telegram_id)
        groups = db.get_groups(telegram_id)
        if not sources or not groups:
            await query.edit_message_text(
                "Сначала добавь хотя бы один источник и одну группу "
                f"({BTN_SOURCES}, {BTN_GROUPS})."
            )
            return
        rows = [[InlineKeyboardButton(s["name"], callback_data=f"rule_src_{s['id']}")] for s in sources]
        await query.edit_message_text("Выбери источник:", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("rule_src_"):
        await query.answer()
        context.user_data["new_rule_source"] = int(data.rsplit("_", 1)[1])
        groups = db.get_groups(telegram_id)
        rows = [[InlineKeyboardButton(f"{g['name']} (id {g['vk_group_id']})",
                                      callback_data=f"rule_grp_{g['id']}")] for g in groups]
        await query.edit_message_text("Теперь выбери группу:", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("rule_grp_"):
        await query.answer()
        source_id = context.user_data.get("new_rule_source")
        group_id = int(data.rsplit("_", 1)[1])
        if not source_id:
            await query.edit_message_text("Что-то пошло не так, начни заново.",
                                          reply_markup=rules_kb(telegram_id))
            return
        try:
            rid = db.add_rule(telegram_id, source_id, group_id)
        except Exception:
            await query.edit_message_text(
                "⚠️ Такое правило (этот источник → эта группа) уже существует.",
                reply_markup=rules_kb(telegram_id),
            )
            return
        r = db.get_rule(rid)
        await query.edit_message_text(rule_detail_text(r), reply_markup=rule_detail_kb(r))
        # Правило создано включённым — сразу раскладываем сегодняшнюю квоту.
        await _plan_and_notify(query, context, rid)
        return

    if data.startswith("rule_open_"):
        await query.answer()
        r = db.get_rule(int(data.rsplit("_", 1)[1]))
        if not r:
            await query.edit_message_text("Правило не найдено.", reply_markup=rules_kb(telegram_id))
            return
        await query.edit_message_text(rule_detail_text(r), reply_markup=rule_detail_kb(r))
        return

    if data.startswith("rule_toggle_"):
        await query.answer()
        rid = int(data.rsplit("_", 1)[1])
        r = db.get_rule(rid)
        was_enabled = r["enabled"]
        db.update_rule(rid, enabled=0 if was_enabled else 1)
        r = db.get_rule(rid)
        await query.edit_message_text(rule_detail_text(r), reply_markup=rule_detail_kb(r))
        if not was_enabled:  # только что включили — разложим сегодняшнюю квоту
            await _plan_and_notify(query, context, rid)
        return

    if data.startswith("rule_now_"):
        rid = int(data.rsplit("_", 1)[1])
        ok, msg = scheduler.schedule_test_now(context.job_queue, rid)
        await query.answer(msg[:200], show_alert=True)
        return

    if data.startswith("rule_order_"):
        await query.answer()
        rid = int(data.rsplit("_", 1)[1])
        r = db.get_rule(rid)
        db.update_rule(rid, order_dir="new" if r["order_dir"] == "old" else "old")
        r = db.get_rule(rid)
        await query.edit_message_text(rule_detail_text(r), reply_markup=rule_detail_kb(r))
        return

    if data.startswith("rule_del_"):
        await query.answer("Удалено")
        db.delete_rule(int(data.rsplit("_", 1)[1]))
        await query.edit_message_text("Твои правила публикации:", reply_markup=rules_kb(telegram_id))
        return

    await query.answer()


# Редактирование текстовых полей правила (число/слоты/описание/длительность).
_RULE_FIELD_PROMPTS = {
    "nday": "Сколько видео в день публиковать? Пришли число (например 3):",
    "slots": "Во сколько публиковать? Пришли часы МСК через запятую (например 9,15,20):",
    "desc": "Пришли текст описания к записям (или «-», чтобы убрать):",
    "dur": ("Фильтр по длительности в секундах. Пришли «min max» (например «30 60»).\n"
            "Только минимум: «30 -». Только максимум: «- 60». Убрать фильтр: «-»."),
}


async def rule_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    # rule_<field>_<id>
    _, field, rid = query.data.split("_")
    context.user_data["edit_rule_id"] = int(rid)
    context.user_data["edit_rule_field"] = field
    await query.edit_message_text(_RULE_FIELD_PROMPTS[field])
    return RULE_EDIT_VALUE


async def rule_edit_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    rid = context.user_data.get("edit_rule_id")
    field = context.user_data.get("edit_rule_field")
    text = update.message.text.strip()
    if rid is None or field is None:
        await update.message.reply_text("Сессия истекла, открой правило заново.")
        return ConversationHandler.END

    error = None
    if field == "nday":
        if not text.isdigit() or int(text) < 1:
            error = "Нужно целое число ≥ 1."
        else:
            db.update_rule(rid, videos_per_day=int(text))
    elif field == "slots":
        hours = scheduler.parse_slots(text)
        if not hours:
            error = "Не понял слоты. Пример: 9,15,20 (часы 0–23)."
        else:
            db.update_rule(rid, slots=",".join(str(h) for h in hours))
    elif field == "desc":
        db.update_rule(rid, description=None if text == "-" else text)
    elif field == "dur":
        lo, hi, err = _parse_duration_input(text)
        if err:
            error = err
        else:
            db.update_rule(rid, min_duration=lo, max_duration=hi)

    if error:
        await update.message.reply_text(f"❌ {error} Попробуй ещё раз или /cancel.")
        return RULE_EDIT_VALUE

    r = db.get_rule(rid)
    await update.message.reply_text(rule_detail_text(r), reply_markup=rule_detail_kb(r))
    context.user_data.pop("edit_rule_id", None)
    context.user_data.pop("edit_rule_field", None)
    return ConversationHandler.END


def _parse_duration_input(text: str) -> tuple[int | None, int | None, str | None]:
    """'30 60' | '30 -' | '- 60' | '-' -> (min, max, error)."""
    if text == "-":
        return None, None, None
    parts = text.split()
    if len(parts) != 2:
        return None, None, "Формат: «min max», например «30 60» (или '-' для пусто)."

    def one(tok):
        if tok == "-":
            return None, None
        if tok.isdigit():
            return int(tok), None
        return None, "не число"

    lo, e1 = one(parts[0])
    hi, e2 = one(parts[1])
    if e1 or e2:
        return None, None, "Границы должны быть числами или '-'."
    if lo is not None and hi is not None and lo > hi:
        return None, None, "Минимум больше максимума."
    return lo, hi, None


# ─── Статус ───────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = update.effective_user.id
    db.ensure_user(telegram_id)
    rules = db.get_rules(telegram_id)
    if not rules:
        await update.message.reply_text("Пока нет ни одного правила. Добавь через 📋 Правила.")
        return

    lines = ["📊 Статус автопубликации:\n"]
    for r in rules:
        mark = "🟢" if r["enabled"] else "⚪"
        published = db.count_published(r["id"])
        queued = len(db.get_scheduled_ids(r["id"]))
        # Сколько ещё доступно к публикации (может быть медленно на больших базах).
        available = "?"
        try:
            exclude = db.get_published_ids(r["id"]) | db.get_scheduled_ids(r["id"])
            src = db.get_source(r["source_id"])
            cands = fetch_candidates(
                src["db_path"], username=src["username"],
                min_duration=r["min_duration"], max_duration=r["max_duration"],
                order_dir=r["order_dir"], exclude_ids=exclude,
            )
            available = str(len(cands))
        except SourceError:
            available = "нет доступа к базе"
        lines.append(
            f"{mark} {r['source_name']} → {r['group_name']}\n"
            f"    {r['videos_per_day']}/день, слоты {r['slots']} · "
            f"опубл. {published} · в очереди {queued} · осталось {available}"
        )
    await update.message.reply_text("\n".join(lines))


# ─── Ошибки / админ-панель ────────────────────────────────────────────────────

def _is_admin(telegram_id: int) -> bool:
    return telegram_id in ADMIN_IDS


def _fmt_ts(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")


def _err_btn_label(e) -> str:
    ts = datetime.fromtimestamp(e["created_at"], tz=MOSCOW_TZ).strftime("%d.%m %H:%M")
    code = f"VK{e['error_code']}" if e["error_code"] is not None else (e["stage"] or "ошибка")
    return f"{ts} · {e['platform'] or '—'} · {code}"[:60]


def _kb(rows):
    return InlineKeyboardMarkup(rows) if rows else None


def _pager_rows(page: int, total: int, prefix: str):
    pages = (total + ERRORS_PAGE_SIZE - 1) // ERRORS_PAGE_SIZE
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"{prefix}_{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"{prefix}_{page + 1}"))
    return [nav] if nav else []


def _own_list_view(telegram_id: int, page: int):
    total = db.count_errors(telegram_id)
    errors = db.get_errors(telegram_id, ERRORS_PAGE_SIZE, page * ERRORS_PAGE_SIZE)
    rows = [[InlineKeyboardButton(_err_btn_label(e), callback_data=f"err_v_{e['id']}")] for e in errors]
    rows += _pager_rows(page, total, "err_self")
    return f"📋 Твои ошибки: {total}", _kb(rows)


def _admin_users_view(page: int):
    total = db.count_users_with_errors()
    if not total:
        return "🛠 Админ-панель ошибок\n\n✅ Ошибок пока нет.", None
    users = db.get_users_with_errors(ERRORS_PAGE_SIZE, page * ERRORS_PAGE_SIZE)
    rows = [[InlineKeyboardButton(
        f"👤 {u['telegram_id']} · {u['cnt']} ошиб. · {_fmt_ts(u['last_at'])}",
        callback_data=f"err_u_{u['telegram_id']}_0")] for u in users]
    rows += _pager_rows(page, total, "err_au")
    return f"🛠 Админ-панель ошибок\nПользователей с ошибками: {total}", _kb(rows)


def _admin_user_errors_view(target_id: int, page: int):
    total = db.count_errors(target_id)
    errors = db.get_errors(target_id, ERRORS_PAGE_SIZE, page * ERRORS_PAGE_SIZE)
    rows = [[InlineKeyboardButton(_err_btn_label(e), callback_data=f"err_v_{e['id']}")] for e in errors]
    rows += _pager_rows(page, total, f"err_u_{target_id}")
    rows.append([InlineKeyboardButton("⬅️ К пользователям", callback_data="err_au_0")])
    return f"👤 Пользователь {target_id}\nОшибок: {total}", InlineKeyboardMarkup(rows)


def _detail_view(e, viewer_is_admin: bool):
    grp = e["vk_group_name"] or "—"
    if e["vk_group_id"]:
        grp += f" (id {e['vk_group_id']})"
    code = f"VK {e['error_code']}" if e["error_code"] is not None else "—"
    text = (
        f"🆔 Ошибка #{e['id']}\n🕒 {_fmt_ts(e['created_at'])} МСК\n"
        f"📍 Этап: {e['stage'] or '—'}\n🎬 Платформа: {e['platform'] or '—'}\n"
        f"👥 Группа: {grp}\n🔢 Код: {code}\n🔗 {e['url'] or '—'}\n\n💬 {e['message'] or '—'}"
    )
    tb = e["traceback"]
    if tb:
        budget = 3500 - len(text)
        if budget > 200:
            snippet = tb if len(tb) <= budget else "…(обрезано)…\n" + tb[-budget:]
            text += f"\n\n🧩 Traceback:\n{snippet}"
    if len(text) > 4096:
        text = text[:4000] + "\n…(обрезано)"
    rows = [[InlineKeyboardButton("📄 Полный traceback файлом", callback_data=f"err_tb_{e['id']}")]]
    rows.append([InlineKeyboardButton(
        "⬅️ Назад", callback_data=f"err_u_{e['telegram_id']}_0" if viewer_is_admin else "err_self_0")])
    return text, InlineKeyboardMarkup(rows)


async def cmd_errors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    db.ensure_user(uid)
    if _is_admin(uid):
        text, kb = _admin_users_view(0)
        await update.message.reply_text(text, reply_markup=kb)
        return
    if db.count_errors(uid) == 0:
        await update.message.reply_text("✅ У тебя нет залогированных ошибок.")
        return
    text, kb = _own_list_view(uid, 0)
    await update.message.reply_text(text, reply_markup=kb)


async def errors_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    uid = update.effective_user.id
    is_admin = _is_admin(uid)
    parts = query.data.split("_")
    kind = parts[1]

    if kind == "self":
        await query.answer()
        text, kb = _own_list_view(uid, int(parts[2]))
        await query.edit_message_text(text, reply_markup=kb)
    elif kind == "au":
        if not is_admin:
            await query.answer("Недостаточно прав", show_alert=True); return
        await query.answer()
        text, kb = _admin_users_view(int(parts[2]))
        await query.edit_message_text(text, reply_markup=kb)
    elif kind == "u":
        if not is_admin:
            await query.answer("Недостаточно прав", show_alert=True); return
        await query.answer()
        text, kb = _admin_user_errors_view(int(parts[2]), int(parts[3]))
        await query.edit_message_text(text, reply_markup=kb)
    elif kind == "v":
        e = db.get_error(int(parts[2]))
        if not e:
            await query.answer("Не найдено", show_alert=True); return
        if not is_admin and e["telegram_id"] != uid:
            await query.answer("Недостаточно прав", show_alert=True); return
        await query.answer()
        text, kb = _detail_view(e, is_admin)
        await query.edit_message_text(text, reply_markup=kb)
    elif kind == "tb":
        e = db.get_error(int(parts[2]))
        if not e:
            await query.answer("Не найдено", show_alert=True); return
        if not is_admin and e["telegram_id"] != uid:
            await query.answer("Недостаточно прав", show_alert=True); return
        await query.answer()
        content = e["traceback"] or e["message"] or "—"
        bio = BytesIO(content.encode("utf-8"))
        bio.name = f"error_{e['id']}.txt"
        await query.message.reply_document(document=bio, filename=f"error_{e['id']}.txt")


async def _cleanup_errors_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    import asyncio
    loop = asyncio.get_running_loop()
    deleted = await loop.run_in_executor(None, db.cleanup_old_errors, ERROR_RETENTION_DAYS)
    if deleted:
        logger.info("Очистка логов ошибок: удалено %s записей", deleted)


# ─── /cancel ──────────────────────────────────────────────────────────────────

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отменено.", reply_markup=main_keyboard())
    return ConversationHandler.END


# ─── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    if not TELEGRAM_TOKEN:
        raise SystemExit("Не задан TELEGRAM_TOKEN в .env")

    db.init_db()

    persistence = PicklePersistence(filepath=os.path.join(db.DATA_DIR, "bot_state.pickle"))
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .persistence(persistence)
        .post_init(scheduler.restore_and_schedule)
        .concurrent_updates(True)
        .connect_timeout(30.0).read_timeout(30.0).write_timeout(30.0).pool_timeout(30.0)
        .get_updates_connect_timeout(30.0).get_updates_read_timeout(30.0)
        .build()
    )

    token_conv = ConversationHandler(
        entry_points=[
            CommandHandler("settoken", cmd_settoken),
            CallbackQueryHandler(settoken_from_button, pattern=r"^settoken_change$"),
        ],
        states={TOKEN_WAIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_token)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=300, name="token_conv", persistent=False, allow_reentry=True,
    )

    groups_conv = ConversationHandler(
        entry_points=[
            CommandHandler("groups", cmd_groups),
            CallbackQueryHandler(groups_button, pattern=r"^(g_add|g_del_|g_rename_|noop$)"),
        ],
        states={
            G_ADD_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, groups_add_id)],
            G_ADD_CONFIRM: [CallbackQueryHandler(groups_add_confirm, pattern=r"^g_(confirmname|manualname)$")],
            G_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, groups_add_name)],
            G_RENAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, groups_rename)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=300, name="groups_conv", persistent=False, allow_reentry=True,
    )

    sources_conv = ConversationHandler(
        entry_points=[
            CommandHandler("sources", cmd_sources),
            CallbackQueryHandler(sources_button, pattern=r"^(src_add|src_del_|src_refresh_)"),
        ],
        states={
            SRC_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, sources_name)],
            SRC_TYPE: [CallbackQueryHandler(sources_type, pattern=r"^src_type_(account|db)$")],
            SRC_ACCOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, sources_account)],
            SRC_PATH: [MessageHandler(filters.TEXT & ~filters.COMMAND, sources_path)],
            SRC_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, sources_user)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=300, name="sources_conv", persistent=False, allow_reentry=True,
    )

    rule_edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(rule_edit_start, pattern=r"^rule_(nday|slots|desc|dur)_\d+$")],
        states={RULE_EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, rule_edit_save)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=300, name="rule_edit_conv", persistent=False, allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("errors", cmd_errors))
    app.add_handler(CommandHandler("admin", cmd_errors))
    app.add_handler(CallbackQueryHandler(errors_callback, pattern=r"^err_"))
    # Кнопки меню — до диалогов.
    app.add_handler(MessageHandler(filters.Text(MENU_BUTTON_TEXTS), main_menu_button))
    app.add_handler(CallbackQueryHandler(handle_token_delete, pattern=r"^settoken_delete$"))
    app.add_handler(token_conv)
    app.add_handler(groups_conv)
    app.add_handler(sources_conv)
    app.add_handler(rule_edit_conv)
    # Правила: остальные кнопки (без ввода текста).
    app.add_handler(CallbackQueryHandler(
        rules_button,
        pattern=r"^(rule_list|rule_add|rule_src_|rule_grp_|rule_open_|rule_toggle_|rule_order_|rule_now_|rule_del_)",
    ))

    scheduler.register_jobs(app)
    app.job_queue.run_repeating(
        _cleanup_errors_job, interval=timedelta(days=ERROR_CLEANUP_INTERVAL_DAYS),
        first=timedelta(minutes=1), name="cleanup_errors",
    )

    logger.info("Автопостер запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
