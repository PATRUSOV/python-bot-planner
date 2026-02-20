import os
import logging
import dotenv
import asyncio
import aiosqlite
from enum import StrEnum, auto
from typing import Final

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.types import (
    Message,
    TelegramObject,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
)
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

# Инициализация логгера для текущего модуля
logger = logging.getLogger(__name__)


class Action(StrEnum):
    DELETE_TASK = auto()
    DELETE_CAT = auto()
    RENAME_CAT = auto()
    SETTINGS = auto()


class TaskCB(CallbackData, prefix="t"):
    action: Action
    id: int


class CategoryCB(CallbackData, prefix="c"):
    action: Action
    id: int


class BotState(StatesGroup):
    waiting_content = State()
    waiting_new_category_name = State()
    waiting_rename_category = State()


# --- Middleware ---


class DbSessionMiddleware(BaseMiddleware):
    def __init__(self, connection: aiosqlite.Connection):
        super().__init__()
        self.connection = connection

    async def __call__(self, handler, event: TelegramObject, data: dict):
        data["db"] = self.connection
        return await handler(event, data)


# --- Keyboard Factory ---


class KeyboardFactory:
    @staticmethod
    async def main_menu(db: aiosqlite.Connection) -> ReplyKeyboardMarkup:
        builder = ReplyKeyboardBuilder()
        async with db.execute("SELECT name FROM categories") as cursor:
            async for (name,) in cursor:
                builder.add(KeyboardButton(text=f"📁 {name}"))

        builder.add(KeyboardButton(text="➕ New Category"))
        builder.adjust(2)
        return builder.as_markup(resize_keyboard=True)

    @staticmethod
    def cancel_kb() -> ReplyKeyboardMarkup:
        builder = ReplyKeyboardBuilder()
        builder.add(KeyboardButton(text="❌ Cancel"))
        return builder.as_markup(resize_keyboard=True)

    @staticmethod
    def task_inline(task_id: int) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(
            text="🗑 Delete", callback_data=TaskCB(action=Action.DELETE_TASK, id=task_id)
        )
        return builder.as_markup()

    @staticmethod
    def category_settings_btn(cat_id: int) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(
            text="⚙️ Category Settings",
            callback_data=CategoryCB(action=Action.SETTINGS, id=cat_id),
        )
        return builder.as_markup()

    @staticmethod
    def category_mgmt_menu(cat_id: int) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(
            text="✏️ Rename",
            callback_data=CategoryCB(action=Action.RENAME_CAT, id=cat_id),
        )
        builder.button(
            text="🔥 Delete",
            callback_data=CategoryCB(action=Action.DELETE_CAT, id=cat_id),
        )
        builder.button(
            text="⬅️ Back", callback_data=CategoryCB(action=Action.SETTINGS, id=cat_id)
        )
        builder.adjust(1)
        return builder.as_markup()


# --- Handlers ---

dp = Dispatcher()


@dp.message(Command("start"))
@dp.message(F.text == "❌ Cancel")
async def cmd_start(message: Message, db: aiosqlite.Connection, state: FSMContext):
    logger.info(f"User {message.from_user.id} accessed Main Menu")
    await state.clear()
    await message.answer("Main Menu:", reply_markup=await KeyboardFactory.main_menu(db))


@dp.message(F.text == "➕ New Category")
async def add_cat_init(message: Message, state: FSMContext):
    logger.debug(f"User {message.from_user.id} started category creation")
    await message.answer(
        "Enter category name:", reply_markup=KeyboardFactory.cancel_kb()
    )
    await state.set_state(BotState.waiting_new_category_name)


@dp.message(BotState.waiting_new_category_name)
async def save_category(message: Message, state: FSMContext, db: aiosqlite.Connection):
    logger.info(f"Saving new category: {message.text}")
    await db.execute(
        "INSERT OR IGNORE INTO categories (name) VALUES (?)", (message.text,)
    )
    await db.commit()
    await state.clear()
    await message.answer(
        f"Category '{message.text}' created.",
        reply_markup=await KeyboardFactory.main_menu(db),
    )


@dp.message(F.text.startswith("📁 "))
async def show_category(message: Message, db: aiosqlite.Connection, state: FSMContext):
    cat_name = message.text[2:]
    logger.debug(f"Opening category: {cat_name}")
    async with db.execute(
        "SELECT id FROM categories WHERE name = ?", (cat_name,)
    ) as cursor:
        row = await cursor.fetchone()

    if not row:
        logger.warning(f"Category {cat_name} not found in DB")
        return

    cat_id = row[0]
    await state.update_data(current_cat_id=cat_id)

    async with db.execute(
        "SELECT id, chat_id, msg_id FROM tasks WHERE cat_id = ?", (cat_id,)
    ) as cursor:
        count = 0
        async for tid, chat_id, msg_id in cursor:
            try:
                await message.bot.copy_message(
                    chat_id=message.chat.id,
                    from_chat_id=chat_id,
                    message_id=msg_id,
                    reply_markup=KeyboardFactory.task_inline(tid),
                )
                count += 1
            except Exception as e:
                logger.error(f"Failed to copy message {msg_id}: {e}")
                continue
        logger.info(f"Copied {count} tasks to user {message.from_user.id}")

    builder = ReplyKeyboardBuilder()
    builder.add(
        KeyboardButton(text=f"➕ Add to {cat_name}"), KeyboardButton(text="❌ Cancel")
    )

    await message.answer(
        f"Content of {cat_name}:", reply_markup=builder.as_markup(resize_keyboard=True)
    )
    await message.answer(
        "Settings:", reply_markup=KeyboardFactory.category_settings_btn(cat_id)
    )


@dp.callback_query(CategoryCB.filter(F.action == Action.SETTINGS))
async def category_settings(call: CallbackQuery, callback_data: CategoryCB):
    logger.debug(f"Opening settings for category ID: {callback_data.id}")
    await call.message.edit_text(
        "Control Panel:",
        reply_markup=KeyboardFactory.category_mgmt_menu(callback_data.id),
    )
    await call.answer()


@dp.callback_query(CategoryCB.filter(F.action == Action.RENAME_CAT))
async def rename_cat_init(
    call: CallbackQuery, callback_data: CategoryCB, state: FSMContext
):
    logger.info(
        f"User {call.from_user.id} initialized rename for cat ID {callback_data.id}"
    )
    await state.update_data(rename_cat_id=callback_data.id)
    await call.message.answer(
        "Enter new name for this category:", reply_markup=KeyboardFactory.cancel_kb()
    )
    await state.set_state(BotState.waiting_rename_category)
    await call.answer()


@dp.message(BotState.waiting_rename_category)
async def save_renamed_category(
    message: Message, state: FSMContext, db: aiosqlite.Connection
):
    data = await state.get_data()
    cat_id = data.get("rename_cat_id")
    logger.info(f"Renaming category {cat_id} to '{message.text}'")

    await db.execute(
        "UPDATE categories SET name = ? WHERE id = ?", (message.text, cat_id)
    )
    await db.commit()
    await state.clear()
    await message.answer(
        f"Category renamed to '{message.text}'",
        reply_markup=await KeyboardFactory.main_menu(db),
    )


@dp.message(F.text.startswith("➕ Add to "))
async def add_task_init(message: Message, state: FSMContext):
    logger.debug(f"User {message.from_user.id} adding new content")
    await message.answer("Send content:", reply_markup=KeyboardFactory.cancel_kb())
    await state.set_state(BotState.waiting_content)


@dp.message(BotState.waiting_content)
async def save_task(message: Message, state: FSMContext, db: aiosqlite.Connection):
    data = await state.get_data()
    cat_id = data.get("current_cat_id")
    preview = (message.text or message.caption or "Media")[:20]
    logger.info(f"Saving task to cat {cat_id}. Preview: {preview}")

    await db.execute(
        "INSERT INTO tasks (cat_id, chat_id, msg_id, preview_text) VALUES (?, ?, ?, ?)",
        (cat_id, message.chat.id, message.message_id, preview),
    )
    await db.commit()
    await state.clear()
    await message.answer("✅ Saved!", reply_markup=await KeyboardFactory.main_menu(db))


@dp.callback_query(TaskCB.filter(F.action == Action.DELETE_TASK))
async def delete_task(
    call: CallbackQuery, callback_data: TaskCB, db: aiosqlite.Connection
):
    logger.info(f"Deleting task ID: {callback_data.id}")
    await db.execute("DELETE FROM tasks WHERE id = ?", (callback_data.id,))
    await db.commit()
    await call.message.delete()
    await call.answer("Deleted")


@dp.callback_query(CategoryCB.filter(F.action == Action.DELETE_CAT))
async def delete_category(
    call: CallbackQuery, callback_data: CategoryCB, db: aiosqlite.Connection
):
    logger.warning(
        f"User {call.from_user.id} is DELETING category ID {callback_data.id}"
    )
    await db.execute("DELETE FROM tasks WHERE cat_id = ?", (callback_data.id,))
    await db.execute("DELETE FROM categories WHERE id = ?", (callback_data.id,))
    await db.commit()
    await call.answer("Category deleted", show_alert=True)
    await call.message.answer(
        "Returning to Main Menu...", reply_markup=await KeyboardFactory.main_menu(db)
    )
    await call.message.delete()


# --- Entry Point ---


async def main():
    dotenv.load_dotenv()

    # Глобальная настройка логгирования
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=LOG_LEVEL, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    DB_NAME: Final = "../databases/tasks_v4.db"
    TOKEN = os.getenv("BOT_TOKEN")

    if not TOKEN:
        logger.critical("BOT_TOKEN is missing in environment variables!")
        raise EnvironmentError("The BOT_TOKEN environment variable is not initialized.")

    logger.info("Starting database connection...")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY, name TEXT UNIQUE)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, cat_id INTEGER, chat_id INTEGER, msg_id INTEGER, preview_text TEXT)"
        )
        await db.commit()

        bot = Bot(token=TOKEN)
        dp.update.middleware(DbSessionMiddleware(db))

        logger.info("Bot is starting polling...")
        await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped manually.")
