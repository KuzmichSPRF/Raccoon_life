import os
import logging
import sqlite3
import asyncio
import sys
from dotenv import load_dotenv # Загрузка переменных из .env
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp
from telegram.ext import Application, CommandHandler, ContextTypes

# Загружаем данные из файла .env
load_dotenv()

# Берем настройки из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_NAME = "users.db"

def init_db():
    """Инициализация базы данных и создание таблицы пользователей."""
    try:
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            # Создаем таблицу users, если она не существует
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()
            logger.info("База данных успешно инициализирована.")
    except sqlite3.Error as e:
        logger.error(f"Ошибка при инициализации БД: {e}")

def add_or_update_user(user):
    """Добавляет нового пользователя или обновляет данные существующего в БД."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO users (user_id, username, first_name, last_name)
            VALUES (?, ?, ?, ?)
        ''', (user.id, user.username, user.first_name, user.last_name))
        conn.commit()

async def post_init(application: Application) -> None:
    """Установка синей кнопки 'Открыть' при запуске"""
    await application.bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(
            text="Открыть", 
            web_app=WebAppInfo(url=WEBAPP_URL)
        )
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        # Сохраняем данные пользователя в БД
        add_or_update_user(user)
        welcome_message = f"👋 Привет, {user.first_name}! Нажми кнопку, чтобы запустить игру:"
    else:
        welcome_message = "👋 Привет! Нажми кнопку, чтобы запустить игру:"

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(text="📰 Играть!", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    await update.message.reply_text(welcome_message, reply_markup=keyboard)

def main() -> None:
    if not BOT_TOKEN:
        print("ОШИБКА: Токен не найден в файле .env!")
        return

    init_db() # Инициализируем БД перед запуском бота
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    
    print("Бот успешно запущен!")
    app.run_polling()

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    main()