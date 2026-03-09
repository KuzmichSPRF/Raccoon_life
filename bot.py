import os
import logging
import sqlite3
import json
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_NAME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.db")

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT, first_name TEXT, last_name TEXT,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_stats (
                user_id INTEGER PRIMARY KEY,
                clown_games INTEGER DEFAULT 0, clown_wins INTEGER DEFAULT 0,
                vladeos_games INTEGER DEFAULT 0, vladeos_wins INTEGER DEFAULT 0,
                tower_max_level INTEGER DEFAULT 0, tower_total_levels INTEGER DEFAULT 0,
                quests TEXT DEFAULT '[]',
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        ''')
        conn.commit()

def update_db_stats(user_id, data):
    """Записывает данные из JSON в таблицу user_stats"""
    try:
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            # Создаем запись, если её нет
            cursor.execute("INSERT OR IGNORE INTO user_stats (user_id) VALUES (?)", (user_id,))
            
            cursor.execute('''
                UPDATE user_stats SET 
                clown_games = ?, clown_wins = ?,
                vladeos_games = ?, vladeos_wins = ?,
                tower_max_level = ?, tower_total_levels = ?,
                quests = ?
                WHERE user_id = ?
            ''', (
                data.get('clown_games', 0), data.get('clown_wins', 0),
                data.get('vladeos_games', 0), data.get('vladeos_wins', 0),
                data.get('tower_max_level', 0), data.get('tower_total_levels', 0),
                json.dumps(data.get('quests', [])),
                user_id
            ))
            conn.commit()
            logger.info(f"Успешное обновление БД для {user_id}")
    except Exception as e:
        logger.error(f"Ошибка БД: {e}")

async def web_app_data_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принимает данные от tg.sendData()"""
    user_id = update.effective_user.id
    raw_data = update.effective_message.web_app_data.data
    
    try:
        data = json.loads(raw_data)
        if data.get('type') == 'sync_stats':
            update_db_stats(user_id, data)
            await update.message.reply_text("✨ Данные успешно сохранены в облаке!")
    except Exception as e:
        logger.error(f"Ошибка обработки WebAppData: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Код сохранения пользователя (оставляем ваш)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(text="📰 Играть!", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    await update.message.reply_text("Привет! Нажми кнопку ниже:", reply_markup=keyboard)

async def post_init(application: Application):
    await application.bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="Играть", web_app=WebAppInfo(url=WEBAPP_URL))
    )

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    app.add_handler(CommandHandler("start", start))
    # ВАЖНО: Хендлер для приема данных из Mini App
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data_handler))
    
    app.run_polling()

if __name__ == '__main__':
    main()
