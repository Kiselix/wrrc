import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from html import escape

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ContentType
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
    Message, PreCheckoutQuery, ReplyKeyboardMarkup, KeyboardButton, InputMediaPhoto,
)
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "")
DB_PATH = os.getenv("DB_PATH", "database.db")
POST_CHAT_ID = os.getenv("POST_CHAT_ID", "")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
router = Router()

class CreatePost(StatesGroup):
    photos = State(); title = State(); price = State(); contact = State()

MAIN_KB = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="Создать пост"), KeyboardButton(text="Мой профиль")],
    [KeyboardButton(text="Купить публикации"), KeyboardButton(text="Купить подписку")],
    [KeyboardButton(text="Помощь"), KeyboardButton(text="Поддержка")],
], resize_keyboard=True)

async def db_exec(sql, params=(), fetch=False, many=False):
    async with aiosqlite.connect(DB_PATH) as db:
        if many: await db.executemany(sql, params)
        else: await db.execute(sql, params)
        await db.commit()
        if fetch:
            cur = await db.execute(sql, params)
            return await cur.fetchall()

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT, name TEXT, posts INTEGER DEFAULT 0, subscription_until TEXT, banned INTEGER DEFAULT 0, free_used INTEGER DEFAULT 0, created_at TEXT, last_active TEXT);
        CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, payload TEXT UNIQUE, stars INTEGER, kind TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, photo_ids TEXT, title TEXT, price TEXT, contact TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS channels (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT UNIQUE, title TEXT, url TEXT, active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS broadcasts (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, created_at TEXT, sent INTEGER DEFAULT 0, failed INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS admin_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT, target_id INTEGER, created_at TEXT);
        INSERT OR IGNORE INTO settings(key,value) VALUES ('subscription_days','30');
        """)
        await db.commit()

def now(): return datetime.now(timezone.utc).isoformat()
def parse_dt(value):
    try: return datetime.fromisoformat(value) if value else None
    except ValueError: return None

async def upsert_user(user):
    await db_exec("INSERT INTO users(id,username,name,created_at,last_active) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET username=excluded.username,name=excluded.name,last_active=excluded.last_active", (user.id, user.username or "", user.full_name, now(), now()))

async def get_user(uid):
    rows = await db_exec("SELECT id,username,name,posts,subscription_until,banned,free_used FROM users WHERE id=?", (uid,), True)
    return rows[0] if rows else None

async def is_active_sub(row):
    return bool(row and parse_dt(row[4]) and parse_dt(row[4]) > datetime.now(timezone.utc))

async def required_channels():
    return await db_exec("SELECT chat_id,title,url FROM channels WHERE active=1", fetch=True)

async def check_access(message: Message, bot: Bot):
    row = await get_user(message.from_user.id)
    if row and row[5]:
        await message.answer("Ваш аккаунт заблокирован."); return False
    channels = await required_channels(); missing = []
    for chat_id, title, url in channels:
        try:
            member = await bot.get_chat_member(chat_id, message.from_user.id)
            if member.status in ("left", "kicked"): missing.append((title, url))
        except Exception: missing.append((title, url))
    if missing:
        kb = [[InlineKeyboardButton(text=f"Подписаться: {t}", url=u)] for t, u in missing]
        kb.append([InlineKeyboardButton(text="Проверить подписку", callback_data="check_sub")])
        await message.answer("Для использования бота подпишитесь на обязательные каналы.", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)); return False
    return True

def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Статистика", callback_data="a_stats"), InlineKeyboardButton(text="Пользователи", callback_data="a_users")],[InlineKeyboardButton(text="Каналы", callback_data="a_channels")],[InlineKeyboardButton(text="Рассылка", callback_data="a_broadcast")]])

async def send_carousel(bot: Bot, chat_id: int, photo_ids: list[str], caption: str):
    if not 1 <= len(photo_ids) <= 10:
        raise ValueError("Telegram media group supports from 1 to 10 photos")
    media = [InputMediaPhoto(media=photo_id, caption=caption if index == 0 else None,
                             parse_mode=ParseMode.HTML) for index, photo_id in enumerate(photo_ids)]
    if len(media) == 1:
        await bot.send_photo(chat_id, photo_ids[0], caption=caption, parse_mode=ParseMode.HTML)
    else:
        await bot.send_media_group(chat_id, media=media)

@router.message(CommandStart())
async def start(message: Message, state: FSMContext, bot: Bot):
    await upsert_user(message.from_user)
    if not await check_access(message, bot): return
    await message.answer("Добро пожаловать! Создавайте рекламные посты и оплачивайте публикации Telegram Stars.", reply_markup=MAIN_KB)

@router.message(Command("help"))
@router.message(F.text == "Помощь")
async def help_msg(message: Message):
    await message.answer("Нажмите «Создать пост», загрузите фото и заполните данные. Первый пост бесплатный. Дальше доступны пакеты публикаций или подписка.")

@router.callback_query(F.data == "check_sub")
async def check_sub(call: CallbackQuery, bot: Bot):
    await call.answer()
    if await check_access(call.message, bot): await call.message.answer("Подписка подтверждена.", reply_markup=MAIN_KB)

@router.message(F.text == "Мой профиль")
@router.message(Command("profile"))
async def profile(message: Message):
    row = await get_user(message.from_user.id)
    sub = parse_dt(row[4]) if row else None
    sub_text = sub.astimezone().strftime("%d.%m.%Y %H:%M") if sub and sub > datetime.now(timezone.utc) else "нет"
    await message.answer(f"Профиль @{escape(message.from_user.username or str(message.from_user.id))}\nДоступно публикаций: {row[3]}\nБесплатный пост использован: {'да' if row[6] else 'нет'}\nПодписка до: {sub_text}")

async def create_start(message: Message, state: FSMContext, bot: Bot):
    if not await check_access(message, bot): return
    await state.clear(); await state.set_state(CreatePost.photos)
    await state.update_data(photos=[])
    await message.answer("Отправьте фотографию товара. Можно отправить несколько, затем напишите «Готово».")

@router.message(F.text == "Создать пост")
@router.message(Command("create"))
async def create_cmd(message: Message, state: FSMContext, bot: Bot): await create_start(message, state, bot)

@router.message(CreatePost.photos, F.photo)
async def add_photo(message: Message, state: FSMContext):
    data = await state.get_data(); photos = data.get("photos", []); photos.append(message.photo[-1].file_id); await state.update_data(photos=photos)
    await message.answer(f"Фото добавлено ({len(photos)}). Отправьте еще или напишите «Готово».")

@router.message(CreatePost.photos, F.text.casefold() == "готово")
async def photos_done(message: Message, state: FSMContext): await state.set_state(CreatePost.title); await message.answer("Введите название товара.")

@router.message(CreatePost.title)
async def title_done(message: Message, state: FSMContext): await state.update_data(title=message.text); await state.set_state(CreatePost.price); await message.answer("Введите цену.")

@router.message(CreatePost.price)
async def price_done(message: Message, state: FSMContext): await state.update_data(price=message.text); await state.set_state(CreatePost.contact); await message.answer("Введите username для заказа, например @username.")

@router.message(CreatePost.contact)
async def contact_done(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data(); data["contact"] = message.text
    caption = f"{escape(data['title'])}\n\nЦена {escape(data['price'])}\nДля заказа пишите {escape(data['contact'])}"
    await state.update_data(**data)
    if data["photos"]: await send_carousel(bot, message.chat.id, data["photos"], caption)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Опубликовать", callback_data="publish")],[InlineKeyboardButton(text="Отменить", callback_data="cancel_post")]])
    await message.answer("Предпросмотр готов. Выберите действие.", reply_markup=kb)

@router.callback_query(F.data == "cancel_post")
async def cancel_post(call: CallbackQuery, state: FSMContext): await state.clear(); await call.answer("Отменено"); await call.message.answer("Создание поста отменено.", reply_markup=MAIN_KB)

@router.callback_query(F.data == "publish")
async def publish(call: CallbackQuery, state: FSMContext, bot: Bot):
    row = await get_user(call.from_user.id); data = await state.get_data()
    if not row: await call.answer("Ошибка профиля", show_alert=True); return
    if not (row[3] > 0 or not row[6] or await is_active_sub(row)):
        await call.answer("Нет доступных публикаций", show_alert=True); await send_buy_menu(call.message); return
    if row[3] > 0 and not await is_active_sub(row): await db_exec("UPDATE users SET posts=posts-1 WHERE id=?", (call.from_user.id,))
    elif not row[6] and not await is_active_sub(row): await db_exec("UPDATE users SET free_used=1 WHERE id=?", (call.from_user.id,))
    photo_ids = ",".join(data.get("photos", [])); await db_exec("INSERT INTO posts(user_id,photo_ids,title,price,contact,created_at) VALUES(?,?,?,?,?,?)", (call.from_user.id, photo_ids, data.get("title",""), data.get("price",""), data.get("contact",""), now()))
    destination = int(POST_CHAT_ID) if POST_CHAT_ID else call.message.chat.id
    caption = f"{escape(data['title'])}\n\nЦена {escape(data['price'])}\nДля заказа пишите {escape(data['contact'])}"
    await send_carousel(bot, destination, data.get("photos", []), caption)
    await state.clear(); await call.answer("Опубликовано"); await call.message.answer("Пост успешно опубликован каруселью.", reply_markup=MAIN_KB)

async def send_buy_menu(message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="1 пост — 50 ⭐", callback_data="buy_1")],[InlineKeyboardButton(text="5 постов — 100 ⭐", callback_data="buy_5")],[InlineKeyboardButton(text="10 постов — 200 ⭐", callback_data="buy_10")],[InlineKeyboardButton(text="Подписка — 100 ⭐", callback_data="buy_sub")]])
    await message.answer("Выберите оплату:", reply_markup=kb)

@router.message(F.text == "Купить публикации")
@router.message(F.text == "Купить подписку")
@router.message(Command("buy"))
async def buy(message: Message): await send_buy_menu(message)

@router.callback_query(F.data.startswith("buy_"))
async def invoice(call: CallbackQuery, bot: Bot):
    kind = call.data[4:]; amounts = {"1": (1,50), "5": (5,100), "10": (10,200), "sub": (0,100)}; qty, stars = amounts[kind]
    payload = f"{kind}:{call.from_user.id}:{int(datetime.now().timestamp())}"
    await bot.send_invoice(call.from_user.id, title="Покупка в боте", description="Публикации или подписка", payload=payload, currency="XTR", prices=[LabeledPrice(label="Telegram Stars", amount=stars)])
    await call.answer()

@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery): await query.answer(ok=True)

@router.message(F.content_type == ContentType.SUCCESSFUL_PAYMENT)
async def payment(message: Message):
    p = message.successful_payment; kind = p.invoice_payload.split(":", 1)[0]
    exists = await db_exec("SELECT id FROM payments WHERE payload=?", (p.invoice_payload,), True)
    if exists: return
    await db_exec("INSERT INTO payments(user_id,payload,stars,kind,created_at) VALUES(?,?,?,?,?)", (message.from_user.id, p.invoice_payload, p.total_amount, kind, now()))
    if kind == "sub":
        days_row = await db_exec("SELECT value FROM settings WHERE key='subscription_days'", fetch=True); days = int(days_row[0][0])
        await db_exec("UPDATE users SET subscription_until=? WHERE id=?", ((datetime.now(timezone.utc)+timedelta(days=days)).isoformat(), message.from_user.id))
    else: await db_exec("UPDATE users SET posts=posts+? WHERE id=?", (int(kind), message.from_user.id))
    await message.answer("Оплата прошла успешно.", reply_markup=MAIN_KB)

@router.message(Command("admin"))
async def admin(message: Message):
    if message.from_user.id == ADMIN_ID: await message.answer("Админ-панель", reply_markup=admin_kb())

def admin_only(message): return message.from_user.id == ADMIN_ID

@router.message(Command("ban"))
async def ban_user(message: Message):
    if not admin_only(message): return
    try: uid = int(message.text.split()[1])
    except (IndexError, ValueError): await message.answer("Использование: /ban ID"); return
    await db_exec("UPDATE users SET banned=1 WHERE id=?", (uid,)); await message.answer("Пользователь заблокирован.")

@router.message(Command("unban"))
async def unban_user(message: Message):
    if not admin_only(message): return
    try: uid = int(message.text.split()[1])
    except (IndexError, ValueError): await message.answer("Использование: /unban ID"); return
    await db_exec("UPDATE users SET banned=0 WHERE id=?", (uid,)); await message.answer("Пользователь разблокирован.")

@router.message(Command("grant"))
async def grant_days(message: Message):
    if not admin_only(message): return
    try: uid, days = map(int, message.text.split()[1:3])
    except (IndexError, ValueError): await message.answer("Использование: /grant ID ДНИ"); return
    until = datetime.now(timezone.utc) + timedelta(days=days)
    await db_exec("UPDATE users SET subscription_until=? WHERE id=?", (until.isoformat(), uid)); await message.answer("Подписка выдана.")

@router.message(Command("addposts"))
async def add_posts(message: Message):
    if not admin_only(message): return
    try: uid, count = map(int, message.text.split()[1:3])
    except (IndexError, ValueError): await message.answer("Использование: /addposts ID КОЛИЧЕСТВО"); return
    await db_exec("UPDATE users SET posts=posts+? WHERE id=?", (count, uid)); await message.answer("Публикации начислены.")

@router.message(Command("channel"))
async def add_channel(message: Message):
    if not admin_only(message): return
    raw = message.text.partition(" ")[2]
    fields = [x.strip() for x in raw.split("|", 2)]
    if len(fields) != 3: await message.answer("Использование: /channel CHAT_ID | НАЗВАНИЕ | URL"); return
    chat_id, title, url = fields
    await db_exec("INSERT OR REPLACE INTO channels(chat_id,title,url,active) VALUES(?,?,?,1)", (chat_id, title, url)); await message.answer("Канал добавлен.")

@router.message(Command("delchannel"))
async def del_channel(message: Message):
    if not admin_only(message): return
    try: chat_id = message.text.split()[1]
    except IndexError: await message.answer("Использование: /delchannel CHAT_ID"); return
    await db_exec("DELETE FROM channels WHERE chat_id=?", (chat_id,)); await message.answer("Канал удален.")

@router.message(Command("broadcast"))
async def broadcast(message: Message, bot: Bot):
    if not admin_only(message): return
    text = message.text.partition(" ")[2].strip()
    if not text: await message.answer("Использование: /broadcast ТЕКСТ"); return
    users = await db_exec("SELECT id FROM users WHERE banned=0", fetch=True); sent = failed = 0
    for (uid,) in users:
        try: await bot.send_message(uid, text); sent += 1
        except Exception: failed += 1
    await db_exec("INSERT INTO broadcasts(text,created_at,sent,failed) VALUES(?,?,?,?)", (text, now(), sent, failed)); await message.answer(f"Рассылка завершена. Отправлено: {sent}, ошибок: {failed}")

@router.callback_query(F.data == "a_stats")
async def stats(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    u = (await db_exec("SELECT COUNT(*) FROM users", fetch=True))[0][0]; p = (await db_exec("SELECT COUNT(*) FROM posts", fetch=True))[0][0]; s = (await db_exec("SELECT COALESCE(SUM(stars),0) FROM payments", fetch=True))[0][0]
    await call.message.answer(f"Пользователи: {u}\nПосты: {p}\nДоход: {s} ⭐")

@router.callback_query(F.data == "a_users")
async def users(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    rows = await db_exec("SELECT id,username,posts,banned FROM users ORDER BY created_at DESC LIMIT 30", fetch=True)
    text = "\n".join(f"{r[0]} @{r[1]} | постов {r[2]} | {'бан' if r[3] else 'активен'}" for r in rows) or "Пользователей нет"
    await call.message.answer(text)

async def main():
    if not BOT_TOKEN: raise RuntimeError("Укажите BOT_TOKEN в .env")
    await init_db(); bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML)); dp = Dispatcher(); dp.include_router(router); await dp.start_polling(bot)

if __name__ == "__main__": asyncio.run(main())
