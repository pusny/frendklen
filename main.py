import asyncio
import os
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationHandlerStop, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)

load_dotenv()

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в .env")

ADMIN_ID = int((os.getenv("ADMIN_ID") or "0").strip() or "0")
ADMIN_CHAT_ID = int((os.getenv("ADMIN_CHAT_ID") or "0").strip() or "0")
TOPIC_APPS = int((os.getenv("TOPIC_APPLICATIONS") or "0").strip() or "0")
TOPIC_TICKETS = int((os.getenv("TOPIC_TICKETS") or "0").strip() or "0")
TOPIC_REPORTS = int((os.getenv("TOPIC_REPORTS") or "0").strip() or "0")
MANAGER = (os.getenv("MANAGER_USERNAME") or "@manager").strip()
UNBAN_PRICE = int((os.getenv("UNBAN_PRICE") or "500").strip() or "500")
ANON_PRICE = int((os.getenv("ANON_PRICE") or "30").strip() or "30")
ANON_FREE_MIN = int((os.getenv("ANON_FREE_MIN") or "60").strip() or "60")
ANON_PAID_DAYS = int((os.getenv("ANON_PAID_DAYS") or "1").strip() or "1")

DATA_DIR = Path((os.getenv("DATA_DIR") or ".").strip()) / "app" / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = str(DATA_DIR / "klencik.db")

MSK = timezone(timedelta(hours=3))

CODE_WORDS = ["клён", "листопад", "янтарь", "осень", "ветер", "огонь", "река",
              "туман", "рассвет", "пепел", "север", "ирис", "кобальт", "стрела", "гром"]

PROFILE_TEMPLATE = "Имя: \nВозраст: \nГород: \nДиагноз: \nОписание о себе: "


def _tk(topic):
    return {"message_thread_id": topic} if topic > 0 else {}


def _init_db():
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            phone TEXT,
            agreed INTEGER DEFAULT 0,
            banned INTEGER DEFAULT 0,
            ban_reason TEXT,
            circle_file_id TEXT,
            circle_phrase TEXT,
            vip INTEGER DEFAULT 0,
            vip_until TEXT,
            likes_left INTEGER DEFAULT 20,
            likes_date TEXT,
            anon_until TEXT,
            anon_free_date TEXT,
            anon_free_used INTEGER DEFAULT 0,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS profiles (
            user_id INTEGER PRIMARY KEY,
            name TEXT, age TEXT, city TEXT, diagnosis TEXT, about TEXT,
            approved INTEGER DEFAULT 0, rejected INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS anon_queue (
            user_id INTEGER PRIMARY KEY, ts TEXT, msg_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS anon_pairs (
            user_a INTEGER, user_b INTEGER, ts TEXT,
            PRIMARY KEY (user_a, user_b)
        );
        CREATE TABLE IF NOT EXISTS prices (key TEXT PRIMARY KEY, val INTEGER);
        CREATE TABLE IF NOT EXISTS admin_msgs (msg_id INTEGER PRIMARY KEY, user_id INTEGER, mtype TEXT);
        CREATE TABLE IF NOT EXISTS skips (id INTEGER PRIMARY KEY AUTOINCREMENT, from_id INTEGER, to_id INTEGER, ts TEXT);
        """)

        cols_u = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "full_name" not in cols_u:
            conn.execute("ALTER TABLE users ADD COLUMN full_name TEXT")

        cols_q = {r[1] for r in conn.execute("PRAGMA table_info(anon_queue)").fetchall()}
        if "msg_id" not in cols_q:
            conn.execute("ALTER TABLE anon_queue ADD COLUMN msg_id INTEGER")

        try:
            cols_l = {r[1] for r in conn.execute("PRAGMA table_info(likes)").fetchall()}
        except Exception:
            cols_l = set()

        if cols_l and "id" not in cols_l:
            try:
                conn.execute("DROP TABLE IF EXISTS likes_old_mig")
                conn.execute("ALTER TABLE likes RENAME TO likes_old_mig")
                conn.execute("CREATE TABLE likes (id INTEGER PRIMARY KEY AUTOINCREMENT, from_id INTEGER, to_id INTEGER, ts TEXT)")
                conn.execute("INSERT INTO likes(from_id,to_id,ts) SELECT from_id,to_id,ts FROM likes_old_mig")
                conn.execute("DROP TABLE likes_old_mig")
            except Exception:
                try:
                    conn.execute("DROP TABLE IF EXISTS likes_old_mig")
                except Exception:
                    pass

        try:
            conn.execute("CREATE TABLE IF NOT EXISTS likes (id INTEGER PRIMARY KEY AUTOINCREMENT, from_id INTEGER, to_id INTEGER, ts TEXT)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_likes_ft ON likes(from_id, to_id, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_skips_ft ON skips(from_id, to_id, ts)")
        except Exception:
            pass

        for k, v in {
            "vip1": int(os.getenv("VIP1_PRICE") or 200),
            "vip2": int(os.getenv("VIP2_PRICE") or 350),
            "vip3": int(os.getenv("VIP3_PRICE") or 550),
            "anon": ANON_PRICE,
            "unban": UNBAN_PRICE,
        }.items():
            conn.execute("INSERT OR IGNORE INTO prices(key,val) VALUES(?,?)", (k, v))
        conn.commit()


_init_db()


def fmt_dt(iso_str):
    if not iso_str:
        return "—"
    if str(iso_str).lower() == 'forever':
        return "Навсегда"
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(MSK)
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return iso_str.replace("T", " ")[:16]


def db_get_user(uid, tg_user=None):
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if not row:
            now = datetime.now(timezone.utc)
            conn.execute(
                "INSERT OR IGNORE INTO users(user_id,created_at,likes_date,anon_free_date) VALUES(?,?,?,?)",
                (uid, now.isoformat(), now.date().isoformat(), now.date().isoformat()),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if tg_user is not None:
            uname = tg_user.username
            fname = (tg_user.full_name or "").strip() or None
            if row["username"] != uname or row["full_name"] != fname:
                conn.execute(
                    "UPDATE users SET username=?, full_name=? WHERE user_id=?",
                    (uname, fname, uid),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        return dict(row)


def db_set(uid, **kw):
    if not kw:
        return
    sets = ",".join(f"{k}=?" for k in kw)
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        conn.execute(f"UPDATE users SET {sets} WHERE user_id=?", (*kw.values(), uid))
        conn.commit()


def db_get_profile(uid):
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT * FROM profiles WHERE user_id=?", (uid,)).fetchone()
        return dict(r) if r else None


def db_save_profile(uid, name, age, city, diagnosis, about):
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO profiles(user_id,name,age,city,diagnosis,about,approved,rejected) "
            "VALUES(?,?,?,?,?,?,0,0)",
            (uid, name, age, city, diagnosis, about),
        )
        conn.commit()


def db_get_price(key):
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        r = conn.execute("SELECT val FROM prices WHERE key=?", (key,)).fetchone()
        return r[0] if r else 0


def db_set_price(key, val):
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        conn.execute("INSERT OR REPLACE INTO prices(key,val) VALUES(?,?)", (key, val))
        conn.commit()


def db_save_admin_msg(mid, uid, mtype):
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        conn.execute("INSERT OR REPLACE INTO admin_msgs(msg_id,user_id,mtype) VALUES(?,?,?)",
                     (mid, uid, mtype))
        conn.commit()


def db_get_admin_msg(mid):
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        r = conn.execute("SELECT user_id,mtype FROM admin_msgs WHERE msg_id=?", (mid,)).fetchone()
        return r


def vip_char_limit(v):
    return {0: 300, 1: 500, 2: 800, 3: 10 ** 9}.get(v, 300)


def vip_like_limit(v):
    return {0: 20, 1: 40, 2: 60, 3: 10 ** 9}.get(v, 20)


def vip_label(v):
    return {0: "нет", 1: "VIP 1 (Низкий)", 2: "VIP 2 (Средний)", 3: "VIP 3 (Высокий)"}.get(v, "нет")


def ensure_daily_likes(uid):
    u = db_get_user(uid)
    today = datetime.now(MSK).date().isoformat()
    if u["likes_date"] != today:
        db_set(uid, likes_left=vip_like_limit(u["vip"]), likes_date=today)


def add_days(base_iso, days):
    now = datetime.now(timezone.utc)
    if base_iso and str(base_iso).lower() == 'forever':
        return 'forever'
    if base_iso:
        try:
            base = datetime.fromisoformat(base_iso)
            if base.tzinfo is None:
                base = base.replace(tzinfo=timezone.utc)
            if base > now:
                return (base + timedelta(days=days)).isoformat()
        except Exception:
            pass
    return (now + timedelta(days=days)).isoformat()


def parse_profile(text):
    required = ["Имя:", "Возраст:", "Город:", "Диагноз:", "Описание о себе:"]
    keys = ["name", "age", "city", "diagnosis", "about"]
    data = {}
    for line in text.strip().splitlines():
        line = line.strip()
        for k, pre in zip(keys, required):
            if line.startswith(pre):
                data[k] = line[len(pre):].strip()
    if len(data) < 5 or any(not v for v in data.values()):
        return None
    return data


AGREEMENT_TEXT = (
    "🍁 <b>Пользовательское соглашение «Клёнчик»</b>\n\n"
    "Нажимая «Согласен», вы подтверждаете, что ознакомлены и полностью принимаете условия:\n\n"
    "1. <b>Ответственность.</b> Администрация «Клёнчика» не несёт ответственности за содержание переписки "
    "в анонимном чате и за любые действия, к которым она может привести.\n"
    "2. <b>Анонимный чат.</b> Все диалоги анонимны. Всё, что происходит после общения в анонимном чате "
    "(в т.ч. переход в личные сообщения), — на ваш собственный страх и риск.\n"
    "3. <b>Данные.</b> Ваш номер телефона используется только для верификации и не передаётся третьим лицам.\n"
    "4. <b>Контент.</b> Запрещены оскорбления, угрозы, распространение незаконной информации, спам и попытки "
    "деанонимизации.\n"
    f"5. <b>Блокировки.</b> При нарушении правил, жалобах или отказе от соглашения аккаунт может быть заблокирован. "
    f"Разбан — от {UNBAN_PRICE} ₽ через менеджера {MANAGER}.\n"
    "6. <b>Возврат.</b> Покупки виртуальных товаров не подлежат возврату.\n"
    "7. <b>Изменения.</b> Администрация вправе менять условия и цены в любое время.\n\n"
    "Если вы не согласны — нажмите «Отказаться»."
)

BAN_ON_REFUSE = (
    "🚫 <b>Вы были заблокированы</b>\n\n"
    "Причина: отказ от пользовательского соглашения «Клёнчика».\n\n"
    f"Разбан за <b>{UNBAN_PRICE} ₽</b> у менеджера: {MANAGER}"
)

PROFILE_REJECTED = (
    "❌ <b>Ваша заявка была отклонена</b>\n\n"
    "В связи с этим ваш аккаунт заблокирован.\n\n"
    f"Разбан за <b>{UNBAN_PRICE} ₽</b> у менеджера: {MANAGER}"
)


def kb_agree():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Согласен", callback_data="agree:yes"),
        InlineKeyboardButton("❌ Отказаться", callback_data="agree:no"),
    ]])


def kb_contact():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Поделиться контактом", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )


def kb_main(is_admin=False):
    rows = [
        [InlineKeyboardButton("🍁 Анкеты", callback_data="menu:profiles"),
         InlineKeyboardButton("💬 Анонимный чат", callback_data="menu:anon")],
        [InlineKeyboardButton("🛒 Магазин", callback_data="menu:shop"),
         InlineKeyboardButton("👤 Мой профиль", callback_data="menu:profile")],
        [InlineKeyboardButton("🆘 Поддержка", callback_data="menu:support")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("🛠 Админ-панель", callback_data="admin:main")])
    return InlineKeyboardMarkup(rows)


def kb_back(cb="menu:main"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=cb)]])


def kb_profile_view():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Переписать анкету", callback_data="profile:edit")],
        [InlineKeyboardButton("◀️ Назад", callback_data="menu:main")],
    ])


def kb_anon():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Найти собеседника", callback_data="anon:find")],
        [InlineKeyboardButton("🛑 Стоп", callback_data="anon:stop")],
        [InlineKeyboardButton("◀️ Назад", callback_data="menu:main")],
    ])


def kb_shop():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🍁 VIP 1 — Низкий", callback_data="shop:vip1")],
        [InlineKeyboardButton("🍁 VIP 2 — Средний", callback_data="shop:vip2")],
        [InlineKeyboardButton("🍁 VIP 3 — Высокий", callback_data="shop:vip3")],
        [InlineKeyboardButton("💬 Доступ в анон-чат", callback_data="shop:anon")],
        [InlineKeyboardButton("◀️ Назад", callback_data="menu:main")],
    ])


def kb_app_review(uid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Принять", callback_data=f"app:accept:{uid}"),
         InlineKeyboardButton("❌ Отклонить", callback_data=f"app:reject:{uid}")],
        [InlineKeyboardButton("✏️ Переписать", callback_data=f"app:rewrite:{uid}")],
    ])


def kb_ticket(uid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✉️ Ответить", callback_data=f"ticket:reply:{uid}")
    ]])


def kb_report(uid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 Забанить", callback_data=f"report:ban:{uid}"),
         InlineKeyboardButton("✅ Игнорировать", callback_data=f"report:ignore:{uid}")],
    ])


def kb_admin():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Пользователей", callback_data="admin:count")],
        [InlineKeyboardButton("🔨 Забанить", callback_data="admin:ban"),
         InlineKeyboardButton("🔓 Разбанить", callback_data="admin:unban")],
        [InlineKeyboardButton("🎁 Выдать VIP", callback_data="admin:give_vip"),
         InlineKeyboardButton("🗑 Снять VIP", callback_data="admin:take_vip")],
        [InlineKeyboardButton("🎁 Выдать анон", callback_data="admin:give_anon"),
         InlineKeyboardButton("🗑 Снять анон", callback_data="admin:take_anon")],
        [InlineKeyboardButton("💲 Цены", callback_data="admin:prices")],
        [InlineKeyboardButton("📋 Список юзеров", callback_data="admin:users")],
        [InlineKeyboardButton("◀️ Назад", callback_data="menu:main")],
    ])


def kb_admin_prices():
    keys = ["vip1", "vip2", "vip3", "anon", "unban"]
    rows = [[InlineKeyboardButton(f"✏️ {k} — {db_get_price(k)}₽", callback_data=f"admin:price_set:{k}")]
            for k in keys]
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="admin:main")])
    return InlineKeyboardMarkup(rows)


async def safe_send(bot, chat_id, text, **kw):
    try:
        return await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, **kw)
    except Exception:
        return None


async def notify_user(bot, uid, text):
    await safe_send(bot, uid, text)
    await safe_send(bot, uid, f"Ваш ID: <code>{uid}</code>")


async def to_admin(bot, topic, text, **kw):
    if not ADMIN_CHAT_ID:
        return None
    return await safe_send(bot, ADMIN_CHAT_ID, text, **_tk(topic), **kw)


async def is_admin(bot, uid):
    if uid == ADMIN_ID:
        return True
    try:
        m = await bot.get_chat_member(ADMIN_CHAT_ID, uid)
        return m.status in ("creator", "administrator")
    except Exception:
        return False


def get_partner(uid):
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        r = conn.execute(
            "SELECT user_a, user_b FROM anon_pairs WHERE user_a=? OR user_b=?",
            (uid, uid),
        ).fetchone()
        if not r:
            return None
        return r[1] if r[0] == uid else r[0]


def anon_time_left(uid):
    u = db_get_user(uid)
    today = datetime.now(MSK).date().isoformat()
    if u["anon_free_date"] != today:
        db_set(uid, anon_free_date=today, anon_free_used=0)
        u = db_get_user(uid)
        
    if u["anon_until"] == 'forever':
        return 99999999
        
    if u["anon_until"]:
        try:
            until = datetime.fromisoformat(u["anon_until"])
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            if until > datetime.now(timezone.utc):
                return int((until - datetime.now(timezone.utc)).total_seconds())
        except Exception:
            pass
    if not u["anon_free_used"]:
        return ANON_FREE_MIN * 60
    return 0


async def ban_mw(update, ctx):
    user = update.effective_user
    if not user or user.id == ADMIN_ID:
        return
    u = db_get_user(user.id, user)
    if u["banned"]:
        try:
            await ctx.bot.send_message(
                user.id,
                f"🚫 Вы забанены.\nПричина: {u['ban_reason'] or 'нарушение правил'}\n\n"
                f"Разбан за <b>{UNBAN_PRICE} ₽</b> у {MANAGER}\nВаш ID: <code>{user.id}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        raise ApplicationHandlerStop


async def ban_cb_mw(update, ctx):
    q = update.callback_query
    if not q:
        return
    user = q.from_user
    if not user or user.id == ADMIN_ID:
        return
    u = db_get_user(user.id, user)
    if u["banned"]:
        await q.answer(f"Забанены. Разбан {UNBAN_PRICE}₽ у {MANAGER}", show_alert=True)
        raise ApplicationHandlerStop


async def moderation_mw(update, ctx):
    q = update.callback_query
    if not q:
        return
    user = q.from_user
    if not user:
        return
    if user.id == ADMIN_ID:
        return
    if q.message and q.message.chat.id == ADMIN_CHAT_ID:
        try:
            m = await ctx.bot.get_chat_member(ADMIN_CHAT_ID, user.id)
            if m.status in ("creator", "administrator"):
                return
        except Exception:
            pass
    u = db_get_user(user.id, user)
    if u["banned"]:
        return
    p = db_get_profile(user.id)
    if p and p["approved"]:
        return
    data = q.data or ""
    if data == "menu:main" or data.startswith("agree:"):
        return
    await q.answer("⏳ Анкета на модерации. Дождитесь одобрения администратором.", show_alert=True)
    raise ApplicationHandlerStop


async def cmd_start(update, ctx):
    msg = update.effective_message
    uid = update.effective_user.id
    if get_partner(uid):
        return await msg.reply_text("🛑 Сначала завершите анонимный чат через кнопку «Стоп».", reply_markup=kb_anon())
    u = db_get_user(uid, update.effective_user)
    if u["banned"]:
        return await msg.reply_text(
            f"🚫 Вы забанены.\nПричина: {u['ban_reason'] or 'нарушение'}\n"
            f"Разбан за <b>{UNBAN_PRICE} ₽</b> у {MANAGER}\nВаш ID: <code>{uid}</code>",
            parse_mode=ParseMode.HTML,
        )
    if not u["agreed"]:
        return await msg.reply_text(AGREEMENT_TEXT, parse_mode=ParseMode.HTML, reply_markup=kb_agree())
    if not u["phone"]:
        return await ask_phone(msg)
    p = db_get_profile(uid)
    if not p or p["rejected"]:
        return await start_circle(msg, ctx, uid)
    if not p["approved"]:
        return await msg.reply_text(
            "⏳ <b>Ваша анкета на модерации.</b>\n\n"
            "Доступ к функциям бота откроется после одобрения администратором.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Проверить статус", callback_data="menu:main"),
            ]]),
        )
    await msg.reply_text(
        "🍁 <b>Главное меню</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_main(uid == ADMIN_ID),
    )


async def ask_phone(msg):
    await msg.reply_text(
        "📱 <b>Верификация телефона</b>\n\n"
        "Нажмите кнопку ниже, чтобы поделиться контактом.\n\n"
        "🔒 Номер сохраняется только для верификации и не передаётся третьим лицам.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_contact(),
    )


async def start_circle(msg, ctx, uid):
    phrase = " · ".join(random.sample(CODE_WORDS, 3))
    db_set(uid, circle_phrase=phrase, circle_file_id=None)
    await msg.reply_text(
        "🎥 <b>Видео-кружок с кодовой фразой</b>\n\n"
        "Запишите видео-кружок и произнесите:\n\n"
        f"<code>{phrase}</code>\n\n"
        "📌 Требования:\n"
        "• Лицо в кадре, не размыто, свет.\n"
        "• Длительность 5–30 сек.\n"
        "• Голос настоящий.\n"
        "• Чужие видео нельзя.\n\n"
        "🕹 Иконка камеры (📷) справа снизу от строки ввода → «Видео-кружок» → удерживайте запись.\n\n"
        "Если пришлёте не видео-кружок — попрошу снова.",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove(),
    )


async def handle_contact(update, ctx):
    msg = update.effective_message
    uid = update.effective_user.id
    if msg.contact.user_id and msg.contact.user_id != uid:
        return await msg.reply_text("❌ Поделитесь своим контактом.")
    db_set(uid, phone=msg.contact.phone_number)
    await msg.reply_text("✅ Телефон сохранён.", reply_markup=ReplyKeyboardRemove())
    await start_circle(msg, ctx, uid)


async def handle_circle(update, ctx, u):
    msg = update.effective_message
    db_set(u["user_id"], circle_file_id=msg.video_note.file_id)
    limit = vip_char_limit(u["vip"])
    limit_txt = "без ограничений" if limit >= 10**9 else f"{limit} символов"
    await msg.reply_text(
        "✅ Кружок получен.\n\n"
        "📝 Заполните анкету строго по шаблону:\n\n"
        f"<code>{PROFILE_TEMPLATE}</code>\n\n"
        "• Нажмите на шаблон — скопируется.\n"
        "• Все поля обязательны.\n"
        f"• Лимит: <b>{limit_txt}</b>.",
        parse_mode=ParseMode.HTML,
    )


async def handle_profile(update, ctx, u, text):
    msg = update.effective_message
    uid = u["user_id"]
    limit = vip_char_limit(u["vip"])
    if len(text) > limit:
        over = len(text) - limit
        return await msg.reply_text(
            f"⚠️ Анкета превышает лимит.\nЛимит: <b>{limit}</b>.\nСейчас: <b>{len(text)}</b>.\n"
            f"Сократите на <b>{over}</b> символ(ов).",
            parse_mode=ParseMode.HTML,
        )
    data = parse_profile(text)
    if not data:
        return await msg.reply_text(
            "❌ Анкета заполнена не по форме.\n\n"
            f"<code>{PROFILE_TEMPLATE}</code>\n\n"
            "Заполните заново.",
            parse_mode=ParseMode.HTML,
        )
    db_save_profile(uid, data["name"], data["age"], data["city"], data["diagnosis"], data["about"])
    await msg.reply_text(
        "✅ Анкета отправлена на модерацию.\n\n"
        "Доступ к функциям бота откроется после одобрения администратором.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Проверить статус", callback_data="menu:main"),
        ]]),
    )

    profile_text = (
        f"🍁 <b>Новая анкета</b>\n\n"
        f"Юзер: <code>{uid}</code> @{escape(update.effective_user.username or '-')}\n"
        f"Телефон: <code>{escape(str(u['phone']))}</code>\n"
        f"Фраза: <code>{escape(u['circle_phrase'] or '—')}</code>\n\n"
        f"<b>Анкета:</b>\n"
        f"Имя: {escape(str(data['name']))}\n"
        f"Возраст: {escape(str(data['age']))}\n"
        f"Город: {escape(str(data['city']))}\n"
        f"Диагноз: {escape(str(data['diagnosis']))}\n"
        f"Описание: {escape(str(data['about']))}"
    )
    sent = await to_admin(ctx.bot, TOPIC_APPS, profile_text, reply_markup=kb_app_review(uid))
    if sent:
        db_save_admin_msg(sent.message_id, uid, "profile")
        if u["circle_file_id"]:
            try:
                await ctx.bot.send_video_note(
                    ADMIN_CHAT_ID, u["circle_file_id"],
                    reply_to_message_id=sent.message_id, **_tk(TOPIC_APPS),
                )
            except Exception:
                pass


async def handle_profile_edit(update, ctx, u, text):
    msg = update.effective_message
    uid = u["user_id"]
    limit = vip_char_limit(u["vip"])
    if len(text) > limit:
        over = len(text) - limit
        ctx.user_data["edit_text_mode"] = True
        return await msg.reply_text(
            f"⚠️ Анкета превышает лимит.\nЛимит: <b>{limit}</b>.\nСейчас: <b>{len(text)}</b>.\n"
            f"Сократите на <b>{over}</b> символ(ов).",
            parse_mode=ParseMode.HTML,
        )
    data = parse_profile(text)
    if not data:
        ctx.user_data["edit_text_mode"] = True
        return await msg.reply_text(
            "❌ Анкета заполнена не по форме.\n\n"
            f"<code>{PROFILE_TEMPLATE}</code>\n\n"
            "Заполните заново.",
            parse_mode=ParseMode.HTML,
        )
    ctx.user_data.pop("edit_text_mode", None)
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        conn.execute(
            "UPDATE profiles SET name=?, age=?, city=?, diagnosis=?, about=? WHERE user_id=?",
            (data["name"], data["age"], data["city"], data["diagnosis"], data["about"], uid),
        )
        conn.commit()
    await msg.reply_text(
        "✅ Анкета обновлена.",
        reply_markup=kb_main(uid == ADMIN_ID),
    )


async def handle_support(update, ctx):
    msg = update.effective_message
    uid = update.effective_user.id
    ctx.user_data.pop("support_mode", None)
    await msg.reply_text("✅ Тикет создан. Ожидайте ответа.", reply_markup=kb_main(uid == ADMIN_ID))
    text = f"🆘 <b>Тикет</b>\nОт: <code>{uid}</code> @{escape(update.effective_user.username or '-')}\n\n{escape(msg.text or '')}"
    sent = await to_admin(ctx.bot, TOPIC_TICKETS, text, reply_markup=kb_ticket(uid))
    if sent:
        db_save_admin_msg(sent.message_id, uid, "ticket")


async def private_message(update, ctx):
    msg = update.effective_message
    if not msg or msg.chat.type != "private":
        return
    uid = update.effective_user.id
    u = db_get_user(uid, update.effective_user)
    if u["banned"]:
        raise ApplicationHandlerStop

    if uid == ADMIN_ID and ctx.user_data.get("admin_input"):
        await handle_admin_input(update, ctx)
        raise ApplicationHandlerStop

    if msg.contact:
        await handle_contact(update, ctx)
        raise ApplicationHandlerStop

    if ctx.user_data.get("support_mode") and msg.text:
        await handle_support(update, ctx)
        raise ApplicationHandlerStop

    if ctx.user_data.get("edit_text_mode") and msg.text:
        await handle_profile_edit(update, ctx, u, msg.text)
        raise ApplicationHandlerStop

    if not u["agreed"]:
        await msg.reply_text(AGREEMENT_TEXT, parse_mode=ParseMode.HTML, reply_markup=kb_agree())
        raise ApplicationHandlerStop

    if not u["phone"]:
        await ask_phone(msg)
        raise ApplicationHandlerStop

    if not u["circle_file_id"]:
        if msg.video_note:
            await handle_circle(update, ctx, u)
        else:
            await msg.reply_text(
                "❌ Вы прислали не видео-кружок.\nЗапишите именно <b>видео-кружок</b> (круглое видео).",
                parse_mode=ParseMode.HTML,
            )
        raise ApplicationHandlerStop

    p = db_get_profile(uid)
    if not p or p["rejected"]:
        if msg.text:
            await handle_profile(update, ctx, u, msg.text)
        else:
            await msg.reply_text("❌ Отправьте анкету текстом по шаблону.")
        raise ApplicationHandlerStop

    if not p["approved"]:
        await msg.reply_text(
            "⏳ <b>Ваша анкета на модерации.</b>\nДождитесь одобрения администратором.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Проверить статус", callback_data="menu:main"),
            ]]),
        )
        raise ApplicationHandlerStop

    if msg.text:
        await msg.reply_text(
            "🍁 Используйте меню:",
            reply_markup=kb_main(uid == ADMIN_ID),
        )
    raise ApplicationHandlerStop


async def anon_relay(update, ctx):
    msg = update.effective_message
    if not msg or msg.chat.type != "private":
        return
    if not (msg.text or msg.photo or msg.video or msg.video_note
            or msg.voice or msg.document or msg.sticker or msg.animation):
        return
    uid = update.effective_user.id
    partner = get_partner(uid)
    if not partner:
        return
    if anon_time_left(uid) <= 0:
        with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
            conn.execute(
                "DELETE FROM anon_pairs WHERE (user_a=? AND user_b=?) OR (user_a=? AND user_b=?)",
                (uid, partner, partner, uid),
            )
            conn.commit()
        price = db_get_price("anon")
        await safe_send(
            ctx.bot, uid,
            f"⏰ Время вышло. Купите доступ на {ANON_PAID_DAYS} дн. за {price}₽ у {MANAGER}.",
            reply_markup=kb_anon(),
        )
        await safe_send(ctx.bot, partner, "🛑 Собеседник отключился.", reply_markup=kb_anon())
        raise ApplicationHandlerStop
    try:
        await ctx.bot.copy_message(partner, msg.chat.id, msg.message_id)
    except Exception:
        pass
    raise ApplicationHandlerStop


async def admin_chat_handler(update, ctx):
    msg = update.effective_message
    if not msg or msg.chat.id != ADMIN_CHAT_ID:
        return
    if msg.from_user.id != ADMIN_ID and not await is_admin(ctx.bot, msg.from_user.id):
        return

    if ctx.user_data.get("ticket_reply_to") and msg.text:
        uid = ctx.user_data.pop("ticket_reply_to")
        await safe_send(ctx.bot, uid, f"📩 <b>Ответ поддержки:</b>\n\n{escape(msg.text)}")
        await msg.reply_text("✅ Отправлено.")
        raise ApplicationHandlerStop

    if msg.reply_to_message:
        target = db_get_admin_msg(msg.reply_to_message.message_id)
        if not target:
            return
        uid, mtype = target
        if mtype == "profile":
            db_set(uid, circle_file_id=None)
            with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
                conn.execute("DELETE FROM profiles WHERE user_id=?", (uid,))
                conn.commit()
            phrase = " · ".join(random.sample(CODE_WORDS, 3))
            db_set(uid, circle_phrase=phrase)
            await safe_send(
                ctx.bot, uid,
                "✏️ Администратор попросил переписать анкету.\n\n"
                "🎥 Запишите видео-кружок с фразой:\n\n"
                f"<code>{phrase}</code>",
            )
            await msg.reply_text(
                f"✏️ Запрос на перепись отправлен пользователю <code>{uid}</code>.",
                parse_mode=ParseMode.HTML,
            )
            raise ApplicationHandlerStop


async def handle_admin_input(update, ctx):
    msg = update.effective_message
    action = ctx.user_data.pop("admin_input", None)
    if not action:
        return
    kind, *args = action
    try:
        if kind == "ban":
            uid = int(msg.text.strip())
            db_set(uid, banned=1, ban_reason="Бан администратором")
            await msg.reply_text(f"🔨 <code>{uid}</code> забанен.", parse_mode=ParseMode.HTML)
            await safe_send(ctx.bot, uid,
                            f"🚫 Забанены администратором.\nРазбан за {UNBAN_PRICE}₽ у {MANAGER}")
        elif kind == "unban":
            uid = int(msg.text.strip())
            db_set(uid, banned=0, ban_reason=None)
            await msg.reply_text(f"🔓 <code>{uid}</code> разбанен.", parse_mode=ParseMode.HTML)
            await safe_send(ctx.bot, uid, "✅ Вы разбанены.")
        elif kind == "give_vip_uid":
            uid = int(msg.text.strip())
            ctx.user_data.pop("admin_input", None)
            ctx.user_data["admin_pending_vip_uid"] = uid
            await msg.reply_text(
                "Введите уровень VIP (1, 2 или 3):",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("1", callback_data=f"admin:vip_lvl:{uid}:1"),
                     InlineKeyboardButton("2", callback_data=f"admin:vip_lvl:{uid}:2"),
                     InlineKeyboardButton("3", callback_data=f"admin:vip_lvl:{uid}:3")],
                    [InlineKeyboardButton("◀️ Отмена", callback_data="admin:main")],
                ]),
            )
        elif kind == "give_vip_days":
            uid, lvl = args
            days = int(msg.text.strip())
            u = db_get_user(uid)
            today = datetime.now(MSK).date().isoformat()
            new_likes = vip_like_limit(lvl)
            
            if days == 0:
                db_set(uid, vip=lvl, vip_until='forever', likes_left=new_likes, likes_date=today)
                await safe_send(ctx.bot, uid, f"🎁 VIP {lvl} выдан навсегда!")
            else:
                db_set(uid, vip=lvl, vip_until=add_days(u["vip_until"], days), likes_left=new_likes, likes_date=today)
                await safe_send(ctx.bot, uid, f"🎁 VIP {lvl} выдан на {days} дн.!")
            await msg.reply_text(f"✅ VIP {lvl} → <code>{uid}</code>", parse_mode=ParseMode.HTML,
                                 reply_markup=kb_admin())
        elif kind == "take_vip":
            uid = int(msg.text.strip())
            today = datetime.now(MSK).date().isoformat()
            db_set(uid, vip=0, vip_until=None, likes_left=vip_like_limit(0), likes_date=today)
            await safe_send(ctx.bot, uid, "⚠️ VIP снят администратором.")
            await msg.reply_text(f"🗑 VIP снят с <code>{uid}</code>.", parse_mode=ParseMode.HTML,
                                 reply_markup=kb_admin())
        elif kind == "give_anon":
            parts = msg.text.strip().split()
            uid = int(parts[0])
            days = int(parts[1]) if len(parts) > 1 else ANON_PAID_DAYS
            if days == 0:
                db_set(uid, anon_until='forever')
                await safe_send(ctx.bot, uid, "🎁 Анон-доступ выдан навсегда!")
            else:
                u = db_get_user(uid)
                db_set(uid, anon_until=add_days(u["anon_until"], days))
                await safe_send(ctx.bot, uid, f"🎁 Анон-доступ выдан на {days} дн.!")
            await msg.reply_text(f"✅ Анон → <code>{uid}</code>", parse_mode=ParseMode.HTML,
                                 reply_markup=kb_admin())
        elif kind == "take_anon":
            uid = int(msg.text.strip())
            db_set(uid, anon_until=None)
            await safe_send(ctx.bot, uid, "⚠️ Анон-доступ снят.")
            await msg.reply_text(f"🗑 Анон снят с <code>{uid}</code>.", parse_mode=ParseMode.HTML,
                                 reply_markup=kb_admin())
        elif kind == "price":
            key = args[0]
            val = int(msg.text.strip())
            db_set_price(key, val)
            await msg.reply_text(f"✅ {key} = {val}₽", reply_markup=kb_admin_prices())
    except Exception:
        await msg.reply_text("❌ Ошибка формата.", reply_markup=kb_admin())


async def cb_agree_yes(update, ctx):
    q = update.callback_query
    uid = q.from_user.id
    db_set(uid, agreed=1)
    await q.message.edit_text("✅ Соглашение принято.")
    await ask_phone(q.message)
    await q.answer()


async def cb_agree_no(update, ctx):
    q = update.callback_query
    uid = q.from_user.id
    db_set(uid, banned=1, ban_reason="Отказ от соглашения")
    await q.message.edit_text("❌ Вы отказались от соглашения.")
    await notify_user(ctx.bot, uid, BAN_ON_REFUSE)
    await q.answer()


async def cb_menu_main(update, ctx):
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    uid = q.from_user.id
    ctx.user_data.pop("support_mode", None)
    ctx.user_data.pop("edit_text_mode", None)
    p = db_get_profile(uid)
    if uid != ADMIN_ID and (not p or not p["approved"]):
        try:
            await q.message.edit_text(
                "⏳ <b>Ваша анкета на модерации.</b>\n\n"
                "Доступ к функциям бота откроется после одобрения администратором.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Проверить статус", callback_data="menu:main"),
                ]]),
            )
        except Exception:
            pass
        return
    try:
        await q.message.edit_text(
            "🍁 <b>Главное меню</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main(uid == ADMIN_ID),
        )
    except Exception:
        pass


async def cb_menu_profiles(update, ctx):
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    uid = q.from_user.id
    try:
        await ensure_daily_likes(uid)
    except Exception:
        pass
    try:
        await show_profile(q, uid)
    except Exception as e:
        try:
            await q.message.answer(
                f"⚠️ <b>Не удалось показать анкету</b>\n<code>{escape(str(e))}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_back("menu:main"),
            )
        except Exception:
            pass


async def show_profile(q, uid):
    today = datetime.now(MSK).date().isoformat()
    r = None
    try:
        with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
            conn.row_factory = sqlite3.Row
            r = conn.execute(
                "SELECT p.* FROM profiles p "
                "WHERE p.approved=1 AND p.user_id != ? "
                "  AND p.user_id NOT IN (SELECT to_id FROM likes WHERE from_id=? AND substr(ts,1,10)=?) "
                "  AND p.user_id NOT IN (SELECT to_id FROM skips WHERE from_id=? AND substr(ts,1,10)=?) "
                "ORDER BY RANDOM() LIMIT 1",
                (uid, uid, today, uid, today),
            ).fetchone()
    except Exception:
        r = None

    if not r:
        try:
            await q.message.edit_text(
                "🍁 <b>Вы пролистали все анкеты!</b>\n\n"
                "Возвращайтесь завтра, чтобы снова листать и ставить лайки.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_back(),
            )
        except Exception:
            pass
        return

    p = dict(r)
    try:
        u = db_get_user(uid)
    except Exception:
        u = {"vip": 0}

    text = (
        f"<b>🍁 Анкета</b>\n\n"
        f"Имя: {escape(str(p.get('name') or '—'))}\n"
        f"Возраст: {escape(str(p.get('age') or '—'))}\n"
        f"Город: {escape(str(p.get('city') or '—'))}\n"
        f"Диагноз: {escape(str(p.get('diagnosis') or '—'))}\n"
        f"О себе: {escape(str(p.get('about') or '—'))}"
    )
    rows = [[
        InlineKeyboardButton("❤️ Лайк", callback_data=f"like:{p['user_id']}"),
        InlineKeyboardButton("➡️ Дальше", callback_data=f"skip:{p['user_id']}"),
        InlineKeyboardButton("🚨", callback_data=f"prof_report:{p['user_id']}"),
    ]]
    if u.get("vip") == 3:
        rows.insert(1, [InlineKeyboardButton("⬅️ Назад", callback_data=f"back:{p['user_id']}")])
    rows.append([InlineKeyboardButton("◀️ В меню", callback_data="menu:main")])

    try:
        await q.message.edit_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
    except Exception:
        try:
            await q.message.answer(
                text, parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(rows),
            )
        except Exception:
            pass


async def cb_like(update, ctx):
    q = update.callback_query
    uid = q.from_user.id
    target = int(q.data.split(":")[1])
    await q.answer()
    try:
        await ensure_daily_likes(uid)
    except Exception:
        pass
    u = db_get_user(uid)
    if u["likes_left"] <= 0:
        try:
            await q.message.edit_text(
                "⚠️ <b>Лимит лайков на сегодня исчерпан.</b>\n"
                "Хотите больше? Купите VIP в магазине.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_back("menu:profiles"),
            )
        except Exception:
            pass
        return

    today = datetime.now(MSK).date().isoformat()
    try:
        with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
            already = conn.execute(
                "SELECT 1 FROM likes WHERE from_id=? AND to_id=? AND substr(ts,1,10)=?",
                (uid, target, today),
            ).fetchone()
            if already:
                await show_profile(q, uid)
                return
            conn.execute(
                "INSERT INTO likes(from_id,to_id,ts) VALUES(?,?,?)",
                (uid, target, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            mutual = conn.execute(
                "SELECT 1 FROM likes WHERE from_id=? AND to_id=?", (target, uid)
            ).fetchone()
    except Exception:
        await q.message.answer("⚠️ Не удалось сохранить лайк. Попробуйте позже.")
        return

    if u["vip"] != 3:
        db_set(uid, likes_left=u["likes_left"] - 1)

    if mutual:
        me = db_get_profile(uid)
        other = db_get_profile(target)
        u1 = db_get_user(uid)
        u2 = db_get_user(target)
        text_me = (
            f"🎉 <b>Взаимный лайк!</b>\n\n"
            f"<b>Мэтч:</b>\n{escape(str(other['name']))}, {escape(str(other['age']))}, {escape(str(other['city']))}\n"
            f"@{escape(u2['username'] or '—')}\n\n"
            f"Вы: {escape(str(me['name']))}, {escape(str(me['age']))}, {escape(str(me['city']))}\n"
            f"@{escape(u1['username'] or '—')}"
        )
        text_other = (
            f"🎉 <b>Взаимный лайк!</b>\n\n"
            f"<b>Мэтч:</b>\n{escape(str(me['name']))}, {escape(str(me['age']))}, {escape(str(me['city']))}\n"
            f"@{escape(u1['username'] or '—')}\n\n"
            f"Вы: {escape(str(other['name']))}, {escape(str(other['age']))}, {escape(str(other['city']))}\n"
            f"@{escape(u2['username'] or '—')}"
        )
        await safe_send(ctx.bot, uid, text_me)
        await safe_send(ctx.bot, target, text_other)
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👀 Посмотреть анкету", callback_data=f"view_liker:{uid}")]
        ])
        await safe_send(ctx.bot, target, "❤️ Кто-то лайкнул вашу анкету. Возможно, взаимно!", reply_markup=kb)

    await show_profile(q, uid)


async def cb_view_liker(update, ctx):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    target = int(q.data.split(":")[1])
    p = db_get_profile(target)
    if not p:
        return await q.message.edit_text("Анкета недоступна.", reply_markup=kb_back())
    
    text = (
        f"<b>🍁 Вам поставили лайк!</b>\n\n"
        f"Имя: {escape(str(p['name']))}\n"
        f"Возраст: {escape(str(p['age']))}\n"
        f"Город: {escape(str(p['city']))}\n"
        f"Диагноз: {escape(str(p['diagnosis']))}\n"
        f"О себе: {escape(str(p['about']))}"
    )
    rows = [
        [InlineKeyboardButton("❤️ Взаимно", callback_data=f"like:{target}")],
        [InlineKeyboardButton("🚨 Жалоба", callback_data=f"prof_report:{target}")],
        [InlineKeyboardButton("◀️ В меню", callback_data="menu:main")]
    ]
    try:
        await q.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
    except Exception:
        pass


async def cb_skip(update, ctx):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    target = int(q.data.split(":")[1])
    today = datetime.now(MSK).date().isoformat()
    try:
        with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
            conn.execute(
                "INSERT INTO skips(from_id,to_id,ts) VALUES(?,?,?)",
                (uid, target, datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
    except Exception:
        pass
    try:
        await show_profile(q, uid)
    except Exception as e:
        try:
            await q.message.answer(f"⚠️ <code>{escape(str(e))}</code>", parse_mode=ParseMode.HTML)
        except Exception:
            pass


async def cb_back_profile(update, ctx):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    u = db_get_user(uid)
    if u["vip"] != 3:
        return
    target = int(q.data.split(":")[1])
    p = db_get_profile(target)
    if not p:
        return
    
    today = datetime.now(MSK).date().isoformat()
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        already = conn.execute("SELECT 1 FROM likes WHERE from_id=? AND to_id=? AND substr(ts,1,10)=?", (uid, target, today)).fetchone()
    
    text = (
        f"<b>🍁 Анкета</b>\n\n"
        f"Имя: {escape(str(p['name']))}\n"
        f"Возраст: {escape(str(p['age']))}\n"
        f"Город: {escape(str(p['city']))}\n"
        f"Диагноз: {escape(str(p['diagnosis']))}\n"
        f"О себе: {escape(str(p['about']))}"
    )
    
    rows = []
    if not already:
        rows.append([InlineKeyboardButton("❤️ Лайк", callback_data=f"like:{target}")])
    
    rows.append([
        InlineKeyboardButton("➡️ Дальше", callback_data=f"skip:{target}"),
        InlineKeyboardButton("🚨", callback_data=f"prof_report:{target}"),
    ])
    rows.append([InlineKeyboardButton("◀️ В меню", callback_data="menu:main")])

    try:
        await q.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
    except Exception:
        try:
            await q.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
        except Exception:
            pass


async def cb_prof_report(update, ctx):
    q = update.callback_query
    await q.answer("🚨 Жалоба отправлена", show_alert=True)
    reporter = q.from_user.id
    target = int(q.data.split(":")[1])
    p = db_get_profile(target)
    if not p:
        return
    text = (
        f"🚨 <b>Жалоба на анкету</b>\n"
        f"От: <code>{reporter}</code> @{escape(q.from_user.username or '-')}\n"
        f"На: <code>{target}</code>\n\n"
        f"<b>Анкета:</b>\n"
        f"Имя: {escape(str(p['name']))}\n"
        f"Возраст: {escape(str(p['age']))}\n"
        f"Город: {escape(str(p['city']))}\n"
        f"Диагноз: {escape(str(p['diagnosis']))}\n"
        f"О себе: {escape(str(p['about']))}"
    )
    await to_admin(ctx.bot, TOPIC_REPORTS, text, reply_markup=kb_report(target))


async def cb_menu_profile(update, ctx):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    u = db_get_user(uid, q.from_user)
    p = db_get_profile(uid)
    
    vip_str = vip_label(u['vip'])
    if u['vip'] > 0 and u['vip_until']:
        if u['vip_until'] == 'forever':
            vip_str += " (Навсегда)"
        else:
            vip_str += f" (до {fmt_dt(u['vip_until'])})"
        
    likes_str = "∞" if u['likes_left'] >= 10**9 else str(u['likes_left'])
    
    text = (
        f"👤 <b>Ваш профиль</b>\n\n"
        f"ID: <code>{uid}</code>\n"
        f"VIP: {vip_str}\n"
        f"Лайков сегодня: {likes_str}\n"
        f"Лимит символов: {'∞' if vip_char_limit(u['vip']) >= 10**9 else vip_char_limit(u['vip'])}\n"
    )
    if u["anon_until"]:
        if u["anon_until"] == 'forever':
            text += "Анон-доступ: Навсегда\n"
        else:
            text += f"Анон-доступ до: {fmt_dt(u['anon_until'])}\n"
    if p:
        text += (
            f"\n<b>Анкета:</b>\n"
            f"Имя: {escape(str(p['name']))}\n"
            f"Возраст: {escape(str(p['age']))}\n"
            f"Город: {escape(str(p['city']))}\n"
            f"Диагноз: {escape(str(p['diagnosis']))}\n"
            f"О себе: {escape(str(p['about']))}"
        )
    try:
        await q.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_profile_view())
    except Exception:
        try:
            await q.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb_profile_view())
        except Exception:
            pass


async def cb_profile_edit(update, ctx):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    u = db_get_user(uid, q.from_user)
    if u["circle_file_id"]:
        ctx.user_data["edit_text_mode"] = True
        limit = vip_char_limit(u["vip"])
        limit_txt = "без ограничений" if limit >= 10**9 else f"{limit} символов"
        p = db_get_profile(uid)
        current = PROFILE_TEMPLATE
        if p:
            current = (
                f"Имя: {p['name']}\n"
                f"Возраст: {p['age']}\n"
                f"Город: {p['city']}\n"
                f"Диагноз: {p['diagnosis']}\n"
                f"Описание о себе: {p['about']}"
            )
        try:
            await q.message.edit_text(
                "✏️ <b>Редактирование анкеты</b>\n\n"
                "Текущая анкета:\n"
                f"<code>{escape(current)}</code>\n\n"
                "Отправьте новую анкету строго по шаблону:\n\n"
                f"<code>{PROFILE_TEMPLATE}</code>\n\n"
                f"📌 Лимит: <b>{limit_txt}</b>.\n"
                "Все поля обязательны.\n"
                "⚠️ Изменения применяются сразу, без модерации.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_back("menu:main"),
            )
        except Exception:
            pass
    else:
        db_set(uid, circle_file_id=None)
        with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
            conn.execute("DELETE FROM profiles WHERE user_id=?", (uid,))
            conn.commit()
        try:
            await q.message.edit_text("✏️ Анкета сброшена. Начинаем заново.", reply_markup=kb_back())
        except Exception:
            pass
        await start_circle(q.message, ctx, uid)


async def cb_menu_shop(update, ctx):
    q = update.callback_query
    await q.answer()
    try:
        await q.message.edit_text(
            "🛒 <b>Магазин «Клёнчик»</b>\n\nВыберите товар:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_shop(),
        )
    except Exception:
        pass


async def cb_shop_item(update, ctx):
    q = update.callback_query
    await q.answer()
    key = q.data.split(":")[1]
    if key == "vip1":
        price = db_get_price("vip1")
        text = (
            f"🍁 <b>VIP 1 — Низкий</b>\nЦена: <b>{price} ₽</b> / мес\n\n"
            "• Анкета показывается чаще.\n"
            "• Лимит анкеты 500 символов.\n"
            "• 40 лайков в сутки."
        )
    elif key == "vip2":
        price = db_get_price("vip2")
        text = (
            f"🍁 <b>VIP 2 — Средний</b>\nЦена: <b>{price} ₽</b> / мес\n\n"
            "• Анкета показывается ещё чаще.\n"
            "• Лимит анкеты 800 символов.\n"
            "• 60 лайков в сутки."
        )
    elif key == "vip3":
        price = db_get_price("vip3")
        text = (
            f"🍁 <b>VIP 3 — Высокий</b>\nЦена: <b>{price} ₽</b> / мес\n\n"
            "• Анкета показывается чаще всех.\n"
            "• Безлимит анкеты.\n"
            "• Безлимит лайков.\n"
            "• Кнопка «⬅️ Назад» к предыдущей анкете."
        )
    else:
        price = db_get_price("anon")
        text = (
            f"💬 <b>Доступ в анонимный чат</b>\nЦена: <b>{price} ₽</b>\n\n"
            f"• +{ANON_PAID_DAYS} дн. анонимного чата сверх бесплатного часа."
        )
    rows = [
        [InlineKeyboardButton("💳 Купить", callback_data=f"shop:buy:{key}")],
        [InlineKeyboardButton("◀️ Назад", callback_data="menu:shop")],
    ]
    try:
        await q.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
    except Exception:
        pass


async def cb_shop_buy(update, ctx):
    q = update.callback_query
    await q.answer()
    key = q.data.split(":")[2]
    uid = q.from_user.id
    price = db_get_price(key)
    names = {"vip1": "VIP 1", "vip2": "VIP 2", "vip3": "VIP 3", "anon": "Доступ в анон-чат"}
    text = (
        f"💳 <b>Оплата</b>\n\n"
        f"Товар: {names.get(key, key)}\n"
        f"Сумма: <b>{price} ₽</b>\n\n"
        f"Напишите менеджеру: {MANAGER}\n"
        f"Пришлите ему ваш ID: <code>{uid}</code>\n\n"
        "После оплаты менеджер выдаст товар."
    )
    try:
        await q.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_back("menu:shop"))
    except Exception:
        pass


async def cb_menu_anon(update, ctx):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    left = anon_time_left(uid)
    if left >= 99999999:
        text = (
            f"💬 <b>Анонимный чат</b>\n\n"
            f"⏳ Осталось: <b>Навсегда</b>\n\n"
            "🔎 Найти собеседника — подберём пару.\n"
            "🛑 Стоп — завершить диалог."
        )
    elif left <= 0:
        price = db_get_price("anon")
        text = (
            f"⏰ <b>Бесплатный час израсходован.</b>\n\n"
            f"Купите доступ на {ANON_PAID_DAYS} дн. — <b>{price} ₽</b>.\n"
            f"Менеджер: {MANAGER}\nВаш ID: <code>{uid}</code>"
        )
    else:
        mins = left // 60
        text = (
            f"💬 <b>Анонимный чат</b>\n\n"
            f"⏳ Осталось: <b>{mins} мин.</b>\n\n"
            "🔎 Найти собеседника — подберём пару.\n"
            "🛑 Стоп — завершить диалог."
        )
    try:
        await q.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_anon())
    except Exception:
        pass


async def cb_anon_find(update, ctx):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    left = anon_time_left(uid)
    if left <= 0:
        return
    if get_partner(uid):
        return

    u = db_get_user(uid)
    if not u["anon_until"] and not u["anon_free_used"]:
        until = (datetime.now(timezone.utc) + timedelta(minutes=ANON_FREE_MIN)).isoformat()
        db_set(uid, anon_until=until, anon_free_used=1)

    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        row = conn.execute(
            "SELECT user_id, msg_id FROM anon_queue WHERE user_id != ? ORDER BY ts LIMIT 1", (uid,)
        ).fetchone()
        if row:
            partner, partner_msg_id = row[0], row[1]
            conn.execute("DELETE FROM anon_queue WHERE user_id IN (?,?)", (uid, partner))
            conn.execute("INSERT OR REPLACE INTO anon_pairs(user_a,user_b,ts) VALUES(?,?,?)",
                         (uid, partner, datetime.now(timezone.utc).isoformat()))
            conn.commit()

            if partner_msg_id:
                try:
                    await ctx.bot.edit_message_text(
                        chat_id=partner, message_id=partner_msg_id,
                        text="🎉 <b>Собеседник найден!</b>\nОбщайтесь. Стоп — для завершения.",
                        parse_mode=ParseMode.HTML, reply_markup=kb_anon()
                    )
                except Exception:
                    await safe_send(ctx.bot, partner, "🎉 <b>Собеседник найден!</b>\nОбщайтесь. Стоп — для завершения.", reply_markup=kb_anon())
            else:
                await safe_send(ctx.bot, partner, "🎉 <b>Собеседник найден!</b>\nОбщайтесь. Стоп — для завершения.", reply_markup=kb_anon())

            try:
                await q.message.edit_text(
                    "🎉 <b>Собеседник найден!</b>\nОбщайтесь. Стоп — для завершения.",
                    parse_mode=ParseMode.HTML, reply_markup=kb_anon(),
                )
            except Exception:
                pass
        else:
            msg_id = q.message.message_id
            conn.execute("INSERT OR REPLACE INTO anon_queue(user_id,ts,msg_id) VALUES(?,?,?)",
                         (uid, datetime.now(timezone.utc).isoformat(), msg_id))
            conn.commit()
            try:
                await q.message.edit_text("🔎 Ищем собеседника...", reply_markup=kb_anon())
            except Exception:
                pass


async def cb_anon_stop(update, ctx):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    partner = get_partner(uid)
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        conn.execute("DELETE FROM anon_queue WHERE user_id=?", (uid,))
        if partner:
            conn.execute(
                "DELETE FROM anon_pairs WHERE (user_a=? AND user_b=?) OR (user_a=? AND user_b=?)",
                (uid, partner, partner, uid),
            )
        conn.commit()
    try:
        await q.message.edit_text("🛑 Чат завершён.", reply_markup=kb_back())
    except Exception:
        pass
    if partner:
        await safe_send(ctx.bot, partner, "🛑 Собеседник завершил чат.", reply_markup=kb_anon())


async def cb_menu_support(update, ctx):
    q = update.callback_query
    await q.answer()
    ctx.user_data["support_mode"] = True
    try:
        await q.message.edit_text(
            "🆘 <b>Поддержка</b>\n\nНапишите ваш вопрос одним сообщением.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back(),
        )
    except Exception:
        pass


async def cb_app_accept(update, ctx):
    q = update.callback_query
    if not await is_admin(ctx.bot, q.from_user.id):
        return await q.answer("Нет прав", show_alert=True)
    uid = int(q.data.split(":")[2])
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        conn.execute("UPDATE profiles SET approved=1, rejected=0 WHERE user_id=?", (uid,))
        conn.commit()
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await safe_send(ctx.bot, uid, "✅ Ваша анкета одобрена! Теперь вам доступны все функции.")
    await safe_send(
        ctx.bot, uid,
        "🍁 <b>Главное меню</b>",
        reply_markup=kb_main(uid == ADMIN_ID),
    )
    await q.answer("Одобрено")


async def cb_app_reject(update, ctx):
    q = update.callback_query
    if not await is_admin(ctx.bot, q.from_user.id):
        return await q.answer("Нет прав", show_alert=True)
    uid = int(q.data.split(":")[2])
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        conn.execute("UPDATE profiles SET approved=0, rejected=1 WHERE user_id=?", (uid,))
        conn.execute("UPDATE users SET banned=1, ban_reason='Анкета отклонена' WHERE user_id=?", (uid,))
        conn.commit()
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await notify_user(ctx.bot, uid, PROFILE_REJECTED)
    await q.answer("Отклонено")


async def cb_app_rewrite(update, ctx):
    q = update.callback_query
    if not await is_admin(ctx.bot, q.from_user.id):
        return await q.answer("Нет прав", show_alert=True)
    uid = int(q.data.split(":")[2])
    db_set(uid, circle_file_id=None)
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        conn.execute("DELETE FROM profiles WHERE user_id=?", (uid,))
        conn.commit()
    phrase = " · ".join(random.sample(CODE_WORDS, 3))
    db_set(uid, circle_phrase=phrase)
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await safe_send(
        ctx.bot, uid,
        "✏️ Администратор попросил переписать анкету.\n\n"
        "🎥 Запишите видео-кружок с фразой:\n\n"
        f"<code>{phrase}</code>",
    )
    await q.answer("Отправлено")


async def cb_ticket_reply(update, ctx):
    q = update.callback_query
    if not await is_admin(ctx.bot, q.from_user.id):
        return await q.answer("Нет прав", show_alert=True)
    uid = int(q.data.split(":")[2])
    ctx.user_data["ticket_reply_to"] = uid
    await q.message.reply_text(f"✍️ Напишите ответ для <code>{uid}</code>:", parse_mode=ParseMode.HTML)
    await q.answer()


async def cb_report_ban(update, ctx):
    q = update.callback_query
    if not await is_admin(ctx.bot, q.from_user.id):
        return await q.answer("Нет прав", show_alert=True)
    uid = int(q.data.split(":")[2])
    db_set(uid, banned=1, ban_reason="Жалоба")
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await safe_send(ctx.bot, uid, f"🚫 Забанены по жалобе.\nРазбан за {UNBAN_PRICE}₽ у {MANAGER}")
    await q.answer("Забанен")


async def cb_report_ignore(update, ctx):
    q = update.callback_query
    if not await is_admin(ctx.bot, q.from_user.id):
        return await q.answer("Нет прав", show_alert=True)
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await q.answer("Игнорировано")


async def cb_admin_main(update, ctx):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ADMIN_ID:
        return
    try:
        await q.message.edit_text("🛠 <b>Админ-панель</b>", parse_mode=ParseMode.HTML, reply_markup=kb_admin())
    except Exception:
        pass


async def cb_admin_count(update, ctx):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ADMIN_ID:
        return
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM users WHERE banned=0").fetchone()[0]
        approved = conn.execute("SELECT COUNT(*) FROM profiles WHERE approved=1").fetchone()[0]
    try:
        await q.message.edit_text(
            f"👥 Всего: <b>{total}</b>\nАктивных: <b>{active}</b>\nАнкет одобрено: <b>{approved}</b>",
            parse_mode=ParseMode.HTML, reply_markup=kb_back("admin:main"),
        )
    except Exception:
        pass


async def cb_admin_ban(update, ctx):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ADMIN_ID:
        return
    ctx.user_data["admin_input"] = ("ban",)
    try:
        await q.message.edit_text("Введите ID для бана:", reply_markup=kb_back("admin:main"))
    except Exception:
        pass


async def cb_admin_unban(update, ctx):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ADMIN_ID:
        return
    ctx.user_data["admin_input"] = ("unban",)
    try:
        await q.message.edit_text("Введите ID для разбана:", reply_markup=kb_back("admin:main"))
    except Exception:
        pass


async def cb_admin_give_vip(update, ctx):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ADMIN_ID:
        return
    ctx.user_data["admin_input"] = ("give_vip_uid",)
    try:
        await q.message.edit_text("Введите ID пользователя:", reply_markup=kb_back("admin:main"))
    except Exception:
        pass


async def cb_admin_vip_lvl(update, ctx):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ADMIN_ID:
        return
    parts = q.data.split(":")
    uid, lvl = int(parts[2]), int(parts[3])
    ctx.user_data.pop("admin_pending_vip_uid", None)
    ctx.user_data["admin_input"] = ("give_vip_days", uid, lvl)
    try:
        await q.message.edit_text(
            f"Введите число дней для VIP {lvl} (0 = навсегда):",
            reply_markup=kb_back("admin:main"),
        )
    except Exception:
        pass


async def cb_admin_take_vip(update, ctx):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ADMIN_ID:
        return
    ctx.user_data["admin_input"] = ("take_vip",)
    try:
        await q.message.edit_text("Введите ID, чтобы снять VIP:", reply_markup=kb_back("admin:main"))
    except Exception:
        pass


async def cb_admin_give_anon(update, ctx):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ADMIN_ID:
        return
    ctx.user_data["admin_input"] = ("give_anon",)
    try:
        await q.message.edit_text(
            "Введите: <code>ID дни</code> (0 = навсегда).\nПример: <code>123456789 30</code>",
            parse_mode=ParseMode.HTML, reply_markup=kb_back("admin:main"),
        )
    except Exception:
        pass


async def cb_admin_take_anon(update, ctx):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ADMIN_ID:
        return
    ctx.user_data["admin_input"] = ("take_anon",)
    try:
        await q.message.edit_text("Введите ID, чтобы снять анон:", reply_markup=kb_back("admin:main"))
    except Exception:
        pass


async def cb_admin_prices(update, ctx):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ADMIN_ID:
        return
    try:
        await q.message.edit_text("💲 <b>Цены:</b>", parse_mode=ParseMode.HTML,
                                  reply_markup=kb_admin_prices())
    except Exception:
        pass


async def cb_admin_price_set(update, ctx):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ADMIN_ID:
        return
    key = q.data.split(":")[2]
    ctx.user_data["admin_input"] = ("price", key)
    try:
        await q.message.edit_text(f"Новая цена для <b>{key}</b>:", parse_mode=ParseMode.HTML,
                                  reply_markup=kb_back("admin:prices"))
    except Exception:
        pass


async def cb_admin_users(update, ctx):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ADMIN_ID:
        return
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT user_id, username, full_name, vip, banned "
            "FROM users ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
    lines = ["📋 <b>Последние 50 юзеров:</b>", ""]
    for r in rows:
        st = "🚫" if r["banned"] else "✅"
        if r["username"]:
            who = f"@{escape(str(r['username']))}"
        else:
            who = escape(str(r["full_name"])) if r["full_name"] else "без имени"
        lines.append(f"{st} <code>{r['user_id']}</code> · {who} · VIP{r['vip']}")
    try:
        await q.message.edit_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back("admin:main"),
        )
    except Exception:
        pass


async def cb_noop(update, ctx):
    await update.callback_query.answer()


async def error_handler(update, ctx):
    pass


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.ChatType.PRIVATE, ban_mw), group=-500)
    app.add_handler(CallbackQueryHandler(ban_cb_mw), group=-500)
    app.add_handler(CallbackQueryHandler(moderation_mw), group=-490)

    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^/start"), cmd_start), group=-450)

    app.add_handler(MessageHandler(filters.Chat(ADMIN_CHAT_ID), admin_chat_handler), group=-400)

    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, anon_relay), group=-300)

    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, private_message), group=0)

    app.add_handler(CallbackQueryHandler(cb_agree_yes, pattern=r"^agree:yes$"))
    app.add_handler(CallbackQueryHandler(cb_agree_no, pattern=r"^agree:no$"))

    app.add_handler(CallbackQueryHandler(cb_menu_main, pattern=r"^menu:main$"))
    app.add_handler(CallbackQueryHandler(cb_menu_profiles, pattern=r"^menu:profiles$"))
    app.add_handler(CallbackQueryHandler(cb_menu_profile, pattern=r"^menu:profile$"))
    app.add_handler(CallbackQueryHandler(cb_menu_shop, pattern=r"^menu:shop$"))
    app.add_handler(CallbackQueryHandler(cb_menu_anon, pattern=r"^menu:anon$"))
    app.add_handler(CallbackQueryHandler(cb_menu_support, pattern=r"^menu:support$"))

    app.add_handler(CallbackQueryHandler(cb_like, pattern=r"^like:"))
    app.add_handler(CallbackQueryHandler(cb_skip, pattern=r"^skip:"))
    app.add_handler(CallbackQueryHandler(cb_back_profile, pattern=r"^back:"))
    app.add_handler(CallbackQueryHandler(cb_prof_report, pattern=r"^prof_report:"))
    app.add_handler(CallbackQueryHandler(cb_view_liker, pattern=r"^view_liker:"))

    app.add_handler(CallbackQueryHandler(cb_profile_edit, pattern=r"^profile:edit$"))

    app.add_handler(CallbackQueryHandler(cb_anon_find, pattern=r"^anon:find$"))
    app.add_handler(CallbackQueryHandler(cb_anon_stop, pattern=r"^anon:stop$"))

    app.add_handler(CallbackQueryHandler(cb_shop_item, pattern=r"^shop:(vip1|vip2|vip3|anon)$"))
    app.add_handler(CallbackQueryHandler(cb_shop_buy, pattern=r"^shop:buy:"))

    app.add_handler(CallbackQueryHandler(cb_app_accept, pattern=r"^app:accept:"))
    app.add_handler(CallbackQueryHandler(cb_app_reject, pattern=r"^app:reject:"))
    app.add_handler(CallbackQueryHandler(cb_app_rewrite, pattern=r"^app:rewrite:"))

    app.add_handler(CallbackQueryHandler(cb_ticket_reply, pattern=r"^ticket:reply:"))
    app.add_handler(CallbackQueryHandler(cb_report_ban, pattern=r"^report:ban:"))
    app.add_handler(CallbackQueryHandler(cb_report_ignore, pattern=r"^report:ignore:"))

    app.add_handler(CallbackQueryHandler(cb_admin_main, pattern=r"^admin:main$"))
    app.add_handler(CallbackQueryHandler(cb_admin_count, pattern=r"^admin:count$"))
    app.add_handler(CallbackQueryHandler(cb_admin_ban, pattern=r"^admin:ban$"))
    app.add_handler(CallbackQueryHandler(cb_admin_unban, pattern=r"^admin:unban$"))
    app.add_handler(CallbackQueryHandler(cb_admin_give_vip, pattern=r"^admin:give_vip$"))
    app.add_handler(CallbackQueryHandler(cb_admin_vip_lvl, pattern=r"^admin:vip_lvl:"))
    app.add_handler(CallbackQueryHandler(cb_admin_take_vip, pattern=r"^admin:take_vip$"))
    app.add_handler(CallbackQueryHandler(cb_admin_give_anon, pattern=r"^admin:give_anon$"))
    app.add_handler(CallbackQueryHandler(cb_admin_take_anon, pattern=r"^admin:take_anon$"))
    app.add_handler(CallbackQueryHandler(cb_admin_prices, pattern=r"^admin:prices$"))
    app.add_handler(CallbackQueryHandler(cb_admin_price_set, pattern=r"^admin:price_set:"))
    app.add_handler(CallbackQueryHandler(cb_admin_users, pattern=r"^admin:users$"))

    app.add_handler(CallbackQueryHandler(cb_noop, pattern=r"^noop$"))

    app.add_error_handler(error_handler)

    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
