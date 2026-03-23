import os
import logging
import dotenv
import asyncio
import aiosqlite
from enum import StrEnum, auto
from typing import Final, Union

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.types import (
    Message,
    TelegramObject,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
)
from aiogram.filters import Command, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

# Logs
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

class Action(StrEnum):
    LIST_ALL = auto()
    EDIT_ITEM = auto()
    RENAME = auto()
    DELETE = auto()
    ADD_NEW = auto()
    DELETE_TASK = auto()
    MAIN_MENU = auto()

class CatOpCB(CallbackData, prefix="op"):
    action: Action
    id: int = 0

class BotState(StatesGroup):
    in_category = State() # Состояние активного просмотра/добавления
    waiting_new_cat = State()
    waiting_rename = State()

# --- Middleware ---

class DbSessionMiddleware(BaseMiddleware):
    def __init__(self, connection: aiosqlite.Connection):
        super().__init__()
        self.connection = connection

    async def __call__(self, handler, event: TelegramObject, data: dict):
        data["db"] = self.connection
        return await handler(event, data)

# --- Keyboards ---

class KBs:
    @staticmethod
    async def main_menu_reply(db: aiosqlite.Connection) -> ReplyKeyboardMarkup:
        builder = ReplyKeyboardBuilder()
        async with db.execute("SELECT name FROM categories") as cursor:
            async for (name,) in cursor:
                builder.add(KeyboardButton(text=f"📁 {name}"))
        builder.add(KeyboardButton(text="⚙️ Manage Categories"))
        builder.adjust(2)
        return builder.as_markup(resize_keyboard=True)

    @staticmethod
    def category_mgmt_list(categories: list) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(text="➕ Add Category", callback_data=CatOpCB(action=Action.ADD_NEW))
        for cid, name in categories:
            builder.button(text=f"📂 {name}", callback_data=CatOpCB(action=Action.EDIT_ITEM, id=cid))
        builder.button(text="⬅️ Back to Menu", callback_data=CatOpCB(action=Action.MAIN_MENU))
        builder.adjust(1)
        return builder.as_markup()

    @staticmethod
    def cat_edit_actions(cat_id: int) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(text="✏️ Rename", callback_data=CatOpCB(action=Action.RENAME, id=cat_id))
        builder.button(text="🔥 Delete", callback_data=CatOpCB(action=Action.DELETE, id=cat_id))
        builder.button(text="⬅️ Back", callback_data=CatOpCB(action=Action.LIST_ALL))
        builder.adjust(2, 1)
        return builder.as_markup()

# --- Helpers ---

async def delete_msg(bot: Bot, chat_id: int, mid: int):
    try: await bot.delete_message(chat_id, mid)
    except: pass

# --- Handlers ---

dp = Dispatcher()

@dp.message(Command("start"))
@dp.message(F.text == "❌ Cancel")
async def cmd_start(message: Message, db: aiosqlite.Connection, state: FSMContext):
    data = await state.get_data()
    await delete_msg(message.bot, message.chat.id, data.get("last_mid"))

    await state.clear()
    kb = await KBs.main_menu_reply(db)
    msg = await message.answer(
        "🗂 **Main Menu**",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    # Inline switcher for management
    inline_kb = InlineKeyboardBuilder().button(text="⚙️ Manage Categories", callback_data=CatOpCB(action=Action.LIST_ALL)).as_markup()
    await msg.edit_reply_markup(reply_markup=inline_kb)
    await state.update_data(last_mid=msg.message_id)

@dp.callback_query(CatOpCB.filter(F.action == Action.MAIN_MENU))
async def back_to_main_callback(call: CallbackQuery, db: aiosqlite.Connection, state: FSMContext):
    await cmd_start(call.message, db, state)
    await call.answer()

@dp.message(F.text == "⚙️ Manage Categories")
@dp.callback_query(CatOpCB.filter(F.action == Action.LIST_ALL))
async def list_cats(event: Message | CallbackQuery, db: aiosqlite.Connection, state: FSMContext):
    cats = []
    async with db.execute("SELECT id, name FROM categories") as cursor:
        async for row in cursor: cats.append(row)

    kb = KBs.category_mgmt_list(cats)
    text = "🛠 **Category Management**"

    if isinstance(event, Message):
        data = await state.get_data()
        await delete_msg(event.bot, event.chat.id, data.get("last_mid"))
        msg = await event.answer(text, reply_markup=kb, parse_mode="Markdown")
        await state.update_data(last_mid=msg.message_id)
    else:
        await event.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        await event.answer()

@dp.message(F.text.startswith("📁 "))
async def show_content(message: Message, db: aiosqlite.Connection, state: FSMContext):
    cat_name = message.text[2:]
    async with db.execute("SELECT id FROM categories WHERE name = ?", (cat_name,)) as cursor:
        row = await cursor.fetchone()
    if not row: return

    cat_id = row[0]
    data = await state.get_data()
    await delete_msg(message.bot, message.chat.id, data.get("last_mid"))

    # Output content
    async with db.execute("SELECT id, chat_id, msg_id FROM tasks WHERE cat_id = ?", (cat_id,)) as cursor:
        async for tid, chat_id, msg_id in cursor:
            try:
                await message.bot.copy_message(
                    message.chat.id, chat_id, msg_id,
                    reply_markup=InlineKeyboardBuilder().button(text="🗑", callback_data=CatOpCB(action=Action.DELETE_TASK, id=tid)).as_markup()
                )
            except: continue

    # Switch state to catch all messages
    await state.set_state(BotState.in_category)
    await state.update_data(current_cat_id=cat_id, current_cat_name=cat_name)

    b = ReplyKeyboardBuilder().add(KeyboardButton(text="❌ Cancel"))
    msg = await message.answer(f"📍 Viewing: **{cat_name}**\n_Send anything to save it here._",
                               parse_mode="Markdown", reply_markup=b.as_markup(resize_keyboard=True))
    await state.update_data(last_mid=msg.message_id)

@dp.message(BotState.in_category)
async def auto_save_handler(message: Message, state: FSMContext, db: aiosqlite.Connection):
    """Ловит всё, что прислано, пока юзер 'внутри' папки"""
    if message.text == "❌ Cancel": return # Filter handled by main handler

    data = await state.get_data()
    cat_id = data.get("current_cat_id")

    await db.execute(
        "INSERT INTO tasks (cat_id, chat_id, msg_id) VALUES (?, ?, ?)",
        (cat_id, message.chat.id, message.message_id)
    )
    await db.commit()
    # Feedback without flooding
    temp = await message.answer("✅ Saved to " + data.get("current_cat_name", "category"))
    await asyncio.sleep(1)
    await temp.delete()

@dp.callback_query(CatOpCB.filter(F.action == Action.DELETE_TASK))
async def delete_task(call: CallbackQuery, callback_data: CatOpCB, db: aiosqlite.Connection):
    await db.execute("DELETE FROM tasks WHERE id = ?", (callback_data.id,))
    await db.commit()
    await call.message.delete()
    await call.answer("Deleted")

@dp.callback_query(CatOpCB.filter(F.action == Action.ADD_NEW))
async def add_cat_init(call: CallbackQuery, state: FSMContext):
    await call.message.delete()
    msg = await call.message.answer("Enter name:", reply_markup=ReplyKeyboardBuilder().add(KeyboardButton(text="❌ Cancel")).as_markup(resize_keyboard=True))
    await state.set_state(BotState.waiting_new_cat)
    await state.update_data(last_mid=msg.message_id)

@dp.message(BotState.waiting_new_cat)
async def save_cat(message: Message, state: FSMContext, db: aiosqlite.Connection):
    await db.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (message.text,))
    await db.commit()
    await cmd_start(message, db, state)

@dp.callback_query(CatOpCB.filter(F.action == Action.EDIT_ITEM))
async def edit_item_menu(call: CallbackQuery, callback_data: CatOpCB):
    await call.message.edit_text(f"📝 Editing ID: {callback_data.id}", reply_markup=KBs.cat_edit_actions(callback_data.id))

@dp.callback_query(CatOpCB.filter(F.action == Action.DELETE))
async def del_cat(call: CallbackQuery, callback_data: CatOpCB, db: aiosqlite.Connection, state: FSMContext):
    await db.execute("DELETE FROM tasks WHERE cat_id = ?", (callback_data.id,))
    await db.execute("DELETE FROM categories WHERE id = ?", (callback_data.id,))
    await db.commit()
    await cmd_start(call.message, db, state)

async def main():
    dotenv.load_dotenv()
    async with aiosqlite.connect("../databases/tasks.db") as db:
        await db.execute("CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY, name TEXT UNIQUE)")
        await db.execute("CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, cat_id INTEGER, chat_id INTEGER, msg_id INTEGER)")
        await db.commit()
        bot = Bot(token=os.getenv("BOT_TOKEN"))
        dp.update.middleware(DbSessionMiddleware(db))
        await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
