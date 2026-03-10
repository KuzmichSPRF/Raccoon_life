import os
import logging
import sqlite3
import json
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp, ParseMode
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
        
        # Миграция: Проверяем и добавляем недостающие колонки, чтобы избежать вылетов на старой БД
        cursor.execute("PRAGMA table_info(user_stats)")
        columns = {info[1] for info in cursor.fetchall()}
        required_columns = {
            'clown_games': 'INTEGER DEFAULT 0', 'clown_wins': 'INTEGER DEFAULT 0',
            'vladeos_games': 'INTEGER DEFAULT 0', 'vladeos_wins': 'INTEGER DEFAULT 0',
            'tower_max_level': 'INTEGER DEFAULT 0', 'tower_total_levels': 'INTEGER DEFAULT 0',
            'quests': "TEXT DEFAULT '[]'"
        }
        for col, dtype in required_columns.items():
            if col not in columns:
                try:
                    cursor.execute(f"ALTER TABLE user_stats ADD COLUMN {col} {dtype}")
                    logger.info(f"Миграция: добавлена колонка {col}")
                except Exception as e:
                    logger.error(f"Ошибка добавления колонки {col}: {e}")
                    
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
                json.dumps(data.get('quests') or []),
                user_id
            ))
            conn.commit()
            logger.info(f"Успешное обновление БД для {user_id}")
            return True
    except Exception as e:
        logger.error(f"Ошибка БД: {e}")
        return False

def get_leaderboard_text():
    """Fetches and formats the leaderboard text based on tower_max_level."""
    try:
        with sqlite3.connect(DB_NAME) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT u.first_name, u.username, s.tower_max_level
                FROM user_stats s
                JOIN users u ON s.user_id = u.user_id
                WHERE s.tower_max_level > 0
                ORDER BY s.tower_max_level DESC
                LIMIT 10
            ''')
            leaders = cursor.fetchall()

            if not leaders:
                return "Пока нету данных для рейтинга. Начните играть!"

            message = "🏆 <b>Топ игроков по башне:</b>\n\n"
            for i, leader in enumerate(leaders):
                display_name = leader['first_name'] or leader['username'] or "Аноним"
                # Basic HTML escaping
                display_name = display_name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                message += f"{i+1}. {display_name} - {leader['tower_max_level']} этаж\n"
            
            return message

    except Exception as e:
        logger.error(f"Error fetching leaderboard: {e}")
        return "Не удалось загрузить рейтинг. Попробуйте позже."

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the leaderboard to the chat."""
    leaderboard_text = get_leaderboard_text()
    await update.message.reply_text(leaderboard_text, parse_mode=ParseMode.HTML)

async def web_app_data_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принимает данные от tg.sendData()"""
    user_id = update.effective_user.id
    raw_data = update.effective_message.web_app_data.data
    
    try:
        data = json.loads(raw_data)
        data_type = data.get('type')

        if data_type == 'sync_stats':
            if update_db_stats(user_id, data):
                await update.message.reply_text("✨ Данные успешно сохранены в облаке!")
            else:
                await update.message.reply_text("⚠️ Не удалось сохранить прогресс. Попробуйте позже.")
    except Exception as e:
        logger.error(f"Ошибка обработки WebAppData: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Код сохранения пользователя (оставляем ваш)
    user = update.effective_user
    try:
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO users (user_id, username, first_name, last_name) VALUES (?, ?, ?, ?)",
                           (user.id, user.username, user.first_name, user.last_name))
            cursor.execute("INSERT OR IGNORE INTO user_stats (user_id) VALUES (?)", (user.id,))
            conn.commit()
            logger.info(f"User {user.id} ({user.username}) started the bot.")
    except Exception as e:
        logger.error(f"Error saving user {user.id} to DB: {e}")

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
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    # ВАЖНО: Хендлер для приема данных из Mini App
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data_handler))
    
    app.run_polling()

if __name__ == '__main__':
    main()
