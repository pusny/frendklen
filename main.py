import asyncio
import os
import random
import logging
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ContentType
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, BotCommand,
)
import aiosqlite
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
SUPERADMIN_ID = int(os.getenv("SUPERADMIN_ID", "0"))
PAYMENT_LINK = os.getenv("PAYMENT_LINK", "")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@support")
DB_PATH = os.getenv("DB_PATH", "data/data.db")

os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

PRICES = {
    "vip_low": (300, "VIP Низкий, 1 месяц"),
    "vip_mid": (500, "VIP Средний, 1 месяц"),
    "vip_high": (700, "VIP Высокий, 1 месяц"),
    "chars_600": (40, "Символы до 600"),
    "unlimited_likes": (200, "Снятие лимита лайков (1 день)"),
    "prev_anketa": (80, "Возврат предыдущей анкеты"),
    "second_media": (60, "Второе фото/видео"),
    "incognito_day": (30, "Чат инкогнито (1 день)"),
}

VIP_LIMITS = {
    0: {"likes": 20, "chars": 300, "photos": 1, "video": False, "priority": 0, "prev": False},
    1: {"likes": 40, "chars": 600, "photos": 1, "video": False, "priority": 1, "prev": False},
    2: {"likes": 60, "chars": 800, "photos": 2, "video": False, "priority": 2, "prev": False},
    3: {"likes": 999999, "chars": 1000, "photos": 2, "video": True, "priority": 3, "prev": True},
}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

router = Router()
VERIFY_WORDS = ["КЛЕН", "ЛИСТ", "БУКЕТ", "ОСЕНЬ", "ВЕТЕР", "ЗАКАТ"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
 tg_id INTEGER PRIMARY KEY, username TEXT, phone TEXT,
 status TEXT DEFAULT 'new', banned INTEGER DEFAULT 0, ban_reason TEXT,
 vip_level INTEGER DEFAULT 0, vip_until TEXT,
 likes_left INTEGER DEFAULT 20, likes_reset_at TEXT,
 photos_used INTEGER DEFAULT 0, video_used INTEGER DEFAULT 0,
 chars_limit INTEGER DEFAULT 300, prev_anketa_id INTEGER,
 attempts_left INTEGER DEFAULT 3, created_at TEXT
);
CREATE TABLE IF NOT EXISTS profiles(
 id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER UNIQUE,
 name TEXT, age INTEGER, pronouns TEXT, city TEXT, diagnosis TEXT,
 reason TEXT, hobby TEXT, photo1 TEXT, photo2 TEXT, video TEXT,
 approved INTEGER DEFAULT 0, created_at TEXT
);
CREATE TABLE IF NOT EXISTS likes(from_tg INTEGER, to_tg INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS purchases(id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, item TEXT, amount INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, status TEXT DEFAULT 'open', created_at TEXT);
CREATE TABLE IF NOT EXISTS ticket_msgs(id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER, from_admin INTEGER, text TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY AUTOINCREMENT, reporter INTEGER, target INTEGER, reason TEXT, created_at TEXT);
"""

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()

def now():
    return datetime.utcnow().isoformat()

async def get_user(tg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

async def upsert_user(tg_id: int, **kw):
    u = await get_user(tg_id)
    if not u:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO users(tg_id, status, likes_left, likes_reset_at, chars_limit, attempts_left, created_at) VALUES(?,?,?,?,?,?,?)",
                (tg_id, "new", 20, now(), 300, 3, now()),
            )
            await db.commit()
        u = await get_user(tg_id)
    if kw:
        sets = ", ".join(f"{k}=?" for k in kw)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(f"UPDATE users SET {sets} WHERE tg_id=?", (*kw.values(), tg_id))
            await db.commit()
        u = await get_user(tg_id)
    return u

async def vip_limits(u):
    lvl = u.get("vip_level", 0) or 0
    if u.get("vip_until"):
        try:
            if datetime.fromisoformat(u["vip_until"]) < datetime.utcnow():
                lvl = 0
                await upsert_user(u["tg_id"], vip_level=0, vip_until=None)
        except Exception:
            pass
    return VIP_LIMITS.get(lvl, VIP_LIMITS[0])

async def refresh_likes(tg_id: int):
    u = await get_user(tg_id)
    if not u:
        return
    lim = await vip_limits(u)
    reset = u.get("likes_reset_at")
    if not reset or datetime.fromisoformat(reset) < datetime.utcnow() - timedelta(days=1):
        await upsert_user(tg_id, likes_left=lim["likes"], likes_reset_at=now())

class S(StatesGroup):
    contact = State()
    video = State()
    form = State()
    support = State()
    report_reason = State()
    admin_wait_id = State()
    admin_wait_amount = State()
    admin_wait_comment = State()

@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    u = await upsert_user(m.from_user.id, username=m.from_user.username or "")
    if u["banned"]:
        reason = u.get("ban_reason") or "не указана"
        return await m.answer(f"🚫 Доступ к боту заблокирован.\nПричина: {reason}")
    if u["status"] == "active":
        return await main_menu(m)
    if u["status"] == "new":
        await state.set_state(S.contact)
        kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📱 Поделиться номером", request_contact=True)]],
            resize_keyboard=True, one_time_keyboard=True)
        return await m.answer(
            "🌸 Добро пожаловать в <b>Кленовый букетик</b>!\n\n"
            "Для регистрации поделитесь номером телефона — кнопка ниже.\n"
            "🔒 Номер остаётся внутри чата с ботом, никуда не передаётся, "
            "используется только для защиты от мультиаккаунтов и подтверждения возраста.",
            reply_markup=kb, parse_mode="HTML")
    if u["status"] == "awaiting_video":
        await state.set_state(S.video)
        return await ask_video(m)
    if u["status"] == "awaiting_form":
        await state.set_state(S.form)
        return await ask_form(m, u)
    if u["status"] == "pending_review":
        return await m.answer("⏳ Ваша анкета на модерации. Ожидайте.")

@router.message(S.contact)
async def on_contact(m: Message, state: FSMContext):
    if not m.contact or m.contact.user_id != m.from_user.id:
        return await m.answer("Пожалуйста, воспользуйтесь кнопкой ниже.", reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📱 Поделиться номером", request_contact=True)]],
            resize_keyboard=True, one_time_keyboard=True))
    await upsert_user(m.from_user.id, phone=m.contact.phone_number, status="awaiting_video", attempts_left=3)
    await state.set_state(S.video)
    await ask_video(m)

async def ask_video(m: Message):
    words = random.sample(VERIFY_WORDS, 3)
    await m.answer(
        "✅ Номер принят.\n\n"
        f"📹 Теперь пришлите <b>видео-кружок</b> (нажмите на скрепку → «Видео-кружок»), "
        f"на котором ваше лицо видно чётко, без бликов, не размазано.\n\n"
        f"Произнесите вслух: <b>{', '.join(words)}</b>\n\n"
        "Это нужно для подтверждения возраста. У вас 3 попытки.",
        parse_mode="HTML", reply_markup=ReplyKeyboardRemove())

@router.message(S.video)
async def on_video(m: Message, state: FSMContext):
    u = await get_user(m.from_user.id)
    if m.content_type != ContentType.VIDEO_NOTE:
        left = (u["attempts_left"] or 1) - 1
        if left <= 0:
            await upsert_user(m.from_user.id, banned=1, ban_reason="Не прошёл верификацию")
            await state.clear()
            return await m.answer("🚫 Вы исчерпали попытки. Доступ заблокирован.")
        await upsert_user(m.from_user.id, attempts_left=left)
        return await m.answer(f"❌ Вы прислали не видео-кружок. Осталось попыток: {left}. "
                              "После этого доступ будет заблокирован.")
    await upsert_user(m.from_user.id, status="awaiting_form", video_used=1)
    await state.set_state(S.form)
    await m.answer("✅ Видео принято.")
    await ask_form(m, await get_user(m.from_user.id), video_note_file_id=m.video_note.file_id)

FORM_TEMPLATE = (
    "<code>1. Имя/Псевдоним:\n"
    "2. Возраст:\n"
    "3. Местоимения:\n"
    "4. Город:\n"
    "5. Диагноз:\n"
    "6. Зачем пришёл/ла:\n"
    "7. Хобби:</code>"
)

async def ask_form(m: Message, u: dict, video_note_file_id: Optional[str] = None):
    if video_note_file_id:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR IGNORE INTO profiles(tg_id, video, approved, created_at) VALUES(?,?,0,?)",
                (m.from_user.id, video_note_file_id, now()))
            await db.commit()
    lim = await vip_limits(u)
    chars = max(u.get("chars_limit") or 300, lim["chars"])
    await m.answer(
        "📝 Заполните анкету, скопировав шаблон тапом ниже:\n\n"
        f"{FORM_TEMPLATE}\n\n"
        f"Лимит символов: <b>{chars}</b>.\n"
        "По желанию прикрепите 1 фото в этом же сообщении.\n"
        "Возраст от 18 лет.",
        parse_mode="HTML")

def parse_form(text: str):
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
    if len(lines) != 7:
        return None
    out = {}
    keys = ["name", "age", "pronouns", "city", "diagnosis", "reason", "hobby"]
    for i, key in enumerate(keys):
        line = lines[i]
        if not line.startswith(f"{i+1}."):
            return None
        val = line.split(".", 1)[1].strip().lstrip(":").strip()
        if not val:
            return None
        out[key] = val
    try:
        age = int(out["age"])
    except Exception:
        return None
    if not (18 <= age <= 99):
        return None
    return out

@router.message(S.form)
async def on_form(m: Message, state: FSMContext):
    u = await get_user(m.from_user.id)
    if not m.text:
        return await m.answer("Пришлите анкету текстом по шаблону.")
    data = parse_form(m.text)
    if not data:
        return await m.answer("❌ Анкета заполнена не по форме. Заполните точно по шаблону.\n\n" + FORM_TEMPLATE, parse_mode="HTML")
    lim = await vip_limits(u)
    chars_limit = max(u.get("chars_limit") or 300, lim["chars"])
    for k in ["reason", "hobby", "diagnosis"]:
        if len(data[k]) > chars_limit:
            return await m.answer(f"❌ Поле «{k}» превышает лимит {chars_limit} символов.")
    photo1 = None
    if m.photo:
        photo1 = m.photo[-1].file_id
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE profiles SET name=?, age=?, pronouns=?, city=?, diagnosis=?, reason=?, hobby=?, photo1=COALESCE(?,photo1), approved=0 WHERE tg_id=?",
            (data["name"], data["age"], data["pronouns"], data["city"], data["diagnosis"],
             data["reason"], data["hobby"], photo1, m.from_user.id))
        await db.commit()
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM profiles WHERE tg_id=?", (m.from_user.id,))
        prof = dict(await cur.fetchone())
    await upsert_user(m.from_user.id, status="pending_review")
    await state.clear()
    await m.answer("📨 Анкета отправлена на проверку. Ожидайте решения модератора.")
    await send_to_admin_chat(m.bot, prof, u)

async def send_to_admin_chat(bot: Bot, prof: dict, u: dict):
    text = (
        f"🆕 <b>Новая анкета</b>\n"
        f"tg_id: <code>{prof['tg_id']}</code>\n"
        f"📞 {u.get('phone')}\n\n"
        f"1. {prof['name']}\n2. {prof['age']}\n3. {prof['pronouns']}\n"
        f"4. {prof['city']}\n5. {prof['diagnosis']}\n6. {prof['reason']}\n7. {prof['hobby']}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"adm:approve:{prof['tg_id']}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm:reject:{prof['tg_id']}")],
        [InlineKeyboardButton(text="✏️ Переделать", callback_data=f"adm:rework:{prof['tg_id']}")],
    ])
    if prof.get("video"):
        await bot.send_video_note(ADMIN_CHAT_ID, prof["video"])
    if prof.get("photo1"):
        await bot.send_photo(ADMIN_CHAT_ID, prof["photo1"])
    await bot.send_message(ADMIN_CHAT_ID, text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("adm:"))
async def adm_verify(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != SUPERADMIN_ID:
        return await cb.answer("Только для супер-админа", show_alert=True)
    _, action, tid = cb.data.split(":")
    tid = int(tid)
    if action == "approve":
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE profiles SET approved=1 WHERE tg_id=?", (tid,))
            await db.execute("UPDATE users SET status='active', banned=0 WHERE tg_id=?", (tid,))
            await db.commit()
        await cb.bot.send_message(tid, "🎉 Ваша анкета одобрена! Добро пожаловать в «Кленовый букетик».")
        await cb.message.edit_reply_markup(reply_markup=None)
    elif action == "reject":
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET status='banned', banned=1, ban_reason='Отклонено модератором' WHERE tg_id=?", (tid,))
            await db.commit()
        await cb.bot.send_message(tid, "🚫 Ваша анкета отклонена. Доступ к боту заблокирован.")
        await cb.message.edit_reply_markup(reply_markup=None)
    elif action == "rework":
        await state.set_state(S.admin_wait_comment)
        await state.update_data(rework_tid=tid)
        await cb.message.answer(f"Введите комментарий для пользователя {tid}:")
    await cb.answer()

@router.message(S.admin_wait_comment)
async def rework_comment(m: Message, state: FSMContext):
    if m.from_user.id != SUPERADMIN_ID:
        return
    data = await state.get_data()
    tid = data.get("rework_tid")
    await state.clear()
    if not tid:
        return await m.answer("Нет активной заявки.")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE profiles SET approved=0 WHERE tg_id=?", (tid,))
        await db.execute("UPDATE users SET status='awaiting_form' WHERE tg_id=?", (tid,))
        await db.commit()
    await m.bot.send_message(tid, f"✏️ Вашу анкету нужно переделать.\nКомментарий: {m.text}\n\nОтправьте /start чтобы заполнить заново.")
    await m.answer("Комментарий отправлен пользователю.")

def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗂 Анкеты", callback_data="menu:ankety")],
        [InlineKeyboardButton(text="💬 Чат инкогнито", callback_data="menu:incognito")],
        [InlineKeyboardButton(text="🛒 Магазин", callback_data="menu:shop")],
        [InlineKeyboardButton(text="🆘 Поддержка", callback_data="menu:support")],
        [InlineKeyboardButton(text="👤 Моя анкета", callback_data="menu:profile")],
    ])

async def main_menu(m: Message):
    await m.answer("🏠 <b>Главное меню</b>\nВыберите раздел:", reply_markup=main_menu_kb(), parse_mode="HTML")

@router.callback_query(F.data == "menu:back")
async def back_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("🏠 <b>Главное меню</b>", reply_markup=main_menu_kb(), parse_mode="HTML")
    await cb.answer()

@router.callback_query(F.data == "menu:ankety")
async def ankety(cb: CallbackQuery):
    await refresh_likes(cb.from_user.id)
    u = await get_user(cb.from_user.id)
    if u["likes_left"] <= 0:
        return await cb.answer("Лимит лайков исчерпан. Купите доп в магазине.", show_alert=True)
    await show_next_profile(cb, 0)

async def show_next_profile(cb: CallbackQuery, offset: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT p.*, u.vip_level FROM profiles p JOIN users u ON u.tg_id=p.tg_id "
            "WHERE p.approved=1 AND p.tg_id!=? ORDER BY u.vip_level DESC, p.id DESC LIMIT 1 OFFSET ?",
            (cb.from_user.id, offset))
        prof = await cur.fetchone()
    if not prof:
        return await cb.answer("Больше анкет нет.", show_alert=True)
    prof = dict(prof)
    text = (f"<b>{prof['name']}</b>, {prof['age']}\n"
            f"📍 {prof['city']} · {prof['pronouns']}\n"
            f"🩺 {prof['diagnosis']}\n"
            f"💭 {prof['reason']}\n"
            f"🎨 {prof['hobby']}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❤️ Лайк", callback_data=f"like:{prof['tg_id']}"),
         InlineKeyboardButton(text="➡️ Скип", callback_data=f"skip:{offset+1}")],
        [InlineKeyboardButton(text="🚨 Жалоба", callback_data=f"report:{prof['tg_id']}")],
        [InlineKeyboardButton(text="⬅️ Меню", callback_data="menu:back")],
    ])
    if prof.get("photo1"):
        await cb.message.answer_photo(prof["photo1"], caption=text, reply_markup=kb, parse_mode="HTML")
    else:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("skip:"))
async def skip(cb: CallbackQuery):
    offset = int(cb.data.split(":")[1])
    await cb.answer("Скип")
    await show_next_profile(cb, offset)

@router.callback_query(F.data.startswith("like:"))
async def like(cb: CallbackQuery):
    target = int(cb.data.split(":")[1])
    await refresh_likes(cb.from_user.id)
    u = await get_user(cb.from_user.id)
    if u["likes_left"] <= 0:
        return await cb.answer("Лимит исчерпан.", show_alert=True)
    await upsert_user(cb.from_user.id, likes_left=u["likes_left"] - 1)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO likes(from_tg, to_tg, created_at) VALUES(?,?,?)",
                         (cb.from_user.id, target, now()))
        await db.commit()
        cur = await db.execute("SELECT 1 FROM likes WHERE from_tg=? AND to_tg=?",
                               (target, cb.from_user.id))
        mutual = await cur.fetchone()
    if mutual:
        try:
            await cb.bot.send_message(target, "💞 Взаимный лайк! Зайдите в раздел «Чат инкогнито».")
        except Exception:
            pass
        await cb.message.answer("💞 Взаимно! Человек получил уведомление.")
    await cb.answer("❤️ Лайк отправлен")
    await show_next_profile(cb, 0)

@router.callback_query(F.data == "menu:incognito")
async def incognito(cb: CallbackQuery):
    u = await get_user(cb.from_user.id)
    if u["vip_level"] == 0:
        return await cb.answer("Доступ по VIP или купите дневной доп.", show_alert=True)
    await cb.message.answer("💬 Отправьте сообщение — оно уйдёт анонимно человеку, с которым у вас взаимный лайк.")
    await cb.answer()

SHOP_ITEMS = {
    "vip_low": "VIP Низкий: 40 лайков/день, 600 символов, приоритет выше. 300₽/мес",
    "vip_mid": "VIP Средний: 60 лайков, 800 символов, 2 фото. 500₽/мес",
    "vip_high": "VIP Высокий: ∞ лайки, 1000 символов, видео, откат. 700₽/мес",
    "chars_600": "Лимит символов до 600. 40₽",
    "unlimited_likes": "Снятие лимита лайков на день. 200₽",
    "prev_anketa": "Возврат предыдущей анкеты. 80₽",
    "second_media": "Второе фото/видео. 60₽",
    "incognito_day": "Чат инкогнито на день. 30₽",
}

@router.callback_query(F.data == "menu:shop")
async def shop(cb: CallbackQuery):
    rows = [[InlineKeyboardButton(text=k, callback_data=f"buyinfo:{k}")] for k in SHOP_ITEMS]
    rows.append([InlineKeyboardButton(text="⬅️ Меню", callback_data="menu:back")])
    await cb.message.edit_text("🛒 Магазин\nВыберите товар:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@router.callback_query(F.data.startswith("buyinfo:"))
async def buy_info(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    text = SHOP_ITEMS.get(key, "—")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Купить", callback_data=f"buy:{key}")],
        [InlineKeyboardButton(text="⬅️ Магазин", callback_data="menu:shop")],
    ])
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data.startswith("buy:"))
async def buy(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    price, name = PRICES[key]
    await cb.message.edit_text(
        f"Оплата: <b>{name}</b> — {price}₽.\n\n"
        f"Перейдите по ссылке и оплатите: {PAYMENT_LINK}\n"
        "После оплаты администратор выдаст доступ вручную.",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:shop")]]))
    await cb.bot.send_message(SUPERADMIN_ID,
        f"💰 Покупка: tg={cb.from_user.id}, {name}, {price}₽")
    await cb.answer()

@router.callback_query(F.data == "menu:support")
async def support(cb: CallbackQuery, state: FSMContext):
    await state.set_state(S.support)
    await cb.message.answer(f"🆘 Опишите проблему одним сообщением. Поддержка: {SUPPORT_USERNAME}")
    await cb.answer()

@router.message(S.support)
async def support_msg(m: Message, state: FSMContext):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("INSERT INTO tickets(tg_id, status, created_at) VALUES(?,?,?)",
                               (m.from_user.id, "open", now()))
        tid = cur.lastrowid
        await db.execute("INSERT INTO ticket_msgs(ticket_id, from_admin, text, created_at) VALUES(?,?,?,?)",
                         (tid, 0, m.text or "[медиа]", now()))
        await db.commit()
    await m.bot.send_message(SUPERADMIN_ID,
        f"🆘 Новый тикет #{tid} от {m.from_user.id}\n\n{m.text}\n\nОтвет: /reply {tid} <текст>")
    await state.clear()
    await m.answer("✅ Обращение отправлено. Ожидайте ответа.")

@router.message(Command("reply"))
async def reply_ticket(m: Message):
    if m.from_user.id != SUPERADMIN_ID:
        return
    parts = m.text.split(" ", 2)
    if len(parts) < 3:
        return await m.answer("Формат: /reply <ticket_id> <текст>")
    tid, txt = int(parts[1]), parts[2]
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT tg_id FROM tickets WHERE id=?", (tid,))
        t = await cur.fetchone()
        if not t:
            return await m.answer("Тикет не найден.")
        await db.execute("INSERT INTO ticket_msgs(ticket_id, from_admin, text, created_at) VALUES(?,?,?,?)",
                         (tid, 1, txt, now()))
        await db.commit()
    await m.bot.send_message(t["tg_id"], f"🆘 Ответ поддержки по тикету #{tid}:\n\n{txt}")
    await m.answer("Ответ отправлен.")

@router.callback_query(F.data == "menu:profile")
async def my_profile(cb: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM profiles WHERE tg_id=?", (cb.from_user.id,))
        prof = await cur.fetchone()
    if not prof:
        return await cb.answer("Анкета не найдена", show_alert=True)
    prof = dict(prof)
    u = await get_user(cb.from_user.id)
    text = (f"<b>Моя анкета</b>\n\n{prof['name']}, {prof['age']}\n"
            f"📍 {prof['city']}\n5. {prof['diagnosis']}\n6. {prof['reason']}\n7. {prof['hobby']}\n\n"
            f"VIP: {u['vip_level']} до {u['vip_until'] or '—'}\n"
            f"Лайков: {u['likes_left']}\nСтатус: {u['status']}")
    await cb.message.answer(text, parse_mode="HTML")
    await cb.answer()

@router.callback_query(F.data.startswith("report:"))
async def report(cb: CallbackQuery, state: FSMContext):
    target = int(cb.data.split(":")[1])
    await state.set_state(S.report_reason)
    await state.update_data(target=target)
    await cb.message.answer("Опишите причину жалобы:")
    await cb.answer()

@router.message(S.report_reason)
async def report_reason(m: Message, state: FSMContext):
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO reports(reporter, target, reason, created_at) VALUES(?,?,?,?)",
                         (m.from_user.id, data["target"], m.text, now()))
        await db.commit()
    await m.bot.send_message(SUPERADMIN_ID,
        f"🚨 Жалоба от {m.from_user.id} на {data['target']}:\n{m.text}")
    await state.clear()
    await m.answer("✅ Жалоба отправлена.")

@router.message(Command("admin"))
async def admin(m: Message):
    if m.from_user.id != SUPERADMIN_ID:
        return
    await m.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_kb(), parse_mode="HTML")

def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="a:users:0")],
        [InlineKeyboardButton(text="📝 На модерации", callback_data="a:pending")],
        [InlineKeyboardButton(text="🚫 Баны", callback_data="a:bans")],
        [InlineKeyboardButton(text="📨 Тикеты", callback_data="a:tickets")],
        [InlineKeyboardButton(text="🚨 Жалобы", callback_data="a:reports")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="a:stats")],
    ])

@router.callback_query(F.data.startswith("a:"))
async def admin_cb(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != SUPERADMIN_ID:
        return await cb.answer("Нет доступа", show_alert=True)
    parts = cb.data.split(":")
    action = parts[1]
    if action == "users":
        offset = int(parts[2])
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT 10 OFFSET ?", (offset,))
            users = [dict(r) for r in await cur.fetchall()]
        rows = [[InlineKeyboardButton(text=f"{u['tg_id']} | {u['status']} | VIP{u['vip_level']}", callback_data=f"a:u:{u['tg_id']}")] for u in users]
        nav = []
        if offset > 0: nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"a:users:{offset-10}"))
        if len(users) == 10: nav.append(InlineKeyboardButton(text="➡️", callback_data=f"a:users:{offset+10}"))
        if nav: rows.append(nav)
        rows.append([InlineKeyboardButton(text="⬅️ Панель", callback_data="a:back")])
        await cb.message.edit_text(f"👥 Пользователи ({offset+1}-{offset+len(users)}):", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    elif action == "u":
        tid = int(parts[2])
        u = await get_user(tid)
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM profiles WHERE tg_id=?", (tid,))
            p = await cur.fetchone()
        txt = f"👤 {tid}\nstatus={u['status']} banned={u['banned']}\nVIP={u['vip_level']} до {u['vip_until']}\nlikes={u['likes_left']}"
        if p:
            p = dict(p)
            txt += f"\n\n{p['name']}, {p['age']}, {p['city']}\n{p['diagnosis']}\n{p['reason']}\n{p['hobby']}"
            if p.get("photo1"): await cb.bot.send_photo(cb.from_user.id, p["photo1"])
            if p.get("video"): await cb.bot.send_video_note(cb.from_user.id, p["video"])
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Забанить", callback_data=f"a:ban:{tid}"),
             InlineKeyboardButton(text="✅ Разбанить", callback_data=f"a:unban:{tid}")],
            [InlineKeyboardButton(text="👑 Выдать VIP", callback_data=f"a:givevip:{tid}")],
            [InlineKeyboardButton(text="🎁 Выдать доп", callback_data=f"a:givedop:{tid}")],
            [InlineKeyboardButton(text="🗑 Удалить анкету", callback_data=f"a:delprof:{tid}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="a:users:0")],
        ])
        await cb.message.edit_text(txt, reply_markup=kb)
    elif action == "ban":
        tid = int(parts[2])
        await state.set_state(S.admin_wait_id)
        await state.update_data(op="ban", tid=tid)
        await cb.message.answer("Введите причину бана:")
    elif action == "unban":
        tid = int(parts[2])
        await upsert_user(tid, banned=0, ban_reason=None, status="active")
        await cb.answer("Разбанен")
    elif action == "givevip":
        tid = int(parts[2])
        await state.set_state(S.admin_wait_amount)
        await state.update_data(op="vip", tid=tid)
        await cb.message.answer("Введите уровень VIP (1/2/3) и дней через пробел, например: 2 30")
    elif action == "givedop":
        tid = int(parts[2])
        await state.set_state(S.admin_wait_amount)
        await state.update_data(op="dop", tid=tid)
        await cb.message.answer("Введите доп (chars600 / unlimited / prev / second / incognito) и дней:")
    elif action == "delprof":
        tid = int(parts[2])
        await state.set_state(S.admin_wait_amount)
        await state.update_data(op="delprof", tid=tid)
        await cb.message.answer("Введите причину удаления анкеты:")
    elif action == "pending":
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM profiles WHERE approved=0 ORDER BY id DESC LIMIT 20")
            rows = [dict(r) for r in await cur.fetchall()]
        if not rows:
            return await cb.answer("Пусто", show_alert=True)
        for p in rows:
            await send_to_admin_chat(cb.bot, p, await get_user(p["tg_id"]))
        await cb.answer("Отправлено в чат")
    elif action == "bans":
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM users WHERE banned=1 ORDER BY created_at DESC LIMIT 20")
            rows = [dict(r) for r in await cur.fetchall()]
        if not rows:
            return await cb.answer("Нет забаненных", show_alert=True)
        txt = "\n".join(f"{u['tg_id']} — {u['ban_reason']}" for u in rows)
        await cb.message.answer(txt)
    elif action == "tickets":
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM tickets WHERE status='open' ORDER BY id DESC LIMIT 20")
            rows = [dict(r) for r in await cur.fetchall()]
        if not rows:
            return await cb.answer("Нет открытых", show_alert=True)
        for t in rows:
            await cb.message.answer(f"#{t['id']} от {t['tg_id']}")
    elif action == "reports":
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM reports ORDER BY id DESC LIMIT 20")
            rows = [dict(r) for r in await cur.fetchall()]
        if not rows:
            return await cb.answer("Пусто", show_alert=True)
        for r in rows:
            await cb.message.answer(f"🚨 {r['reporter']} → {r['target']}: {r['reason']}")
    elif action == "stats":
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT COUNT(*) FROM users"); total = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM users WHERE status='active'"); active = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM users WHERE status='pending_review'"); pend = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM users WHERE banned=1"); bans = (await cur.fetchone())[0]
            cur = await db.execute("SELECT vip_level, COUNT(*) FROM users GROUP BY vip_level"); vips = await cur.fetchall()
        await cb.message.answer(f"📊 Всего: {total}\nАктивных: {active}\nНа модерации: {pend}\nЗабанено: {bans}\nVIP: {vips}")
    elif action == "back":
        await cb.message.edit_text("🛠 <b>Админ-панель</b>", reply_markup=admin_kb(), parse_mode="HTML")
    await cb.answer()

@router.message(S.admin_wait_amount)
async def admin_amount(m: Message, state: FSMContext):
    if m.from_user.id != SUPERADMIN_ID:
        return
    d = await state.get_data()
    op, tid = d.get("op"), d.get("tid")
    await state.clear()
    if op == "vip":
        try:
            lvl, days = map(int, m.text.split())
        except Exception:
            return await m.answer("Формат: уровень дней")
        until = (datetime.utcnow() + timedelta(days=days)).isoformat()
        lim = VIP_LIMITS.get(lvl, VIP_LIMITS[1])
        await upsert_user(tid, vip_level=lvl, vip_until=until, chars_limit=lim["chars"],
                          likes_left=lim["likes"], photos_used=0)
        await m.answer(f"✅ VIP {lvl} выдан на {days} дней пользователю {tid}")
        try: await m.bot.send_message(tid, f"👑 Вам выдан VIP уровня {lvl} на {days} дней!")
        except Exception: pass
    elif op == "dop":
        parts = m.text.split()
        if len(parts) != 2:
            return await m.answer("Формат: dop days")
        dop, days = parts[0], int(parts[1])
        await grant_dop(tid, dop, days, m.bot)
        await m.answer(f"✅ {dop} выдан {tid} на {days} дней")
    elif op == "delprof":
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM profiles WHERE tg_id=?", (tid,))
            await db.execute("UPDATE users SET status='rejected' WHERE tg_id=?", (tid,))
            await db.commit()
        await m.answer(f"🗑 Анкета {tid} удалена. Причина: {m.text}")
        try: await m.bot.send_message(tid, f"🗑 Ваша анкета удалена модератором. Причина: {m.text}")
        except Exception: pass

async def grant_dop(tid, dop, days, bot):
    u = await get_user(tid)
    if dop == "chars600":
        await upsert_user(tid, chars_limit=600)
    elif dop == "unlimited":
        await upsert_user(tid, likes_left=999999)
    elif dop == "second":
        await upsert_user(tid, photos_used=0)
    elif dop == "prev":
        pass
    elif dop == "incognito":
        until = (datetime.utcnow() + timedelta(days=max(days,1))).isoformat()
        await upsert_user(tid, vip_until=until, vip_level=max(u["vip_level"], 1))
    try:
        await bot.send_message(tid, f"🎁 Вам выдан доп: {dop} (дней: {days})")
    except Exception:
        pass

@router.message(S.admin_wait_id)
async def admin_reason(m: Message, state: FSMContext):
    if m.from_user.id != SUPERADMIN_ID:
        return
    d = await state.get_data()
    await state.clear()
    if d.get("op") == "ban":
        tid = d["tid"]
        await upsert_user(tid, banned=1, ban_reason=m.text, status="banned")
        await m.answer(f"🚫 {tid} забанен")
        try: await m.bot.send_message(tid, f"🚫 Доступ заблокирован. Причина: {m.text}")
        except Exception: pass

@router.message(F.chat.type == ChatType.PRIVATE, F.text)
async def pm_router(m: Message, state: FSMContext):
    cur = await state.get_state()
    if cur:
        return
    if m.text and m.text.startswith("/"):
        return
    await m.answer("Используйте /start для навигации.")

async def main():
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await db_init()
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="admin", description="Админ-панель"),
    ])
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
