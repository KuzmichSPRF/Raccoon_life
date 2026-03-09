import os
import logging
import sqlite3
import json
import asyncio
import sys
from dotenv import load_dotenv # Загрузка переменных из .env
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# Загружаем данные из файла .env
load_dotenv()

# Берем настройки из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Используем абсолютный путь, чтобы база данных создавалась в папке с ботом
DB_NAME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.db")

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
            # Таблица общей статистики
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_stats (
                    user_id INTEGER PRIMARY KEY,
                    games_played INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    tower_max_level INTEGER DEFAULT 0
                )
            ''')
            # Таблица прохождения квестов (журнал)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS quest_completions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    quest_name TEXT,
                    completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

async def web_app_data_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка статистики из WebApp"""
    user = update.effective_user
    try:
        data = json.loads(update.effective_message.web_app_data.data)
        if data.get('type') == 'sync_stats':
            games = data.get('games', 0)
            wins = data.get('wins', 0)
            tower = data.get('tower', 0)
            quests = data.get('quests', [])

            with sqlite3.connect(DB_NAME) as conn:
                c = conn.cursor()
                # Обновляем статистику
                c.execute('''INSERT OR REPLACE INTO user_stats (user_id, games_played, wins, tower_max_level)
                             VALUES (?, ?, ?, ?)''', (user.id, games, wins, tower))
                
                # Записываем новые квесты
                existing = {row[0] for row in c.execute("SELECT quest_name FROM quest_completions WHERE user_id = ?", (user.id,))}
                for q in quests:
                    if q not in existing:
                        c.execute("INSERT INTO quest_completions (user_id, quest_name) VALUES (?, ?)", (user.id, q))
                
                conn.commit()
            
            if data.get('source') == 'quest':
                await update.message.reply_text("🎉 Поздравляем с прохождением этапа!")
            else:
                await update.message.reply_text(f"💾 Данные сохранены!\n🎮 Игр: {games}\n🏆 Побед: {wins}\n🏰 Башня: {tower} этаж")
    except Exception as e:
        logger.error(f"Ошибка WebApp: {e}")

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
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data_handler))
    
    print("Бот успешно запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()