# -*- coding: utf-8 -*-
"""
Telegram-бот «3D-прогулки по планам» для ссылки в шапке Instagram.

Что умеет:
  • приветствие и меню: демо, тарифы, примеры, заявка, FAQ, вопрос
  • выдаёт демо-прогулку и PDF-чек-лист (в т.ч. по кодовому слову ПЛАН)
  • анкета заказа из 5 вопросов с приёмом плана → заявка приходит вам
  • вы отвечаете клиенту прямо из бота (Reply на заявку)
  • дожим: через 24 ч и через 3 дня тем, кто взял демо, но не заказал
  • /stats — сколько пришло, взяли демо, оставили заявку, по источникам

Запуск: python bot.py   (настройки в файле settings.txt)
"""
import asyncio
import logging
import os
import sqlite3
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

import texts as T

BASE = Path(__file__).resolve().parent


# ---------------------------------------------------------------- настройки
def load_env(path: Path) -> None:
    """Простой загрузчик .env без лишних библиотек."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


load_env(BASE / "settings.txt")  # настройки: откройте settings.txt Блокнотом
load_env(BASE / ".env")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
DEMO_URL = os.environ.get("DEMO_URL", "https://example.com")
INSTAGRAM_URL = os.environ.get("INSTAGRAM_URL", "")
CHANNEL_URL = os.environ.get("CHANNEL_URL", "")
CONTACT = os.environ.get("CONTACT_USERNAME", "")
CHECKLIST_FILE = BASE / os.environ.get("CHECKLIST_FILE", "files/checklist.pdf")
DEMO_VIDEO_FILE = BASE / os.environ.get("DEMO_VIDEO_FILE", "files/demo.mp4")
CASES_DIR = BASE / os.environ.get("CASES_DIR", "files/cases")
FOLLOWUP_1_HOURS = float(os.environ.get("FOLLOWUP_1_HOURS", "24"))
FOLLOWUP_2_HOURS = float(os.environ.get("FOLLOWUP_2_HOURS", "72"))
CODEWORDS = {"план", "plan", "3d", "3д"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")


# ---------------------------------------------------------------- база
db = sqlite3.connect(BASE / "bot.db")
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY, name TEXT, username TEXT, source TEXT,
        started_at REAL, demo_at REAL, ordered INTEGER DEFAULT 0,
        f1_sent INTEGER DEFAULT 0, f2_sent INTEGER DEFAULT 0, blocked INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS links (admin_msg_id INTEGER PRIMARY KEY, user_id INTEGER);
    """
)
db.commit()


def upsert_user(m: Message, source: str | None = None) -> None:
    u = m.from_user
    row = db.execute("SELECT id FROM users WHERE id=?", (u.id,)).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO users (id, name, username, source, started_at) VALUES (?,?,?,?,?)",
            (u.id, u.full_name, u.username or "", source or "прямой вход", time.time()),
        )
    else:
        db.execute("UPDATE users SET name=?, username=?, blocked=0 WHERE id=?",
                   (u.full_name, u.username or "", u.id))
    db.commit()


def mark(user_id: int, field: str, value) -> None:
    db.execute(f"UPDATE users SET {field}=? WHERE id=?", (value, user_id))
    db.commit()


def link(admin_msg_id: int, user_id: int) -> None:
    db.execute("INSERT OR REPLACE INTO links VALUES (?,?)", (admin_msg_id, user_id))
    db.commit()


# ---------------------------------------------------------------- клавиатуры
def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=T.BTN_DEMO)],
            [KeyboardButton(text=T.BTN_PRICES), KeyboardButton(text=T.BTN_CASES)],
            [KeyboardButton(text=T.BTN_ORDER)],
            [KeyboardButton(text=T.BTN_HOW), KeyboardButton(text=T.BTN_CONTACT)],
        ],
        resize_keyboard=True,
    )


def inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r] for r in rows]
    )


ORDER_BTN = inline([[(T.BTN_ORDER, "order"), (T.BTN_PRICES, "prices")]])

OBJECTS = {"o_flat": T.OBJ_FLAT, "o_ab": T.OBJ_AB, "o_house": T.OBJ_HOUSE, "o_unsure": T.OBJ_UNSURE}
GOALS = {"g_buy": T.GOAL_BUY, "g_reno": T.GOAL_RENO, "g_replan": T.GOAL_REPLAN,
         "g_sell": T.GOAL_SELL, "g_build": T.GOAL_BUILD}


# ---------------------------------------------------------------- состояния
class Order(StatesGroup):
    object = State()
    area = State()
    goal = State()
    plan = State()
    wishes = State()


class Ask(StatesGroup):
    question = State()


router = Router()


def who(m_or_u) -> dict:
    u = m_or_u.from_user
    return {"name": u.full_name, "username": f"@{u.username}" if u.username else "(без @username)",
            "user_id": u.id}


# ---------------------------------------------------------------- админ: ответы и статистика
@router.message(F.from_user.id == ADMIN_ID, F.reply_to_message)
async def admin_reply(m: Message, bot: Bot):
    row = db.execute("SELECT user_id FROM links WHERE admin_msg_id=?",
                     (m.reply_to_message.message_id,)).fetchone()
    if not row:
        return await m.answer("Это сообщение не связано с клиентом. Ответьте на заявку или вопрос.")
    try:
        if m.text:
            await bot.send_message(row[0], T.ADMIN_REPLY_PREFIX + m.html_text)
        else:
            await bot.send_message(row[0], T.ADMIN_REPLY_PREFIX.strip())
            await m.copy_to(row[0])
        await m.answer(T.ADMIN_REPLY_SENT)
    except Exception as e:  # клиент заблокировал бота и т.п.
        await m.answer(T.ADMIN_REPLY_FAIL.format(error=e))


@router.message(Command("stats"), F.from_user.id == ADMIN_ID)
async def stats(m: Message):
    total, demo, orders = db.execute(
        "SELECT COUNT(*), COUNT(demo_at), SUM(ordered) FROM users").fetchone()
    by_src = db.execute(
        "SELECT source, COUNT(*), COUNT(demo_at), SUM(ordered) FROM users GROUP BY source ORDER BY 2 DESC"
    ).fetchall()
    lines = [f"👥 Пришли: {total}\n🏠 Взяли демо: {demo}\n✍️ Заявки: {orders or 0}\n", "<b>По источникам</b> (пришли / демо / заявки):"]
    lines += [f"• {s}: {a} / {b} / {c or 0}" for s, a, b, c in by_src]
    await m.answer("\n".join(lines))


@router.message(Command("myid"))
async def myid(m: Message):
    await m.answer(f"Ваш ID: <code>{m.from_user.id}</code>")


# ---------------------------------------------------------------- старт
@router.message(CommandStart())
async def start(m: Message, command: CommandObject, state: FSMContext, bot: Bot):
    await state.clear()
    payload = (command.args or "").strip().lower()
    upsert_user(m, source=payload or None)
    if payload in CODEWORDS:
        await m.answer(T.WELCOME_CODEWORD, reply_markup=main_menu())
        await send_demo(m, bot)
    else:
        await m.answer(T.WELCOME, reply_markup=main_menu())


@router.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(T.CANCELLED, reply_markup=main_menu())


@router.message(Command("menu"))
async def menu(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(T.WELCOME, reply_markup=main_menu())


# ---------------------------------------------------------------- демо
async def send_demo(m: Message, bot: Bot):
    upsert_user(m)
    await m.answer(T.DEMO.format(demo_url=DEMO_URL), disable_web_page_preview=False)
    if DEMO_VIDEO_FILE.exists():
        await m.answer_video(FSInputFile(DEMO_VIDEO_FILE))
    if CHECKLIST_FILE.exists():
        await m.answer_document(FSInputFile(CHECKLIST_FILE), caption=T.DEMO_CHECKLIST_CAPTION)
    await m.answer(T.DEMO_AFTER, reply_markup=ORDER_BTN)
    row = db.execute("SELECT demo_at FROM users WHERE id=?", (m.from_user.id,)).fetchone()
    if row and row[0] is None:
        mark(m.from_user.id, "demo_at", time.time())


@router.message(StateFilter(None), F.text == T.BTN_DEMO)
async def demo(m: Message, bot: Bot):
    await send_demo(m, bot)


@router.message(StateFilter(None), F.text.lower().in_(CODEWORDS))
async def codeword(m: Message, bot: Bot):
    upsert_user(m, source="кодовое слово")
    await m.answer(T.WELCOME_CODEWORD, reply_markup=main_menu())
    await send_demo(m, bot)


# ---------------------------------------------------------------- тарифы, кейсы, FAQ
@router.message(StateFilter(None), F.text == T.BTN_PRICES)
async def prices(m: Message):
    await m.answer(T.PRICES, reply_markup=inline([[(T.BTN_ORDER, "order")]]))


@router.callback_query(F.data == "prices")
async def prices_cb(c: CallbackQuery):
    await c.answer()
    await c.message.answer(T.PRICES, reply_markup=inline([[(T.BTN_ORDER, "order")]]))


@router.message(StateFilter(None), F.text == T.BTN_CASES)
async def cases(m: Message):
    await m.answer(T.CASES)
    if CASES_DIR.exists():
        pics = sorted(p for p in CASES_DIR.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"})[:10]
        if len(pics) == 1:
            await m.answer_photo(FSInputFile(pics[0]))
        elif pics:
            await m.answer_media_group([InputMediaPhoto(media=FSInputFile(p)) for p in pics])
    links = []
    if INSTAGRAM_URL:
        links.append(f'📸 <a href="{INSTAGRAM_URL}">Instagram</a>')
    if CHANNEL_URL:
        links.append(f'📣 <a href="{CHANNEL_URL}">Telegram-канал</a>')
    await m.answer("   ".join(links) or "Хотите так же? 👇", reply_markup=ORDER_BTN, disable_web_page_preview=True)


@router.message(StateFilter(None), F.text == T.BTN_HOW)
async def how(m: Message):
    await m.answer(T.HOW, reply_markup=inline([[(T.BTN_ORDER, "order")]]))


# ---------------------------------------------------------------- вопрос Полине
@router.message(StateFilter(None), F.text == T.BTN_CONTACT)
async def contact(m: Message, state: FSMContext):
    await state.set_state(Ask.question)
    text = T.CONTACT.format(contact=CONTACT) if CONTACT else T.CONTACT.split("\n\nИли")[0]
    await m.answer(text)


async def forward_question(m: Message, bot: Bot):
    upsert_user(m)
    sent = await bot.send_message(ADMIN_ID, T.ADMIN_QUESTION.format(text=m.html_text or "(вложение)", **who(m)))
    link(sent.message_id, m.from_user.id)
    if not m.text:
        copied = await m.copy_to(ADMIN_ID)
        link(copied.message_id, m.from_user.id)
    await m.answer(T.QUESTION_SENT, reply_markup=main_menu())


@router.message(Ask.question)
async def question(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    await forward_question(m, bot)


# ---------------------------------------------------------------- анкета заказа
async def begin_order(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Order.object)
    await state.update_data(files=[])
    await m.answer(T.ORDER_START, reply_markup=ReplyKeyboardRemove())
    await m.answer(T.Q_OBJECT, reply_markup=inline([[(v, k)] for k, v in OBJECTS.items()]))


@router.message(StateFilter(None), F.text == T.BTN_ORDER)
async def order(m: Message, state: FSMContext):
    upsert_user(m)
    await begin_order(m, state)


@router.callback_query(F.data == "order")
async def order_cb(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await begin_order(c.message, state)


@router.callback_query(Order.object, F.data.in_(OBJECTS))
async def got_object(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.update_data(object=OBJECTS[c.data])
    await c.message.edit_text(f"{T.Q_OBJECT}\n<b>{OBJECTS[c.data]}</b>")
    await state.set_state(Order.area)
    await c.message.answer(T.Q_AREA)


@router.message(Order.object)
async def object_text(m: Message):
    await m.answer(T.PLEASE_USE_BUTTONS)


@router.message(Order.area, F.text)
async def got_area(m: Message, state: FSMContext):
    await state.update_data(area=m.text[:100])
    await state.set_state(Order.goal)
    await m.answer(T.Q_GOAL, reply_markup=inline([[(v, k)] for k, v in GOALS.items()]))


@router.callback_query(Order.goal, F.data.in_(GOALS))
async def got_goal(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.update_data(goal=GOALS[c.data])
    await c.message.edit_text(f"{T.Q_GOAL}\n<b>{GOALS[c.data]}</b>")
    await state.set_state(Order.plan)
    await c.message.answer(T.Q_PLAN, reply_markup=inline([[(T.BTN_PLAN_LATER, "plan_done")]]))


@router.message(Order.goal)
async def goal_text(m: Message):
    await m.answer(T.PLEASE_USE_BUTTONS)


@router.message(Order.plan, F.photo | F.document)
async def got_plan(m: Message, state: FSMContext):
    data = await state.get_data()
    files = data.get("files", [])
    if m.photo:
        files.append(("photo", m.photo[-1].file_id))
    else:
        files.append(("doc", m.document.file_id))
    await state.update_data(files=files)
    await m.answer(T.PLAN_MORE, reply_markup=inline([[(T.BTN_PLAN_DONE, "plan_done")]]))


@router.callback_query(Order.plan, F.data == "plan_done")
async def plan_done(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(Order.wishes)
    await c.message.answer(T.Q_WISHES)


@router.message(Order.plan)
async def plan_other(m: Message):
    await m.answer(T.PLEASE_SEND_FILE, reply_markup=inline([[(T.BTN_PLAN_LATER, "plan_done")]]))


@router.message(Order.wishes, F.text)
async def got_wishes(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    upsert_user(m)
    src = db.execute("SELECT source FROM users WHERE id=?", (m.from_user.id,)).fetchone()
    files = data.get("files", [])
    text = T.ADMIN_NEW_LEAD.format(
        source=src[0] if src else "—",
        object=data.get("object", "—"), area=data.get("area", "—"), goal=data.get("goal", "—"),
        wishes=m.html_text, plan=f"{len(files)} файл(ов) ниже" if files else "пришлёт позже",
        **who(m),
    )
    sent = await bot.send_message(ADMIN_ID, text)
    link(sent.message_id, m.from_user.id)
    for kind, fid in files:
        f = await (bot.send_photo(ADMIN_ID, fid) if kind == "photo" else bot.send_document(ADMIN_ID, fid))
        link(f.message_id, m.from_user.id)
    mark(m.from_user.id, "ordered", 1)
    extra = "" if m.from_user.username else T.NEED_USERNAME
    await m.answer(T.ORDER_DONE + extra, reply_markup=main_menu())


# ---------------------------------------------------------------- всё остальное → вопрос Полине
@router.message(StateFilter(None), F.from_user.id != ADMIN_ID)
async def fallback(m: Message, bot: Bot):
    await forward_question(m, bot)


# ---------------------------------------------------------------- дожим
async def followups(bot: Bot):
    while True:
        now = time.time()
        jobs = [
            ("f1_sent", FOLLOWUP_1_HOURS, T.FOLLOWUP_1, ORDER_BTN),
            ("f2_sent", FOLLOWUP_2_HOURS, T.FOLLOWUP_2, None),
        ]
        for field, hours, text, kb in jobs:
            rows = db.execute(
                f"SELECT id FROM users WHERE demo_at IS NOT NULL AND ordered=0 AND blocked=0 "
                f"AND {field}=0 AND demo_at < ?", (now - hours * 3600,)
            ).fetchall()
            for (uid,) in rows:
                try:
                    await bot.send_message(uid, text, reply_markup=kb)
                except Exception as e:
                    log.info("follow-up to %s failed: %s", uid, e)
                    mark(uid, "blocked", 1)
                mark(uid, field, 1)
                await asyncio.sleep(0.1)
        await asyncio.sleep(600)  # проверка раз в 10 минут


# ---------------------------------------------------------------- запуск
async def main():
    if not BOT_TOKEN or not ADMIN_ID:
        raise SystemExit("Заполните BOT_TOKEN и ADMIN_ID в файле settings.txt")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    asyncio.create_task(followups(bot))
    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
