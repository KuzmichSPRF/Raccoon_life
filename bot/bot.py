"""
Raccoon Life Bot - Backend API
Синхронизация игровой статистики и урона по боссу
"""
import os
import logging
import sqlite3
import random
import json
import hmac
import hashlib
import html
import time
import base64
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import asyncio
import traceback
from urllib.parse import parse_qsl
from pathlib import Path
from threading import Thread
from io import BytesIO
import requests
from PIL import Image, ImageDraw, ImageOps, ImageFilter
from flask import Flask, jsonify, request, send_from_directory, redirect
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler, PreCheckoutQueryHandler
from telegram import BotCommand
from telegram.error import RetryAfter
from dotenv import load_dotenv

# Пути к файлам
# Определяем абсолютный путь к директории, где находится этот скрипт (bot.py)
# Это делает пути независимыми от того, откуда запускается скрипт
BOT_DIR = Path(__file__).resolve().parent
# Определяем корневую директорию проекта
PROJECT_DIR = BOT_DIR.parent

# Загрузка переменных окружения (из bot/.env или корня проекта)
load_dotenv(dotenv_path=str(BOT_DIR / ".env"))
load_dotenv(dotenv_path=str(PROJECT_DIR / ".env"))
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
FLASK_PORT = int(os.getenv("FLASK_PORT", 5000))

# Переменные для интеграции с ботом объявлений
SALEBOT_TOKEN = os.getenv("SALEBOT_TOKEN")
SALEBOT_DB_PATH = os.getenv("SALEBOT_DB_PATH", str(BOT_DIR / "salebot.db"))

# Кошелек получателя для покупок за TON
TON_RECIPIENT_WALLET = os.getenv("TON_RECIPIENT_WALLET", "UQCr6tyXHAXmxyexwgRltYNZSOwIioAMQO5PP-F2NqvQGgwX")

TON_OFFERS = [
    {
        "id": "pack_02",
        "ton": 2.0,
        "cones": 20000,
        "bonus": "",
        "popular": False,
        "title_ru": "Пакет «Старт»",
        "title_en": "«Start» Pack"
    },
    {
        "id": "pack_05",
        "ton": 5.0,
        "cones": 60000,
        "bonus": "+20%",
        "popular": False,
        "title_ru": "Пакет «Грибник»",
        "title_en": "«Mushroomer» Pack"
    },
    {
        "id": "pack_10",
        "ton": 10.0,
        "cones": 150000,
        "bonus": "+50%",
        "popular": True,
        "title_ru": "Пакет «Богатый Енот»",
        "title_en": "«Rich Raccoon» Pack"
    },
    {
        "id": "pack_30",
        "ton": 30.0,
        "cones": 500000,
        "bonus": "+66%",
        "popular": False,
        "title_ru": "Пакет «Хранилище Леса»",
        "title_en": "«Forest Vault» Pack"
    }
]

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
security_logger = logging.getLogger('security')  # Отдельный logger для security событий

DB_PATH = BOT_DIR / "users.db"
WEBAPP_DIR = PROJECT_DIR / "webapp"
SETS_IMG_DIR = WEBAPP_DIR / "images" / "sets"
SETS_IMG_DIR.mkdir(parents=True, exist_ok=True)

logger.info(f"Database path: {DB_PATH.absolute()}")
logger.info(f"WebApp static folder: {WEBAPP_DIR.absolute()}")

# Flask приложение
app = Flask(__name__, static_folder=str(WEBAPP_DIR.absolute()), static_url_path='')
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # 32MB для загрузки фонов и наборов фишек

# Настройка CORS с ограничениями по происхождению
ALLOWED_ORIGINS = [
    WEBAPP_URL,  # Основной домен WebApp
    'https://*.telegram.org',  # Telegram домены
] if WEBAPP_URL else []

CORS(app, origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS else ["*"])

# Настройка Rate Limiting для защиты от brute-force и spam
# Используем user_id из Telegram для идентификации, иначе IP
def get_user_identifier():
    """Получает идентификатор пользователя для rate limiting"""
    user_id = request.headers.get('X-Telegram-User-Id')
    if user_id:
        return f'user:{user_id}'
    # Для API endpoints с initData
    init_data = request.headers.get('X-Telegram-Init-Data')
    if init_data:
        auth_user = validate_webapp_data(init_data)
        if auth_user and auth_user.get('id'):
            return f'user:{auth_user["id"]}'
    # Fallback на IP
    return f'ip:{get_remote_address()}'

limiter = Limiter(
    key_func=get_user_identifier,
    app=app,
    default_limits=["10000 per day", "1000 per hour"],
    storage_uri="memory://"
)

def get_moscow_now():
    """Текущее время в Москве."""
    try:
        return datetime.now(ZoneInfo("Europe/Moscow"))
    except ZoneInfoNotFoundError:
        return datetime.now() # Fallback


def parse_moscow_datetime(value):
    """Парсит строку времени как московское время."""
    try:
        tz = ZoneInfo("Europe/Moscow")
    except ZoneInfoNotFoundError:
        tz = None # Fallback

    if not value:
        return None

    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=tz) if tz else parsed
        except ValueError:
            continue

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None

    
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz) if tz else parsed
    return parsed.astimezone(tz) if tz else parsed


# Обработчик ошибок rate limit
@app.errorhandler(429)
def ratelimit_handler(e):
    """Обработчик превышения лимита запросов"""
    user_id = request.headers.get('X-Telegram-User-Id', 'unknown')
    security_logger.warning(f"🚨 RATE LIMIT EXCEEDED: user_id={user_id}, path={request.path}")
    return jsonify({
        'error': 'Too Many Requests',
        'message': 'Превышен лимит запросов. Пожалуйста, подождите.'
    }), 429


# Обработчик ошибки превышения размера запроса
@app.errorhandler(413)
def request_entity_too_large(e):
    """Обработчик превышения размера запроса"""
    security_logger.warning(f"🚨 PAYLOAD TOO LARGE: path={request.path}, content_length={request.content_length}")
    return jsonify({
        'error': 'Payload Too Large',
        'message': 'Размер запроса превышает допустимый лимит (1MB)'
    }), 413

@app.before_request
def log_request_info():
    """Логирование входящих запросов для отладки."""
    # Логируем каждый запрос, чтобы видеть, что доходит до сервера
    logger.info(
        f"➡️ Request: {request.method} {request.path} from {request.remote_addr} | User-Agent: {request.user_agent.string}"
    )

@app.after_request
def log_response_info(response):
    """Логирование исходящих ответов для отладки."""
    logger.info(f"⬅️ Response: {response.status_code} for {request.method} {request.path}")
    return response


# ==================== БАЗА ДАННЫХ ====================

def get_db_connection():
    """Создает подключение к базе данных"""
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def get_salebot_db_connection():
    """Создает подключение к базе данных salebot в режиме только для чтения."""
    if not os.path.exists(SALEBOT_DB_PATH):
        logger.error(f"🚨 База данных salebot не найдена по пути: {SALEBOT_DB_PATH}")
        return None
    # Открываем в режиме "только для чтения" (uri=True, mode=ro) для безопасности
    conn = sqlite3.connect(f"file:{SALEBOT_DB_PATH}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Инициализация базы данных - создание таблиц"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Таблица пользователей
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_banned INTEGER DEFAULT 0,
                banned_at TIMESTAMP,
                ban_reason TEXT,
                referrer_id INTEGER,
                wallet_address TEXT
            )
        ''')
        
        # Таблица статистики игроков
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_stats (
                user_id INTEGER PRIMARY KEY,
                clown_games INTEGER DEFAULT 0,
                clown_wins INTEGER DEFAULT 0,
                vladeos_games INTEGER DEFAULT 0,
                vladeos_wins INTEGER DEFAULT 0,
                tower_max_level INTEGER DEFAULT 0,
                tower_total_levels INTEGER DEFAULT 0,
                roulette_games INTEGER DEFAULT 0,
                roulette_wins INTEGER DEFAULT 0,
                roulette_cones_won INTEGER DEFAULT 0,
                roulette_cones_lost INTEGER DEFAULT 0,
                quests TEXT DEFAULT '[]',
                last_daily_bonus TIMESTAMP,
                raccoon_taps INTEGER DEFAULT 0,
                hollow_level INTEGER DEFAULT 1,
                last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                boar_status INTEGER DEFAULT 0,
                boar_arrived_at TIMESTAMP,
                boar_last_burn TIMESTAMP,
                boar_eaten_total INTEGER DEFAULT 0,
                boar_notified INTEGER DEFAULT 0,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        ''')
        
        # Таблица урона по боссу
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS boss_damage (
                user_id INTEGER PRIMARY KEY,
                total_damage INTEGER DEFAULT 0,
                hits INTEGER DEFAULT 0,
                last_hit TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        ''')
        
        # Глобальная таблица босса
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS boss_global (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                current_hp INTEGER DEFAULT 1000000000,
                max_hp INTEGER DEFAULT 1000000000,
                kill_count INTEGER DEFAULT 0,
                last_reset TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Таблица шишек
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_tokens (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER DEFAULT 0,
                total_earned INTEGER DEFAULT 0,
                total_spent INTEGER DEFAULT 0,
                last_earn TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        ''')
        
        # Таблица совместных крафтов
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS coop_crafts (
                craft_id INTEGER PRIMARY KEY AUTOINCREMENT,
                initiator_id INTEGER,
                item_name TEXT,
                start_grade TEXT,
                target_grade TEXT,
                status TEXT DEFAULT 'open', -- open, in_progress, completed, cancelled
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_private INTEGER DEFAULT 0,
                FOREIGN KEY(initiator_id) REFERENCES users(user_id)
            )
        ''')

        # Таблица этапов совместного крафта
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS coop_craft_stages (
                stage_id INTEGER PRIMARY KEY AUTOINCREMENT,
                craft_id INTEGER,
                stage_index INTEGER,
                from_grade TEXT,
                to_grade TEXT,
                material_req TEXT,
                reward_amount REAL DEFAULT 0,
                reward_currency TEXT DEFAULT 'TON',
                contributor_id INTEGER,
                status TEXT DEFAULT 'pending', -- pending, pledged, completed
                contribution_type TEXT, -- 'items', 'gum', 'all'
                FOREIGN KEY(craft_id) REFERENCES coop_crafts(craft_id),
                FOREIGN KEY(contributor_id) REFERENCES users(user_id)
            )
        ''')

        # Таблица активности в чатах
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_activity (
                chat_id INTEGER,
                user_id INTEGER,
                last_message_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, user_id)
            )
        ''')

        # Таблица инвентаря пользователей
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_inventory (
                user_id INTEGER,
                item_id TEXT,
                quantity INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, item_id),
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        ''')
        # Таблица событий тотализатора
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tot_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                image_url TEXT,
                side1_name TEXT,
                side1_odds REAL,
                side2_name TEXT,
                side2_odds REAL,
                draw_name TEXT DEFAULT 'Ничья',
                draw_odds REAL DEFAULT 1.0,
                start_time TEXT,
                status TEXT DEFAULT 'draft', -- draft, active, locked, finished
                winner INTEGER DEFAULT 0,
                event_type TEXT DEFAULT 'standard', -- standard, exact_score
                exact_score_odds REAL DEFAULT 1.0,
                result_score TEXT
            )
        ''')

        # Таблица ставок тотализатора
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tot_bets (
                bet_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER,
                user_id INTEGER,
                side INTEGER,
                prediction TEXT,
                amount REAL,
                currency TEXT DEFAULT 'CG',
                status TEXT DEFAULT 'pending', -- pending, accepted, rejected, won, lost, paid
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(event_id) REFERENCES tot_events(event_id),
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        ''')

        # Таблица для скрытых объявлений из магазина
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS hidden_announcements (
                announcement_id INTEGER PRIMARY KEY
            )
        ''')

        # Таблица обработанных платежей (для защиты от повторных начислений и истории транзакций)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS processed_payments (
                payment_id TEXT PRIMARY KEY,
                user_id INTEGER,
                payment_type TEXT,
                amount REAL,
                cones_amount INTEGER,
                tx_boc TEXT,
                comment TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Таблица инвентаря пользователя
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_inventory (
                user_id INTEGER NOT NULL,
                item_id TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, item_id)
            )
        ''')

        # Таблица рефералов (кто кого пригласил)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS referrals (
                user_id INTEGER PRIMARY KEY,
                referrer_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(user_id),
                FOREIGN KEY(referrer_id) REFERENCES users(user_id)
            )
        ''')

        # Таблица пользовательских сетов фишек
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS custom_chip_sets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author_id INTEGER NOT NULL,
                author_name TEXT,
                title TEXT NOT NULL,
                description TEXT,
                chips_count INTEGER NOT NULL,
                background_image TEXT,
                chips_json TEXT NOT NULL,
                preview_collage TEXT,
                status TEXT DEFAULT 'pending',
                votes_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                approved_at TIMESTAMP,
                FOREIGN KEY(author_id) REFERENCES users(user_id)
            )
        ''')

        # Таблица голосов за сеты фишек
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS custom_chip_votes (
                set_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (set_id, user_id),
                FOREIGN KEY(set_id) REFERENCES custom_chip_sets(id),
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        ''')

        # Инициализация босса
        cursor.execute('''
            INSERT OR IGNORE INTO boss_global (id, current_hp, max_hp, kill_count)
            VALUES (1, 1000000000, 1000000000, 0)
        ''')
        
        conn.commit()

        # Миграции: добавляем недостающие колонки
        _add_missing_columns(cursor)
        conn.commit()
        
        # Принудительный пересчет квестов (только qt) для всех игроков в рейтинге
        _recalculate_quests(cursor)
        conn.commit()
        
        # Миграция: создание таблицы игровых сессий
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='game_sessions'")
        if not cursor.fetchone():
            cursor.execute('''
                CREATE TABLE game_sessions (
                    user_id INTEGER NOT NULL,
                    game_type TEXT NOT NULL,
                    state TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, game_type)
                )
            ''')
        else:
            # Миграция существующей таблицы: пересоздаём с составным ключом если нужно
            cursor.execute("PRAGMA table_info(game_sessions)")
            cols_info = cursor.fetchall()
            pk_cols = [row[1] for row in cols_info if row[5] > 0]  # row[5] = pk index
            if pk_cols == ['user_id']:  # старый схема с одним PK
                try:
                    cursor.execute('ALTER TABLE game_sessions RENAME TO game_sessions_old')
                    cursor.execute('''
                        CREATE TABLE game_sessions (
                            user_id INTEGER NOT NULL,
                            game_type TEXT NOT NULL,
                            state TEXT,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (user_id, game_type)
                        )
                    ''')
                    # Переносим данные из старой таблицы (без дубликатов)
                    cursor.execute('''
                        INSERT OR IGNORE INTO game_sessions (user_id, game_type, state, updated_at)
                        SELECT user_id, COALESCE(game_type, 'clown'), state, updated_at
                        FROM game_sessions_old
                    ''')
                    cursor.execute('DROP TABLE game_sessions_old')
                    logger.info("✅ Миграция game_sessions: PRIMARY KEY обновлён до (user_id, game_type)")
                except Exception as e:
                    logger.error(f"Ошибка миграции game_sessions: {e}")

        # Создаём записи в user_tokens для всех пользователей у которых их нет
        cursor.execute('''
            INSERT OR IGNORE INTO user_tokens (user_id, balance, total_earned, total_spent)
            SELECT user_id, 0, 0, 0 FROM users
        ''')
        conn.commit()

        logger.info("✅ База данных инициализирована")
        
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации БД: {e}")
        raise
    finally:
        conn.close()


def _recalculate_quests(cursor):
    """Пересчитывает quests_completed для всех пользователей, учитывая только реальные квесты (qt)"""
    try:
        cursor.execute("SELECT user_id, quests FROM user_stats WHERE quests IS NOT NULL AND quests != '[]'")
        rows = cursor.fetchall()
        updated_count = 0
        for row in rows:
            try:
                quests_list = json.loads(row['quests'])
                if isinstance(quests_list, list):
                    # Считаем только элементы, которые начинаются на 'qt'
                    actual_count = len([q for q in quests_list if isinstance(q, str) and q.startswith('qt')])
                    cursor.execute("UPDATE user_stats SET quests_completed = ? WHERE user_id = ?", (actual_count, row['user_id']))
                    updated_count += 1
            except Exception:
                continue
        logger.info(f"✅ Пересчет quests_completed завершен. Актуализировано {updated_count} игроков.")
    except Exception as e:
        logger.error(f"Ошибка пересчета квестов: {e}")

def _add_missing_columns(cursor):
    """Добавляет недостающие колонки в существующие таблицы"""

    # Проверка users на наличие is_banned
    cursor.execute("PRAGMA table_info(users)")
    users_cols = {row[1] for row in cursor.fetchall()}

    if 'is_banned' not in users_cols:
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
            cursor.execute("ALTER TABLE users ADD COLUMN banned_at TIMESTAMP")
            cursor.execute("ALTER TABLE users ADD COLUMN ban_reason TEXT")
            logger.info("Миграция: добавлены колонки бана в users")
        except Exception as e:
            logger.error(f"Ошибка миграции users ban columns: {e}")

    # Проверка boss_damage на наличие hits
    cursor.execute("PRAGMA table_info(boss_damage)")
    boss_damage_cols = {row[1] for row in cursor.fetchall()}

    if 'hits' not in boss_damage_cols:
        try:
            cursor.execute("ALTER TABLE boss_damage ADD COLUMN hits INTEGER DEFAULT 0")
            logger.info("Миграция: добавлена колонка hits в boss_damage")
        except Exception as e:
            logger.error(f"Ошибка миграции boss_damage.hits: {e}")

    # Проверка boss_global на наличие kill_count
    cursor.execute("PRAGMA table_info(boss_global)")
    boss_global_cols = {row[1] for row in cursor.fetchall()}

    if 'kill_count' not in boss_global_cols:
        try:
            cursor.execute("ALTER TABLE boss_global ADD COLUMN kill_count INTEGER DEFAULT 0")
            logger.info("Миграция: добавлена колонка kill_count в boss_global")
        except Exception as e:
            logger.error(f"Ошибка миграции boss_global.kill_count: {e}")

    # Проверка наличия таблицы user_tokens
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_tokens'")
    if not cursor.fetchone():
        try:
            cursor.execute('''
                CREATE TABLE user_tokens (
                    user_id INTEGER PRIMARY KEY,
                    balance INTEGER DEFAULT 0,
                    total_earned INTEGER DEFAULT 0,
                    total_spent INTEGER DEFAULT 0,
                    last_earn TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(user_id)
                )
            ''')
            logger.info("Миграция: создана таблица user_tokens")
        except Exception as e:
            logger.error(f"Ошибка миграции user_tokens: {e}")

    # Проверка наличия колонок рулетки в user_stats
    cursor.execute("PRAGMA table_info(user_stats)")
    user_stats_cols = {row[1] for row in cursor.fetchall()}
    if 'roulette_games' not in user_stats_cols:
        try:
            cursor.execute("ALTER TABLE user_stats ADD COLUMN roulette_games INTEGER DEFAULT 0")
            cursor.execute("ALTER TABLE user_stats ADD COLUMN roulette_wins INTEGER DEFAULT 0")
            logger.info("Миграция: добавлены колонки рулетки в user_stats")
        except Exception as e:
            logger.error(f"Ошибка миграции user_stats roulette: {e}")

    if 'roulette_cones_won' not in user_stats_cols:
        try:
            cursor.execute("ALTER TABLE user_stats ADD COLUMN roulette_cones_won INTEGER DEFAULT 0")
            cursor.execute("ALTER TABLE user_stats ADD COLUMN roulette_cones_lost INTEGER DEFAULT 0")
            logger.info("Миграция: добавлены колонки roulette_cones в user_stats")
        except Exception as e:
            logger.error(f"Ошибка миграции user_stats roulette_cones: {e}")

    # Проверка наличия колонок квестов в user_stats
    if 'quests_completed' not in user_stats_cols:
        try:
            cursor.execute("ALTER TABLE user_stats ADD COLUMN quests_completed INTEGER DEFAULT 0")
            cursor.execute("ALTER TABLE user_stats ADD COLUMN last_quest_time TIMESTAMP")
            
            # Пытаемся автоматически посчитать количество квестов для старых игроков
            try:
                cursor.execute("UPDATE user_stats SET quests_completed = json_array_length(quests) WHERE quests IS NOT NULL AND quests != '[]'")
            except Exception:
                pass
                
            logger.info("Миграция: добавлены колонки quests_completed и last_quest_time в user_stats")
        except Exception as e:
            logger.error(f"Ошибка миграции user_stats quests: {e}")

    if 'tutorials_seen' not in user_stats_cols:
        try:
            cursor.execute("ALTER TABLE user_stats ADD COLUMN tutorials_seen TEXT DEFAULT '[]'")
            logger.info("Миграция: добавлена колонка tutorials_seen в user_stats")
        except Exception as e:
            logger.error(f"Ошибка миграции user_stats.tutorials_seen: {e}")

    if 'last_news_submit' not in user_stats_cols:
        try:
            cursor.execute("ALTER TABLE user_stats ADD COLUMN last_news_submit TIMESTAMP")
            logger.info("Миграция: добавлена колонка last_news_submit в user_stats")
        except Exception as e:
            logger.error(f"Ошибка миграции user_stats last_news_submit: {e}")

    if 'energy' not in user_stats_cols:
        try:
            cursor.execute("ALTER TABLE user_stats ADD COLUMN energy INTEGER DEFAULT 30")
            logger.info("Миграция: добавлена колонка energy в user_stats")
        except Exception as e:
            logger.error(f"Ошибка миграции user_stats.energy: {e}")

    if 'energy_last_updated' not in user_stats_cols:
        try:
            cursor.execute("ALTER TABLE user_stats ADD COLUMN energy_last_updated TIMESTAMP")
            cursor.execute("UPDATE user_stats SET energy_last_updated = CURRENT_TIMESTAMP WHERE energy_last_updated IS NULL")
            logger.info("Миграция: добавлена колонка energy_last_updated в user_stats")
        except Exception as e:
            logger.error(f"Ошибка миграции user_stats.energy_last_updated: {e}")

    if 'last_daily_bonus' not in user_stats_cols:
        try:
            cursor.execute("ALTER TABLE user_stats ADD COLUMN last_daily_bonus TIMESTAMP")
            logger.info("Миграция: добавлена колонка last_daily_bonus в user_stats")
        except Exception as e:
            logger.error(f"Ошибка миграции user_stats.last_daily_bonus: {e}")

    if 'raccoon_taps' not in user_stats_cols:
        try:
            cursor.execute("ALTER TABLE user_stats ADD COLUMN raccoon_taps INTEGER DEFAULT 0")
            logger.info("Миграция: добавлена колонка raccoon_taps в user_stats")
        except Exception as e:
            logger.error(f"Ошибка миграции user_stats.raccoon_taps: {e}")

    if 'hollow_level' not in user_stats_cols:
        try:
            cursor.execute("ALTER TABLE user_stats ADD COLUMN hollow_level INTEGER DEFAULT 1")
            cursor.execute("UPDATE user_stats SET hollow_level = 1 WHERE hollow_level IS NULL")
            logger.info("Миграция: добавлена колонка hollow_level в user_stats")
        except Exception as e:
            logger.error(f"Ошибка миграции user_stats.hollow_level: {e}")

    if 'last_activity' not in user_stats_cols:
        try:
            cursor.execute("ALTER TABLE user_stats ADD COLUMN last_activity TIMESTAMP")
            cursor.execute("UPDATE user_stats SET last_activity = COALESCE(last_daily_bonus, CURRENT_TIMESTAMP)")
            logger.info("Миграция: добавлена колонка last_activity в user_stats")
        except Exception as e:
            logger.error(f"Ошибка миграции user_stats.last_activity: {e}")

    if 'boar_status' not in user_stats_cols:
        try:
            cursor.execute("ALTER TABLE user_stats ADD COLUMN boar_status INTEGER DEFAULT 0")
            cursor.execute("ALTER TABLE user_stats ADD COLUMN boar_arrived_at TIMESTAMP")
            cursor.execute("ALTER TABLE user_stats ADD COLUMN boar_last_burn TIMESTAMP")
            cursor.execute("ALTER TABLE user_stats ADD COLUMN boar_eaten_total INTEGER DEFAULT 0")
            cursor.execute("ALTER TABLE user_stats ADD COLUMN boar_notified INTEGER DEFAULT 0")
            logger.info("Миграция: добавлены колонки механики Кабана в user_stats")
        except Exception as e:
            logger.error(f"Ошибка миграции колонок кабана в user_stats: {e}")

    # Проверка users на наличие wallet_address
    cursor.execute("PRAGMA table_info(users)")
    users_cols = {row[1] for row in cursor.fetchall()}
    if 'wallet_address' not in users_cols:
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN wallet_address TEXT")
            logger.info("Миграция: добавлена колонка wallet_address в users")
        except Exception as e:
            logger.error(f"Ошибка миграции users.wallet_address: {e}")

    # Проверка coop_craft_stages на наличие item/gum contributor_id
    cursor.execute("PRAGMA table_info(coop_craft_stages)")
    coop_stages_cols = {row[1] for row in cursor.fetchall()}
    if 'item_contributor_id' not in coop_stages_cols:
        try:
            cursor.execute("ALTER TABLE coop_craft_stages ADD COLUMN item_contributor_id INTEGER")
            cursor.execute("ALTER TABLE coop_craft_stages ADD COLUMN gum_contributor_id INTEGER")
            logger.info("Миграция: добавлены колонки item_contributor_id, gum_contributor_id в coop_craft_stages")
        except Exception as e:
            logger.error(f"Ошибка миграции coop_craft_stages для мульти-крафта: {e}")

    if 'reward_amount' not in coop_stages_cols:
        try:
            cursor.execute("ALTER TABLE coop_craft_stages ADD COLUMN reward_amount REAL DEFAULT 0")
            cursor.execute("ALTER TABLE coop_craft_stages ADD COLUMN reward_currency TEXT DEFAULT 'TON'")
            logger.info("Миграция: добавлены колонки вознаграждения в coop_craft_stages")
        except Exception as e:
            logger.error(f"Ошибка миграции coop_craft_stages rewards: {e}")

    # Проверка coop_craft_stages на наличие contribution_type
    cursor.execute("PRAGMA table_info(coop_craft_stages)")
    coop_stages_cols = {row[1] for row in cursor.fetchall()}
    if 'contribution_type' not in coop_stages_cols:
        try:
            cursor.execute("ALTER TABLE coop_craft_stages ADD COLUMN contribution_type TEXT")
            logger.info("Миграция: добавлена колонка contribution_type в coop_craft_stages")
        except Exception as e:
            logger.error(f"Ошибка миграции coop_craft_stages.contribution_type: {e}")

    # Проверка coop_crafts на наличие is_private
    cursor.execute("PRAGMA table_info(coop_crafts)")
    coop_crafts_cols = {row[1] for row in cursor.fetchall()}
    if 'is_private' not in coop_crafts_cols:
        try:
            cursor.execute("ALTER TABLE coop_crafts ADD COLUMN is_private INTEGER DEFAULT 0")
            logger.info("Миграция: добавлена колонка is_private в coop_crafts")
        except Exception as e:
            logger.error(f"Ошибка миграции coop_crafts.is_private: {e}")

    # Проверка tot_events на наличие draw_odds
    cursor.execute("PRAGMA table_info(tot_events)")
    tot_events_cols = {row[1] for row in cursor.fetchall()}
    if 'draw_odds' not in tot_events_cols:
        try:
            cursor.execute("ALTER TABLE tot_events ADD COLUMN draw_name TEXT DEFAULT 'Ничья'")
            cursor.execute("ALTER TABLE tot_events ADD COLUMN draw_odds REAL DEFAULT 1.0")
            logger.info("Миграция: добавлены колонки ничьей в tot_events")
        except Exception as e:
            logger.error(f"Ошибка миграции tot_events draw: {e}")

    if 'image_url' not in tot_events_cols:
        try:
            cursor.execute("ALTER TABLE tot_events ADD COLUMN image_url TEXT")
            logger.info("Миграция: добавлена колонка image_url в tot_events")
        except Exception as e:
            logger.error(f"Ошибка миграции tot_events image_url: {e}")

    if 'event_type' not in tot_events_cols:
        try:
            cursor.execute("ALTER TABLE tot_events ADD COLUMN event_type TEXT DEFAULT 'standard'")
            cursor.execute("ALTER TABLE tot_events ADD COLUMN exact_score_odds REAL DEFAULT 1.0")
            cursor.execute("ALTER TABLE tot_events ADD COLUMN result_score TEXT")
            logger.info("Миграция: добавлены колонки exact_score в tot_events")
        except Exception as e:
            logger.error(f"Ошибка миграции tot_events exact_score: {e}")
            
    # Проверка tot_bets на наличие currency
    cursor.execute("PRAGMA table_info(tot_bets)")
    tot_bets_cols = {row[1] for row in cursor.fetchall()}
    if 'currency' not in tot_bets_cols:
        try:
            cursor.execute("ALTER TABLE tot_bets ADD COLUMN currency TEXT DEFAULT 'CG'")
            logger.info("Миграция: добавлена колонка currency в tot_bets")
        except Exception as e:
            logger.error(f"Ошибка миграции tot_bets currency: {e}")

    if 'prediction' not in tot_bets_cols:
        try:
            cursor.execute("ALTER TABLE tot_bets ADD COLUMN prediction TEXT")
            logger.info("Миграция: добавлена колонка prediction в tot_bets")
        except Exception as e:
            logger.error(f"Ошибка миграции tot_bets prediction: {e}")

    # Проверка user_stats на наличие roulette_total_bets
    cursor.execute("PRAGMA table_info(user_stats)")
    user_stats_cols = {row[1] for row in cursor.fetchall()}
    if 'roulette_total_bets' not in user_stats_cols:
        try:
            cursor.execute("ALTER TABLE user_stats ADD COLUMN roulette_total_bets INTEGER DEFAULT 0")
            logger.info("Миграция: добавлена колонка roulette_total_bets в user_stats")
        except Exception as e:
            logger.error(f"Ошибка миграции user_stats roulette_total_bets: {e}")

    # Проверка users на наличие referrer_id
    if 'referrer_id' not in users_cols:
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN referrer_id INTEGER")
            logger.info("Миграция: добавлена колонка referrer_id в users")
        except Exception as e:
            logger.error(f"Ошибка миграции users referrer_id: {e}")

    # Проверка наличия таблицы referrals
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='referrals'")
    if not cursor.fetchone():
        try:
            cursor.execute('''
                CREATE TABLE referrals (
                    user_id INTEGER PRIMARY KEY,
                    referrer_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(user_id),
                    FOREIGN KEY(referrer_id) REFERENCES users(user_id)
                )
            ''')
            logger.info("Миграция: создана таблица referrals")
        except Exception as e:
            logger.error(f"Ошибка миграции таблицы referrals: {e}")

def ensure_user_exists(user_id: int, user_data: dict = None):
    """Гарантирует существование пользователя в БД и сохраняет его username/имя"""
    if not user_id:
        return
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Проверяем существует ли пользователь
        cursor.execute("SELECT user_id, username, first_name, last_name FROM users WHERE user_id = ?", (user_id,))
        existing = cursor.fetchone()

        clean_username = sanitize_string(user_data.get('username', ''), max_length=64) if user_data else ''
        clean_first_name = sanitize_string(user_data.get('first_name', ''), max_length=128) if user_data else ''
        clean_last_name = sanitize_string(user_data.get('last_name', ''), max_length=128) if user_data else ''

        if existing:
            # Обновляем только непустые поля, не затирая уже сохраненные данные
            updates = []
            params = []
            if clean_username and clean_username != (existing['username'] or ''):
                updates.append("username = ?")
                params.append(clean_username)
            if clean_first_name and clean_first_name != (existing['first_name'] or ''):
                updates.append("first_name = ?")
                params.append(clean_first_name)
            if clean_last_name and clean_last_name != (existing['last_name'] or ''):
                updates.append("last_name = ?")
                params.append(clean_last_name)

            if updates:
                params.append(user_id)
                cursor.execute(f"UPDATE users SET {', '.join(updates)} WHERE user_id = ?", params)
                logger.info(f"✅ Пользователь {user_id} обновлен: username='{clean_username or existing['username']}'")
        else:
            # Создаем нового пользователя
            cursor.execute('''
                INSERT INTO users (user_id, username, first_name, last_name)
                VALUES (?, ?, ?, ?)
            ''', (user_id, clean_username, clean_first_name, clean_last_name))
            logger.info(f"✅ Новый пользователь {user_id} зарегистрирован: username='{clean_username}'")

        # Создаем запись статистики если нет
        cursor.execute('INSERT OR IGNORE INTO user_stats (user_id) VALUES (?)', (user_id,))
        
        # Создаем запись токенов если нет (с балансом 0)
        cursor.execute('INSERT OR IGNORE INTO user_tokens (user_id, balance, total_earned, total_spent) VALUES (?, 0, 0, 0)', (user_id,))

        conn.commit()

    except Exception as e:
        logger.error(f"Ошибка ensure_user_exists: {e}")
        raise
    finally:
        conn.close()

# ==================== БЕЗОПАСНОСТЬ API ====================

def validate_webapp_data(init_data: str) -> dict:
    """Проверяет подлинность данных от Telegram WebApp"""
    if not init_data:
        return None
    if not BOT_TOKEN:
        # Dev fallback: parse user data without hash verification when token is not configured
        try:
            parsed_data = dict(parse_qsl(init_data))
            if 'user' in parsed_data:
                return json.loads(parsed_data['user'])
        except Exception:
            return None
        return None
    try:
        parsed_data = dict(parse_qsl(init_data))
        if 'hash' not in parsed_data:
            security_logger.warning(f"🚨 INVALID INIT DATA: отсутствует hash")
            return None

        hash_val = parsed_data.pop('hash')
        sorted_keys = sorted(parsed_data.keys())
        data_check_string = '\n'.join([f"{k}={parsed_data[k]}" for k in sorted_keys])

        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if calculated_hash == hash_val:
            if 'user' in parsed_data:
                user_data = json.loads(parsed_data['user'])
                security_logger.info(f"✅ AUTH SUCCESS: user_id={user_data.get('id')}")
                return user_data
        else:
            security_logger.warning(f"🚨 INVALID HASH: calculated hash mismatch")
        return None
    except Exception as e:
        security_logger.error(f"🚨 VALIDATION ERROR: {e}")
        logger.error(f"Error validating initData: {e}")
        return None


def sanitize_string(value: str, max_length: int = 255) -> str:
    """
    Санизирует строку - удаляет опасные символы и ограничивает длину
    
    Args:
        value: Входная строка
        max_length: Максимальная длина строки
    
    Returns:
        Очищенная строка
    """
    if not value:
        return ''
    
    # Преобразуем в строку если нужно
    value = str(value)
    
    # Обрезаем до максимальной длины
    value = value[:max_length]
    
    # Удаляем null-символы
    value = value.replace('\x00', '')
    
    # Экранируем потенциально опасные HTML-символы
    value = value.replace('<', '&lt;').replace('>', '&gt;')
    
    return value.strip()


def validate_integer(value, min_val: int = None, max_val: int = None, default: int = 0) -> int:
    """
    Валидирует и преобразует значение в целое число
    
    Args:
        value: Входное значение
        min_val: Минимальное допустимое значение
        max_val: Максимальное допустимое значение
        default: Значение по умолчанию при ошибке
    
    Returns:
        Валидированное целое число
    """
    try:
        result = int(value)
        
        if min_val is not None and result < min_val:
            return min_val
        if max_val is not None and result > max_val:
            return max_val
            
        return result
    except (ValueError, TypeError):
        return default


def validate_list(value, default: list = None) -> list:
    """
    Валидирует список
    
    Args:
        value: Входное значение
        default: Значение по умолчанию
    
    Returns:
        Валидированный список
    """
    if default is None:
        default = []
        
    if isinstance(value, list):
        # Ограничиваем количество элементов
        return value[:100]
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return parsed[:100]
    except (json.JSONDecodeError, TypeError):
        pass
    return default

@app.before_request
def auto_register_user_from_request():
    """Автоматически регистрирует или обновляет пользователя из X-Telegram-Init-Data или JSON тела при ЛЮБОМ запросе к API"""
    if request.path.startswith('/api/'):
        try:
            init_data = request.headers.get('X-Telegram-Init-Data')
            if init_data:
                auth_user = validate_webapp_data(init_data)
                if auth_user and auth_user.get('id'):
                    ensure_user_exists(int(auth_user['id']), auth_user)
            elif request.is_json:
                data = request.get_json(silent=True)
                if data:
                    user_id = data.get('userId') or data.get('user_id')
                    if user_id:
                        user_info = {}
                        if data.get('username'): user_info['username'] = data['username']
                        if data.get('first_name'): user_info['first_name'] = data['first_name']
                        if data.get('last_name'): user_info['last_name'] = data['last_name']
                        if user_info:
                            ensure_user_exists(int(user_id), user_info)
        except Exception as e:
            logger.debug(f"Auto-register before_request notice: {e}")

# ==================== ИГРОВЫЕ СЕССИИ ====================

def get_game_session(user_id: int, game_type: str) -> dict:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT state FROM game_sessions WHERE user_id = ? AND game_type = ?', (user_id, game_type))
        row = cursor.fetchone()
        if row: return json.loads(row['state'])
    except Exception as e:
        logger.error(f"Error get_game_session: {e}")
    finally:
        conn.close()
    return None

def save_game_session(user_id: int, game_type: str, state: dict):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            INSERT INTO game_sessions (user_id, game_type, state, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, game_type) DO UPDATE SET
                state = excluded.state,
                updated_at = CURRENT_TIMESTAMP
        ''', (user_id, game_type, json.dumps(state)))
        conn.commit()
    finally:
        conn.close()

def clear_game_session(user_id: int, game_type: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM game_sessions WHERE user_id = ? AND game_type = ?', (user_id, game_type))
    conn.commit()
    conn.close()

# ==================== API ФУНКЦИИ ====================

def save_user_stats(user_id: int, stats_data: dict, user_data: dict = None) -> bool:
    """
    Сохраняет статистику игрока в базу данных
    
    Args:
        user_id: ID пользователя в Telegram
        stats_data: Словарь с данными статистики
        user_data: Словарь с данными пользователя (username, first_name, last_name)
    
    Returns:
        True если успешно, False иначе
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Гарантируем существование пользователя (с обновлением данных)
        if user_data:
            ensure_user_exists(user_id, user_data)
        else:
            ensure_user_exists(user_id)
        
        # Получаем текущие квесты, чтобы предотвратить их удаление при сбросе кэша клиента
        cursor.execute('SELECT quests, tutorials_seen FROM user_stats WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        existing_quests = []
        existing_tutorials = []
        if row:
            if row['quests']:
                try:
                    existing_quests = json.loads(row['quests'])
                except (json.JSONDecodeError, TypeError):
                    pass
            if row['tutorials_seen']:
                try:
                    existing_tutorials = json.loads(row['tutorials_seen'])
                except (json.JSONDecodeError, TypeError):
                    pass
                
        incoming_quests = stats_data.get('quests', [])
        if not isinstance(incoming_quests, list):
            incoming_quests = []
        if not isinstance(existing_quests, list):
            existing_quests = []

        existing_quests = [str(q) for q in existing_quests if q is not None]
        incoming_quests = [str(q) for q in incoming_quests if q is not None]
        merged_quests = list(dict.fromkeys(existing_quests + incoming_quests))
        
        # Если квестов стало больше, обновляем время
        quests_updated = len(merged_quests) > len(existing_quests)
        time_update_sql = ", last_quest_time = CURRENT_TIMESTAMP" if quests_updated else ""
        
        # Для рейтинга считаем только реальные квесты (ID начинается на qt)
        actual_quests_count = len([q for q in merged_quests if isinstance(q, str) and q.startswith('qt')])

        # Объединяем просмотренные туториалы
        incoming_tutorials = stats_data.get('tutorials_seen', [])
        if not isinstance(incoming_tutorials, list):
            incoming_tutorials = []
        if not isinstance(existing_tutorials, list):
            existing_tutorials = []

        existing_tutorials = [str(t) for t in existing_tutorials if t is not None]
        incoming_tutorials = [str(t) for t in incoming_tutorials if t is not None]
        merged_tutorials = list(dict.fromkeys(existing_tutorials + incoming_tutorials))

        # Обновляем статистику
        cursor.execute(f'''
            UPDATE user_stats SET
                clown_games = MAX(clown_games, ?),
                clown_wins = MAX(clown_wins, ?),
                vladeos_games = MAX(vladeos_games, ?),
                vladeos_wins = MAX(vladeos_wins, ?),
                tower_max_level = MAX(tower_max_level, ?),
                tower_total_levels = MAX(tower_total_levels, ?),
                roulette_games = MAX(roulette_games, ?),
                roulette_wins = MAX(roulette_wins, ?),
                roulette_cones_won = MAX(roulette_cones_won, ?),
                roulette_cones_lost = MAX(roulette_cones_lost, ?),
                raccoon_taps = MAX(COALESCE(raccoon_taps, 0), ?),
                quests = ?,
                quests_completed = ?,
                tutorials_seen = ?,
                last_activity = CURRENT_TIMESTAMP
                {time_update_sql}
            WHERE user_id = ?
        ''', (
            int(stats_data.get('clown_games', 0)),
            int(stats_data.get('clown_wins', 0)),
            int(stats_data.get('vladeos_games', 0)),
            int(stats_data.get('vladeos_wins', 0)),
            int(stats_data.get('tower_max_level', 0)),
            int(stats_data.get('tower_total_levels', 0)),
            int(stats_data.get('roulette_games', 0)),
            int(stats_data.get('roulette_wins', 0)),
            int(stats_data.get('roulette_cones_won', 0)),
            int(stats_data.get('roulette_cones_lost', 0)),
            int(stats_data.get('raccoon_taps', 0)),
            json.dumps(merged_quests),
            actual_quests_count,
            json.dumps(merged_tutorials),
            user_id
        ))
        
        conn.commit()
        logger.info(f"✅ Статистика сохранена для user_id={user_id}")
        return True
        
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения статистики: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def add_boss_damage(user_id: int, damage: int) -> dict:
    """
    Добавляет урон по боссу и уменьшает HP босса
    
    Args:
        user_id: ID пользователя в Telegram
        damage: Количество урона
    
    Returns:
        Словарь с текущим состоянием босса или None при ошибке
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Гарантируем существование пользователя
        ensure_user_exists(user_id)
        
        # Обновляем урон игрока
        cursor.execute('''
            INSERT INTO boss_damage (user_id, total_damage, hits, last_hit)
            VALUES (?, ?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                total_damage = total_damage + excluded.total_damage,
                hits = hits + 1,
                last_hit = CURRENT_TIMESTAMP
        ''', (user_id, damage))
        
        # Уменьшаем HP босса
        cursor.execute('''
            UPDATE boss_global 
            SET current_hp = current_hp - ?
            WHERE id = 1 AND current_hp > 0
        ''', (damage,))
        
        # Проверяем состояние босса
        cursor.execute('SELECT current_hp, max_hp, kill_count FROM boss_global WHERE id = 1')
        row = cursor.fetchone()
        
        boss_killed = False
        if row and row['current_hp'] <= 0:
            # Босс умер - возрождаем
            boss_killed = True
            cursor.execute('''
                UPDATE boss_global SET
                    current_hp = max_hp,
                    kill_count = kill_count + 1,
                    last_reset = CURRENT_TIMESTAMP
                WHERE id = 1
            ''')
            logger.info(f"💀 БОСС УБИТ! user_id={user_id}, kill_count={row['kill_count'] + 1}")
        
        conn.commit()
        
        # Получаем актуальное состояние босса
        cursor.execute('SELECT current_hp, max_hp, kill_count FROM boss_global WHERE id = 1')
        boss_row = cursor.fetchone()
        
        boss_info = {
            'current_hp': boss_row['current_hp'] if boss_row else 1000000000,
            'max_hp': boss_row['max_hp'] if boss_row else 1000000000,
            'kill_count': boss_row['kill_count'] if boss_row else 0,
            'boss_killed': boss_killed
        }
        
        logger.info(f"💥 Урон нанесен: user_id={user_id}, damage={damage}, HP={boss_info['current_hp']:,}")
        return boss_info
        
    except Exception as e:
        logger.error(f"❌ Ошибка добавления урона: {e}")
        conn.rollback()
        return None
    finally:
        conn.close()


def get_boss_hp() -> dict:
    """Получает текущее состояние единого мирового босса и статистику рейда"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute('SELECT current_hp, max_hp, kill_count FROM boss_global WHERE id = 1')
        row = cursor.fetchone()
        
        current_hp = row['current_hp'] if row else 1000000000
        max_hp = row['max_hp'] if row else 1000000000
        kill_count = row['kill_count'] if row else 0

        # Статистика участников рейда
        cursor.execute('SELECT COUNT(*) as cnt, COALESCE(SUM(total_damage), 0) as total_dmg FROM boss_damage WHERE total_damage > 0')
        stat_row = cursor.fetchone()
        total_fighters = stat_row['cnt'] if stat_row else 0
        total_raid_damage = stat_row['total_dmg'] if stat_row else 0

        # Топ-5 дамагеров по боссу
        cursor.execute('''
            SELECT bd.user_id, bd.total_damage, bd.hits,
                   u.username, u.first_name, u.last_name
            FROM boss_damage bd
            LEFT JOIN users u ON bd.user_id = u.user_id
            WHERE bd.total_damage > 0
            ORDER BY bd.total_damage DESC
            LIMIT 5
        ''')
        top_rows = cursor.fetchall()
        top_damagers = []
        for r in top_rows:
            name = r['first_name'] or ''
            if r['last_name']: name += f" {r['last_name']}"
            if not name.strip() and r['username']: name = f"@{r['username']}"
            if not name.strip(): name = f"Игрок #{r['user_id']}"
            top_damagers.append({
                'user_id': r['user_id'],
                'name': name.strip(),
                'username': r['username'] or '',
                'total_damage': r['total_damage'],
                'hits': r['hits']
            })

        return {
            'current_hp': current_hp,
            'max_hp': max_hp,
            'kill_count': kill_count,
            'total_fighters': total_fighters,
            'total_raid_damage': total_raid_damage,
            'top_damagers': top_damagers
        }
        
    except Exception as e:
        logger.error(f"Ошибка get_boss_hp: {e}")
        return {
            'current_hp': 1000000000,
            'max_hp': 1000000000,
            'kill_count': 0,
            'total_fighters': 0,
            'total_raid_damage': 0,
            'top_damagers': []
        }
    finally:
        conn.close()


def calculate_overall_score(user_data_row: dict) -> dict:
    """
    Рассчитывает общий счет по формуле:
    наличие шишек + пройденные квесты * 10 000 + каждая сыгранная игра * 100 + (ставки игрока в рулетке / 2) + за каждый прочитанный номер газеты 500 очков
    """
    # 1. Наличие шишек
    balance = user_data_row.get('balance') or 0

    # 2. Пройденные квесты * 10 000
    quests_completed = user_data_row.get('quests_completed') or 0
    quests_points = quests_completed * 10000

    # 3. Каждая сыгранная игра * 100
    clown_games = user_data_row.get('clown_games') or 0
    vladeos_games = user_data_row.get('vladeos_games') or 0
    tower_total_levels = user_data_row.get('tower_total_levels') or 0
    roulette_games = user_data_row.get('roulette_games') or 0
    total_games = clown_games + vladeos_games + tower_total_levels + roulette_games
    games_points = total_games * 100

    # 4. Ставки игрока в рулетке (вес уменьшен вдвое: x0.5)
    roulette_total_bets = user_data_row.get('roulette_total_bets')
    if roulette_total_bets is None or roulette_total_bets == 0:
        roulette_total_bets = (user_data_row.get('roulette_cones_lost') or 0) + (user_data_row.get('roulette_games') or 0) * 10
    roulette_bets_points = int((roulette_total_bets or 0) * 0.5)

    # 5. За каждый прочитанный номер газеты 500 очков
    quests_json = user_data_row.get('quests') or '[]'
    newspapers_read = 0
    try:
        if isinstance(quests_json, str):
            q_list = json.loads(quests_json)
        else:
            q_list = quests_json or []
        newspapers_read = sum(1 for q in q_list if isinstance(q, str) and (q.startswith('news_') or q.startswith('caps_news')))
    except Exception:
        newspapers_read = 0
    newspapers_points = newspapers_read * 500

    total_score = balance + quests_points + games_points + roulette_bets_points + newspapers_points
    return {
        'total_score': total_score,
        'balance': balance,
        'quests_completed': quests_completed,
        'total_games': total_games,
        'roulette_bets': roulette_total_bets,
        'roulette_bets_points': roulette_bets_points,
        'newspapers_read': newspapers_read
    }


def get_player_stats(user_id: int) -> dict:
    """Получает статистику игрока включая общий счет"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            SELECT us.clown_games, us.clown_wins, us.vladeos_games, us.vladeos_wins,
                   us.tower_max_level, us.tower_total_levels, us.quests, us.tutorials_seen,
                   us.roulette_games, us.roulette_wins,
                   us.roulette_cones_won, us.roulette_cones_lost,
                   COALESCE(us.roulette_total_bets, 0) as roulette_total_bets,
                   COALESCE(us.quests_completed, 0) as quests_completed,
                   COALESCE(us.raccoon_taps, 0) as raccoon_taps,
                   COALESCE(us.hollow_level, 1) as hollow_level,
                   COALESCE(ut.balance, 0) as balance
            FROM user_stats us
            LEFT JOIN user_tokens ut ON us.user_id = ut.user_id
            WHERE us.user_id = ?
        ''', (user_id,))
        row = cursor.fetchone()
        
        if row:
            row_dict = dict(row)
            overall = calculate_overall_score(row_dict)
            return {
                'clown_games': row['clown_games'],
                'clown_wins': row['clown_wins'],
                'vladeos_games': row['vladeos_games'],
                'vladeos_wins': row['vladeos_wins'],
                'tower_max_level': row['tower_max_level'],
                'tower_total_levels': row['tower_total_levels'],
                'roulette_games': row['roulette_games'],
                'roulette_wins': row['roulette_wins'],
                'roulette_cones_won': row['roulette_cones_won'],
                'roulette_cones_lost': row['roulette_cones_lost'],
                'roulette_total_bets': row['roulette_total_bets'],
                'quests_completed': row['quests_completed'],
                'raccoon_taps': row['raccoon_taps'],
                'hollow_level': row['hollow_level'],
                'overall_score': overall['total_score'],
                'balance': row['balance'],
                'quests': json.loads(row['quests']) if row['quests'] else [],
                'tutorials_seen': json.loads(row['tutorials_seen']) if row['tutorials_seen'] else []
            }
        return {}
        
    except Exception as e:
        logger.error(f"Ошибка get_player_stats: {e}")
        return {}
    finally:
        conn.close()


def get_boss_damage(user_id: int) -> dict:
    """Получает урон игрока по боссу"""
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute('SELECT total_damage, hits, last_hit FROM boss_damage WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()

        if row:
            return {
                'total_damage': row['total_damage'],
                'hits': row['hits'],
                'last_hit': row['last_hit']
            }
        return {'total_damage': 0, 'hits': 0, 'last_hit': None}

    except Exception as e:
        logger.error(f"Ошибка get_boss_damage: {e}")
        return {'total_damage': 0, 'hits': 0, 'last_hit': None}
    finally:
        conn.close()


def get_leaderboard(limit: int = 10) -> list:
    """
    Получает топ игроков по балансу шишек

    Args:
        limit: Количество игроков в рейтинге (по умолчанию 10)

    Returns:
        Список словарей с данными игроков
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Получаем топ игроков по шишкам с данными пользователя
        cursor.execute('''
            SELECT
                u.user_id,
                u.username,
                u.first_name,
                u.last_name,
                ut.balance,
                ut.total_earned,
                ut.total_spent
            FROM user_tokens ut
            JOIN users u ON ut.user_id = u.user_id
            WHERE ut.balance > 0 OR ut.total_earned > 0
            ORDER BY ut.balance DESC, ut.total_earned DESC
            LIMIT ?
        ''', (limit,))

        rows = cursor.fetchall()
        leaderboard = []

        for i, row in enumerate(rows):
            # Формируем имя: username или first_name last_name
            name = row['username'] if row['username'] else f"{row['first_name'] or ''} {row['last_name'] or ''}".strip()
            if not name:
                name = f"Игрок #{row['user_id']}"

            leaderboard.append({
                'rank': i + 1,
                'user_id': row['user_id'],
                'name': name,
                'balance': row['balance'],
                'total_earned': row['total_earned'],
                'total_spent': row['total_spent']
            })

        logger.info(f"🏆 Token Leaderboard: получено {len(leaderboard)} игроков")
        return leaderboard

    except Exception as e:
        logger.error(f"Ошибка get_leaderboard: {e}")
        return []
    finally:
        conn.close()


def get_boss_leaderboard(limit: int = 10) -> list:
    """
    Получает топ игроков по урону по боссу

    Args:
        limit: Количество игроков в рейтинге (по умолчанию 10)

    Returns:
        Список словарей с данными игроков
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Получаем топ игроков по урону с данными пользователя
        cursor.execute('''
            SELECT
                u.user_id,
                u.username,
                u.first_name,
                u.last_name,
                bd.total_damage,
                bd.hits,
                bd.last_hit
            FROM boss_damage bd
            JOIN users u ON bd.user_id = u.user_id
            WHERE bd.total_damage > 0
            ORDER BY bd.total_damage DESC
            LIMIT ?
        ''', (limit,))

        rows = cursor.fetchall()
        leaderboard = []

        for i, row in enumerate(rows):
            # Формируем имя: username или first_name last_name
            name = row['username'] if row['username'] else f"{row['first_name'] or ''} {row['last_name'] or ''}".strip()
            if not name:
                name = f"Игрок #{row['user_id']}"

            leaderboard.append({
                'rank': i + 1,
                'user_id': row['user_id'],
                'name': name,
                'total_damage': row['total_damage'],
                'hits': row['hits'],
                'last_hit': row['last_hit']
            })

        logger.info(f"🏆 Boss Leaderboard: получено {len(leaderboard)} игроков")
        return leaderboard

    except Exception as e:
        logger.error(f"Ошибка get_boss_leaderboard: {e}")
        return []
    finally:
        conn.close()


def get_quests_leaderboard(limit: int = 10) -> list:
    """Получает топ игроков по количеству пройденных квестов"""
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Сортировка: сначала по количеству (убывание), затем по времени (возрастание - кто быстрее тот выше)
        cursor.execute('''
            SELECT
                u.user_id,
                u.username,
                u.first_name,
                u.last_name,
                us.quests_completed,
                us.last_quest_time
            FROM user_stats us
            JOIN users u ON us.user_id = u.user_id
            WHERE us.quests_completed > 0
            ORDER BY us.quests_completed DESC, us.last_quest_time ASC
            LIMIT ?
        ''', (limit,))

        rows = cursor.fetchall()
        leaderboard = []

        for i, row in enumerate(rows):
            name = row['username'] if row['username'] else f"{row['first_name'] or ''} {row['last_name'] or ''}".strip()
            if not name:
                name = f"Игрок #{row['user_id']}"

            leaderboard.append({
                'rank': i + 1,
                'user_id': row['user_id'],
                'name': name,
                'quests_completed': row['quests_completed']
            })

        return leaderboard
    except Exception as e:
        logger.error(f"Ошибка get_quests_leaderboard: {e}")
        return []
    finally:
        conn.close()





def get_overall_leaderboard(limit: int = 10) -> list:
    """
    Получает общий рейтинг игроков по формуле:
    наличие шишек + пройденные квесты * 10 000 + каждая сыгранная игра * 100 + ставки игрока в рулетке + прочитанные газеты * 500
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute('''
            SELECT 
                u.user_id,
                u.username,
                u.first_name,
                u.last_name,
                COALESCE(ut.balance, 0) as balance,
                COALESCE(us.quests_completed, 0) as quests_completed,
                COALESCE(us.clown_games, 0) as clown_games,
                COALESCE(us.vladeos_games, 0) as vladeos_games,
                COALESCE(us.tower_total_levels, 0) as tower_total_levels,
                COALESCE(us.roulette_games, 0) as roulette_games,
                COALESCE(us.roulette_cones_lost, 0) as roulette_cones_lost,
                COALESCE(us.roulette_cones_won, 0) as roulette_cones_won,
                COALESCE(us.roulette_total_bets, 0) as roulette_total_bets,
                COALESCE(us.quests, '[]') as quests
            FROM users u
            LEFT JOIN user_stats us ON u.user_id = us.user_id
            LEFT JOIN user_tokens ut ON u.user_id = ut.user_id
        ''')
        rows = [dict(row) for row in cursor.fetchall()]
        
        all_players = []
        for r in rows:
            calc = calculate_overall_score(r)
            name = r['username'] if r['username'] else f"{r['first_name'] or ''} {r['last_name'] or ''}".strip()
            if not name:
                name = f"Игрок #{r['user_id']}"
            
            all_players.append({
                'user_id': r['user_id'],
                'name': name,
                'score': calc['total_score'],
                'balance': calc['balance'],
                'quests_completed': calc['quests_completed'],
                'total_games': calc['total_games'],
                'roulette_bets': calc['roulette_bets'],
                'newspapers_read': calc['newspapers_read']
            })

        all_players.sort(key=lambda p: (-p['score'], p['user_id']))

        filtered = [p for p in all_players if p['score'] > 0]
        if not filtered and all_players:
            filtered = all_players

        leaderboard = []
        for i, p in enumerate(filtered[:limit]):
            p_copy = dict(p)
            p_copy['rank'] = i + 1
            leaderboard.append(p_copy)

        logger.info(f"🏆 Overall Leaderboard: получено {len(leaderboard)} игроков")
        return leaderboard
    except Exception as e:
        logger.error(f"Ошибка get_overall_leaderboard: {e}")
        return []
    finally:
        conn.close()


def get_user_rank_in_leaderboard(user_id: int, lb_type: str = 'tokens') -> dict:
    """
    Получает позицию и статистику конкретного пользователя в рейтинге.
    """
    if not user_id or user_id <= 0:
        return None

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        if lb_type == 'overall':
            cursor.execute('''
                SELECT 
                    u.user_id,
                    u.username,
                    u.first_name,
                    u.last_name,
                    COALESCE(ut.balance, 0) as balance,
                    COALESCE(us.quests_completed, 0) as quests_completed,
                    COALESCE(us.clown_games, 0) as clown_games,
                    COALESCE(us.vladeos_games, 0) as vladeos_games,
                    COALESCE(us.tower_total_levels, 0) as tower_total_levels,
                    COALESCE(us.roulette_games, 0) as roulette_games,
                    COALESCE(us.roulette_cones_lost, 0) as roulette_cones_lost,
                    COALESCE(us.roulette_cones_won, 0) as roulette_cones_won,
                    COALESCE(us.roulette_total_bets, 0) as roulette_total_bets,
                    COALESCE(us.quests, '[]') as quests
                FROM users u
                LEFT JOIN user_stats us ON u.user_id = us.user_id
                LEFT JOIN user_tokens ut ON u.user_id = ut.user_id
            ''')
            rows = [dict(row) for row in cursor.fetchall()]
            all_players = []
            user_player = None
            for r in rows:
                calc = calculate_overall_score(r)
                name = r['username'] if r['username'] else f"{r['first_name'] or ''} {r['last_name'] or ''}".strip()
                if not name:
                    name = f"Игрок #{r['user_id']}"
                p_data = {
                    'user_id': r['user_id'],
                    'name': name,
                    'score': calc['total_score'],
                    'balance': calc['balance'],
                    'quests_completed': calc['quests_completed'],
                    'total_games': calc['total_games'],
                    'roulette_bets': calc['roulette_bets'],
                    'newspapers_read': calc['newspapers_read']
                }
                all_players.append(p_data)
                if r['user_id'] == user_id:
                    user_player = p_data

            if not user_player:
                return None

            all_players.sort(key=lambda p: (-p['score'], p['user_id']))
            user_rank_num = None
            for idx, p in enumerate(all_players, 1):
                if p['user_id'] == user_id:
                    user_rank_num = idx
                    break

            return {
                'rank': user_rank_num,
                'user_id': user_id,
                'name': user_player['name'],
                'score': user_player['score'],
                'balance': user_player['balance'],
                'quests_completed': user_player['quests_completed'],
                'total_games': user_player['total_games'],
                'roulette_bets': user_player['roulette_bets'],
                'newspapers_read': user_player['newspapers_read']
            }

        elif lb_type == 'quests':
            cursor.execute('''
                SELECT u.user_id, u.username, u.first_name, u.last_name, us.quests_completed, us.last_quest_time
                FROM users u
                LEFT JOIN user_stats us ON u.user_id = us.user_id
                WHERE u.user_id = ?
            ''', (user_id,))
            row = cursor.fetchone()
            if not row:
                return None

            name = row['username'] if row['username'] else f"{row['first_name'] or ''} {row['last_name'] or ''}".strip()
            if not name:
                name = f"Игрок #{row['user_id']}"

            qc = row['quests_completed'] or 0
            lqt = row['last_quest_time']

            if qc <= 0:
                return {
                    'rank': None,
                    'user_id': user_id,
                    'name': name,
                    'quests_completed': 0
                }

            cursor.execute('''
                SELECT COUNT(*) + 1
                FROM user_stats us
                JOIN users u ON us.user_id = u.user_id
                WHERE us.quests_completed > 0
                  AND (
                    (us.quests_completed > ?) OR
                    (us.quests_completed = ? AND us.last_quest_time IS NOT NULL AND ? IS NOT NULL AND us.last_quest_time < ?) OR
                    (us.quests_completed = ? AND (us.last_quest_time = ? OR (us.last_quest_time IS NULL AND ? IS NULL)) AND us.user_id < ?)
                  )
            ''', (qc, qc, lqt, lqt, qc, lqt, lqt, user_id))
            rank = cursor.fetchone()[0]

            return {
                'rank': rank,
                'user_id': user_id,
                'name': name,
                'quests_completed': qc
            }

        elif lb_type == 'boss':
            cursor.execute('''
                SELECT u.user_id, u.username, u.first_name, u.last_name, bd.total_damage, bd.hits, bd.last_hit
                FROM users u
                LEFT JOIN boss_damage bd ON u.user_id = bd.user_id
                WHERE u.user_id = ?
            ''', (user_id,))
            row = cursor.fetchone()
            if not row:
                return None

            name = row['username'] if row['username'] else f"{row['first_name'] or ''} {row['last_name'] or ''}".strip()
            if not name:
                name = f"Игрок #{row['user_id']}"

            td = row['total_damage'] or 0
            if td <= 0:
                return {
                    'rank': None,
                    'user_id': user_id,
                    'name': name,
                    'total_damage': 0,
                    'hits': 0
                }

            cursor.execute('''
                SELECT COUNT(*) + 1
                FROM boss_damage bd
                JOIN users u ON bd.user_id = u.user_id
                WHERE bd.total_damage > 0
                  AND (
                    (bd.total_damage > ?) OR
                    (bd.total_damage = ? AND bd.user_id < ?)
                  )
            ''', (td, td, user_id))
            rank = cursor.fetchone()[0]

            return {
                'rank': rank,
                'user_id': user_id,
                'name': name,
                'total_damage': td,
                'hits': row['hits'] or 0,
                'last_hit': row['last_hit']
            }

        else:  # 'tokens'
            cursor.execute('''
                SELECT u.user_id, u.username, u.first_name, u.last_name, ut.balance, ut.total_earned, ut.total_spent
                FROM users u
                LEFT JOIN user_tokens ut ON u.user_id = ut.user_id
                WHERE u.user_id = ?
            ''', (user_id,))
            row = cursor.fetchone()
            if not row:
                return None

            name = row['username'] if row['username'] else f"{row['first_name'] or ''} {row['last_name'] or ''}".strip()
            if not name:
                name = f"Игрок #{row['user_id']}"

            bal = row['balance'] or 0
            earned = row['total_earned'] or 0
            spent = row['total_spent'] or 0

            if bal <= 0 and earned <= 0:
                return {
                    'rank': None,
                    'user_id': user_id,
                    'name': name,
                    'balance': bal,
                    'total_earned': earned,
                    'total_spent': spent
                }

            cursor.execute('''
                SELECT COUNT(*) + 1
                FROM user_tokens ut
                JOIN users u ON ut.user_id = u.user_id
                WHERE (ut.balance > 0 OR ut.total_earned > 0)
                  AND (
                    (ut.balance > ?) OR
                    (ut.balance = ? AND ut.total_earned > ?) OR
                    (ut.balance = ? AND ut.total_earned = ? AND ut.user_id < ?)
                  )
            ''', (bal, bal, earned, bal, earned, user_id))
            rank = cursor.fetchone()[0]

            return {
                'rank': rank,
                'user_id': user_id,
                'name': name,
                'balance': bal,
                'total_earned': earned,
                'total_spent': spent
            }

    except Exception as e:
        logger.error(f"Ошибка get_user_rank_in_leaderboard: {e}")
        return None
    finally:
        conn.close()


def get_user_by_username(username: str) -> dict:
    """
    Ищет пользователя по username (с @ или без)
    Возвращает словарь с user_id или None
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Убираем @ если есть
        original_username = username.lstrip('@')
        username = original_username.lower()

        logger.info(f"🔍 Поиск пользователя по username: '{username}'")

        # Ищем по username
        cursor.execute('''
            SELECT user_id, username, first_name, last_name
            FROM users
            WHERE LOWER(username) = ?
        ''', (username,))
        row = cursor.fetchone()

        if row:
            logger.info(f"✅ Найден пользователь по username: {row['user_id']} ({row['username']})")
            return {
                'user_id': row['user_id'],
                'username': row['username'],
                'first_name': row['first_name'],
                'last_name': row['last_name']
            }

        # Если не найдено, пробуем найти по first_name + last_name
        logger.info(f"🔍 Не найдено по username, пробуем поиск по имени...")
        cursor.execute('''
            SELECT user_id, username, first_name, last_name
            FROM users
            WHERE LOWER(first_name) = ? OR LOWER(last_name) = ?
        ''', (username, username))
        row = cursor.fetchone()

        if row:
            logger.info(f"✅ Найден пользователь по имени: {row['user_id']} ({row['first_name']} {row['last_name']})")
            return {
                'user_id': row['user_id'],
                'username': row['username'],
                'first_name': row['first_name'],
                'last_name': row['last_name']
            }

        logger.warning(f"❌ Пользователь '{username}' не найден")
        return None

    except Exception as e:
        logger.error(f"Ошибка get_user_by_username: {e}")
        return None
    finally:
        conn.close()


def get_user_by_id_or_username(identifier: str) -> dict:
    """
    Ищет пользователя по ID или username
    Возвращает словарь с user_id и информацией о пользователе
    """
    # Пробуем как ID (число)
    try:
        user_id = int(identifier)
        # Получаем информацию о пользователе по ID
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT user_id, username, first_name, last_name 
            FROM users 
            WHERE user_id = ?
        ''', (user_id,))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return {
                'user_id': row['user_id'],
                'username': row['username'],
                'first_name': row['first_name'],
                'last_name': row['last_name']
            }
        return None
    except ValueError:
        # Это не число, ищем по username
        return get_user_by_username(identifier)


def get_user_tokens(user_id: int) -> dict:
    """
    Получает баланс шишек пользователя

    Args:
        user_id: ID пользователя в Telegram

    Returns:
        Словарь с данными о токенах
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Гарантируем существование пользователя (и создаём запись в user_tokens если нет)
        ensure_user_exists(user_id)

        cursor.execute('SELECT balance, total_earned, total_spent, last_earn FROM user_tokens WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()

        if row:
            result = {
                'balance': row['balance'],
                'total_earned': row['total_earned'],
                'total_spent': row['total_spent'],
                'last_earn': row['last_earn']
            }
            logger.debug(f"🪙 get_user_tokens: user_id={user_id}, balance={result['balance']}")
            return result

        logger.warning(f"⚠️ get_user_tokens: запись не найдена для user_id={user_id}")
        return {'balance': 0, 'total_earned': 0, 'total_spent': 0, 'last_earn': None}

    except Exception as e:
        logger.error(f"Ошибка get_user_tokens: {e}")
        return {'balance': 0, 'total_earned': 0, 'total_spent': 0, 'last_earn': None}
    finally:
        conn.close()


def is_user_banned(user_id: int) -> bool:
    """
    Проверяет, забанен ли пользователь

    Args:
        user_id: ID пользователя в Telegram

    Returns:
        True если забанен, False иначе
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT is_banned FROM users WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        return bool(row and row[0])
    except Exception as e:
        logger.error(f"Ошибка is_user_banned: {e}")
        return False
    finally:
        conn.close()


def add_tokens(user_id: int, amount: int, reason: str = '') -> dict:
    """
    Начисляет шишки пользователю

    Args:
        user_id: ID пользователя в Telegram
        amount: Количество шишек для начисления
        reason: Причина начисления (для логирования)

    Returns:
        Словарь с обновленным балансом или None при ошибке
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Гарантируем существование пользователя
        ensure_user_exists(user_id)

        cursor.execute('''
            INSERT INTO user_tokens (user_id, balance, total_earned, last_earn)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                balance = balance + excluded.balance,
                total_earned = total_earned + excluded.total_earned,
                last_earn = CURRENT_TIMESTAMP
        ''', (user_id, amount, amount))

        conn.commit()

        # Получаем обновленный баланс
        cursor.execute('SELECT balance, total_earned, total_spent FROM user_tokens WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()

        tokens_info = {
            'balance': row['balance'],
            'total_earned': row['total_earned'],
            'total_spent': row['total_spent'],
            'earned_now': amount,
            'reason': reason
        }

        logger.info(f"💰 +{amount} Шишек: user_id={user_id}, reason={reason}, balance={tokens_info['balance']}")
        return tokens_info

    except Exception as e:
        logger.error(f"❌ Ошибка add_tokens: {e}")
        conn.rollback()
        return None
    finally:
        conn.close()


def spend_tokens(user_id: int, amount: int, reason: str = '') -> dict:
    """
    Списывает шишки у пользователя

    Args:
        user_id: ID пользователя в Telegram
        amount: Количество шишек для списания
        reason: Причина списания

    Returns:
        Словарь с обновленным балансом или None при ошибке/недостатке средств
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Проверяем текущий баланс
        cursor.execute('SELECT balance FROM user_tokens WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()

        if not row or row['balance'] < amount:
            logger.warning(f"⚠️ Недостаточно шишек: user_id={user_id}, нужно={amount}, есть={row['balance'] if row else 0}")
            return None

        cursor.execute('''
            UPDATE user_tokens SET
                balance = balance - ?,
                total_spent = total_spent + ?,
                last_earn = CURRENT_TIMESTAMP
            WHERE user_id = ?
        ''', (amount, amount, user_id))

        conn.commit()

        # Получаем обновленный баланс
        cursor.execute('SELECT balance, total_earned, total_spent FROM user_tokens WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()

        tokens_info = {
            'balance': row['balance'],
            'total_earned': row['total_earned'],
            'total_spent': row['total_spent'],
            'spent_now': amount,
            'reason': reason
        }

        logger.info(f"💸 -{amount} Шишек: user_id={user_id}, reason={reason}, balance={tokens_info['balance']}")
        return tokens_info

    except Exception as e:
        logger.error(f"❌ Ошибка spend_tokens: {e}")
        conn.rollback()
        return None
    finally:
        conn.close()


def get_daily_bonus_status(user_id: int) -> dict:
    """
    Проверяет статус ежедневного бонуса (1000 шишек каждые 24 часа).
    Возвращает can_claim, time_remaining (сек), last_claim, bonus_amount (1000).
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        ensure_user_exists(user_id)
        cursor.execute("SELECT last_daily_bonus FROM user_stats WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        
        last_daily_bonus_str = row['last_daily_bonus'] if row and 'last_daily_bonus' in row.keys() and row['last_daily_bonus'] else None
        
        now = get_moscow_now()
        bonus_amount = 1000
        cooldown_seconds = 24 * 3600  # 86400 секунд (24 часа)

        if not last_daily_bonus_str:
            return {
                'can_claim': True,
                'time_remaining': 0,
                'last_claim': None,
                'bonus_amount': bonus_amount,
                'server_time': int(now.timestamp())
            }

        last_dt = parse_moscow_datetime(last_daily_bonus_str)
        if not last_dt:
            return {
                'can_claim': True,
                'time_remaining': 0,
                'last_claim': None,
                'bonus_amount': bonus_amount,
                'server_time': int(now.timestamp())
            }

        # Вычисляем разницу во времени
        elapsed = (now - last_dt).total_seconds()
        if elapsed >= cooldown_seconds:
            return {
                'can_claim': True,
                'time_remaining': 0,
                'last_claim': last_daily_bonus_str,
                'bonus_amount': bonus_amount,
                'server_time': int(now.timestamp())
            }
        else:
            remaining = int(cooldown_seconds - elapsed)
            return {
                'can_claim': False,
                'time_remaining': max(0, remaining),
                'last_claim': last_daily_bonus_str,
                'bonus_amount': bonus_amount,
                'server_time': int(now.timestamp())
            }
    except Exception as e:
        logger.error(f"❌ Ошибка get_daily_bonus_status: {e}")
        return {
            'can_claim': False,
            'time_remaining': 86400,
            'last_claim': None,
            'bonus_amount': 1000,
            'server_time': int(time.time())
        }
    finally:
        conn.close()


def update_user_activity(user_id: int):
    """Обновляет время последней активности пользователя (заход, действия в игре)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        now_moscow = get_moscow_now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("UPDATE user_stats SET last_activity = ? WHERE user_id = ?", (now_moscow, user_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Ошибка update_user_activity для {user_id}: {e}")


def send_boar_notification(user_id: int):
    """Отправляет уведомление в Telegram о вторжении кабана в дупло."""
    if not BOT_TOKEN:
        return
    try:
        text = (
            "🐗 <b>ТРЕВОГА В ДУПЛЕ!</b>\n\n"
            "Вы не заходили в игру больше недели, и в ваше уютное Дупло забрался <b>Дикий Кабан</b>!\n\n"
            "🔥 <b>Он поедает по 100 ваших шишек каждый час!</b>\n\n"
            "🥾 Скорее откройте приложение, зайдите в Дупло и прогоните наглеца!"
        )
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": user_id,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=5)
        if resp.status_code == 200:
            logger.info(f"🐗 Уведомление о кабане успешно отправлено игроку {user_id}")
        else:
            logger.warning(f"Не удалось отправить уведомление о кабане {user_id}: {resp.text}")
    except Exception as e:
        logger.error(f"Ошибка send_boar_notification для {user_id}: {e}")


def process_boar_for_user(user_id: int) -> dict:
    """
    Проверяет статус кабана для пользователя:
    - Отсчет с последнего дня захода игрока (last_activity).
    - Если прошло >= 7 дней: кабан приходит в дупло.
    - Сжигает по 100 фишек/шишек за каждый прошедший час.
    - Отправляет уведомление в Telegram.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        ensure_user_exists(user_id)
        cursor.execute("""
            SELECT last_activity, last_daily_bonus, boar_status, boar_arrived_at, 
                   boar_last_burn, boar_eaten_total, boar_notified 
            FROM user_stats 
            WHERE user_id = ?
        """, (user_id,))
        row = cursor.fetchone()
        if not row:
            return {'boar_active': False, 'boar_eaten_total': 0, 'boar_rate': 100, 'days_inactive': 0}

        now = get_moscow_now()
        last_act_str = row['last_activity'] or row['last_daily_bonus']
        last_act_dt = parse_moscow_datetime(last_act_str) if last_act_str else now
        if not last_act_dt:
            last_act_dt = now

        inactive_seconds = max(0, (now - last_act_dt).total_seconds())
        days_inactive = inactive_seconds / 86400.0

        boar_status = row['boar_status'] or 0
        boar_arrived_str = row['boar_arrived_at']
        boar_last_burn_str = row['boar_last_burn']
        boar_eaten_total = row['boar_eaten_total'] or 0
        boar_notified = row['boar_notified'] or 0

        BOAR_TRIGGER_SECONDS = 7 * 86400  # 7 дней
        BOAR_BURN_RATE = 100              # 100 шишек в час

        if inactive_seconds >= BOAR_TRIGGER_SECONDS:
            if boar_status == 0:
                boar_status = 1
                arrived_dt = last_act_dt + timedelta(days=7)
                boar_arrived_str = arrived_dt.strftime("%Y-%m-%d %H:%M:%S")
                boar_last_burn_str = boar_arrived_str
                boar_eaten_total = 0
                boar_notified = 0

            # Расчет сжигания шишек по 100 в час
            burn_dt = parse_moscow_datetime(boar_last_burn_str) if boar_last_burn_str else (last_act_dt + timedelta(days=7))
            if not burn_dt:
                burn_dt = now
            
            hours_passed = int((now - burn_dt).total_seconds() // 3600)
            if hours_passed > 0:
                needed_burn = hours_passed * BOAR_BURN_RATE
                tokens_info = get_user_tokens(user_id)
                cur_bal = tokens_info['balance']
                actual_burn = min(cur_bal, needed_burn)
                if actual_burn > 0:
                    spend_tokens(user_id, actual_burn, 'boar_eat_cones')
                
                boar_eaten_total += actual_burn
                new_burn_dt = burn_dt + timedelta(hours=hours_passed)
                boar_last_burn_str = new_burn_dt.strftime("%Y-%m-%d %H:%M:%S")

            cursor.execute("""
                UPDATE user_stats 
                SET boar_status = 1, boar_arrived_at = ?, boar_last_burn = ?, 
                    boar_eaten_total = ?, boar_notified = ?
                WHERE user_id = ?
            """, (boar_arrived_str, boar_last_burn_str, boar_eaten_total, boar_notified, user_id))
            conn.commit()

            # Отправка Telegram уведомления если еще не отправляли
            if boar_notified == 0:
                try:
                    send_boar_notification(user_id)
                    cursor.execute("UPDATE user_stats SET boar_notified = 1 WHERE user_id = ?", (user_id,))
                    conn.commit()
                    boar_notified = 1
                except Exception as e:
                    logger.error(f"Ошибка отправки уведомления кабана: {e}")

            return {
                'boar_active': True,
                'boar_arrived_at': boar_arrived_str,
                'boar_eaten_total': boar_eaten_total,
                'boar_rate': BOAR_BURN_RATE,
                'days_inactive': round(days_inactive, 1)
            }
        else:
            if boar_status == 1:
                cursor.execute("""
                    UPDATE user_stats 
                    SET boar_status = 0, boar_arrived_at = NULL, boar_last_burn = NULL, 
                        boar_notified = 0, boar_eaten_total = 0 
                    WHERE user_id = ?
                """, (user_id,))
                conn.commit()

            return {
                'boar_active': False,
                'boar_eaten_total': 0,
                'boar_rate': BOAR_BURN_RATE,
                'days_inactive': round(days_inactive, 1)
            }
    except Exception as e:
        logger.error(f"❌ Ошибка process_boar_for_user({user_id}): {e}")
        return {
            'boar_active': False,
            'boar_eaten_total': 0,
            'boar_rate': 100,
            'days_inactive': 0
        }
    finally:
        conn.close()


def chase_boar_from_hollow(user_id: int) -> dict:
    """Прогоняет кабана из дупла и обновляет время активности игрока."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        ensure_user_exists(user_id)
        cursor.execute("SELECT boar_status, boar_eaten_total FROM user_stats WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        was_boar = bool(row and row['boar_status'] == 1)
        eaten = row['boar_eaten_total'] if row and row['boar_eaten_total'] else 0

        now_moscow = get_moscow_now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("""
            UPDATE user_stats 
            SET boar_status = 0, boar_arrived_at = NULL, boar_last_burn = NULL, 
                boar_notified = 0, boar_eaten_total = 0, last_activity = ?
            WHERE user_id = ?
        """, (now_moscow, user_id))
        conn.commit()

        logger.info(f"🥾 Игрок {user_id} прогнал кабана из дупла! (Было съедено: {eaten} шишек)")

        hollow_st = get_hollow_status(user_id)
        tokens = get_user_tokens(user_id)

        return {
            'status': 'ok',
            'was_boar': was_boar,
            'eaten_total': eaten,
            'message': '🥾 Вы с криками прогнали кабана из дупла! Ваши запасы снова в безопасности.',
            'hollow': hollow_st,
            'balance': tokens['balance']
        }
    except Exception as e:
        logger.error(f"❌ Ошибка chase_boar_from_hollow: {e}")
        return {'status': 'error', 'message': str(e)}
    finally:
        conn.close()


def get_hollow_status(user_id: int) -> dict:
    """
    Получает актуальное состояние Дупла (уровень 1..10, урожай, стоимость прокачки, таймер, кабан).
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        ensure_user_exists(user_id)
        
        # Обрабатываем статус кабана
        boar_info = process_boar_for_user(user_id)

        cursor.execute("SELECT COALESCE(hollow_level, 1) as hollow_level, last_daily_bonus FROM user_stats WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        level = max(1, min(10, row['hollow_level'] if row and row['hollow_level'] else 1))
        last_claim_str = row['last_daily_bonus'] if row and 'last_daily_bonus' in row.keys() and row['last_daily_bonus'] else None

        now = get_moscow_now()
        cooldown_seconds = 24 * 3600
        daily_yield = 1000 * (2 ** (level - 1))
        next_yield = 1000 * (2 ** level) if level < 10 else None
        upgrade_cost_cones = 100000 * (2 ** (level - 1)) if level < 10 else None
        upgrade_cost_gram = 2 * (2 ** (level - 1)) if level < 10 else None

        can_claim = True
        time_remaining = 0

        if last_claim_str:
            last_dt = parse_moscow_datetime(last_claim_str)
            if last_dt:
                elapsed = (now - last_dt).total_seconds()
                if elapsed < cooldown_seconds:
                    can_claim = False
                    time_remaining = int(cooldown_seconds - elapsed)

        return {
            'status': 'ok',
            'level': level,
            'max_level': 10,
            'daily_yield': daily_yield,
            'next_yield': next_yield,
            'upgrade_cost_cones': upgrade_cost_cones,
            'upgrade_cost_gram': upgrade_cost_gram,
            'can_claim': can_claim,
            'time_remaining': max(0, time_remaining),
            'last_claim': last_claim_str,
            'starter_offer_available': (level == 1),
            'starter_offer_ton': 10.0,
            'starter_offer_target_level': 4,
            'boar_active': boar_info.get('boar_active', False),
            'boar_eaten_total': boar_info.get('boar_eaten_total', 0),
            'boar_rate': boar_info.get('boar_rate', 100),
            'days_inactive': boar_info.get('days_inactive', 0),
            'server_time': int(now.timestamp())
        }
    except Exception as e:
        logger.error(f"❌ Ошибка get_hollow_status: {e}")
        return {
            'status': 'error',
            'level': 1,
            'max_level': 10,
            'daily_yield': 1000,
            'next_yield': 2000,
            'upgrade_cost_cones': 100000,
            'upgrade_cost_gram': 2,
            'can_claim': True,
            'time_remaining': 0,
            'last_claim': None,
            'starter_offer_available': True,
            'starter_offer_ton': 10.0,
            'starter_offer_target_level': 4,
            'boar_active': False,
            'boar_eaten_total': 0,
            'boar_rate': 100,
            'days_inactive': 0,
            'server_time': int(time.time())
        }
    finally:
        conn.close()


def claim_daily_bonus(user_id: int) -> dict:
    """
    Забирает урожай Дупла (1000 * 2^(level-1) шишек), если 24 часа прошло.
    """
    hollow_st = get_hollow_status(user_id)
    if hollow_st.get('boar_active'):
        return {
            'status': 'error',
            'message': '🐗 В дупле сидит дикий кабан! Сначала прогоните кабана, чтобы собрать урожай.',
            'boar_active': True,
            'time_remaining': hollow_st.get('time_remaining', 86400),
            'bonus_amount': hollow_st.get('daily_yield', 1000)
        }

    if not hollow_st.get('can_claim'):
        return {
            'status': 'error',
            'message': 'Урожай пока не созрел',
            'time_remaining': hollow_st.get('time_remaining', 86400),
            'bonus_amount': hollow_st.get('daily_yield', 1000)
        }

    level = hollow_st.get('level', 1)
    daily_yield = 1000 * (2 ** (level - 1))

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        now_moscow = get_moscow_now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("UPDATE user_stats SET last_daily_bonus = ?, last_activity = ?, raccoon_taps = 0 WHERE user_id = ?", (now_moscow, now_moscow, user_id))
        conn.commit()

        # Начисляем урожай шишек пользователю
        add_tokens(user_id, daily_yield, f'hollow_harvest_lvl{level}')
        tokens = get_user_tokens(user_id)

        logger.info(f"🌳 Игрок {user_id} собрал урожай Дупла: +{daily_yield:,} шишек (Ур. {level})! Новый баланс: {tokens['balance']}")

        return {
            'status': 'ok',
            'claimed': daily_yield,
            'level': level,
            'new_balance': tokens['balance'],
            'time_remaining': 86400,
            'last_claim': now_moscow,
            'message': f'Вам начислено +{daily_yield:,} шишек!'
        }
    except Exception as e:
        logger.error(f"❌ Ошибка claim_daily_bonus: {e}")
        return {
            'status': 'error',
            'message': str(e)
        }
    finally:
        conn.close()


def upgrade_hollow_cones(user_id: int) -> dict:
    """
    Прокачка Дупла за шишки (100,000 * 2^(level-1))
    """
    hollow_st = get_hollow_status(user_id)
    level = hollow_st.get('level', 1)
    if level >= 10:
        return {'status': 'error', 'message': 'Дупло уже максимального 10-го уровня!'}

    cost = 100000 * (2 ** (level - 1))
    tokens = get_user_tokens(user_id)
    if tokens['balance'] < cost:
        return {
            'status': 'error',
            'message': f'Недостаточно шишек! Нужно {cost:,}, у вас {tokens["balance"]:,}'
        }

    # Списываем шишки
    spend_result = spend_tokens(user_id, cost, f'hollow_upgrade_lvl{level}_to_{level+1}')
    if not spend_result:
        return {'status': 'error', 'message': 'Ошибка списания шишек'}

    new_level = level + 1
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE user_stats SET hollow_level = ? WHERE user_id = ?", (new_level, user_id))
        conn.commit()
        logger.info(f"🌳 Игрок {user_id} прокачал Дупло за {cost:,} шишек до уровня {new_level}!")
    except Exception as e:
        logger.error(f"Ошибка сохранения hollow_level: {e}")
    finally:
        conn.close()

    new_tokens = get_user_tokens(user_id)
    new_hollow_st = get_hollow_status(user_id)
    new_yield = 1000 * (2 ** (new_level - 1))

    # Отправляем уведомление администратору
    try:
        user_name = f"Игрок #{user_id}"
        conn_u = get_db_connection()
        try:
            cursor_u = conn_u.cursor()
            cursor_u.execute("SELECT username, first_name, last_name FROM users WHERE user_id = ?", (user_id,))
            u_row = cursor_u.fetchone()
            if u_row:
                user_name = f"@{u_row['username']}" if u_row['username'] else (f"{u_row['first_name'] or ''} {u_row['last_name'] or ''}".strip() or f"Игрок #{user_id}")
        finally:
            conn_u.close()

        admin_text = (
            f"🌳 <b>ПРОКАЧКА ДУПЛА ЗА ШИШКИ!</b>\n\n"
            f"👤 <b>Игрок:</b> {html.escape(user_name)}\n"
            f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
            f"🌲 <b>Потрачено шишек:</b> {cost:,} 🌰\n"
            f"🏆 <b>Новый уровень Дупла:</b> {new_level} / 10\n"
            f"🌰 <b>Добыча в сутки:</b> +{new_yield:,} Шишек/24ч\n"
            f"💳 <b>Остаток баланса:</b> {new_tokens['balance']:,} Шишек"
        )
        notify_admin(admin_text)
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления админу о прокачке Дупла за шишки: {e}")

    return {
        'status': 'ok',
        'new_level': new_level,
        'new_balance': new_tokens['balance'],
        'hollow': new_hollow_st,
        'message': f'🎉 Дупло успешно прокачано до уровня {new_level}!\nДобыча: {new_yield:,} шишек/сутки'
    }





# ==================== INVENTORY & ITEMS SYSTEM ====================
ITEMS_REGISTRY = {
    'mega_cone': {
        'id': 'mega_cone',
        'name_ru': 'Мегашишка',
        'name_en': 'Mega Cone',
        'desc_ru': 'Легендарный артефакт! Мгновенно повышает уровень Дупла на +1 (до 10-го ур.).',
        'desc_en': 'Legendary artifact! Instantly raises your Hollow level by +1 (up to lvl 10).',
        'icon': 'mega_cone.png',
        'usable': True,
        'rarity': 'legendary'
    },
    'energy_drink': {
        'id': 'energy_drink',
        'name_ru': 'Энергетик «Бодрый Енот»',
        'name_en': 'Raccoon Energy Drink',
        'desc_ru': 'Бодрящий напиток! Мгновенно восстанавливает 100% здоровья (HP) во всех играх и дает +500 шишек.',
        'desc_en': 'Energizing drink! Fully restores 100% health (HP) in all games and grants +500 cones.',
        'icon': 'item_energy_drink.svg',
        'usable': True,
        'rarity': 'rare'
    },
    'golden_cookie': {
        'id': 'golden_cookie',
        'name_ru': 'Золотое Печенье',
        'name_en': 'Golden Cookie',
        'desc_ru': 'Хрустящее печенье с золотой крошкой! При использовании дает +3,000 шишек.',
        'desc_en': 'Crispy cookie with gold sprinkles! Grants +3,000 cones upon use.',
        'icon': 'item_golden_cookie.svg',
        'usable': True,
        'rarity': 'epic'
    },
    'trash_shield': {
        'id': 'trash_shield',
        'name_ru': 'Мусорный Щит',
        'name_en': 'Trash Lid Shield',
        'desc_ru': 'Непробиваемая крышка от бака! При активации приносит ценный лут на +5,000 шишек.',
        'desc_en': 'Impenetrable garbage lid! Grants valuable loot worth +5,000 cones upon use.',
        'icon': 'item_trash_shield.svg',
        'usable': True,
        'rarity': 'epic'
    },
    'lucky_clover': {
        'id': 'lucky_clover',
        'name_ru': 'Счастливая Фишка',
        'name_en': 'Lucky Clover Chip',
        'desc_ru': 'Талисман удачи Енотов! При использовании дарит случайный куш от 2,000 до 7,777 шишек.',
        'desc_en': 'Lucky raccoon charm! Grants a random jackpot from 2,000 to 7,777 cones.',
        'icon': 'item_lucky_clover.svg',
        'usable': True,
        'rarity': 'rare'
    },
    'ancient_key': {
        'id': 'ancient_key',
        'name_ru': 'Ключ от Сейфа',
        'name_en': 'Raccoon Vault Key',
        'desc_ru': 'Древний золотой ключ от тайного сейфа! При открытии дает джекпот +10,000 шишек!',
        'desc_en': 'Ancient golden key to the secret vault! Unlocks a massive stash of +10,000 cones!',
        'icon': 'item_ancient_key.svg',
        'usable': True,
        'rarity': 'legendary'
    }
}


def check_minigame_loot_drop(user_id: int, game_name: str, base_chance: float = 0.10):
    """
    Проверяет шанс выпадения редкого предмета в мини-играх.
    1% шанс на Мегашишку во всех играх, плюс шанс base_chance на остальные предметы.
    """
    chosen_item_id = None

    # Ровно 1% шанс на выпадение Мегашишки во всех играх
    if random.random() < 0.01:
        chosen_item_id = 'mega_cone'
    elif random.random() <= base_chance:
        # Пул остальных трофеев
        loot_pool = [
            ('energy_drink', 36),
            ('lucky_clover', 26),
            ('golden_cookie', 20),
            ('trash_shield', 12),
            ('ancient_key', 6)
        ]
        items, weights = zip(*loot_pool)
        chosen_item_id = random.choices(items, weights=weights, k=1)[0]
    
    if not chosen_item_id:
        return None
    
    add_inventory_item(user_id, chosen_item_id, 1)
    meta = ITEMS_REGISTRY.get(chosen_item_id, {})
    
    logger.info(f"🎁 Лут-дроп! Игрок {user_id} в игре {game_name} выбил предмет: {chosen_item_id}")
    
    return {
        'id': chosen_item_id,
        'name_ru': meta.get('name_ru', chosen_item_id),
        'name_en': meta.get('name_en', chosen_item_id),
        'desc_ru': meta.get('desc_ru', ''),
        'desc_en': meta.get('desc_en', ''),
        'icon': meta.get('icon', 'cone.png'),
        'rarity': meta.get('rarity', 'rare')
    }


def get_user_inventory(user_id: int) -> list:
    """Получить инвентарь пользователя"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        ensure_user_exists(user_id)
        cursor.execute("SELECT item_id, quantity, updated_at FROM user_inventory WHERE user_id = ? AND quantity > 0", (user_id,))
        rows = cursor.fetchall()
        inventory = []
        for r in rows:
            item_id = r['item_id']
            meta = ITEMS_REGISTRY.get(item_id, {
                'id': item_id,
                'name_ru': item_id,
                'name_en': item_id,
                'desc_ru': 'Предмет инвентаря',
                'desc_en': 'Inventory item',
                'icon': 'cone.png',
                'usable': True,
                'rarity': 'rare'
            })
            inventory.append({
                'item_id': item_id,
                'quantity': r['quantity'],
                'name_ru': meta.get('name_ru'),
                'name_en': meta.get('name_en'),
                'desc_ru': meta.get('desc_ru'),
                'desc_en': meta.get('desc_en'),
                'icon': meta.get('icon'),
                'usable': meta.get('usable', True),
                'rarity': meta.get('rarity', 'common'),
                'updated_at': str(r['updated_at']) if r['updated_at'] else None
            })
        return inventory
    except Exception as e:
        logger.error(f"❌ Ошибка get_user_inventory: {e}")
        return []
    finally:
        conn.close()


def add_inventory_item(user_id: int, item_id: str, quantity: int = 1) -> dict:
    """Добавить предмет в инвентарь пользователя"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        ensure_user_exists(user_id)
        cursor.execute('''
            INSERT INTO user_inventory (user_id, item_id, quantity, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, item_id) DO UPDATE SET 
                quantity = quantity + excluded.quantity,
                updated_at = CURRENT_TIMESTAMP
        ''', (user_id, item_id, quantity))
        conn.commit()
        logger.info(f"🎒 Добавлен предмет {item_id} (x{quantity}) пользователю {user_id}")
        return {'status': 'ok', 'user_id': user_id, 'item_id': item_id, 'quantity': quantity}
    except Exception as e:
        logger.error(f"❌ Ошибка add_inventory_item: {e}")
        conn.rollback()
        return {'status': 'error', 'message': str(e)}
    finally:
        conn.close()


def use_inventory_item(user_id: int, item_id: str) -> dict:
    """Использовать предмет из инвентаря"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        ensure_user_exists(user_id)
        cursor.execute("SELECT quantity FROM user_inventory WHERE user_id = ? AND item_id = ?", (user_id, item_id))
        row = cursor.fetchone()
        if not row or row['quantity'] <= 0:
            return {'status': 'error', 'message': 'У вас нет этого предмета в инвентаре!'}

        if item_id == 'mega_cone':
            # Проверяем уровень Дупла
            hollow_st = get_hollow_status(user_id)
            current_lvl = hollow_st.get('level', 1)
            if current_lvl >= 10:
                return {'status': 'error', 'message': 'Ваше Дупло уже достигло максимального 10-го уровня!'}

            # Списываем 1 предмет
            new_qty = row['quantity'] - 1
            if new_qty <= 0:
                cursor.execute("DELETE FROM user_inventory WHERE user_id = ? AND item_id = ?", (user_id, item_id))
            else:
                cursor.execute("UPDATE user_inventory SET quantity = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND item_id = ?", (new_qty, user_id, item_id))

            new_level = current_lvl + 1
            cursor.execute("UPDATE user_stats SET hollow_level = ? WHERE user_id = ?", (new_level, user_id))
            conn.commit()

            new_hollow = get_hollow_status(user_id)
            new_yield = 1000 * (2 ** (new_level - 1))
            logger.info(f"🌟 Игрок {user_id} использовал Мегашишку! Дупло повышено до {new_level} уровня.")

            # Уведомление админу
            try:
                user_name = f"Игрок #{user_id}"
                cursor.execute("SELECT username, first_name, last_name FROM users WHERE user_id = ?", (user_id,))
                u_row = cursor.fetchone()
                if u_row:
                    user_name = f"@{u_row['username']}" if u_row['username'] else (f"{u_row['first_name'] or ''} {u_row['last_name'] or ''}".strip() or f"Игрок #{user_id}")
                admin_text = (
                    f"🌟 <b>АКТИВИРОВАНА МЕГАШИШКА!</b>\n\n"
                    f"👤 <b>Игрок:</b> {html.escape(user_name)}\n"
                    f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
                    f"🏆 <b>Новый уровень Дупла:</b> {new_level} / 10\n"
                    f"🌰 <b>Добыча в сутки:</b> +{new_yield:,} Шишек/24ч\n"
                    f"🎒 <b>Осталось Мегашишек:</b> {new_qty}"
                )
                notify_admin(admin_text)
            except Exception as e_adm:
                logger.error(f"Ошибка notify_admin при использовании Мегашишки: {e_adm}")

            return {
                'status': 'ok',
                'message': f'🎉 Мегашишка успешно активирована!\nУровень Дупла повышен до {new_level} (+{new_yield:,} шишек/сутки)!',
                'item_id': item_id,
                'new_level': new_level,
                'remaining_quantity': new_qty,
                'hollow': new_hollow,
                'inventory': get_user_inventory(user_id)
            }

        # Списываем 1 предмет
        new_qty = row['quantity'] - 1
        if new_qty <= 0:
            cursor.execute("DELETE FROM user_inventory WHERE user_id = ? AND item_id = ?", (user_id, item_id))
        else:
            cursor.execute("UPDATE user_inventory SET quantity = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND item_id = ?", (new_qty, user_id, item_id))
        conn.commit()

        if item_id == 'energy_drink':
            reward = 500
            # Полное восстановление 100% здоровья (HP) и энергии (NRG) во всех играх
            for g_type in ['tower', 'clown']:
                sess = get_game_session(user_id, g_type)
                if sess:
                    sess['pHP'] = 100
                    sess['pNRG'] = 100
                    save_game_session(user_id, g_type, sess)

            add_tokens(user_id, reward, 'item_energy_drink')
            logger.info(f"⚡ Игрок {user_id} выпил Энергетик (+{reward} шишек, 100% HP восстановлено)")
            return {
                'status': 'ok',
                'message': '⚡ Энергетик выпит! Здоровье (HP) полностью восстановлено на 100% (+500 шишек)!',
                'item_id': item_id,
                'remaining_quantity': new_qty,
                'reward': reward,
                'hp_restored': 100,
                'inventory': get_user_inventory(user_id)
            }

        elif item_id == 'golden_cookie':
            reward = 3000
            conn.commit()
            add_tokens(user_id, reward, 'item_golden_cookie')
            logger.info(f"🍪 Игрок {user_id} съел Золотое Печенье (+{reward} шишек)")
            return {
                'status': 'ok',
                'message': f'🍪 Золотое Печенье съедено! Начислено +{reward:,} шишек!',
                'item_id': item_id,
                'remaining_quantity': new_qty,
                'reward': reward,
                'inventory': get_user_inventory(user_id)
            }

        elif item_id == 'trash_shield':
            reward = 5000
            conn.commit()
            add_tokens(user_id, reward, 'item_trash_shield')
            logger.info(f"🛡️ Игрок {user_id} активировал Мусорный Щит (+{reward} шишек)")
            return {
                'status': 'ok',
                'message': f'🛡️ Мусорный Щит активирован! Получена ценная добыча +{reward:,} шишек!',
                'item_id': item_id,
                'remaining_quantity': new_qty,
                'reward': reward,
                'inventory': get_user_inventory(user_id)
            }

        elif item_id == 'lucky_clover':
            reward = random.choice([2222, 3333, 4444, 5555, 7777])
            conn.commit()
            add_tokens(user_id, reward, 'item_lucky_clover')
            logger.info(f"🍀 Игрок {user_id} активировал Счастливую Фишку (+{reward} шишек)")
            return {
                'status': 'ok',
                'message': f'🍀 Удача Енота сработала! Вы сорвали куш +{reward:,} шишек!',
                'item_id': item_id,
                'remaining_quantity': new_qty,
                'reward': reward,
                'inventory': get_user_inventory(user_id)
            }

        elif item_id == 'ancient_key':
            reward = 10000
            conn.commit()
            add_tokens(user_id, reward, 'item_ancient_key')
            logger.info(f"🗝️ Игрок {user_id} открыл Сейф Енотов (+{reward} шишек)")
            return {
                'status': 'ok',
                'message': f'🗝️ Тайный Сейф Енотов взломан! Вы получили джекпот +{reward:,} шишек!',
                'item_id': item_id,
                'remaining_quantity': new_qty,
                'reward': reward,
                'inventory': get_user_inventory(user_id)
            }

        else:
            conn.commit()
            return {'status': 'error', 'message': 'Этот предмет нельзя использовать прямо сейчас'}
    except Exception as e:
        logger.error(f"❌ Ошибка use_inventory_item: {e}")
        conn.rollback()
        return {'status': 'error', 'message': f'Внутренняя ошибка: {e}'}
    finally:
        conn.close()


def set_user_wallet_address(user_id: int, wallet_address: str = None) -> bool:
    """
    Привязывает или отвязывает TON кошелек пользователя
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            INSERT INTO users (user_id, wallet_address)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET wallet_address = excluded.wallet_address
        ''', (user_id, wallet_address))
        conn.commit()
        logger.info(f"💎 Обновлен TON кошелек для user_id={user_id}: {wallet_address or 'отвязан'}")
        return True
    except Exception as e:
        logger.error(f"Ошибка set_user_wallet_address: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def get_user_wallet_address(user_id: int) -> str:
    """
    Получает привязанный TON кошелек пользователя
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT wallet_address FROM users WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        return row['wallet_address'] if row and row['wallet_address'] else None
    except Exception as e:
        logger.error(f"Ошибка get_user_wallet_address: {e}")
        return None
    finally:
        conn.close()


def register_referral(user_id: int, referrer_id: int) -> bool:
    """
    Регистрирует реферальную связь: кто кого пригласил.
    user_id - новый/приглашенный игрок.
    referrer_id - пригласивший игрок.
    """
    if not user_id or not referrer_id:
        return False
    try:
        user_id = int(user_id)
        referrer_id = int(referrer_id)
    except (ValueError, TypeError):
        return False

    if user_id == referrer_id:
        return False  # Нельзя пригласить самого себя

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Гарантируем, что оба пользователя зарегистрированы
        ensure_user_exists(user_id)
        ensure_user_exists(referrer_id)

        # Проверяем, есть ли уже реферер у пользователя
        cursor.execute("SELECT referrer_id FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row and row['referrer_id']:
            # Уже привязан реферер
            return False

        # Записываем реферера в users и в таблицу referrals
        cursor.execute("UPDATE users SET referrer_id = ? WHERE user_id = ? AND (referrer_id IS NULL OR referrer_id = 0)", (referrer_id, user_id))
        cursor.execute("INSERT OR IGNORE INTO referrals (user_id, referrer_id) VALUES (?, ?)", (user_id, referrer_id))
        conn.commit()

        logger.info(f"👥 Реферал зарегистрирован: пользователь {user_id} приглашён игроком {referrer_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка register_referral: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def get_user_referral_stats(user_id: int) -> dict:
    """
    Возвращает статистику приглашений:
    - referrals_count: сколько человек пригласил user_id
    - referrer_id: кто пригласил user_id (или None)
    - referrer_username: имя/юзернейм пригласившего
    - referrals_list: список приглашенных пользователей
    """
    if not user_id:
        return {'referrals_count': 0, 'referrer_id': None, 'referrer_username': None, 'referrals_list': []}
    try:
        user_id = int(user_id)
    except (ValueError, TypeError):
        return {'referrals_count': 0, 'referrer_id': None, 'referrer_username': None, 'referrals_list': []}

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # 1. Сколько человек пригласил
        cursor.execute("SELECT COUNT(*) as count FROM users WHERE referrer_id = ?", (user_id,))
        count_row = cursor.fetchone()
        referrals_count = count_row['count'] if count_row else 0

        # 2. Кто пригласил этого пользователя
        cursor.execute("SELECT referrer_id FROM users WHERE user_id = ?", (user_id,))
        ref_row = cursor.fetchone()
        referrer_id = ref_row['referrer_id'] if ref_row and ref_row['referrer_id'] else None

        referrer_username = None
        if referrer_id:
            cursor.execute("SELECT username, first_name FROM users WHERE user_id = ?", (referrer_id,))
            inviter_row = cursor.fetchone()
            if inviter_row:
                referrer_username = inviter_row['username'] or inviter_row['first_name'] or str(referrer_id)

        # 3. Список рефералов (до 50 последних)
        cursor.execute("""
            SELECT user_id, username, first_name, registered_at 
            FROM users 
            WHERE referrer_id = ? 
            ORDER BY registered_at DESC LIMIT 50
        """, (user_id,))
        referrals_list = [dict(r) for r in cursor.fetchall()]

        return {
            'referrals_count': referrals_count,
            'referrer_id': referrer_id,
            'referrer_username': referrer_username,
            'referrals_list': referrals_list
        }
    except Exception as e:
        logger.error(f"❌ Ошибка get_user_referral_stats: {e}")
        return {
            'referrals_count': 0,
            'referrer_id': None,
            'referrer_username': None,
            'referrals_list': []
        }
    finally:
        conn.close()


MAX_ENERGY = 30
ENERGY_REGEN_SECONDS = 300  # 5 минут = 300 секунд

def parse_db_timestamp(ts_str):
    """Парсинг временной метки из БД"""
    if not ts_str:
        return datetime.now(timezone.utc)
    if isinstance(ts_str, datetime):
        return ts_str if ts_str.tzinfo else ts_str.replace(tzinfo=timezone.utc)
    ts_str = str(ts_str).replace('T', ' ')
    try:
        dt = datetime.strptime(ts_str.split('.')[0], "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def get_user_energy(user_id: int) -> dict:
    """
    Получает актуальное количество энергии пользователя с серверным расчетом восстановления (1 ед. каждые 5 мин)
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        ensure_user_exists(user_id)
        cursor.execute('SELECT energy, energy_last_updated FROM user_stats WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()

        now = datetime.now(timezone.utc)

        if not row or row['energy'] is None:
            cursor.execute('''
                INSERT INTO user_stats (user_id, energy, energy_last_updated)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET 
                    energy = COALESCE(user_stats.energy, excluded.energy),
                    energy_last_updated = COALESCE(user_stats.energy_last_updated, excluded.energy_last_updated)
            ''', (user_id, MAX_ENERGY))
            conn.commit()
            return {
                'energy': MAX_ENERGY,
                'max_energy': MAX_ENERGY,
                'seconds_to_next': 0
            }

        current_energy = row['energy'] if row['energy'] is not None else MAX_ENERGY
        last_updated = parse_db_timestamp(row['energy_last_updated'])
        elapsed_seconds = int((now - last_updated).total_seconds())

        if current_energy < MAX_ENERGY and elapsed_seconds > 0:
            recovered = elapsed_seconds // ENERGY_REGEN_SECONDS
            if recovered > 0:
                new_energy = min(MAX_ENERGY, current_energy + recovered)
                if new_energy >= MAX_ENERGY:
                    cursor.execute('''
                        UPDATE user_stats SET energy = ?, energy_last_updated = CURRENT_TIMESTAMP WHERE user_id = ?
                    ''', (MAX_ENERGY, user_id))
                    conn.commit()
                    return {
                        'energy': MAX_ENERGY,
                        'max_energy': MAX_ENERGY,
                        'seconds_to_next': 0
                    }
                else:
                    new_last_updated_dt = last_updated.timestamp() + (recovered * ENERGY_REGEN_SECONDS)
                    new_last_updated_str = datetime.fromtimestamp(new_last_updated_dt, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    cursor.execute('''
                        UPDATE user_stats SET energy = ?, energy_last_updated = ? WHERE user_id = ?
                    ''', (new_energy, new_last_updated_str, user_id))
                    conn.commit()
                    current_energy = new_energy
                    last_updated = datetime.fromtimestamp(new_last_updated_dt, tz=timezone.utc)
                    elapsed_seconds = int((now - last_updated).total_seconds())

        seconds_to_next = 0
        if current_energy < MAX_ENERGY:
            seconds_to_next = max(0, ENERGY_REGEN_SECONDS - (elapsed_seconds % ENERGY_REGEN_SECONDS))

        return {
            'energy': current_energy,
            'max_energy': MAX_ENERGY,
            'seconds_to_next': seconds_to_next
        }
    except Exception as e:
        logger.error(f"Ошибка get_user_energy: {e}")
        return {'energy': MAX_ENERGY, 'max_energy': MAX_ENERGY, 'seconds_to_next': 0}
    finally:
        conn.close()


def consume_user_energy(user_id: int, amount: int = 1, game: str = '') -> dict:
    """
    Списывает энергию на сервере
    """
    state = get_user_energy(user_id)
    if state['energy'] < amount:
        return {
            'success': False,
            'energy': state['energy'],
            'max_energy': MAX_ENERGY,
            'seconds_to_next': state['seconds_to_next'],
            'message': 'Недостаточно энергии'
        }

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        new_energy = state['energy'] - amount
        if state['energy'] == MAX_ENERGY:
            cursor.execute('''
                UPDATE user_stats SET energy = ?, energy_last_updated = CURRENT_TIMESTAMP WHERE user_id = ?
            ''', (new_energy, user_id))
        else:
            cursor.execute('''
                UPDATE user_stats SET energy = ? WHERE user_id = ?
            ''', (new_energy, user_id))
        conn.commit()
        logger.info(f"⚡ Сервер: списана энергия (-{amount}) для user_id={user_id} (игра: {game}), осталось={new_energy}")

        return {
            'success': True,
            'energy': new_energy,
            'max_energy': MAX_ENERGY,
            'seconds_to_next': ENERGY_REGEN_SECONDS if state['energy'] == MAX_ENERGY else state['seconds_to_next']
        }
    except Exception as e:
        logger.error(f"Ошибка consume_user_energy: {e}")
        conn.rollback()
        return {'success': False, 'energy': state['energy'], 'max_energy': MAX_ENERGY, 'seconds_to_next': state['seconds_to_next']}
    finally:
        conn.close()


# ==================== API ROUTES ====================

@app.route('/')
def index_route():
    """Главная страница - отдает index.html"""
    return app.send_static_file('index.html')


@app.route('/<path:filename>')
def static_files(filename):
    """Отдача статических файлов из webapp/"""
    return app.send_static_file(filename)


@app.route('/image/<path:filename>')
def image_files(filename):
    """Отдача картинок из webapp/images/ (для обратной совместимости)."""
    webapp_img_dir = WEBAPP_DIR / 'images'
    return send_from_directory(str(webapp_img_dir), filename)


@app.route('/api/boss_hp', methods=['GET'])
def api_get_boss_hp():
    """Получить HP босса"""
    try:
        boss_info = get_boss_hp()
        response = jsonify({'status': 'ok', 'boss': boss_info})
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    except Exception as e:
        logger.error(f"❌ Ошибка в api_get_boss_hp: {e}")
        return jsonify({'status': 'error', 'error': str(e)}), 500


@app.route('/api/player_stats', methods=['GET'])
def api_get_player_stats():
    """Получить статистику игрока"""
    try:
        user_id = request.args.get('userId') or request.headers.get('X-Telegram-User-Id', 0)

        if not user_id:
            return jsonify({'error': 'user_id required'}), 400

        user_id = int(user_id)

        stats = get_player_stats(user_id)
        damage = get_boss_damage(user_id)

        return jsonify({
            'status': 'ok',
            'stats': stats,
            'boss_damage': damage
        })

    except Exception as e:
        logger.error(f"Ошибка api_get_player_stats: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/user_full_profile', methods=['GET'])
def api_get_user_full_profile():
    """Получить полное досье статистики игрока для WebApp"""
    try:
        user_id = request.args.get('userId') or request.headers.get('X-Telegram-User-Id', 0)
        if not user_id:
            return jsonify({'error': 'user_id required'}), 400

        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return jsonify({'error': 'invalid user_id'}), 400

        profile = get_full_user_profile_admin(str(user_id))
        if not profile:
            return jsonify({'error': 'User not found'}), 404

        return jsonify({
            'status': 'ok',
            'profile': profile
        })
    except Exception as e:
        logger.error(f"Ошибка api_get_user_full_profile: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/tonconnect-manifest.json', methods=['GET'])
def tonconnect_manifest_route():
    """Отдача манифеста TON Connect 2.0"""
    response = app.send_static_file('tonconnect-manifest.json')
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Content-Type'] = 'application/json; charset=utf-8'
    return response


@app.route('/api/wallet/link', methods=['POST'])
def api_link_wallet():
    """Привязка TON кошелька к профилю пользователя"""
    try:
        data = request.get_json() or {}
        user_id = data.get('userId') or request.headers.get('X-Telegram-User-Id', 0)
        wallet_address = data.get('wallet_address', '').strip()

        if not user_id:
            return jsonify({'error': 'userId required'}), 400
        if not wallet_address:
            return jsonify({'error': 'wallet_address required'}), 400

        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return jsonify({'error': 'invalid user_id'}), 400

        success = set_user_wallet_address(user_id, wallet_address)
        if success:
            return jsonify({'status': 'ok', 'wallet_address': wallet_address})
        else:
            return jsonify({'error': 'Failed to save wallet address'}), 500
    except Exception as e:
        logger.error(f"Ошибка api_link_wallet: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/wallet/disconnect', methods=['POST'])
def api_disconnect_wallet():
    """Отвязка TON кошелька"""
    try:
        data = request.get_json() or {}
        user_id = data.get('userId') or request.headers.get('X-Telegram-User-Id', 0)
        if not user_id:
            return jsonify({'error': 'userId required'}), 400

        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return jsonify({'error': 'invalid user_id'}), 400

        set_user_wallet_address(user_id, None)
        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f"Ошибка api_disconnect_wallet: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/energy', methods=['GET'])
def api_get_energy():
    """Получить актуальную энергию пользователя (серверный расчет)"""
    try:
        user_id = request.args.get('userId') or request.headers.get('X-Telegram-User-Id', 0)
        if not user_id:
            return jsonify({'error': 'userId required'}), 400
        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return jsonify({'error': 'invalid user_id'}), 400

        energy_data = get_user_energy(user_id)
        response = jsonify({'status': 'ok', **energy_data})
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response
    except Exception as e:
        logger.error(f"Ошибка api_get_energy: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/energy/consume', methods=['POST'])
def api_consume_energy():
    """Списать энергию при запуске игры (серверный контроль)"""
    try:
        data = request.get_json() or {}
        user_id = data.get('userId') or request.headers.get('X-Telegram-User-Id', 0)
        amount = int(data.get('amount', 1))
        game = str(data.get('game', ''))

        if not user_id:
            return jsonify({'error': 'userId required'}), 400
        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return jsonify({'error': 'invalid user_id'}), 400

        res = consume_user_energy(user_id, amount=amount, game=game)
        response = jsonify({'status': 'ok', **res})
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response
    except Exception as e:
        logger.error(f"Ошибка api_consume_energy: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/stars/create_invoice', methods=['POST'])
def api_stars_create_invoice():
    """Создать ссылку на оплату 100 Telegram Stars за 10,000 шишек"""
    try:
        data = request.get_json() or {}
        user_id = data.get('userId') or request.headers.get('X-Telegram-User-Id', 0)
        lang = data.get('lang', 'ru')

        if not user_id:
            return jsonify({'error': 'userId required'}), 400
        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return jsonify({'error': 'invalid user_id'}), 400

        title = "10,000 Шишек" if lang == 'ru' else "10,000 Pinecones"
        desc = "Пакет 10,000 шишек в игре Raccoon Life" if lang == 'ru' else "10,000 pinecones pack in Raccoon Life"
        payload = f"cones_10000_{user_id}_{int(time.time())}"

        tg_url = f"https://api.telegram.org/bot{BOT_TOKEN}/createInvoiceLink"
        body = {
            "title": title,
            "description": desc,
            "payload": payload,
            "currency": "XTR",
            "prices": [{"label": title, "amount": 100}],
            "provider_token": ""
        }

        resp = requests.post(tg_url, json=body, timeout=10)
        tg_res = resp.json()

        if tg_res.get('ok'):
            invoice_link = tg_res.get('result')
            logger.info(f"🌟 Создан инвойс Stars на 100 Звёзд для user_id={user_id}")
            response = jsonify({'status': 'ok', 'invoice_link': invoice_link})
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            return response
        else:
            logger.error(f"Ошибка создания invoice link: {tg_res}")
            return jsonify({'error': tg_res.get('description', 'Failed to create invoice link')}), 400

    except Exception as e:
        logger.error(f"Ошибка api_stars_create_invoice: {e}")
        return jsonify({'error': str(e)}), 500


def notify_admin(text: str):
    """Отправляет служебное уведомление администратору в Telegram"""
    if not ADMIN_ID or not BOT_TOKEN:
        logger.warning("⚠️ notify_admin: ADMIN_ID или BOT_TOKEN не заданы")
        return
    try:
        api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": ADMIN_ID,
            "text": text,
            "parse_mode": "HTML"
        }
        resp = requests.post(api_url, json=payload, timeout=10)
        if resp.status_code == 200:
            logger.info("📬 Уведомление админу успешно доставлено")
        else:
            logger.error(f"⚠️ Ошибка отправки уведомления админу: {resp.text}")
    except Exception as e:
        logger.error(f"⚠️ Ошибка notify_admin: {e}")


@app.route('/api/ton/offers', methods=['GET'])
def api_get_ton_offers():
    """Получить список оферов на шишки за TON и адрес кошелька получателя"""
    try:
        response = jsonify({
            'status': 'ok',
            'recipient_wallet': TON_RECIPIENT_WALLET,
            'offers': TON_OFFERS
        })
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response
    except Exception as e:
        logger.error(f"Ошибка api_get_ton_offers: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/ton/notify_payment', methods=['POST'])
@limiter.limit("15 per minute")
def api_ton_notify_payment():
    """Уведомление и автоматическое зачисление шишек при оплате через GRAM (TON)"""
    try:
        if not request.is_json:
            return jsonify({'error': 'Content-Type must be application/json'}), 400

        data = request.get_json()
        user_id = data.get('userId') or data.get('user_id')
        pack_id = data.get('packId')
        tx_boc = data.get('txBoc', '')
        comment = data.get('comment', '')
        payment_id = data.get('paymentId') or (tx_boc[:64] if tx_boc else f"tx_{user_id}_{pack_id}_{int(time.time())}")

        if not user_id or not pack_id:
            return jsonify({'error': 'userId and packId required'}), 400

        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return jsonify({'error': 'invalid user_id'}), 400

        # Проверяем, это стандартный пакет или пакет прокачки Дупла
        is_hollow_starter = (pack_id == "hollow_starter_10ton")
        is_hollow_gram = pack_id.startswith("hollow_gram_")

        if is_hollow_starter:
            ton_amount = 10.0
            cones_amount = 0
            pack_title = "⚡ Прокачай дупло! (+3 Уровня)"
        elif is_hollow_gram:
            try:
                from_level = int(pack_id.split('_')[-1])
            except:
                from_level = 1
            ton_amount = float(2 * (2 ** (from_level - 1)))
            cones_amount = 0
            pack_title = f"🌳 Прокачка Дупла ({from_level} → {from_level + 1} ур.)"
        else:
            selected_pack = next((p for p in TON_OFFERS if p['id'] == pack_id), None)
            if not selected_pack:
                return jsonify({'error': 'Unknown pack'}), 400
            cones_amount = selected_pack['cones']
            ton_amount = selected_pack['ton']
            pack_title = selected_pack['title_ru']

        # Проверяем, не была ли эта транзакция уже обработана
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            if tx_boc:
                cursor.execute("SELECT payment_id FROM processed_payments WHERE tx_boc = ?", (tx_boc,))
                if cursor.fetchone():
                    logger.warning(f"⚠️ Повторная попытка обработки платежа с BOC: {tx_boc[:20]}...")
                    user_tokens = get_user_tokens(user_id)
                    return jsonify({
                        'status': 'already_processed',
                        'cones_added': 0,
                        'balance': user_tokens['balance']
                    })

            # Записываем в таблицу processed_payments
            cursor.execute('''
                INSERT OR IGNORE INTO processed_payments (payment_id, user_id, payment_type, amount, cones_amount, tx_boc, comment)
                VALUES (?, ?, 'gram', ?, ?, ?, ?)
            ''', (payment_id, user_id, ton_amount, cones_amount, tx_boc, comment))
            conn.commit()

            # Обрабатываем прокачку Дупла
            final_hollow_level = 1
            if is_hollow_starter:
                cursor.execute("UPDATE user_stats SET hollow_level = MAX(COALESCE(hollow_level, 1), 4) WHERE user_id = ?", (user_id,))
                conn.commit()
                cursor.execute("SELECT hollow_level FROM user_stats WHERE user_id = ?", (user_id,))
                row_h = cursor.fetchone()
                final_hollow_level = row_h['hollow_level'] if row_h else 4
                logger.info(f"⚡ Прокачай дупло! {user_id} получил {final_hollow_level}-й уровень Дупла за 10 TON!")
            elif is_hollow_gram:
                cursor.execute("UPDATE user_stats SET hollow_level = MIN(10, COALESCE(hollow_level, 1) + 1) WHERE user_id = ?", (user_id,))
                conn.commit()
                cursor.execute("SELECT hollow_level FROM user_stats WHERE user_id = ?", (user_id,))
                row_h = cursor.fetchone()
                final_hollow_level = row_h['hollow_level'] if row_h else 2
                logger.info(f"🌳 Прокачка Дупла за GRAM: {user_id} повысил уровень Дупла до {final_hollow_level}!")
        finally:
            conn.close()

        # Если это пакет с шишками — начисляем
        if cones_amount > 0:
            result = add_tokens(user_id, cones_amount, f'gram_purchase:{pack_id}:{ton_amount}GRAM')
            logger.info(f"💎 Автоматическое начисление: {user_id} получил {cones_amount} шишек за {ton_amount} GRAM")
        else:
            result = get_user_tokens(user_id)

        # Получаем данные пользователя для отчета админу
        user_name = f"Игрок #{user_id}"
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT username, first_name, last_name FROM users WHERE user_id = ?", (user_id,))
            u_row = cursor.fetchone()
            if u_row:
                user_name = f"@{u_row['username']}" if u_row['username'] else (f"{u_row['first_name'] or ''} {u_row['last_name'] or ''}".strip() or f"Игрок #{user_id}")
        finally:
            conn.close()

        # Формируем и отправляем уведомление администратору
        if is_hollow_starter:
            admin_text = (
                f"⚡ <b>ПРОКАЧАЙ ДУПЛО! (ОПЛАТА 10 TON)</b>\n\n"
                f"👤 <b>Игрок:</b> {html.escape(user_name)}\n"
                f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
                f"💎 <b>Оплачено:</b> 10.0 TON\n"
                f"🏆 <b>Уровень Дупла:</b> {final_hollow_level} / 10 (+3 Уровня!)\n"
                f"🌰 <b>Добыча:</b> {1000 * (2**(final_hollow_level-1)):,} Шишек/сутки\n"
                f"👛 <b>Кошелек:</b> <code>{TON_RECIPIENT_WALLET}</code>\n"
                f"📝 <b>Комментарий:</b> <code>{html.escape(comment or f'rl_{user_id}_{pack_id}')}</code>"
            )
        elif is_hollow_gram:
            admin_text = (
                f"🌳 <b>ПРОКАЧКА ДУПЛА ЗА GRAM!</b>\n\n"
                f"👤 <b>Игрок:</b> {html.escape(user_name)}\n"
                f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
                f"💎 <b>Оплачено:</b> {ton_amount} GRAM\n"
                f"🏆 <b>Новый уровень:</b> {final_hollow_level} / 10\n"
                f"🌰 <b>Добыча:</b> {1000 * (2**(final_hollow_level-1)):,} Шишек/сутки\n"
                f"👛 <b>Кошелек:</b> <code>{TON_RECIPIENT_WALLET}</code>\n"
                f"📝 <b>Комментарий:</b> <code>{html.escape(comment or f'rl_{user_id}_{pack_id}')}</code>"
            )
        else:
            admin_text = (
                f"💎 <b>НОВАЯ ОПЛАТА GRAM!</b>\n\n"
                f"👤 <b>Игрок:</b> {html.escape(user_name)}\n"
                f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
                f"📦 <b>Пакет:</b> {pack_title}\n"
                f"💎 <b>Сумма:</b> {ton_amount} GRAM\n"
                f"🌲 <b>Начислено игроку:</b> +{cones_amount:,} Шишек\n"
                f"💳 <b>Новый баланс игрока:</b> {result['balance'] if result else 0:,} Шишек\n"
                f"👛 <b>Кошелек:</b> <code>{TON_RECIPIENT_WALLET}</code>\n"
                f"📝 <b>Комментарий:</b> <code>{html.escape(comment or f'rl_{user_id}_{pack_id}')}</code>"
            )

        if tx_boc:
            admin_text += f"\n🔗 <b>BOC:</b> <code>{html.escape(tx_boc[:48])}...</code>"

        notify_admin(admin_text)

        # Отправляем подтверждение и поздравление игроку в Telegram
        if BOT_TOKEN and user_id:
            try:
                if is_hollow_starter:
                    user_msg = (
                        f"⚡ <b>Оплата 10 TON прошла успешно!</b>\n\n"
                        f"🌳 Ваше <b>Дупло</b> прокачано до <b>{final_hollow_level}-го уровня</b> (+3 Уровня)!\n"
                        f"🌰 Новая добыча: <b>{1000 * (2**(final_hollow_level-1)):,} Шишек каждые 24 часа</b>!\n\n"
                        f"Приятной игры в <b>Raccoon Life</b>! 🦝"
                    )
                elif is_hollow_gram:
                    user_msg = (
                        f"🌳 <b>Оплата {ton_amount} GRAM прошла успешно!</b>\n\n"
                        f"✨ Ваше <b>Дупло</b> прокачано до <b>{final_hollow_level}-го уровня</b>!\n"
                        f"🌰 Новая добыча: <b>{1000 * (2**(final_hollow_level-1)):,} Шишек каждые 24 часа</b>!\n\n"
                        f"Приятной игры в <b>Raccoon Life</b>! 🦝"
                    )
                else:
                    user_msg = (
                        f"💎 <b>Оплата {ton_amount} GRAM прошла успешно!</b>\n\n"
                        f"✨ На ваш игровой баланс зачислено <b>+{cones_amount:,} Шишек</b>!\n"
                        f"💳 Ваш текущий баланс: <b>{result['balance'] if result else 0:,} Шишек</b>.\n\n"
                        f"Приятной игры в <b>Raccoon Life</b>! 🦝"
                    )
                requests.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": user_id, "text": user_msg, "parse_mode": "HTML"},
                    timeout=6
                )
            except Exception as e:
                logger.error(f"⚠️ Ошибка отправки Telegram сообщения игроку {user_id}: {e}")

        return jsonify({
            'status': 'ok',
            'cones_added': cones_amount,
            'balance': result['balance'] if result else 0,
            'hollow_level': final_hollow_level if (is_hollow_starter or is_hollow_gram) else None
        })

    except Exception as e:
        logger.error(f"Ошибка api_ton_notify_payment: {e}")
        return jsonify({'error': str(e)}), 500


def check_blockchain_incoming_transactions():
    """
    Проверяет блокчейн TON на наличие входящих транзакций на кошелек TON_RECIPIENT_WALLET.
    При обнаружении транзакции с комментарием rl_{userId}_{packId}:
    1. Проверяет хэш транзакции в processed_payments.
    2. Если транзакция новая и сумма достаточна — автоматически начисляет шишки или прокачивает Дупло.
    3. Отправляет подтверждение игроку в Telegram.
    4. Отправляет уведомление администратору.
    """
    if not TON_RECIPIENT_WALLET:
        return
    try:
        url = f"https://toncenter.com/api/v2/getTransactions?address={TON_RECIPIENT_WALLET}&limit=20"
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            return
        data = resp.json()
        if not data.get('ok') or not data.get('result'):
            return

        txs = data['result']
        for tx in txs:
            try:
                tx_id = tx.get('transaction_id', {})
                tx_hash = tx_id.get('hash')
                if not tx_hash:
                    continue

                in_msg = tx.get('in_msg', {})
                raw_value = in_msg.get('value', 0)
                try:
                    nano_amount = int(raw_value)
                except (ValueError, TypeError):
                    nano_amount = 0

                comment = in_msg.get('message', '').strip()
                if not comment.startswith('rl_'):
                    continue

                # Формат комментария: rl_{userId}_{packId}
                parts = comment.split('_')
                if len(parts) < 3:
                    continue

                try:
                    user_id = int(parts[1])
                except ValueError:
                    continue

                pack_id = '_'.join(parts[2:])
                is_hollow_starter = (pack_id == "hollow_starter_10ton")
                is_hollow_gram = pack_id.startswith("hollow_gram_")

                if is_hollow_starter:
                    ton_amount = 10.0
                    cones_amount = 0
                    pack_title = "⚡ Прокачай дупло! (+3 Уровня)"
                elif is_hollow_gram:
                    try:
                        from_level = int(pack_id.split('_')[-1])
                    except:
                        from_level = 1
                    ton_amount = float(2 * (2 ** (from_level - 1)))
                    cones_amount = 0
                    pack_title = f"🌳 Прокачка Дупла ({from_level} → {from_level + 1} ур.)"
                else:
                    selected_pack = next((p for p in TON_OFFERS if p['id'] == pack_id), None)
                    if not selected_pack:
                        continue
                    cones_amount = selected_pack['cones']
                    ton_amount = selected_pack['ton']
                    pack_title = selected_pack['title_ru']

                expected_nano = int(ton_amount * 1e9)
                if nano_amount < int(expected_nano * 0.95):  # с учетом допустимой погрешности
                    continue

                # Проверяем, обрабатывали ли мы уже этот tx_hash
                conn = get_db_connection()
                try:
                    cursor = conn.cursor()
                    cursor.execute("SELECT payment_id FROM processed_payments WHERE payment_id = ? OR tx_boc = ?", (tx_hash, tx_hash))
                    if cursor.fetchone():
                        continue # Уже обработан

                    # Записываем транзакцию
                    cursor.execute('''
                        INSERT OR IGNORE INTO processed_payments (payment_id, user_id, payment_type, amount, cones_amount, tx_boc, comment)
                        VALUES (?, ?, 'gram_blockchain', ?, ?, ?, ?)
                    ''', (tx_hash, user_id, ton_amount, cones_amount, tx_hash, comment))
                    conn.commit()

                    final_hollow_level = 1
                    if is_hollow_starter:
                        cursor.execute("UPDATE user_stats SET hollow_level = MAX(COALESCE(hollow_level, 1), 4) WHERE user_id = ?", (user_id,))
                        conn.commit()
                        cursor.execute("SELECT hollow_level FROM user_stats WHERE user_id = ?", (user_id,))
                        row_h = cursor.fetchone()
                        final_hollow_level = row_h['hollow_level'] if row_h else 4
                        logger.info(f"⚡ [Блокчейн] Прокачай дупло: {user_id} получил {final_hollow_level}-й уровень Дупла!")
                    elif is_hollow_gram:
                        cursor.execute("UPDATE user_stats SET hollow_level = MIN(10, COALESCE(hollow_level, 1) + 1) WHERE user_id = ?", (user_id,))
                        conn.commit()
                        cursor.execute("SELECT hollow_level FROM user_stats WHERE user_id = ?", (user_id,))
                        row_h = cursor.fetchone()
                        final_hollow_level = row_h['hollow_level'] if row_h else 2
                        logger.info(f"🌳 [Блокчейн] Прокачка Дупла: {user_id} получил {final_hollow_level}-й уровень Дупла!")
                finally:
                    conn.close()

                # Если это пакет с шишками — начисляем
                if cones_amount > 0:
                    result = add_tokens(user_id, cones_amount, f'blockchain_gram_purchase:{pack_id}:{ton_amount}GRAM')
                    logger.info(f"💎 [Блокчейн-верификатор] Успешно зачислено +{cones_amount} шишек игроку {user_id} (tx: {tx_hash[:16]}...)")
                else:
                    result = get_user_tokens(user_id)

                # Получаем данные пользователя для отчета админу
                user_name = f"Игрок #{user_id}"
                conn = get_db_connection()
                try:
                    cursor = conn.cursor()
                    cursor.execute("SELECT username, first_name, last_name FROM users WHERE user_id = ?", (user_id,))
                    u_row = cursor.fetchone()
                    if u_row:
                        user_name = f"@{u_row['username']}" if u_row['username'] else (f"{u_row['first_name'] or ''} {u_row['last_name'] or ''}".strip() or f"Игрок #{user_id}")
                finally:
                    conn.close()

                # Уведомление админу
                if is_hollow_starter:
                    admin_text = (
                        f"⚡ <b>ПРОКАЧАЙ ДУПЛО (БЛОКЧЕЙН ПОДТВЕРЖДЕН)!</b>\n\n"
                        f"👤 <b>Игрок:</b> {html.escape(user_name)}\n"
                        f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
                        f"💎 <b>Оплачено:</b> 10.0 TON\n"
                        f"🏆 <b>Уровень Дупла:</b> {final_hollow_level} / 10\n"
                        f"🌰 <b>Добыча:</b> {1000 * (2**(final_hollow_level-1)):,} Шишек/сутки\n"
                        f"👛 <b>Кошелек:</b> <code>{TON_RECIPIENT_WALLET}</code>\n"
                        f"📝 <b>Комментарий:</b> <code>{html.escape(comment)}</code>\n"
                        f"🔗 <b>TX Hash:</b> <code>{html.escape(tx_hash)}</code>"
                    )
                elif is_hollow_gram:
                    admin_text = (
                        f"🌳 <b>ПРОКАЧКА ДУПЛА ЗА GRAM (БЛОКЧЕЙН ПОДТВЕРЖДЕН)!</b>\n\n"
                        f"👤 <b>Игрок:</b> {html.escape(user_name)}\n"
                        f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
                        f"💎 <b>Оплачено:</b> {ton_amount} GRAM\n"
                        f"🏆 <b>Новый уровень:</b> {final_hollow_level} / 10\n"
                        f"🌰 <b>Добыча:</b> {1000 * (2**(final_hollow_level-1)):,} Шишек/сутки\n"
                        f"👛 <b>Кошелек:</b> <code>{TON_RECIPIENT_WALLET}</code>\n"
                        f"📝 <b>Комментарий:</b> <code>{html.escape(comment)}</code>\n"
                        f"🔗 <b>TX Hash:</b> <code>{html.escape(tx_hash)}</code>"
                    )
                else:
                    admin_text = (
                        f"💎 <b>НОВАЯ ОПЛАТА GRAM (БЛОКЧЕЙН ПОДТВЕРЖДЕН)!</b>\n\n"
                        f"👤 <b>Игрок:</b> {html.escape(user_name)}\n"
                        f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
                        f"📦 <b>Пакет:</b> {pack_title}\n"
                        f"💎 <b>Сумма:</b> {ton_amount} GRAM\n"
                        f"🌲 <b>Начислено игроку:</b> +{cones_amount:,} Шишек\n"
                        f"💳 <b>Новый баланс игрока:</b> {result['balance'] if result else 0:,} Шишек\n"
                        f"👛 <b>Кошелек:</b> <code>{TON_RECIPIENT_WALLET}</code>\n"
                        f"📝 <b>Комментарий:</b> <code>{html.escape(comment)}</code>\n"
                        f"🔗 <b>TX Hash:</b> <code>{html.escape(tx_hash)}</code>"
                    )
                notify_admin(admin_text)

                # Уведомление игроку в Telegram
                if BOT_TOKEN and user_id:
                    try:
                        if is_hollow_starter:
                            user_msg = (
                                f"⚡ <b>Оплата 10 TON подтверждена в блокчейне!</b>\n\n"
                                f"🌳 Ваше <b>Дупло</b> прокачано до <b>{final_hollow_level}-го уровня</b> (+3 Уровня)!\n"
                                f"🌰 Новая добыча: <b>{1000 * (2**(final_hollow_level-1)):,} Шишек каждые 24 часа</b>!\n\n"
                                f"Приятной игры в <b>Raccoon Life</b>! 🦝"
                            )
                        elif is_hollow_gram:
                            user_msg = (
                                f"🌳 <b>Оплата {ton_amount} GRAM подтверждена в блокчейне!</b>\n\n"
                                f"✨ Ваше <b>Дупло</b> прокачано до <b>{final_hollow_level}-го уровня</b>!\n"
                                f"🌰 Новая добыча: <b>{1000 * (2**(final_hollow_level-1)):,} Шишек каждые 24 часа</b>!\n\n"
                                f"Приятной игры в <b>Raccoon Life</b>! 🦝"
                            )
                        else:
                            user_msg = (
                                f"💎 <b>Оплата {ton_amount} GRAM подтверждена в блокчейне!</b>\n\n"
                                f"✨ На ваш игровой баланс зачислено <b>+{cones_amount:,} Шишек</b>!\n"
                                f"💳 Ваш текущий баланс: <b>{result['balance'] if result else 0:,} Шишек</b>.\n\n"
                                f"Приятной игры в <b>Raccoon Life</b>! 🦝"
                            )
                        requests.post(
                            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                            json={"chat_id": user_id, "text": user_msg, "parse_mode": "HTML"},
                            timeout=6
                        )
                    except Exception as e:
                        logger.error(f"Ошибка отправки подтверждения игроку {user_id}: {e}")

            except Exception as item_err:
                logger.error(f"Ошибка обработки отдельной транзакции: {item_err}")

    except Exception as e:
        logger.debug(f"Ошибка check_blockchain_incoming_transactions: {e}")


def ton_blockchain_watcher_thread():
    """Фоновый поток для автоматической проверки входящих платежей в блокчейне TON"""
    while True:
        try:
            check_blockchain_incoming_transactions()
        except Exception as e:
            logger.error(f"Ошибка в ton_blockchain_watcher_thread: {e}")
        time.sleep(15)


def boar_watcher_thread():
    """Фоновый поток для проверки неактивных игроков и механики Кабана (каждые 5 минут)."""
    logger.info("🐗 Boar watcher thread started")
    while True:
        try:
            time.sleep(300)  # проверка каждые 5 минут
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM user_stats")
            rows = cursor.fetchall()
            conn.close()

            for r in rows:
                try:
                    process_boar_for_user(r['user_id'])
                except Exception as user_e:
                    logger.error(f"Ошибка проверки кабана для user {r['user_id']}: {user_e}")
        except Exception as e:
            logger.error(f"Ошибка в boar_watcher_thread: {e}")
            time.sleep(30)


@app.route('/api/leaderboard', methods=['GET'])
def api_get_leaderboard():
    """Получить рейтинг игроков"""
    try:
        limit = request.args.get('limit', 10)
        limit = int(limit) if limit else 10
        limit = min(limit, 100)  # Максимум 100 игроков
        
        lb_type = request.args.get('type', 'tokens')
        user_id = request.args.get('userId') or request.headers.get('X-Telegram-User-Id', 0)
        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            user_id = 0

        if lb_type == 'overall':
            leaderboard = get_overall_leaderboard(limit)
        elif lb_type == 'quests':
            leaderboard = get_quests_leaderboard(limit)
        elif lb_type == 'boss':
            leaderboard = get_boss_leaderboard(limit)
        else:
            leaderboard = get_leaderboard(limit)

        user_rank = get_user_rank_in_leaderboard(user_id, lb_type) if user_id > 0 else None

        response = jsonify({
            'status': 'ok',
            'leaderboard': leaderboard,
            'user_rank': user_rank,
            'type': lb_type
        })
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    except Exception as e:
        logger.error(f"Ошибка api_get_leaderboard: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/tokens', methods=['GET'])
def api_get_tokens():
    """Получить баланс шишек пользователя и статус ежедневного бонуса"""
    try:
        user_id = request.args.get('userId') or request.headers.get('X-Telegram-User-Id', 0)

        logger.info(f"📥 API /api/tokens: userId={user_id}")

        if not user_id:
            logger.warning("⚠️ userId не указан")
            return jsonify({'error': 'user_id required'}), 400

        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return jsonify({'error': 'invalid user_id'}), 400

        logger.info(f"🔍 Запрос шишек для user_id={user_id}")
        
        tokens = get_user_tokens(user_id)
        bonus_status = get_daily_bonus_status(user_id)
        hollow_status = get_hollow_status(user_id)

        # Получаем актуальные тапы енота
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COALESCE(raccoon_taps, 0) FROM user_stats WHERE user_id = ?", (user_id,))
        tap_row = cursor.fetchone()
        conn.close()
        raccoon_taps = tap_row[0] if tap_row else 0

        logger.info(f"💰 Ответ: balance={tokens['balance']}, can_claim_bonus={hollow_status.get('can_claim')}, raccoon_taps={raccoon_taps}, hollow_lvl={hollow_status.get('level')}")
        
        return jsonify({
            'status': 'ok',
            'tokens': tokens,
            'bonus': bonus_status,
            'hollow': hollow_status,
            'raccoon_taps': raccoon_taps
        })

    except Exception as e:
        logger.error(f"Ошибка api_get_tokens: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/hollow/status', methods=['GET'])
def api_hollow_status():
    """Получить статус Дупла (уровень, урожай, стоимость прокачки, таймер)"""
    try:
        user_id = request.args.get('userId') or request.headers.get('X-Telegram-User-Id', 0)
        if not user_id:
            return jsonify({'error': 'user_id required'}), 400
        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return jsonify({'error': 'invalid user_id'}), 400

        status = get_hollow_status(user_id)
        return jsonify(status)
    except Exception as e:
        logger.error(f"Ошибка api_hollow_status: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/hollow/claim', methods=['POST'])
@limiter.limit("30 per minute")
def api_hollow_claim():
    """Собрать урожай Дупла"""
    try:
        if not request.is_json:
            return jsonify({'error': 'Content-Type must be application/json'}), 400

        data = request.get_json()
        user_id = data.get('userId') or data.get('user_id')
        if not user_id:
            return jsonify({'error': 'userId required'}), 400

        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return jsonify({'error': 'invalid user_id'}), 400

        result = claim_daily_bonus(user_id)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Ошибка api_hollow_claim: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/hollow/upgrade_cones', methods=['POST'])
@limiter.limit("30 per minute")
def api_hollow_upgrade_cones():
    """Прокачать Дупло за шишки"""
    try:
        if not request.is_json:
            return jsonify({'error': 'Content-Type must be application/json'}), 400

        data = request.get_json()
        user_id = data.get('userId') or data.get('user_id')
        if not user_id:
            return jsonify({'error': 'userId required'}), 400

        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return jsonify({'error': 'invalid user_id'}), 400

        result = upgrade_hollow_cones(user_id)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Ошибка api_hollow_upgrade_cones: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/hollow/chase_boar', methods=['POST'])
@limiter.limit("30 per minute")
def api_hollow_chase_boar():
    """Прогнать дикого кабана из Дупла"""
    try:
        if not request.is_json:
            return jsonify({'error': 'Content-Type must be application/json'}), 400

        data = request.get_json()
        user_id = data.get('userId') or data.get('user_id')
        if not user_id:
            return jsonify({'error': 'userId required'}), 400

        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return jsonify({'error': 'invalid user_id'}), 400

        result = chase_boar_from_hollow(user_id)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Ошибка api_hollow_chase_boar: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/inventory', methods=['GET'])
def api_get_inventory():
    """Получить инвентарь пользователя"""
    try:
        user_id = request.args.get('userId') or request.headers.get('X-Telegram-User-Id', 0)
        if not user_id:
            return jsonify({'error': 'user_id required'}), 400
        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return jsonify({'error': 'invalid user_id'}), 400

        items = get_user_inventory(user_id)
        return jsonify({
            'status': 'ok',
            'user_id': user_id,
            'items': items
        })
    except Exception as e:
        logger.error(f"Ошибка api_get_inventory: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/inventory/use', methods=['POST'])
@limiter.limit("30 per minute")
def api_use_inventory():
    """Использовать предмет из инвентаря"""
    try:
        if not request.is_json:
            return jsonify({'error': 'Content-Type must be application/json'}), 400

        data = request.get_json()
        user_id = data.get('userId') or data.get('user_id')
        item_id = data.get('itemId') or data.get('item_id')

        if not user_id or not item_id:
            return jsonify({'error': 'userId and itemId required'}), 400

        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return jsonify({'error': 'invalid user_id'}), 400

        result = use_inventory_item(user_id, item_id)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Ошибка api_use_inventory: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/bonus/claim', methods=['POST'])
@limiter.limit("30 per minute")
def api_bonus_claim():
    """Забрать ежедневный бонус 1000 шишек"""
    try:
        if not request.is_json:
            return jsonify({'error': 'Content-Type must be application/json'}), 400

        data = request.get_json()
        user_id = data.get('userId') or data.get('user_id')

        if not user_id:
            return jsonify({'error': 'user_id required'}), 400

        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return jsonify({'error': 'invalid user_id'}), 400

        if is_user_banned(user_id):
            return jsonify({'status': 'error', 'message': 'User is banned'}), 403

        result = claim_daily_bonus(user_id)
        status_code = 200 if result.get('status') == 'ok' else 400
        return jsonify(result), status_code
    except Exception as e:
        logger.error(f"Ошибка api_bonus_claim: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/referral/register', methods=['POST'])
@limiter.limit("30 per minute")
def api_register_referral():
    """Зарегистрировать реферала из Mini App"""
    try:
        data = request.get_json() or {}
        user_id = data.get('userId') or data.get('user_id')
        referrer_id = data.get('referrerId') or data.get('referrer_id')

        # Если передан start_param
        start_param = data.get('start_param') or data.get('startParam') or ''
        if not referrer_id and start_param:
            clean_param = str(start_param).replace('ref_', '').replace('ref', '')
            try:
                referrer_id = int(clean_param)
            except (ValueError, TypeError):
                pass

        if not user_id or not referrer_id:
            return jsonify({'error': 'userId and referrerId required'}), 400

        try:
            user_id = int(user_id)
            referrer_id = int(referrer_id)
        except (ValueError, TypeError):
            return jsonify({'error': 'invalid IDs'}), 400

        success = register_referral(user_id, referrer_id)
        stats = get_user_referral_stats(user_id)
        return jsonify({
            'status': 'ok',
            'registered': success,
            'referral_stats': stats
        })
    except Exception as e:
        logger.error(f"Ошибка api_register_referral: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/referrals', methods=['GET'])
def api_get_referrals():
    """Получить реферальную статистику игрока (сколько пригласил, кто пригласил)"""
    try:
        user_id = request.args.get('userId') or request.headers.get('X-Telegram-User-Id', 0)
        if not user_id:
            return jsonify({'error': 'user_id required'}), 400

        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return jsonify({'error': 'invalid user_id'}), 400

        stats = get_user_referral_stats(user_id)
        return jsonify({
            'status': 'ok',
            'referral_stats': stats
        })
    except Exception as e:
        logger.error(f"Ошибка api_get_referrals: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/security/log', methods=['POST'])
def api_security_log():
    """
    Endpoint для приёма security логов от клиентов
    Логи отправляются в SIEM систему или сохраняются для анализа
    """
    try:
        if not request.is_json:
            return jsonify({'error': 'Content-Type must be application/json'}), 400

        data = request.get_json()
        logs = data.get('logs', [])

        if not isinstance(logs, list):
            return jsonify({'error': 'logs must be an array'}), 400

        # Обрабатываем каждый лог
        for log_entry in logs:
            event_type = log_entry.get('event_type', 'UNKNOWN')
            message = log_entry.get('message', '')
            user_id = log_entry.get('user_id')
            details = log_entry.get('details', {})
            timestamp = log_entry.get('timestamp', '')
            game = log_entry.get('game', 'unknown')

            # Логируем в security logger
            security_logger.info(
                f"CLIENT_SECURITY_LOG: game={game}, event={event_type}, user_id={user_id}, message={message}",
                extra={
                    'client_log': True,
                    'game': game,
                    'event_type': event_type,
                    'user_id': user_id,
                    'details': details,
                    'timestamp': timestamp
                }
            )

            # Проверка на подозрительную активность
            if event_type in ['SUSPICIOUS_ACTIVITY', 'AUTH_ERROR']:
                security_logger.warning(
                    f"🚨 CLIENT ALERT: game={game}, event={event_type}, user_id={user_id}, message={message}",
                    extra={'details': details}
                )

        return jsonify({'status': 'ok', 'received': len(logs)})

    except Exception as e:
        logger.error(f"Ошибка api_security_log: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/casino/roulette', methods=['POST'])
@limiter.limit("20 per minute")
def api_casino_roulette():
    """
    Игра в рулетку
    
    Принимает:
    - userId: ID пользователя
    - betType: тип ставки (red, black, green, half)
    - betAmount: сумма ставки
    """
    try:
        if not request.is_json:
            return jsonify({'error': 'Content-Type must be application/json'}), 400

        data = request.get_json()
        user_id = data.get('userId') or data.get('user_id')
        bet_type = data.get('betType', 'red')
        bet_amount = data.get('betAmount', 10)

        if not user_id:
            return jsonify({'error': 'user_id required'}), 400

        user_id = int(user_id)
        bet_amount = int(bet_amount)
        
        # Проверка авторизации
        init_data = request.headers.get('X-Telegram-Init-Data')
        auth_user = validate_webapp_data(init_data)
        if not auth_user or str(auth_user.get('id')) != str(user_id):
            logger.warning(f"🚨 БЛОКИРОВКА Рулетки: неверная подпись или подделка ID!")
            return jsonify({'error': 'Unauthorized'}), 403

        if bet_amount < 1000:
            return jsonify({'error': 'Минимальная ставка в рулетке: 1,000 шишек', 'minBet': 1000}), 400

        # Конфигурация рулетки (15 секторов: 1 зелёный, 7 красных, 7 чёрных)
        segments = [
            {'type': 'green', 'value': 0},
            {'type': 'red', 'value': 1}, {'type': 'black', 'value': 2},
            {'type': 'red', 'value': 3}, {'type': 'black', 'value': 4},
            {'type': 'red', 'value': 5}, {'type': 'black', 'value': 6},
            {'type': 'red', 'value': 7}, {'type': 'black', 'value': 8},
            {'type': 'red', 'value': 9}, {'type': 'black', 'value': 10},
            {'type': 'red', 'value': 11}, {'type': 'black', 'value': 12},
            {'type': 'red', 'value': 13}, {'type': 'black', 'value': 14}
        ]

        # Множители (RTP ~95%)
        multipliers = {
            'red': 2,
            'black': 2,
            'green': 14
        }

        # Списываем ставку
        spend_result = spend_tokens(user_id, bet_amount, 'roulette_bet')
        if not spend_result:
            return jsonify({'error': 'Insufficient tokens'}), 400

        # Генерация случайного результата
        import random
        normal_segments = [s for s in segments if s['type'] != 'jackpot']
        result_segment = random.choice(normal_segments)

        # Проверка выигрыша
        win = False
        if bet_type == 'red' and result_segment['type'] == 'red':
            win = True
        elif bet_type == 'black' and result_segment['type'] == 'black':
            win = True
        elif bet_type == 'green' and result_segment['type'] == 'green':
            win = True

        # ДЖЕКПОТ 0.1% (1 из 1000) и МЕГАШИШКА 1.5% (1 из ~67)
        is_jackpot = False
        is_mega_cone = False
        item_won = None

        roll = random.random()
        if roll < 0.001:
            is_jackpot = True
            win = True
            result_segment = {'type': 'jackpot', 'value': 777}
        elif roll < 0.016:  # 1.5% шанс на Мегашишку
            is_mega_cone = True
            win = True
            result_segment = {'type': 'mega_cone', 'value': 888}
            add_inventory_item(user_id, 'mega_cone', 1)
            item_won = {
                'id': 'mega_cone',
                'name': 'Мегашишка',
                'icon': 'mega_cone.png',
                'desc': 'Повышает уровень Дупла на +1'
            }
            logger.info(f"🌟 Roulette MEGA CONE: user_id={user_id} выиграл Мегашишку!")

        win_amount = 0
        if win:
            if is_jackpot:
                win_amount = bet_amount * 100
                add_tokens(user_id, win_amount, f'roulette_jackpot:{bet_type}')
                logger.info(f"💎 Roulette JACKPOT: user_id={user_id}, bet={bet_amount}, win={win_amount}")
            elif is_mega_cone:
                win_amount = bet_amount # Возвращаем ставку + Мегашишка в инвентарь
                add_tokens(user_id, win_amount, f'roulette_megacone:{bet_type}')
            else:
                win_amount = int(bet_amount * multipliers.get(bet_type, 2))
                add_tokens(user_id, win_amount, f'roulette_win:{bet_type}')
                logger.info(f"🎰 Roulette WIN: user_id={user_id}, bet={bet_amount}, win={win_amount}")
        else:
            logger.info(f"🎰 Roulette LOSE: user_id={user_id}, bet={bet_amount}")

        # Обновляем статистику рулетки
        cones_won = win_amount if win else 0
        cones_lost = bet_amount if not win else 0
        
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                UPDATE user_stats SET 
                    roulette_games = roulette_games + 1,
                    roulette_wins = roulette_wins + ?,
                    roulette_cones_won = roulette_cones_won + ?,
                    roulette_cones_lost = roulette_cones_lost + ?,
                    roulette_total_bets = COALESCE(roulette_total_bets, 0) + ?
                WHERE user_id = ?
            ''', (1 if win else 0, cones_won, cones_lost, bet_amount, user_id))
            conn.commit()
        except Exception as e:
            logger.error(f"Ошибка обновления статистики рулетки: {e}")
        finally:
            conn.close()

        return jsonify({
            'status': 'ok',
            'result': {
                'number': result_segment['value'],
                'type': result_segment['type']
            },
            'win': win,
            'winAmount': win_amount,
            'isJackpot': is_jackpot,
            'isMegaCone': is_mega_cone,
            'itemWon': item_won
        })

    except Exception as e:
        logger.error(f"Ошибка api_casino_roulette: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/submit_news', methods=['POST'])
@limiter.limit("5 per minute")
def api_submit_news():
    """Отправка новости от пользователя администратору"""
    try:
        if not request.is_json:
            return jsonify({'error': 'Content-Type must be application/json'}), 400

        data = request.get_json()
        user_id = data.get('userId') or data.get('user_id')
        text = sanitize_string(data.get('text', ''), max_length=1024)
        topic = sanitize_string(data.get('topic', 'Другое'), max_length=100)
        is_anonymous = bool(data.get('isAnonymous', False))
        image_base64 = data.get('image')

        if not user_id or (not text and not image_base64):
            return jsonify({'error': 'Missing data'}), 400

        user_id = int(user_id)

        # Проверка авторизации
        init_data = request.headers.get('X-Telegram-Init-Data')
        auth_user = validate_webapp_data(init_data)
        if not auth_user or str(auth_user.get('id')) != str(user_id):
            return jsonify({'error': 'Unauthorized'}), 403

        if is_user_banned(user_id):
            return jsonify({'error': 'User is banned'}), 403

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            logger.info(f"Отправка новости: user_id={user_id}, ADMIN_ID={ADMIN_ID}")
            # Проверка кулдауна (3 часа) для всех, кроме администратора
            if user_id != ADMIN_ID:
                cursor.execute('SELECT last_news_submit FROM user_stats WHERE user_id = ?', (user_id,))
                row = cursor.fetchone()
                
                if row and row['last_news_submit']:
                    last_submit = datetime.strptime(row['last_news_submit'], "%Y-%m-%d %H:%M:%S")
                    time_diff = datetime.utcnow() - last_submit
                    if time_diff < timedelta(hours=3):
                        remaining = timedelta(hours=3) - time_diff
                        hours, remainder = divmod(remaining.seconds, 3600)
                        minutes, _ = divmod(remainder, 60)
                        return jsonify({
                            'status': 'cooldown', 
                            'message': f'Подождите {hours}ч {minutes}м перед следующей отправкой.'
                        }), 400

            # Обновляем время последней отправки
            cursor.execute('''UPDATE user_stats SET last_news_submit = CURRENT_TIMESTAMP WHERE user_id = ?''', (user_id,))
            conn.commit()
        finally:
            conn.close()

        # Формируем системную часть сообщения
        system_info = f"📰 <b>Новое сообщение от игрока!</b>\n"
        system_info += f"🏷 <b>Тема:</b> {html.escape(topic)}\n"
        system_info += f"➖➖➖➖➖➖\n"
        
        if is_anonymous:
            system_info += "🕵️‍♂️ <b>Отправитель:</b> Анонимно"
        else:
            username = html.escape(auth_user.get('username', 'Нет_юзернейма'))
            first_name = html.escape(auth_user.get('first_name', 'Без_имени'))
            system_info += f"👤 <b>Отправитель:</b> {first_name} (@{username})\n🆔 <b>ID:</b> <code>{user_id}</code>"

        reply_markup = {
            "inline_keyboard": [[
                {"text": "✅ Опубликовать", "callback_data": "publish_news"}
            ]]
        }
        
        photo_to_send = None
        if image_base64:
            try:
                image_bytes = base64.b64decode(image_base64)
                photo_to_send = BytesIO(image_bytes)
            except Exception as e:
                logger.error(f"Ошибка декодирования изображения: {e}")
                photo_to_send = None

        api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/"
        
        if photo_to_send:
            # Отправляем фото с подписью
            files = {'photo': ('news_image.jpg', photo_to_send, 'image/jpeg')}
            # Текст новости становится подписью
            caption = f"{html.escape(text)}\n\n{system_info}" if text else system_info
            payload = {
                "chat_id": ADMIN_ID, 
                "caption": caption, 
                "parse_mode": "HTML", 
                "reply_markup": json.dumps(reply_markup)
            }
            response = requests.post(api_url + "sendPhoto", data=payload, files=files, timeout=20)
        else:
            # Отправляем только текст
            full_text = f"{html.escape(text)}\n\n{system_info}"
            payload = {
                "chat_id": ADMIN_ID, 
                "text": full_text, 
                "parse_mode": "HTML", 
                "reply_markup": reply_markup
            }
            response = requests.post(api_url + "sendMessage", json=payload, timeout=10)

        if response.status_code != 200:
            logger.error(f"Telegram API Error (submit_news): {response.text}")
            return jsonify({'error': f'Сбой Telegram API: {response.status_code}'}), 500

        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f"Ошибка api_submit_news: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/boss/attack', methods=['POST'])
@limiter.limit("120 per minute")
def api_boss_attack():
    """Серверная логика атаки по боссу"""
    try:
        if not request.is_json:
            logger.warning("🚨 Boss attack: не JSON запрос")
            return jsonify({'error': 'Content-Type must be application/json'}), 400

        data = request.get_json()
        user_id = data.get('userId') or data.get('user_id')
        action = data.get('action', 'basic')

        logger.info(f"💥 Boss attack: user_id={user_id}, action={action}")

        if not user_id:
            logger.warning("⚠️ Boss attack: нет user_id")
            return jsonify({'status': 'error', 'error': 'user_id required', 'message': 'user_id required'}), 400

        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return jsonify({'status': 'error', 'error': 'invalid user_id', 'message': 'invalid user_id'}), 400

        # Криптографическая проверка авторизации (если init_data передана)
        init_data = request.headers.get('X-Telegram-Init-Data')
        if init_data:
            auth_user = validate_webapp_data(init_data)
            if auth_user:
                user_id = int(auth_user.get('id'))

        # Проверка бана
        if is_user_banned(user_id):
            security_logger.warning(f"🚨 BANNED USER: user_id={user_id} попытался атаковать босса")
            logger.warning(f"⚠️ Забаненный пользователь попытался атаковать босса")
            return jsonify({'status': 'error', 'error': 'User is banned', 'message': 'User is banned'}), 403

        # Античит: Проверка сессии босса и кулдауна действий
        boss_session = get_game_session(user_id, 'boss') or {'energy': 0, 'last_action_ts': 0}
        now_ts = time.time()
        last_ts = boss_session.get('last_action_ts', 0)

        # Ограничение по скорости (не чаще 1 действия в 0.35 сек)
        if now_ts - last_ts < 0.35:
            return jsonify({'status': 'error', 'message': 'Action too fast. Please wait.'}), 429

        current_energy = boss_session.get('energy', 0)

        damage = 0
        heal = 0
        is_crit = False
        energy_change = 0

        # Серверная логика урона и валидация затрат энергии
        if action == 'basic':
            damage = random.randint(50, 100)
            energy_change = 20
            is_crit = random.random() < 0.15
        elif action == 'strong':
            if current_energy < 40:
                return jsonify({'status': 'error', 'message': 'Not enough energy for strong attack'}), 400
            damage = random.randint(150, 250)
            energy_change = -40
            is_crit = random.random() < 0.15
        elif action == 'ultimate':
            if current_energy < 80:
                return jsonify({'status': 'error', 'message': 'Not enough energy for ultimate attack'}), 400
            damage = random.randint(400, 700)
            energy_change = -80
            is_crit = True
        elif action == 'heal':
            if current_energy < 50:
                return jsonify({'status': 'error', 'message': 'Not enough energy for heal'}), 400
            heal = random.randint(30, 50)
            energy_change = -50
        else:
            logger.warning(f"⚠️ Неизвестное действие: {action}")
            return jsonify({'error': 'Invalid action'}), 400

        # Обновляем серверную энергию игрока в сессии
        new_energy = max(0, min(100, current_energy + energy_change))
        save_game_session(user_id, 'boss', {'energy': new_energy, 'last_action_ts': now_ts})

        if is_crit and damage > 0:
            damage = int(damage * 2)

        # Ответный удар босса (40% шанс)
        boss_damage = 0
        if random.random() < 0.4:
            boss_damage = random.randint(1, 100)

        logger.info(f"💥 Attack params: damage={damage}, boss_damage={boss_damage}, energy={energy_change}, crit={is_crit}")

        # Применяем урон по боссу в БД
        boss_info = None
        tokens_earned = 0
        if damage > 0:
            boss_info = add_boss_damage(user_id, damage)

            # Начисляем шишки за урон: 1 шишка за каждые 20 урона
            tokens_earned = damage // 20
            if tokens_earned > 0:
                add_tokens(user_id, tokens_earned, f'boss_attack:{damage}')

        if not boss_info:
            boss_info = get_boss_hp()

        logger.info(f"✅ Boss attack success: user_id={user_id}, boss_hp={boss_info['current_hp']}")

        # Шанс выпадения лута при атаке босса (4%)
        loot_drop = check_minigame_loot_drop(user_id, 'world_boss', base_chance=0.04)

        return jsonify({
            'status': 'ok',
            'damage': damage,
            'is_crit': is_crit,
            'heal': heal,
            'energy_change': energy_change,
            'boss_damage': boss_damage,
            'boss_hp': boss_info['current_hp'],
            'tokens_earned': tokens_earned,
            'loot_drop': loot_drop
        })

    except Exception as e:
        logger.error(f"❌ Ошибка api_boss_attack: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/game/vladeos', methods=['POST'])
@limiter.limit("60 per minute")
def api_game_vladeos():
    """Логика Vladeos PvP"""
    try:
        data = request.get_json() or {}
        user_id = data.get('userId') or data.get('user_id')
        if not user_id:
            return jsonify({'status': 'error', 'error': 'user_id required'}), 400
        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return jsonify({'status': 'error', 'error': 'invalid user_id'}), 400

        init_data = request.headers.get('X-Telegram-Init-Data')
        if init_data:
            auth_user = validate_webapp_data(init_data)
            if auth_user:
                user_id = int(auth_user.get('id'))
            
        is_win = random.random() < 0.05
        loot_drop = None
        if is_win:
            v_score = random.randint(1, 90)
            p_score = v_score + 1
            add_tokens(user_id, 100, 'vladeos_win')
            loot_drop = check_minigame_loot_drop(user_id, 'vladeos', base_chance=0.20)
        else:
            p_score = random.randint(1, 90)
            v_score = p_score + 1
            
        return jsonify({'status': 'ok', 'win': is_win, 'p_score': p_score, 'v_score': v_score, 'loot_drop': loot_drop})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/game/battleship', methods=['POST'])
@limiter.limit("60 per minute")
def api_game_battleship():
    """Античит Морского боя - кулдаун 10 сек"""
    try:
        data = request.get_json() or {}
        user_id = data.get('userId') or data.get('user_id')
        if not user_id:
            return jsonify({'status': 'error', 'error': 'user_id required'}), 400
        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return jsonify({'status': 'error', 'error': 'invalid user_id'}), 400

        init_data = request.headers.get('X-Telegram-Init-Data')
        if init_data:
            auth_user = validate_webapp_data(init_data)
            if auth_user:
                user_id = int(auth_user.get('id'))
            
        state = get_game_session(user_id, 'battleship')
        now = time.time()
        if state and (now - state.get('last_win', 0)) < 10:
            return jsonify({'error': 'Too fast'}), 400
            
        save_game_session(user_id, 'battleship', {'last_win': now})
        add_tokens(user_id, 100, 'battleship_win')
        loot_drop = check_minigame_loot_drop(user_id, 'battleship', base_chance=0.15)
        return jsonify({'status': 'ok', 'loot_drop': loot_drop})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/game/clown', methods=['POST'])
@limiter.limit("120 per minute")
def api_game_clown():
    """Логика Битвы Фишек (Клоун)"""
    try:
        data = request.get_json() or {}
        user_id = data.get('userId') or data.get('user_id')
        if not user_id:
            return jsonify({'status': 'error', 'error': 'user_id required'}), 400
        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return jsonify({'status': 'error', 'error': 'invalid user_id'}), 400

        action = data.get('action', 'attack')
        init_data = request.headers.get('X-Telegram-Init-Data')
        if init_data:
            auth_user = validate_webapp_data(init_data)
            if auth_user:
                user_id = int(auth_user.get('id'))
        
        if action == 'start':
            state = {'pHP': 100, 'pNRG': 0, 'bHP': 100, 'bNRG': 0}
            save_game_session(user_id, 'clown', state)
            return jsonify({'status': 'ok', 'state': state})

        state = get_game_session(user_id, 'clown')
        if not state:
            state = {'pHP': 100, 'pNRG': 0, 'bHP': 100, 'bNRG': 0}
            save_game_session(user_id, 'clown', state)

        # Обработка лечения печенькой (два варианта: 'cookie' и 'cookie_heal')
        if action in ['cookie', 'cookie_heal']:
            spend_result = spend_tokens(user_id, 100, 'cookie_heal')
            if not spend_result:
                return jsonify({'status': 'error', 'error': 'Недостаточно токенов!'}), 400
            state['pHP'] = 100
            save_game_session(user_id, 'clown', state)
            return jsonify({'status': 'ok', 'state': state, 'tokens': spend_result})
            
        dmg, heal, cost = 0, 0, 0
        is_crit = random.random() < 0.2
        
        if action == 'attack':
            dmg = 10
            state['pNRG'] = min(100, state['pNRG'] + 20)
        elif action == 'trash':
            dmg = 25
            cost = 40
        elif action == 'snack':
            heal = 30
            cost = 30
        elif action == 'rage':
            dmg = 50
            cost = 80
            is_crit = True
        
        if state['pNRG'] < cost:
            return jsonify({'status': 'error', 'error': 'Not enough energy'}), 400
        state['pNRG'] -= cost
        if is_crit and dmg > 0:
            dmg = int(dmg * 1.5)
        
        state['bHP'] = max(0, state['bHP'] - dmg)
        state['pHP'] = min(100, state['pHP'] + heal)
        
        player_log = {'dmg': dmg, 'heal': heal, 'is_crit': is_crit, 'action': action, 'pHP': state['pHP'], 'pNRG': state['pNRG'], 'bHP': state['bHP'], 'bNRG': state['bNRG']}
        
        if state['bHP'] <= 0:
            add_tokens(user_id, 10, 'clown_win')
            clear_game_session(user_id, 'clown')
            loot_drop = check_minigame_loot_drop(user_id, 'clown', base_chance=0.12)
            return jsonify({'status': 'ok', 'state': state, 'player_log': player_log, 'game_over': True, 'win': True, 'loot_drop': loot_drop})
            
        b_dmg, b_heal = 0, 0
        b_crit = random.random() < 0.15
        b_action = 'attack'
        
        if state['bNRG'] >= 70:
            b_dmg = 40
            state['bNRG'] -= 70
            b_action = 'bomb'
        elif state['bHP'] < 40 and state['bNRG'] >= 30:
            b_heal = 25
            state['bNRG'] -= 30
            b_action = 'heal'
        else:
            b_dmg = 12
            state['bNRG'] = min(100, state['bNRG'] + 25)
        
        if b_crit and b_dmg > 0:
            b_dmg = int(b_dmg * 1.5)
        state['pHP'] = max(0, state['pHP'] - b_dmg)
        state['bHP'] = min(100, state['bHP'] + b_heal)
        
        bot_log = {'dmg': b_dmg, 'heal': b_heal, 'is_crit': b_crit, 'action': b_action, 'pHP': state['pHP'], 'pNRG': state['pNRG'], 'bHP': state['bHP'], 'bNRG': state['bNRG']}
        game_over = state['pHP'] <= 0
        if game_over:
            clear_game_session(user_id, 'clown')
        else:
            save_game_session(user_id, 'clown', state)
            
        return jsonify({'status': 'ok', 'state': state, 'player_log': player_log, 'bot_log': bot_log, 'game_over': game_over, 'win': False})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/game/tower', methods=['POST'])
@limiter.limit("120 per minute")
def api_game_tower():
    """Логика Башни"""
    try:
        data = request.get_json() or {}
        user_id = data.get('userId') or data.get('user_id')
        if not user_id:
            return jsonify({'status': 'error', 'error': 'user_id required'}), 400
        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return jsonify({'status': 'error', 'error': 'invalid user_id'}), 400

        action = data.get('action', 'attack')
        level = data.get('level', 1)
        
        init_data = request.headers.get('X-Telegram-Init-Data')
        if init_data:
            auth_user = validate_webapp_data(init_data)
            if auth_user:
                user_id = int(auth_user.get('id'))
        
        if action == 'start':
            is_boss = (level % 10 == 0)
            base_hp = 300 if is_boss else random.randint(80, 180)
            base_dmg = 18 if is_boss else random.randint(8, 18)
            scale = (0.2 + (level * 0.08)) if is_boss else (0.15 + (level * 0.1))
            
            current_energy = min(100, max(0, int(data.get('currentEnergy', 0))))
            current_hp = int(data.get('currentHP', 100))
            if current_hp <= 0: current_hp = 100
            
            state = {'level': level, 'pHP': current_hp, 'pNRG': current_energy, 'eHP': int(base_hp * scale), 'eMaxHP': int(base_hp * scale), 'eDmg': max(1, int(base_dmg * scale))}
            save_game_session(user_id, 'tower', state)
            return jsonify({'status': 'ok', 'state': state})
            
        state = get_game_session(user_id, 'tower')
        if not state:
            state = {'level': level, 'pHP': 100, 'pNRG': 0, 'eHP': 100, 'eMaxHP': 100, 'eDmg': 10}
            save_game_session(user_id, 'tower', state)
        
        if action == 'cookie':
            spend_result = spend_tokens(user_id, 100, 'cookie_heal')
            if not spend_result: return jsonify({'status': 'error', 'error': 'Недостаточно токенов!'}), 400
            state['pHP'] = 100
            save_game_session(user_id, 'tower', state)
            return jsonify({'status': 'ok', 'state': state, 'tokens': spend_result})
            
        dmg, heal, cost = 0, 0, 0
        is_crit = random.random() < 0.2
        
        if action == 'attack': dmg = 15; state['pNRG'] = min(100, state['pNRG'] + 20)
        elif action == 'trash': dmg = 30; cost = 40
        elif action == 'snack': heal = 40; cost = 30
        elif action == 'rage': dmg = 80; cost = 80; is_crit = True
        
        if state['pNRG'] < cost: return jsonify({'status': 'error', 'error': 'Not enough energy'}), 400
        state['pNRG'] -= cost
        if is_crit and dmg > 0: dmg = int(dmg * 1.5)
        
        state['eHP'] -= dmg
        state['pHP'] = min(100, state['pHP'] + heal)
        
        player_log = {'dmg': dmg, 'heal': heal, 'is_crit': is_crit, 'action': action, 'pHP': state['pHP'], 'pNRG': state['pNRG'], 'eHP': state['eHP']}
        
        if state['eHP'] <= 0:
            state['pHP'] = min(100, state['pHP'] + 30)
            multiplier = ((state['level'] - 1) // 10) + 1
            add_tokens(user_id, multiplier, f"tower_level:{state['level']}")
            
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE user_stats SET 
                tower_total_levels = tower_total_levels + 1,
                tower_max_level = MAX(tower_max_level, ?)
                WHERE user_id = ?
            ''', (state['level'], user_id))
            conn.commit()
            conn.close()
            
            clear_game_session(user_id, 'tower')
            # Дроп лута (30% на этажах боссов, 8% на обычных этажах)
            is_boss_floor = (state['level'] % 10 == 0)
            loot_drop = check_minigame_loot_drop(user_id, 'tower', base_chance=(0.30 if is_boss_floor else 0.08))
            return jsonify({'status': 'ok', 'state': state, 'player_log': player_log, 'game_over': True, 'win': True, 'loot_drop': loot_drop})
            
        e_dmg = int(state['eDmg'] * random.uniform(0.8, 1.2))
        e_crit = random.random() < 0.1
        if e_crit: e_dmg = int(e_dmg * 1.5)
        state['pHP'] = max(0, state['pHP'] - e_dmg)
        
        bot_log = {'dmg': e_dmg, 'is_crit': e_crit, 'pHP': state['pHP'], 'pNRG': state['pNRG'], 'eHP': state['eHP']}
        game_over = state['pHP'] <= 0
        if game_over: clear_game_session(user_id, 'tower')
        else: save_game_session(user_id, 'tower', state)
            
        return jsonify({'status': 'ok', 'state': state, 'player_log': player_log, 'bot_log': bot_log, 'game_over': game_over, 'win': False})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/game/archive', methods=['POST'])
@limiter.limit("60 per minute")
def api_game_archive():
    """Логика Этажа 11: Главный Архив (Сортировка Хаоса)"""
    try:
        data = request.get_json() or {}
        user_id = data.get('userId') or data.get('user_id')
        if not user_id:
            return jsonify({'status': 'error', 'error': 'user_id required'}), 400
        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return jsonify({'status': 'error', 'error': 'invalid user_id'}), 400

        action = data.get('action')
        
        init_data = request.headers.get('X-Telegram-Init-Data')
        if init_data:
            auth_user = validate_webapp_data(init_data)
            if auth_user:
                user_id = int(auth_user.get('id'))
        
        if action == 'start':
            state = {'score': 0, 'required': 20, 'status': 'playing'}
            save_game_session(user_id, 'archive', state)
            return jsonify({'status': 'ok', 'state': state})
            
        state = get_game_session(user_id, 'archive')
        if not state:
            state = {'score': 0, 'required': 20, 'status': 'playing'}
            save_game_session(user_id, 'archive', state)
        
        if action == 'sort_success':
            state['score'] += 1
            if state['score'] >= state['required']:
                add_tokens(user_id, 150, 'archive_win')
                clear_game_session(user_id, 'archive')
                return jsonify({'status': 'ok', 'win': True, 'item_found': '666-G-ОШ-А'})
            
            save_game_session(user_id, 'archive', state)
            return jsonify({'status': 'ok', 'state': state, 'win': False})
            
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/game/library', methods=['POST'])
@limiter.limit("60 per minute")
def api_game_library():
    """Логика Этажа 10: Библиотека (Стелс-режим офисного планктона)"""
    try:
        data = request.get_json() or {}
        user_id = data.get('userId') or data.get('user_id')
        if not user_id:
            return jsonify({'status': 'error', 'error': 'user_id required'}), 400
        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return jsonify({'status': 'error', 'error': 'invalid user_id'}), 400

        action = data.get('action')
        
        init_data = request.headers.get('X-Telegram-Init-Data')
        if init_data:
            auth_user = validate_webapp_data(init_data)
            if auth_user:
                user_id = int(auth_user.get('id'))
        
        if action == 'start':
            state = {'progress': 0, 'required': 100, 'suspicion': 0, 'max_suspicion': 3}
            save_game_session(user_id, 'library', state)
            return jsonify({'status': 'ok', 'state': state})
            
        state = get_game_session(user_id, 'library')
        if not state: return jsonify({'status': 'error', 'error': 'No active session'}), 400
        
        if action == 'step_success':
            state['progress'] += 10
            if state['progress'] >= state['required']:
                add_tokens(user_id, 200, 'library_win')
                clear_game_session(user_id, 'library')
                return jsonify({'status': 'ok', 'win': True, 'stamp_received': True})
                
            save_game_session(user_id, 'library', state)
            return jsonify({'status': 'ok', 'state': state, 'win': False})
            
        elif action == 'step_fail':
            state['suspicion'] += 1
            if state['suspicion'] >= state['max_suspicion']:
                clear_game_session(user_id, 'library')
                return jsonify({'status': 'ok', 'game_over': True, 'reason': 'Маргарита Эдуардовна вас услышала!'})
            save_game_session(user_id, 'library', state)
            return jsonify({'status': 'ok', 'state': state, 'game_over': False})
            
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/sync', methods=['POST'])
@limiter.limit("30 per minute")
def api_sync():
    """
    Основной endpoint для синхронизации данных

    Принимает:
    - type: 'sync_stats' или 'boss_damage'
    - userId: ID пользователя
    - Данные в зависимости от типа
    """
    try:
        if not request.is_json:
            return jsonify({'error': 'Content-Type must be application/json'}), 400

        data = request.get_json()
        data_type = data.get('type')
        user_id = data.get('userId') or data.get('user_id')

        logger.info(f"📥 API sync: type={data_type}")

        # Криптографическая проверка авторизации для важных действий
        if data_type in ['earn_tokens', 'spend_tokens', 'boss_damage']:
            init_data = request.headers.get('X-Telegram-Init-Data')
            auth_user = validate_webapp_data(init_data)

            if not auth_user:
                security_logger.warning(f"🚨 БЛОКИРОВКА: Запрос {data_type} без валидной подписи Telegram! user_id={user_id}")
                return jsonify({'status': 'error', 'message': 'Unauthorized. Please use Telegram App.'}), 403

            user_id = int(auth_user.get('id'))
            data['userId'] = user_id
            data['user_id'] = user_id

        if data_type == 'sync_stats':
            return handle_sync_stats(data)
        elif data_type == 'boss_damage':
            return handle_boss_damage(data)
        elif data_type == 'earn_tokens':
            return handle_earn_tokens(data)
        elif data_type == 'spend_tokens':
            return handle_spend_tokens(data)
        else:
            logger.warning(f"⚠️ Неизвестный тип синхронизации: {data_type}")
            return jsonify({'status': 'ok'})  # Игнорируем неизвестные типы
            
    except Exception as e:
        logger.error(f"❌ API sync error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


def handle_sync_stats(data: dict):
    """Обработка синхронизации статистики"""
    user_id = data.get('userId') or data.get('user_id')

    if not user_id:
        logger.warning("⚠️ sync_stats без user_id")
        return jsonify({'status': 'error', 'message': 'user_id required'}), 400

    # Валидация user_id
    user_id = validate_integer(user_id, min_val=1, max_val=2**63-1)
    logger.info(f"👤 sync_stats: user_id={user_id}")

    # Проверяем, не забанен ли пользователь
    if is_user_banned(user_id):
        security_logger.warning(f"🚨 BANNED USER: user_id={user_id} попытался синхронизировать данные")
        logger.warning(f"⚠️ Забаненный пользователь попытался синхронизировать данные: user_id={user_id}")
        return jsonify({'status': 'error', 'message': 'User is banned'}), 403

    # Извлекаем данные пользователя (если есть) с санитизацией
    user_data = None
    if 'username' in data or 'first_name' in data:
        user_data = {
            'username': sanitize_string(data.get('username', ''), max_length=64),
            'first_name': sanitize_string(data.get('first_name', ''), max_length=128),
            'last_name': sanitize_string(data.get('last_name', ''), max_length=128)
        }
        logger.info(f"   Данные пользователя: {user_data}")

    # Извлекаем данные статистики с валидацией
    stats_data = {
        'clown_games': validate_integer(data.get('clown_games', 0), min_val=0, max_val=100000),
        'clown_wins': validate_integer(data.get('clown_wins', 0), min_val=0, max_val=100000),
        'vladeos_games': validate_integer(data.get('vladeos_games', 0), min_val=0, max_val=100000),
        'vladeos_wins': validate_integer(data.get('vladeos_wins', 0), min_val=0, max_val=100000),
        'tower_max_level': validate_integer(data.get('tower_max_level', 0), min_val=0, max_val=10000),
        'tower_total_levels': validate_integer(data.get('tower_total_levels', 0), min_val=0, max_val=1000000),
        'roulette_games': validate_integer(data.get('roulette_games', 0), min_val=0, max_val=1000000),
        'roulette_wins': validate_integer(data.get('roulette_wins', 0), min_val=0, max_val=1000000),
        'roulette_cones_won': validate_integer(data.get('roulette_cones_won', 0), min_val=0, max_val=1000000000),
        'roulette_cones_lost': validate_integer(data.get('roulette_cones_lost', 0), min_val=0, max_val=1000000000),
        'raccoon_taps': validate_integer(data.get('raccoon_taps', 0), min_val=0, max_val=1000),
        'quests': validate_list(data.get('quests', []), default=[]),
        'tutorials_seen': validate_list(data.get('tutorials_seen', []), default=[])
    }

    logger.info(f"📊 Данные статистики: {stats_data}")

    if save_user_stats(user_id, stats_data, user_data):
        logger.info(f"✅ sync_stats успешно: user_id={user_id}")
        return jsonify({'status': 'ok'})
    else:
        logger.error(f"❌ sync_stats ошибка: user_id={user_id}")
        return jsonify({'status': 'error', 'message': 'Database error'}), 500


def handle_boss_damage(data: dict):
    """Обработка урона по боссу"""
    user_id = data.get('userId') or data.get('user_id')

    if not user_id:
        logger.warning("⚠️ boss_damage без user_id")
        return jsonify({'status': 'error', 'message': 'user_id required'}), 400

    # Валидация user_id
    user_id = validate_integer(user_id, min_val=1, max_val=2**63-1)

    # Проверяем, не забанен ли пользователь
    if is_user_banned(user_id):
        security_logger.warning(f"🚨 BANNED USER: user_id={user_id} попытался нанести урон боссу")
        logger.warning(f"⚠️ Забаненный пользователь попытался нанести урон: user_id={user_id}")
        return jsonify({'status': 'error', 'message': 'User is banned'}), 403

    try:
        damage = int(data.get('damage', 0))
    except (ValueError, TypeError):
        damage = 0

    if damage <= 0:
        logger.warning(f"⚠️ boss_damage: damage={damage}")
        return jsonify({'status': 'error', 'message': 'damage must be > 0'}), 400

    # Валидация урона
    damage = validate_integer(damage, min_val=1, max_val=10000, default=0)

    # АНТИЧИТ: Максимальный урон от ульты с критом в игре ~800. Берем лимит 3000 с запасом.
    if damage > 3000:
        security_logger.warning(f"🚨 CHEAT ATTEMPT: user_id={user_id} попытался нанести {damage} урона! Обрезано до 3000.")
        logger.warning(f"🚨 АНТИЧИТ: user_id={user_id} попытался нанести {damage} урона! Обрезано до 3000.")
        damage = 3000

    logger.info(f"💥 boss_damage: user_id={user_id}, damage={damage}")

    boss_info = add_boss_damage(user_id, damage)

    # Начисляем шишки: 10 Шишек за каждые 10000 урона
    if boss_info:
        tokens_earned = (damage // 10000) * 10
        if tokens_earned > 0:
            add_tokens(user_id, tokens_earned, f'boss_damage:{damage}')
            logger.info(f"💰 Начислено {tokens_earned} Шишек за урон боссу")

        return jsonify({'status': 'ok', 'boss': boss_info})
    else:
        return jsonify({'status': 'error', 'message': 'Database error'}), 500


def handle_earn_tokens(data: dict):
    """Обработка начисления шишек за победы и квесты"""
    user_id = data.get('userId') or data.get('user_id')

    if not user_id:
        logger.warning("⚠️ earn_tokens без user_id")
        return jsonify({'status': 'error', 'message': 'user_id required'}), 400

    user_id = int(user_id)

    # Проверяем, не забанен ли пользователь
    if is_user_banned(user_id):
        logger.warning(f"⚠️ Забаненный пользователь попытался получить шишки: user_id={user_id}")
        return jsonify({'status': 'error', 'message': 'User is banned'}), 403

    amount = data.get('amount', 0)
    reason = data.get('reason', 'unknown')

    try:
        amount = int(amount)
    except (ValueError, TypeError):
        logger.warning(f"⚠️ earn_tokens: invalid amount={amount}")
        return jsonify({'status': 'error', 'message': 'amount must be integer'}), 400

    if amount <= 0:
        logger.warning(f"⚠️ earn_tokens: amount={amount}")
        return jsonify({'status': 'error', 'message': 'amount must be > 0'}), 400

    # АНТИЧИТ: Запрет прямого начисления для игр со своей серверной логикой
    if reason.startswith(('clown_win', 'vladeos_win', 'battleship_win')):
        logger.warning(f"🚨 АНТИЧИТ: user_id={user_id} попытался напрямую начислить шишки за {reason} в обход логики игры!")
        return jsonify({'status': 'error', 'message': 'Invalid reward channel. Use game endpoint.'}), 403

    # АНТИЧИТ: Жесткие лимиты наград в зависимости от причины
    max_allowed = 1000  # Глобальный лимит для неизвестных причин
    if reason.startswith('tower_level:'):
        max_allowed = 100
    elif reason.startswith('read_news:') or reason == 'welcome_bonus':
        max_allowed = 1000  # welcome_bonus = 1000
    elif reason.startswith('quest_complete:'):
        max_allowed = 10000
    elif reason == 'season_2_complete':
        max_allowed = 50000
    elif reason.startswith('find_chip_win'):
        max_allowed = 100
        # Античит: Кулдаун для 'Найди Фишку' (минимум 3 сек между победами)
        fc_state = get_game_session(user_id, 'find_chip')
        now_ts = time.time()
        if fc_state and (now_ts - fc_state.get('last_win', 0)) < 3.0:
            logger.warning(f"🚨 АНТИЧИТ: user_id={user_id} слишком быстро победил в 'Найди Фишку' (<3s)!")
            return jsonify({'status': 'error', 'message': 'Too fast'}), 400
        save_game_session(user_id, 'find_chip', {'last_win': now_ts})
    elif reason.startswith('raccoon_tap'):
        max_allowed = 1000

    if amount > max_allowed:
        logger.warning(f"🚨 АНТИЧИТ: user_id={user_id} запросил {amount} токенов за {reason}. Ограничено до {max_allowed}!")
        amount = max_allowed
        
    # АНТИЧИТ: Строгая проверка через БД на одноразовые награды
    if reason.startswith('quest_complete:') or reason in ['season_2_complete', 'welcome_bonus']:
        quest_id = reason.split(':', 1)[1] if ':' in reason else reason
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('SELECT quests FROM user_stats WHERE user_id = ?', (user_id,))
            row = cursor.fetchone()
            existing_quests = []
            if row and row['quests']:
                try:
                    existing_quests = json.loads(row['quests'])
                except:
                    pass
            
            if quest_id in existing_quests:
                logger.warning(f"🚨 АНТИЧИТ: Игрок {user_id} пытается повторно получить награду за {quest_id}!")
                return jsonify({'status': 'error', 'message': 'Reward already claimed'}), 400
            
            # Обратная совместимость для старых игроков (welcome_bonus)
            if quest_id == 'welcome_bonus':
                cursor.execute('SELECT total_earned FROM user_tokens WHERE user_id = ?', (user_id,))
                t_row = cursor.fetchone()
                if t_row and t_row[0] >= 1000:
                    existing_quests.append(quest_id)
                    cursor.execute('UPDATE user_stats SET quests = ? WHERE user_id = ?', (json.dumps(existing_quests), user_id))
                    conn.commit()
                    return jsonify({'status': 'error', 'message': 'Reward already claimed (legacy)'}), 400

            # Сразу помечаем квест (или бонус) как выполненный, чтобы заблокировать абуз
            existing_quests.append(quest_id)
            
            if quest_id == 'welcome_bonus':
                cursor.execute('UPDATE user_stats SET quests = ? WHERE user_id = ?', (json.dumps(existing_quests), user_id))
            else:
                # Пересчитываем реальные квесты
                actual_count = len([q for q in existing_quests if q.startswith('qt')])
                
                cursor.execute('''UPDATE user_stats SET quests = ?, quests_completed = ?, last_quest_time = CURRENT_TIMESTAMP WHERE user_id = ?''', 
                               (json.dumps(existing_quests), actual_count, user_id))
            conn.commit()
        except Exception as e:
            logger.error(f"Ошибка проверки квеста: {e}")
        finally:
            conn.close()

    # АНТИЧИТ: Строгий учет тапов енота против автокликеров (макс. 1000 в сутки)
    if reason.startswith('raccoon_tap'):
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT COALESCE(raccoon_taps, 0) FROM user_stats WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            current_taps = row[0] if row else 0

            if current_taps >= 1000:
                logger.warning(f"🚨 АНТИЧИТ: user_id={user_id} исчерпал суточный лимит тапов ({current_taps}/1000)!")
                return jsonify({'status': 'error', 'message': 'Daily tap limit reached (1000/1000)'}), 400

            # Начисляем только остаток до 1000
            allowed_taps = min(amount, 1000 - current_taps)
            amount = allowed_taps
            cursor.execute("UPDATE user_stats SET raccoon_taps = raccoon_taps + ? WHERE user_id = ?", (amount, user_id))
            conn.commit()
        except Exception as e:
            logger.error(f"Ошибка обновления raccoon_taps в user_stats: {e}")
            return jsonify({'status': 'error', 'message': 'Database error'}), 500
        finally:
            conn.close()

    logger.info(f"💰 earn_tokens: user_id={user_id}, amount={amount}, reason={reason}")

    result = add_tokens(user_id, amount, reason)

    if result:
        loot_drop = None
        if reason.startswith('find_chip_win') or reason.startswith('clown_win') or reason.startswith('battleship_win') or reason.startswith('vladeos_win'):
            loot_drop = check_minigame_loot_drop(user_id, reason, base_chance=0.12)
        return jsonify({'status': 'ok', 'tokens': result, 'loot_drop': loot_drop})
    else:
        return jsonify({'status': 'error', 'message': 'Database error'}), 500


def handle_spend_tokens(data: dict):
    """Обработка списания шишек"""
    user_id = data.get('userId') or data.get('user_id')

    if not user_id:
        logger.warning("⚠️ spend_tokens без user_id")
        return jsonify({'status': 'error', 'message': 'user_id required'}), 400

    user_id = int(user_id)

    # Проверяем, не забанен ли пользователь
    if is_user_banned(user_id):
        logger.warning(f"⚠️ Забаненный пользователь попытался потратить шишки: user_id={user_id}")
        return jsonify({'status': 'error', 'message': 'User is banned'}), 403

    amount = data.get('amount', 0)
    reason = data.get('reason', 'unknown')

    try:
        amount = int(amount)
    except (ValueError, TypeError):
        logger.warning(f"⚠️ spend_tokens: invalid amount={amount}")
        return jsonify({'status': 'error', 'message': 'amount must be integer'}), 400

    if amount <= 0:
        logger.warning(f"⚠️ spend_tokens: amount={amount}")
        return jsonify({'status': 'error', 'message': 'amount must be > 0'}), 400

    logger.info(f"💸 spend_tokens: user_id={user_id}, amount={amount}, reason={reason}")

    result = spend_tokens(user_id, amount, reason)

    if result:
        return jsonify({'status': 'ok', 'tokens': result})
    else:
        return jsonify({'status': 'error', 'message': 'Insufficient tokens'}), 400


@app.route('/api/announcement_image/<path:file_id>')
def api_announcement_image(file_id):
    """
    Прокси для получения изображения объявления по его file_id.
    Делает запрос к Telegram API второго бота и редиректит на URL файла.
    """
    if not SALEBOT_TOKEN:
        logger.error("🚨 SALEBOT_TOKEN не настроен! Не могу получить изображение.")
        return "Server configuration error: SALEBOT_TOKEN is not set.", 500

    try:
        # 1. Получаем информацию о файле от Telegram
        get_file_url = f"https://api.telegram.org/bot{SALEBOT_TOKEN}/getFile"
        response = requests.get(get_file_url, params={'file_id': file_id}, timeout=5)
        response.raise_for_status()
        
        file_data = response.json()
        if not file_data.get('ok'):
            logger.error(f"❌ Telegram API (getFile) error: {file_data.get('description')}")
            return "File not found on Telegram", 404
            
        file_path = file_data['result']['file_path']
        
        # 2. Формируем URL для скачивания и делаем редирект
        image_url = f"https://api.telegram.org/file/bot{SALEBOT_TOKEN}/{file_path}"
        
        # Редирект пользователя на прямой URL файла в Telegram
        return redirect(image_url, code=302)

    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Ошибка сети при получении файла объявления: {e}")
        return "Network error while fetching image", 503
    except Exception as e:
        logger.error(f"❌ Ошибка в api_announcement_image: {e}")
        return "Internal server error", 500


@app.route('/api/announcements', methods=['GET'])
def api_get_announcements():
    """
    Получает список объявлений из базы данных второго бота (salebot).
    """
    user_id = request.args.get('userId', 0, type=int)
    is_admin = (user_id == ADMIN_ID)

    sale_conn = get_salebot_db_connection()
    if not sale_conn:
        return jsonify({'status': 'ok', 'announcements': [], 'is_admin': is_admin, 'message': 'Сервер объявлений временно недоступен'})

    # Получаем ID скрытых объявлений из локальной БД
    hidden_ids = set()
    local_conn = get_db_connection()
    try:
        local_cursor = local_conn.cursor()
        local_cursor.execute("SELECT announcement_id FROM hidden_announcements")
        hidden_ids = {row['announcement_id'] for row in local_cursor.fetchall()}
    except Exception as e:
        logger.error(f"❌ Ошибка получения скрытых объявлений: {e}")
    finally:
        local_conn.close()

    try:
        cursor = sale_conn.cursor()
        cursor.execute("""
            SELECT id, caption, photo_file_id 
            FROM announcements 
            WHERE status = 'approved' 
            ORDER BY approved_at DESC 
            LIMIT 100
        """)
        rows = cursor.fetchall()
        
        announcements = []
        for row in rows:
            # Отфильтровываем скрытые объявления
            if row['id'] in hidden_ids:
                continue

            caption_lines = row['caption'].split('\n', 1)
            title = caption_lines[0].strip()
            description = caption_lines[1].strip() if len(caption_lines) > 1 else ''
            image_url = f"/api/announcement_image/{row['photo_file_id']}" if row['photo_file_id'] else None
            
            announcements.append({
                "id": row['id'],
                "title": title,
                "description": description,
                "image_url": image_url
            })
            
        return jsonify({'status': 'ok', 'announcements': announcements, 'is_admin': is_admin})

    except Exception as e:
        logger.error(f"❌ Ошибка в api_get_announcements: {e}")
        return jsonify({'status': 'error', 'error': 'Внутренняя ошибка сервера.'}), 500
    finally:
        if sale_conn:
            sale_conn.close()


@app.route('/api/admin/announcements/hide', methods=['POST'])
def api_admin_hide_announcement():
    """Скрывает объявление из магазина (только для админа)"""
    try:
        if not request.is_json:
            return jsonify({'error': 'JSON required'}), 400
        
        data = request.get_json()
        user_id = int(data.get('userId', 0))
        announcement_id = int(data.get('announcementId', 0))

        if user_id != ADMIN_ID:
            security_logger.warning(f"🚨 UNAUTHORIZED HIDE ATTEMPT: user_id={user_id} tried to hide announcement_id={announcement_id}")
            return jsonify({'error': 'Unauthorized'}), 403

        if not announcement_id:
            return jsonify({'error': 'announcementId is required'}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT OR IGNORE INTO hidden_announcements (announcement_id) VALUES (?)", (announcement_id,))
            conn.commit()
            logger.info(f"🙈 Объявление #{announcement_id} скрыто админом {user_id}")
            return jsonify({'status': 'ok'})
        finally:
            conn.close()

    except Exception as e:
        logger.error(f"❌ Ошибка в api_admin_hide_announcement: {e}")
        return jsonify({'error': str(e)}), 500

# ==================== СОВМЕСТНЫЙ КРАФТ (COOP CRAFT) ====================

@app.route('/api/craft/create', methods=['POST'])
@limiter.limit("10 per minute")
def api_craft_create():
    """
    Инициация совместного крафта.
    Ожидает: userId, itemName, startGrade, targetGrade, stages (список объектов {from, to, material})
    """
    try:
        if not request.is_json:
            return jsonify({'error': 'JSON required'}), 400

        data = request.get_json()
        user_id = int(data.get('userId', 0))
        
        auth_user = validate_webapp_data(request.headers.get('X-Telegram-Init-Data'))
        if not auth_user or str(auth_user.get('id')) != str(user_id):
            return jsonify({'error': 'Unauthorized'}), 403

        item_name = sanitize_string(data.get('itemName', 'Неизвестная фишка'))
        start_grade = sanitize_string(data.get('startGrade', 'Common'))
        target_grade = sanitize_string(data.get('targetGrade', 'Diamond'))
        is_private = 1 if data.get('isPrivate') else 0
        stages = data.get('stages', [])

        if not stages:
            return jsonify({'error': 'No stages provided'}), 400

        ensure_user_exists(user_id, auth_user)

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            # Создаем запись о крафте
            cursor.execute('''
                INSERT INTO coop_crafts (initiator_id, item_name, start_grade, target_grade, is_private)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, item_name, start_grade, target_grade, is_private))
            
            craft_id = cursor.lastrowid
            
            # Добавляем этапы
            for idx, stage in enumerate(stages):
                from_grade = sanitize_string(stage.get('from', ''))
                to_grade = sanitize_string(stage.get('to', ''))
                material_req = sanitize_string(stage.get('material', ''))
                reward_amount = stage.get('rewardAmount', 0)
                try:
                    reward_amount = float(reward_amount)
                except (ValueError, TypeError):
                    reward_amount = 0.0
                reward_currency = sanitize_string(stage.get('rewardCurrency', 'TON'))
                
                cursor.execute('''
                    INSERT INTO coop_craft_stages (craft_id, stage_index, from_grade, to_grade, material_req, reward_amount, reward_currency)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (craft_id, idx, from_grade, to_grade, material_req, reward_amount, reward_currency))
                
            conn.commit()
            logger.info(f"🛠️ Создан крафт #{craft_id} игроком {user_id} ({item_name}: {start_grade} -> {target_grade})")

            # Отправляем уведомление в группу, если крафт публичный
            if not is_private:
                try:
                    initiator_name = auth_user.get('first_name', f"Игрок {user_id}")
                    if auth_user.get('username'):
                        initiator_name = f"@{auth_user.get('username')}"
                    
                    # Sanitize for HTML
                    initiator_name_safe = sanitize_string(initiator_name)
                    item_name_safe = sanitize_string(item_name)
                    start_grade_safe = sanitize_string(start_grade)
                    target_grade_safe = sanitize_string(target_grade)

                    message_text = (
                        f"🛠️ <b>Новый публичный крафт на доске!</b>\n\n"
                        f"<b>Инициатор:</b> {initiator_name_safe}\n"
                        f"<b>Предмет:</b> {item_name_safe}\n"
                        f"<b>Цель:</b> {start_grade_safe} ➔ {target_grade_safe}\n\n"
                        f"Присоединяйтесь и помогайте в создании редких фишек!"
                    )

                    # В группах надежнее использовать обычную ссылку (deep link), а не web_app кнопку
                    bot_app_url = "https://t.me/Raccoon_Life_bot/app"
                    deep_link = f"{bot_app_url}?startapp=craft_{craft_id}"
                    keyboard = [[{"text": "🤝 Присоединиться", "url": deep_link}]]

                    response = requests.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                        json={
                            "chat_id": "@the_raccoon_times_group",
                            "message_thread_id": 3552,
                            "text": message_text, 
                            "parse_mode": "HTML", 
                            "reply_markup": {"inline_keyboard": keyboard} if keyboard else None
                        },
                        timeout=10
                    )
                    
                    if response.status_code != 200:
                        logger.error(f"❌ Ошибка Telegram API при отправке в группу: {response.text}")
                    else:
                        logger.info(f"📢 Отправлено уведомление о новом крафте #{craft_id} в группу.")
                except Exception as e:
                    logger.error(f"❌ Ошибка отправки уведомления о крафте: {e}")

            return jsonify({'status': 'ok', 'craft_id': craft_id})
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Ошибка api_craft_create: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/craft/active', methods=['GET'])
def api_craft_active():
    """Получение списка активных крафтов (для присоединения других игроков)"""
    user_id = request.args.get('userId', 0)
    try:
        user_id = int(user_id)
    except (ValueError, TypeError):
        user_id = 0
        
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if user_id == ADMIN_ID:
            # Админ видит все активные крафты (включая чужие приватные)
            cursor.execute('''
                SELECT c.craft_id, c.initiator_id, c.item_name, c.start_grade, c.target_grade, c.status, c.created_at,
                       u.username, u.first_name
                FROM coop_crafts c
                LEFT JOIN users u ON c.initiator_id = u.user_id
                WHERE c.status IN ('open', 'in_progress')
                ORDER BY c.created_at DESC LIMIT 50
            ''')
        else:
            # Обычный игрок видит публичные крафты ЛИБО свои приватные
            cursor.execute('''
                SELECT c.craft_id, c.initiator_id, c.item_name, c.start_grade, c.target_grade, c.status, c.created_at,
                       u.username, u.first_name
                FROM coop_crafts c
                LEFT JOIN users u ON c.initiator_id = u.user_id
                WHERE c.status IN ('open', 'in_progress') AND (c.is_private = 0 OR c.initiator_id = ?)
                ORDER BY c.created_at DESC LIMIT 50
            ''', (user_id,))
        crafts_rows = cursor.fetchall()
        
        crafts = []
        for row in crafts_rows:
            craft = dict(row)
            craft['initiator_username'] = craft.get('username')
            craft['can_delete'] = (user_id == craft['initiator_id'] or user_id == ADMIN_ID)
            craft['initiator_name'] = craft.pop('username') or craft.pop('first_name') or f"Игрок {craft['initiator_id']}"
            
            # Получаем этапы для каждого крафта
            cursor.execute('''
                SELECT 
                    s.stage_id, s.stage_index, s.from_grade, s.to_grade, s.material_req, s.status, s.reward_amount, s.reward_currency,
                    s.item_contributor_id, s.gum_contributor_id,
                    item_user.username as item_username, item_user.first_name as item_first_name,
                    gum_user.username as gum_username, gum_user.first_name as gum_first_name
                FROM coop_craft_stages s
                LEFT JOIN users item_user ON s.item_contributor_id = item_user.user_id
                LEFT JOIN users gum_user ON s.gum_contributor_id = gum_user.user_id
                WHERE s.craft_id = ?
                ORDER BY s.stage_index ASC
            ''', (craft['craft_id'],))
            
            stages = []
            for s_row in cursor.fetchall():
                stage = dict(s_row)
                stage['item_contributor_username'] = stage.get('item_username')
                if stage['item_contributor_id']:
                    stage['item_contributor_name'] = stage.pop('item_username') or stage.pop('item_first_name') or f"Игрок {stage['item_contributor_id']}"
                else:
                    stage['item_contributor_name'] = None
                    stage.pop('item_username', None)
                    stage.pop('item_first_name', None)
                
                stage['gum_contributor_username'] = stage.get('gum_username')
                if stage['gum_contributor_id']:
                    stage['gum_contributor_name'] = stage.pop('gum_username') or stage.pop('gum_first_name') or f"Игрок {stage['gum_contributor_id']}"
                else:
                    stage['gum_contributor_name'] = None
                    stage.pop('gum_username', None)
                    stage.pop('gum_first_name', None)

                stages.append(stage)
                
            craft['stages'] = stages
            crafts.append(craft)
            
        return jsonify({'status': 'ok', 'crafts': crafts})
    except Exception as e:
        logger.error(f"Ошибка api_craft_active: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/craft/get', methods=['GET'])
def api_craft_get():
    """Получение конкретного крафта по ID (для перехода по приватной ссылке)"""
    user_id = request.args.get('userId', 0)
    craft_id = request.args.get('craftId', 0)
    try:
        craft_id = int(craft_id)
        user_id = int(user_id)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid ID'}), 400
        
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            SELECT c.craft_id, c.initiator_id, c.item_name, c.start_grade, c.target_grade, c.status, c.created_at,
                   u.username, u.first_name
            FROM coop_crafts c
            LEFT JOIN users u ON c.initiator_id = u.user_id
            WHERE c.craft_id = ?
        ''', (craft_id,))
        row = cursor.fetchone()
        
        if not row:
            return jsonify({'error': 'Craft not found'}), 404
            
        craft = dict(row)
        craft['initiator_username'] = craft.get('username')
        craft['can_delete'] = (user_id == craft['initiator_id'] or user_id == ADMIN_ID)
        craft['initiator_name'] = craft.pop('username') or craft.pop('first_name') or f"Игрок {craft['initiator_id']}"
        
        cursor.execute('''
            SELECT 
                s.stage_id, s.stage_index, s.from_grade, s.to_grade, s.material_req, s.status, s.reward_amount, s.reward_currency,
                s.item_contributor_id, s.gum_contributor_id,
                item_user.username as item_username, item_user.first_name as item_first_name,
                gum_user.username as gum_username, gum_user.first_name as gum_first_name
            FROM coop_craft_stages s
            LEFT JOIN users item_user ON s.item_contributor_id = item_user.user_id
            LEFT JOIN users gum_user ON s.gum_contributor_id = gum_user.user_id
            WHERE s.craft_id = ?
            ORDER BY s.stage_index ASC
        ''', (craft_id,))
        
        stages = []
        for s_row in cursor.fetchall():
            stage = dict(s_row)
            stage['item_contributor_username'] = stage.get('item_username')
            if stage['item_contributor_id']:
                stage['item_contributor_name'] = stage.pop('item_username') or stage.pop('item_first_name') or f"Игрок {stage['item_contributor_id']}"
            else:
                stage['item_contributor_name'] = None
            
            stage['gum_contributor_username'] = stage.get('gum_username')
            if stage['gum_contributor_id']:
                stage['gum_contributor_name'] = stage.pop('gum_username') or stage.pop('gum_first_name') or f"Игрок {stage['gum_contributor_id']}"
            else:
                stage['gum_contributor_name'] = None
            stages.append(stage)
            
        craft['stages'] = stages
        return jsonify({'status': 'ok', 'crafts': [craft]}) # Возвращаем массивом для унификации с фронтендом
    finally:
        conn.close()

@app.route('/api/craft/pledge', methods=['POST'])
@limiter.limit("20 per minute")
def api_craft_pledge():
    """Игрок вызывается предоставить материалы для этапа (pledge)"""
    try:
        data = request.get_json()
        user_id = int(data.get('userId', 0))
        stage_id = int(data.get('stageId', 0))
        pledge_type = sanitize_string(data.get('pledgeType', 'all')) # 'items', 'gum', или 'all'
        
        auth_user = validate_webapp_data(request.headers.get('X-Telegram-Init-Data'))
        if not auth_user or str(auth_user.get('id')) != str(user_id):
            return jsonify({'error': 'Unauthorized'}), 403

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            # Проверяем, свободен ли этап
            cursor.execute('''
                SELECT s.status, s.item_contributor_id, s.gum_contributor_id, s.craft_id, s.material_req, s.stage_index,
                       c.initiator_id, c.item_name
                FROM coop_craft_stages s
                JOIN coop_crafts c ON s.craft_id = c.craft_id
                WHERE s.stage_id = ?
            ''', (stage_id,))
            row = cursor.fetchone()
            if not row:
                return jsonify({'error': 'Stage not found'}), 404

            # Проверяем, что можно взять на себя
            if pledge_type == 'items' and row['item_contributor_id'] is not None:
                return jsonify({'error': 'Фишки для этого этапа уже предоставляет другой игрок.'}), 400
            if pledge_type == 'gum' and row['gum_contributor_id'] is not None:
                return jsonify({'error': '$GUM для этого этапа уже предоставляет другой игрок.'}), 400
            if pledge_type == 'all' and (row['item_contributor_id'] is not None or row['gum_contributor_id'] is not None):
                return jsonify({'error': 'Часть этого этапа уже занята другим игроком.'}), 400
                
            craft_id = row['craft_id']
            initiator_id = row['initiator_id']
            item_name = row['item_name']
            stage_index = row['stage_index']
            
            # Назначаем игрока
            ensure_user_exists(user_id, auth_user)
            if pledge_type == 'items':
                cursor.execute("UPDATE coop_craft_stages SET item_contributor_id = ? WHERE stage_id = ?", (user_id, stage_id))
            elif pledge_type == 'gum':
                cursor.execute("UPDATE coop_craft_stages SET gum_contributor_id = ? WHERE stage_id = ?", (user_id, stage_id))
            elif pledge_type == 'all':
                cursor.execute("UPDATE coop_craft_stages SET item_contributor_id = ?, gum_contributor_id = ? WHERE stage_id = ?", (user_id, user_id, stage_id))
            else:
                return jsonify({'error': 'Invalid pledge type'}), 400

            # Проверяем, нужно ли обновить статус этапа
            needs_items = 'фишк' in row['material_req']
            needs_gum = '$GUM' in row['material_req']
            
            # Получаем обновленные данные
            cursor.execute('SELECT item_contributor_id, gum_contributor_id FROM coop_craft_stages WHERE stage_id = ?', (stage_id,))
            updated_row = cursor.fetchone()
            
            items_now_pledged = updated_row['item_contributor_id'] is not None
            gum_now_pledged = updated_row['gum_contributor_id'] is not None

            if (not needs_items or items_now_pledged) and (not needs_gum or gum_now_pledged):
                cursor.execute("UPDATE coop_craft_stages SET status = 'pledged' WHERE stage_id = ?", (stage_id,))

            # Проверяем, заполнились ли все этапы крафта. Если да - переводим крафт в in_progress
            cursor.execute("SELECT COUNT(*) FROM coop_craft_stages WHERE craft_id = ? AND status = 'pending'", (craft_id,))
            if cursor.fetchone()[0] == 0:
                cursor.execute("UPDATE coop_crafts SET status = 'in_progress' WHERE craft_id = ?", (craft_id,))
                
            conn.commit()
            logger.info(f"🤝 Игрок {user_id} взялся за '{pledge_type}' для этапа #{stage_id} (Крафт #{craft_id})")
            
            # Отправка уведомления создателю крафта (если это не он сам берет этап)
            if initiator_id != user_id:
                try:
                    contributor_name = auth_user.get('first_name', f"Игрок {user_id}")
                    if auth_user.get('username'):
                        contributor_name = f"@{auth_user.get('username')}"
                    
                    safe_item = sanitize_string(item_name).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    safe_contributor = sanitize_string(contributor_name).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    
                    pledge_str = 'предоставит фишки' if pledge_type == 'items' else 'предоставит $GUM' if pledge_type == 'gum' else 'предоставит все ресурсы'

                    msg_text = (
                        f"🤝 <b>Отличные новости!</b>\n\n"
                        f"Пользователь <b>{safe_contributor}</b> вызвался помочь вам с крафтом <b>{safe_item}</b>\n"
                        f"<i>(Этап {stage_index + 1}: {pledge_str})</i>\n\n"
                        f"Зайдите в игру, чтобы проверить статус!"
                    )
                    
                    bot_app_url = "https://t.me/Raccoon_Life_bot/app"
                    deep_link = f"{bot_app_url}?startapp=craft_{craft_id}"
                    keyboard = [[{"text": "🛠 Открыть крафт", "url": deep_link}]]

                    requests.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                        json={"chat_id": initiator_id, "text": msg_text, "parse_mode": "HTML", "reply_markup": {"inline_keyboard": keyboard}},
                        timeout=5
                    )
                    logger.info(f"📬 Уведомление о помощнике отправлено создателю {initiator_id}")
                except Exception as e:
                    logger.error(f"❌ Ошибка отправки уведомления инициатору крафта: {e}")

            return jsonify({'status': 'ok'})
        finally:
            conn.close()
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/craft/complete_stage', methods=['POST'])
@limiter.limit("20 per minute")
def api_craft_complete_stage():
    """Отметить этап как выполненный"""
    try:
        data = request.get_json()
        user_id = int(data.get('userId', 0))
        stage_id = int(data.get('stageId', 0))
        
        auth_user = validate_webapp_data(request.headers.get('X-Telegram-Init-Data'))
        if not auth_user or str(auth_user.get('id')) != str(user_id):
            return jsonify({'error': 'Unauthorized'}), 403

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            # Получаем информацию об этапе и крафте
            cursor.execute('''
                SELECT s.status, s.craft_id, c.initiator_id, s.stage_index
                FROM coop_craft_stages s
                JOIN coop_crafts c ON s.craft_id = c.craft_id
                WHERE s.stage_id = ?
            ''', (stage_id,))
            row = cursor.fetchone()
            
            if not row:
                return jsonify({'error': 'Stage not found'}), 404
                
            # Только инициатор крафта может отметить этап выполненным
            if user_id != row['initiator_id']:
                return jsonify({'error': 'Permission denied'}), 403
                
            if row['status'] == 'completed':
                return jsonify({'error': 'Stage already completed'}), 400
                
            craft_id = row['craft_id']
            stage_index = row['stage_index']
            
            # Проверяем, завершены ли все предыдущие этапы
            cursor.execute('''
                SELECT COUNT(*) FROM coop_craft_stages 
                WHERE craft_id = ? AND stage_index < ? AND status != 'completed'
            ''', (craft_id, stage_index))
            if cursor.fetchone()[0] > 0:
                return jsonify({'error': 'Сначала необходимо завершить предыдущие этапы!'}), 400

            # Обновляем статус этапа
            cursor.execute("UPDATE coop_craft_stages SET status = 'completed' WHERE stage_id = ?", (stage_id,))
            
            # Проверяем завершение всего крафта
            cursor.execute("SELECT COUNT(*) FROM coop_craft_stages WHERE craft_id = ? AND status != 'completed'", (craft_id,))
            if cursor.fetchone()[0] == 0:
                cursor.execute("UPDATE coop_crafts SET status = 'completed' WHERE craft_id = ?", (craft_id,))
                logger.info(f"🏆 Крафт #{craft_id} успешно ЗАВЕРШЕН!")
                
            conn.commit()
            return jsonify({'status': 'ok'})
        finally:
            conn.close()
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/craft/delete', methods=['POST'])
@limiter.limit("20 per minute")
def api_craft_delete():
    """Удаление объявления о крафте (создателем или админом)"""
    try:
        data = request.get_json()
        user_id = int(data.get('userId', 0))
        craft_id = int(data.get('craftId', 0))
        
        auth_user = validate_webapp_data(request.headers.get('X-Telegram-Init-Data'))
        if not auth_user or str(auth_user.get('id')) != str(user_id):
            return jsonify({'error': 'Unauthorized'}), 403

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            # Получаем информацию о крафте
            cursor.execute('SELECT initiator_id FROM coop_crafts WHERE craft_id = ?', (craft_id,))
            row = cursor.fetchone()
            
            if not row:
                return jsonify({'error': 'Craft not found'}), 404
                
            # Только инициатор крафта или админ могут удалить
            if user_id != row['initiator_id'] and user_id != ADMIN_ID:
                return jsonify({'error': 'Permission denied'}), 403
            
            # Удаляем сначала этапы, потом сам крафт
            cursor.execute("DELETE FROM coop_craft_stages WHERE craft_id = ?", (craft_id,))
            cursor.execute("DELETE FROM coop_crafts WHERE craft_id = ?", (craft_id,))
            
            conn.commit()
            logger.info(f"🗑️ Крафт #{craft_id} удален пользователем {user_id}")
            return jsonify({'status': 'ok'})
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Ошибка api_craft_delete: {e}")
        return jsonify({'error': str(e)}), 500

# ==================== TOTALIZATOR API ====================

@app.route('/api/tot/events', methods=['GET'])
def api_tot_events():
    """Список активных событий для пользователей"""
    conn = None
    try:
        user_id = request.args.get('userId', 0)
        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            user_id = 0
            
        is_admin = (user_id == ADMIN_ID)

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tot_events WHERE status IN ('active', 'locked') ORDER BY event_id DESC")
        events = [dict(row) for row in cursor.fetchall()]
        
        current_time = get_moscow_now()
        changed = False
        for e in events:
            if e['status'] == 'active' and e['start_time']:
                try:
                    start_dt = parse_moscow_datetime(e['start_time'])
                    if start_dt and current_time >= start_dt:
                        cursor.execute("SELECT user_id, amount, currency FROM tot_bets WHERE event_id = ? AND status = 'pending'", (e['event_id'],))
                        for b in cursor.fetchall():
                            if b['currency'] == 'Шишки':
                                add_tokens(b['user_id'], int(b['amount']), f"tot_refund:{e['event_id']}")
                        cursor.execute("UPDATE tot_events SET status = 'locked' WHERE event_id = ?", (e['event_id'],))
                        cursor.execute("DELETE FROM tot_bets WHERE event_id = ? AND status = 'pending'", (e['event_id'],))
                        e['status'] = 'locked'
                        changed = True
                except Exception:
                    pass
        if changed:
            conn.commit()
            
        return jsonify({'status': 'ok', 'events': events, 'is_admin': is_admin})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/tot/bet', methods=['POST'])
@limiter.limit("10 per minute")
def api_tot_bet():
    """Сделать ставку"""
    conn = None
    try:
        if not request.is_json:
            return jsonify({'error': 'JSON required'}), 400
        data = request.get_json()
        user_id = int(data.get('userId', 0))
        event_id = int(data.get('eventId', 0))
        side = int(data.get('side', 1))
        amount = int(data.get('amount', 0))
        currency = data.get('currency', 'CG')

        auth_user = validate_webapp_data(request.headers.get('X-Telegram-Init-Data'))
        if not auth_user or str(auth_user.get('id')) != str(user_id):
            return jsonify({'error': 'Unauthorized'}), 403

        ensure_user_exists(user_id, auth_user)

        if amount <= 0:
            return jsonify({'error': 'Сумма должна быть больше 0'}), 400

        prediction = data.get('prediction') or data.get('score')
        if event_id <= 0:
            return jsonify({'error': 'eventId required'}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT status, start_time, title, side1_name, side2_name, draw_name, event_type, exact_score_odds FROM tot_events WHERE event_id = ?", (event_id,))
        event = cursor.fetchone()
        if not event:
            return jsonify({'error': 'Событие не существует'}), 400
            
        status = event['status']
        if status == 'active' and event['start_time']:
            try:
                start_dt = parse_moscow_datetime(event['start_time'])
                if start_dt and get_moscow_now() >= start_dt:
                    cursor.execute("SELECT user_id, amount, currency FROM tot_bets WHERE event_id = ? AND status = 'pending'", (event_id,))
                    for b in cursor.fetchall():
                        if b['currency'] == 'Шишки':
                            add_tokens(b['user_id'], int(b['amount']), f"tot_refund:{event_id}")
                    cursor.execute("UPDATE tot_events SET status = 'locked' WHERE event_id = ?", (event_id,))
                    cursor.execute("DELETE FROM tot_bets WHERE event_id = ? AND status = 'pending'", (event_id,))
                    conn.commit()
                    status = 'locked'
            except Exception:
                pass
                
        if status != 'active':
            return jsonify({'error': 'Событие неактивно или время ставок истекло'}), 400

        if event['event_type'] == 'exact_score':
            if not isinstance(prediction, str):
                return jsonify({'error': 'prediction required'}), 400
            prediction = prediction.strip().replace(' ', '')
            if not prediction or not (prediction.count(':') == 1 and prediction.split(':')[0].isdigit() and prediction.split(':')[1].isdigit()):
                return jsonify({'error': 'Введите счёт в формате 2:1'}), 400
        else:
            prediction = None

        cursor.execute("SELECT 1 FROM tot_bets WHERE event_id = ? AND user_id = ? AND status IN ('pending', 'accepted') LIMIT 1", (event_id, user_id))
        existing_bet = cursor.fetchone()
        if existing_bet:
            return jsonify({'error': 'У вас уже есть активная ставка по этому событию'}), 400

        if currency == 'Шишки':
            spend_result = spend_tokens(user_id, amount, f'tot_bet:{event_id}')
            if not spend_result:
                return jsonify({'error': 'Недостаточно шишек на балансе'}), 400
        
        cursor.execute("INSERT INTO tot_bets (event_id, user_id, side, prediction, amount, currency, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                       (event_id, user_id, side, prediction, amount, currency))
        conn.commit()
        
        # Отправка уведомления администратору
        try:
            username = auth_user.get('username', 'Нет_юзернейма').replace('<', '&lt;').replace('>', '&gt;').replace('&', '&amp;')
            first_name = auth_user.get('first_name', 'Без_имени').replace('<', '&lt;').replace('>', '&gt;').replace('&', '&amp;')
            
            side_name = event['side1_name'] if side == 1 else (event['side2_name'] if side == 2 else event['draw_name'])
            selection_label = str(prediction or side_name or '—')
            safe_title = str(event['title']).replace('<', '&lt;').replace('>', '&gt;').replace('&', '&amp;')
            safe_side = str(selection_label).replace('<', '&lt;').replace('>', '&gt;').replace('&', '&amp;')
            
            msg = (
                f"🎲 <b>Новая заявка на ставку!</b>\n\n"
                f"👤 <b>Игрок:</b> {first_name} (@{username})\n"
                f"🆔 <b>ID:</b> <code>{user_id}</code>\n\n"
                f"🏆 <b>Событие:</b> #{event_id} {safe_title}\n"
                f"🎯 <b>Выбор:</b> {safe_side}\n"
                f"💰 <b>Сумма:</b> {amount} {currency}\n\n"
                f"<i>Зайдите в WebApp ➔ Тотализатор ➔ Админ ➔ Заявки, чтобы принять или отклонить.</i>"
            )
            
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": ADMIN_ID, "text": msg, "parse_mode": "HTML"},
                timeout=5
            )
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления о ставке админу: {e}")
            
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/tot/my_bets', methods=['GET'])
def api_tot_my_bets():
    """История ставок пользователя"""
    conn = None
    try:
        user_id = int(request.args.get('userId', 0))
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT b.*, e.title, e.side1_name, e.side2_name, e.draw_name, e.side1_odds, e.side2_odds, e.draw_odds, e.event_type, e.exact_score_odds, e.result_score
            FROM tot_bets b
            JOIN tot_events e ON b.event_id = e.event_id
            WHERE b.user_id = ?
            ORDER BY b.created_at DESC
        ''', (user_id,))
        bets = [dict(row) for row in cursor.fetchall()]
        return jsonify({'status': 'ok', 'bets': bets})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/admin/tot/create', methods=['POST'])
def api_admin_tot_create():
    """Создать событие (Админ)"""
    conn = None
    try:
        if not request.is_json:
            return jsonify({'error': 'JSON required'}), 400
        data = request.get_json()
        user_id = int(data.get('userId', 0))
        if user_id != ADMIN_ID:
            return jsonify({'error': 'Unauthorized'}), 403

        conn = get_db_connection()
        cursor = conn.cursor()
        
        event_type = data.get('event_type', 'standard')
        s1_odds = float(data.get('side1_odds') or 1.0)
        s2_odds = float(data.get('side2_odds') or 1.0)
        draw_odds = float(data.get('draw_odds') or 1.0)
        exact_score_odds = float(data.get('exact_score_odds') or 1.0)
        image_url = data.get('image_url')
        
        cursor.execute('''
            INSERT INTO tot_events (title, image_url, side1_name, side1_odds, side2_name, side2_odds, draw_name, draw_odds, start_time, status, event_type, exact_score_odds)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
        ''', (data.get('title'), image_url, data.get('side1_name'), s1_odds,
              data.get('side2_name'), s2_odds, data.get('draw_name', 'Ничья'), draw_odds, data.get('start_time'), event_type, exact_score_odds))
        event_id = cursor.lastrowid
        conn.commit()
        return jsonify({'status': 'ok', 'event_id': event_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/admin/tot/events', methods=['GET'])
def api_admin_tot_events():
    """Список всех событий (Админ)"""
    conn = None
    try:
        user_id = int(request.args.get('userId', 0))
        if user_id != ADMIN_ID:
            return jsonify({'error': 'Unauthorized'}), 403

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tot_events ORDER BY event_id DESC")
        events = [dict(row) for row in cursor.fetchall()]
        
        current_time = get_moscow_now()
        changed = False
        for e in events:
            if e['status'] == 'active' and e['start_time']:
                try:
                    start_dt = parse_moscow_datetime(e['start_time'])
                    if start_dt and current_time >= start_dt:
                        cursor.execute("SELECT user_id, amount, currency FROM tot_bets WHERE event_id = ? AND status = 'pending'", (e['event_id'],))
                        for b in cursor.fetchall():
                            if b['currency'] == 'Шишки':
                                add_tokens(b['user_id'], int(b['amount']), f"tot_refund:{e['event_id']}")
                        cursor.execute("UPDATE tot_events SET status = 'locked' WHERE event_id = ?", (e['event_id'],))
                        cursor.execute("DELETE FROM tot_bets WHERE event_id = ? AND status = 'pending'", (e['event_id'],))
                        e['status'] = 'locked'
                        changed = True
                except Exception:
                    pass
        if changed:
            conn.commit()
            
        return jsonify({'status': 'ok', 'events': events})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/admin/tot/status', methods=['POST'])
def api_admin_tot_status():
    """Изменить статус события (Админ)"""
    conn = None
    try:
        if not request.is_json:
            return jsonify({'error': 'JSON required'}), 400
        data = request.get_json()
        user_id = int(data.get('userId', 0))
        if user_id != ADMIN_ID:
            return jsonify({'error': 'Unauthorized'}), 403

        event_id = int(data.get('eventId', 0))
        action = data.get('action')
        winner = int(data.get('winner', 0))

        conn = get_db_connection()
        cursor = conn.cursor()
        
        if action == 'locked':
            cursor.execute("SELECT user_id, amount, currency FROM tot_bets WHERE event_id = ? AND status = 'pending'", (event_id,))
            for b in cursor.fetchall():
                if b['currency'] == 'Шишки':
                    add_tokens(b['user_id'], int(b['amount']), f"tot_refund:{event_id}")
            cursor.execute("DELETE FROM tot_bets WHERE event_id = ? AND status = 'pending'", (event_id,))
            cursor.execute("UPDATE tot_events SET status = 'locked' WHERE event_id = ?", (event_id,))
        elif action == 'finished':
            result_score = data.get('result_score')
            cursor.execute("SELECT event_type FROM tot_events WHERE event_id = ?", (event_id,))
            event_row = cursor.fetchone()
            if event_row and event_row['event_type'] == 'exact_score':
                if not result_score:
                    return jsonify({'error': 'result_score required'}), 400
                cursor.execute("UPDATE tot_events SET status = 'finished', winner = 0, result_score = ? WHERE event_id = ?", (result_score, event_id))
                cursor.execute("UPDATE tot_bets SET status = 'won' WHERE event_id = ? AND status = 'accepted' AND prediction = ?", (event_id, result_score))
                cursor.execute("UPDATE tot_bets SET status = 'lost' WHERE event_id = ? AND status = 'accepted' AND prediction != ?", (event_id, result_score))
            else:
                cursor.execute("UPDATE tot_events SET status = 'finished', winner = ?, result_score = NULL WHERE event_id = ?", (winner, event_id))
                cursor.execute("UPDATE tot_bets SET status = 'won' WHERE event_id = ? AND status = 'accepted' AND side = ?", (event_id, winner))
                cursor.execute("UPDATE tot_bets SET status = 'lost' WHERE event_id = ? AND status = 'accepted' AND side != ?", (event_id, winner))
        elif action == 'paid':
            cursor.execute("SELECT b.bet_id, b.user_id, b.amount, b.currency, e.side1_odds, e.side2_odds, e.draw_odds, e.winner, e.event_type, e.exact_score_odds FROM tot_bets b JOIN tot_events e ON b.event_id = e.event_id WHERE b.event_id = ? AND b.status = 'won'", (event_id,))
            bets = cursor.fetchall()
            for b in bets:
                if b['event_type'] == 'exact_score':
                    odds = b['exact_score_odds'] or 1.0
                else:
                    odds = b['side1_odds'] if b['winner'] == 1 else (b['side2_odds'] if b['winner'] == 2 else b['draw_odds'])
                win_amount = int(b['amount'] * odds)
                if b['currency'] == 'Шишки':
                    add_tokens(b['user_id'], win_amount, f'tot_win:{event_id}')
                elif b['currency'] == 'CG':
                    add_tokens(b['user_id'], win_amount, f'tot_win_cg:{event_id}')
                    add_tokens(b['user_id'], win_amount * 10, f'tot_win_cones:{event_id}')
            cursor.execute("UPDATE tot_bets SET status = 'paid' WHERE event_id = ? AND status = 'won'", (event_id,))
        elif action == 'active':
            cursor.execute("UPDATE tot_events SET status = 'active' WHERE event_id = ?", (event_id,))
            
        conn.commit()
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/admin/tot/bets', methods=['GET'])
def api_admin_tot_bets():
    """Список ставок (Админ)"""
    conn = None
    try:
        user_id = int(request.args.get('userId', 0))
        status = request.args.get('status', 'pending')
        if user_id != ADMIN_ID:
            return jsonify({'error': 'Unauthorized'}), 403

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT b.*, e.side1_name, e.side2_name, e.draw_name, e.side1_odds, e.side2_odds, e.draw_odds, e.event_type, e.exact_score_odds, e.result_score, COALESCE(u.username, '') as username
            FROM tot_bets b
            JOIN tot_events e ON b.event_id = e.event_id
            LEFT JOIN users u ON b.user_id = u.user_id
            WHERE b.status = ?
            ORDER BY b.created_at ASC
        ''', (status,))
        bets = [dict(row) for row in cursor.fetchall()]
        return jsonify({'status': 'ok', 'bets': bets})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/admin/tot/bet_status', methods=['POST'])
def api_admin_tot_bet_status():
    """Одобрить/Отклонить ставку (Админ)"""
    conn = None
    try:
        if not request.is_json:
            return jsonify({'error': 'JSON required'}), 400
        data = request.get_json()
        user_id = int(data.get('userId', 0))
        if user_id != ADMIN_ID:
            return jsonify({'error': 'Unauthorized'}), 403

        bet_id = int(data.get('betId', 0))
        action = data.get('action')

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT b.user_id, b.amount, b.currency, b.status, b.side, b.prediction, e.title, e.side1_odds, e.side2_odds, e.draw_odds, e.winner, e.event_type, e.exact_score_odds, b.event_id
            FROM tot_bets b 
            JOIN tot_events e ON b.event_id = e.event_id 
            WHERE b.bet_id = ?
        ''', (bet_id,))
        bet = cursor.fetchone()
        
        if bet:
            msg_text = ""
            if bet['status'] == 'pending':
                if action == 'accept':
                    cursor.execute("UPDATE tot_bets SET status = 'accepted' WHERE bet_id = ?", (bet_id,))
                    msg_text = f"✅ Ваша ставка ({bet['amount']} {bet['currency']}) на <b>{bet['title']}</b> ПРИНЯТА."
                elif action == 'reject':
                    cursor.execute("UPDATE tot_bets SET status = 'rejected' WHERE bet_id = ?", (bet_id,))
                    if bet['currency'] == 'Шишки':
                        add_tokens(bet['user_id'], int(bet['amount']), f"tot_refund:{bet['event_id']}")
                    msg_text = f"❌ Ваша ставка на <b>{bet['title']}</b> ОТКЛОНЕНА."
                conn.commit()
            elif bet['status'] == 'won' and action == 'pay':
                if bet['event_type'] == 'exact_score':
                    odds = bet['exact_score_odds'] or 1.0
                else:
                    odds = bet['side1_odds'] if bet['winner'] == 1 else (bet['side2_odds'] if bet['winner'] == 2 else bet['draw_odds'])
                win_amount = int(bet['amount'] * odds)
                cursor.execute("UPDATE tot_bets SET status = 'paid' WHERE bet_id = ?", (bet_id,))
                conn.commit()
                
                if bet['currency'] == 'Шишки':
                    add_tokens(bet['user_id'], win_amount, f"tot_win:{bet['event_id']}")
                elif bet['currency'] == 'CG':
                    add_tokens(bet['user_id'], win_amount, f"tot_win_cg:{bet['event_id']}")
                    add_tokens(bet['user_id'], win_amount * 10, f"tot_win_cones:{bet['event_id']}")
                if bet['currency'] == 'CG':
                    msg_text = f"🎉 <b>Ставка сыграла!</b>\nСобытие: <b>{bet['title']}</b>\nВаш выигрыш: <b>{win_amount} CG</b> и <b>{win_amount * 10} Шишек</b> начислены на баланс!"
                else:
                    msg_text = f"🎉 <b>Ставка сыграла!</b>\nСобытие: <b>{bet['title']}</b>\nВаш выигрыш: <b>{win_amount} {bet['currency']}</b> начислен на баланс!"
            else:
                return jsonify({'error': 'Ставка уже обработана или неверное действие'}), 400
            
            try:
                requests.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": bet['user_id'], "text": msg_text, "parse_mode": "HTML"},
                    timeout=5
                )
            except Exception as e:
                logger.error(f"Ошибка отправки уведомления о ставке: {e}")
                
            return jsonify({'status': 'ok'})
        return jsonify({'error': 'Ставка не найдена'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/admin/tot/delete', methods=['POST'])
def api_admin_tot_delete():
    """Удалить событие и все его ставки (Админ)"""
    conn = None
    try:
        if not request.is_json:
            return jsonify({'error': 'JSON required'}), 400
        data = request.get_json()
        user_id = int(data.get('userId', 0))
        if user_id != ADMIN_ID:
            return jsonify({'error': 'Unauthorized'}), 403

        event_id = int(data.get('eventId', 0))

        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT user_id, amount, currency FROM tot_bets WHERE event_id = ? AND status IN ('pending', 'accepted')", (event_id,))
        for b in cursor.fetchall():
            if b['currency'] == 'Шишки':
                add_tokens(b['user_id'], int(b['amount']), f"tot_refund:{event_id}")
        # Сначала удаляем все ставки, привязанные к этому событию
        cursor.execute("DELETE FROM tot_bets WHERE event_id = ?", (event_id,))
        # Затем удаляем само событие
        cursor.execute("DELETE FROM tot_events WHERE event_id = ?", (event_id,))
        
        conn.commit()
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()


# ==================== СЕТЫ ФИШЕК (CUSTOM CHIP SETS) ====================

def decode_base64_image(data_str: str) -> bytes:
    """Декодирует base64 строку или data URL в байты."""
    if not data_str:
        return b""
    try:
        if "," in data_str:
            data_str = data_str.split(",", 1)[1]
        data_str = data_str.strip().replace(" ", "+")
        missing_padding = len(data_str) % 4
        if missing_padding:
            data_str += "=" * (4 - missing_padding)
        return base64.b64decode(data_str)
    except Exception as e:
        logger.error(f"❌ Ошибка decode_base64_image: {e}")
        return b""


def generate_round_chip(raw_bytes: bytes, diameter: int = 320) -> Image.Image:
    """
    Превращает исходное изображение в круглую четкую фишку без мутных бликов.
    """
    img = Image.open(BytesIO(raw_bytes)).convert("RGBA")
    w, h = img.size
    min_dim = min(w, h)
    left = (w - min_dim) // 2
    top = (h - min_dim) // 2
    img = img.crop((left, top, left + min_dim, top + min_dim))
    img = img.resize((diameter, diameter), Image.Resampling.LANCZOS)

    # Круглая маска с anti-aliasing (supersampling 4x)
    scale = 4
    big_size = diameter * scale
    mask = Image.new('L', (big_size, big_size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse((0, 0, big_size - 1, big_size - 1), fill=255)
    mask = mask.resize((diameter, diameter), Image.Resampling.LANCZOS)

    chip = Image.new('RGBA', (diameter, diameter), (0, 0, 0, 0))
    chip.paste(img, (0, 0), mask)

    # Аккуратный четкий золотистый кант (без мутных бликов поверх картинки)
    overlay = Image.new('RGBA', (big_size, big_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.ellipse((0, 0, big_size - 1, big_size - 1), outline=(20, 20, 25, 230), width=scale * 3)
    draw.ellipse((scale * 3, scale * 3, big_size - 1 - scale * 3, big_size - 1 - scale * 3), outline=(255, 215, 0, 190), width=scale * 2)
    draw.ellipse((scale * 5, scale * 5, big_size - 1 - scale * 5, big_size - 1 - scale * 5), outline=(0, 0, 0, 90), width=scale)

    overlay = overlay.resize((diameter, diameter), Image.Resampling.LANCZOS)
    chip = Image.alpha_composite(chip, overlay)
    return chip


def generate_set_collage(bg_bytes: bytes, chip_images: list, count: int) -> Image.Image:
    """
    Создает коллаж сета: каждая фишка размещается на отдельном квадратном фоне.
    """
    cols = 3
    rows = (count + 2) // 3
    
    tile_size = 280
    chip_size = 220
    gap = 30
    margin = 35

    canvas_w = margin * 2 + cols * tile_size + (cols - 1) * gap
    canvas_h = margin * 2 + rows * tile_size + (rows - 1) * gap

    # Темный стильный фон холста
    canvas = Image.new('RGBA', (canvas_w, canvas_h), (18, 18, 22, 255))

    # Подготавливаем квадратный фон для плашек
    bg_tile = None
    card_mask = None
    corner_radius = 24
    if bg_bytes:
        try:
            raw_bg = Image.open(BytesIO(bg_bytes)).convert("RGBA")
            bg_min = min(raw_bg.width, raw_bg.height)
            b_left = (raw_bg.width - bg_min) // 2
            b_top = (raw_bg.height - bg_min) // 2
            bg_cropped = raw_bg.crop((b_left, b_top, b_left + bg_min, b_top + bg_min))
            bg_tile = bg_cropped.resize((tile_size, tile_size), Image.Resampling.LANCZOS)
            
            # Маска со скругленными углами для квадратного фона
            scale = 4
            big_tile_w = tile_size * scale
            card_mask = Image.new('L', (big_tile_w, big_tile_w), 0)
            c_draw = ImageDraw.Draw(card_mask)
            c_draw.rounded_rectangle((0, 0, big_tile_w - 1, big_tile_w - 1), radius=corner_radius * scale, fill=255)
            card_mask = card_mask.resize((tile_size, tile_size), Image.Resampling.LANCZOS)
        except Exception as e:
            logger.error(f"Ошибка подготовки фона плитки: {e}")
            bg_tile = None

    for i, raw_chip in enumerate(chip_images[:count]):
        r = i // cols
        c = i % cols
        tile_x = margin + c * (tile_size + gap)
        tile_y = margin + r * (tile_size + gap)

        # Тень под квадратной карточкой
        tile_shadow = Image.new('RGBA', (tile_size + 24, tile_size + 24), (0, 0, 0, 0))
        sh_draw = ImageDraw.Draw(tile_shadow)
        sh_draw.rounded_rectangle((6, 10, tile_size + 18, tile_size + 20), radius=26, fill=(0, 0, 0, 160))
        tile_shadow = tile_shadow.filter(ImageFilter.GaussianBlur(10))
        canvas.alpha_composite(tile_shadow, (tile_x - 12, tile_y - 8))

        # 1. Отдельный квадратный фон фишки
        if bg_tile and card_mask:
            card_img = Image.new('RGBA', (tile_size, tile_size), (0, 0, 0, 0))
            card_img.paste(bg_tile, (0, 0), card_mask)

            # Рамка для квадратной карточки
            frame_overlay = Image.new('RGBA', (tile_size * 4, tile_size * 4), (0, 0, 0, 0))
            f_draw = ImageDraw.Draw(frame_overlay)
            f_draw.rounded_rectangle((0, 0, tile_size * 4 - 1, tile_size * 4 - 1), radius=corner_radius * 4, outline=(255, 255, 255, 80), width=4)
            frame_overlay = frame_overlay.resize((tile_size, tile_size), Image.Resampling.LANCZOS)
            card_img = Image.alpha_composite(card_img, frame_overlay)

            canvas.alpha_composite(card_img, (tile_x, tile_y))
        else:
            card_img = Image.new('RGBA', (tile_size, tile_size), (28, 28, 34, 255))
            canvas.alpha_composite(card_img, (tile_x, tile_y))

        # 2. Сама фишка (круглая, четкая, по центру квадратного фона)
        chip_img = generate_round_chip(raw_chip, diameter=chip_size)
        chip_offset = (tile_size - chip_size) // 2
        chip_x = tile_x + chip_offset
        chip_y = tile_y + chip_offset

        # Тень под фишкой внутри квадратного фона
        chip_shadow = Image.new('RGBA', (chip_size + 20, chip_size + 20), (0, 0, 0, 0))
        cs_draw = ImageDraw.Draw(chip_shadow)
        cs_draw.ellipse((6, 8, chip_size + 14, chip_size + 16), fill=(0, 0, 0, 180))
        chip_shadow = chip_shadow.filter(ImageFilter.GaussianBlur(6))
        canvas.alpha_composite(chip_shadow, (chip_x - 10, chip_y - 8))

        canvas.alpha_composite(chip_img, (chip_x, chip_y))

    return canvas


def send_chip_set_moderation_card(set_id: int, author_id: int, author_name: str, title: str, description: str, chips_count: int, collage_path: Path):
    """Отправляет карточку модерации сета админу в Telegram."""
    if not ADMIN_ID or not BOT_TOKEN:
        return
    try:
        caption = (
            f"🎨 <b>МОДЕРАЦИЯ НОВОГО СЕТА ФИШЕК</b>\n\n"
            f"🆔 <b>Сет ID:</b> <code>{set_id}</code>\n"
            f"👤 <b>Автор:</b> {html.escape(author_name or 'Аноним')} (<code>{author_id}</code>)\n"
            f"🏷️ <b>Название:</b> <b>{html.escape(title)}</b>\n"
            f"📝 <b>Описание:</b> {html.escape(description or '—')}\n"
            f"🔢 <b>Количество фишек:</b> {chips_count} шт.\n\n"
            f"<i>Выберите действие:</i>"
        )
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ Одобрить и опубликовать", "callback_data": f"chip_set_approve_{set_id}"},
                    {"text": "❌ Отклонить", "callback_data": f"chip_set_reject_{set_id}"}
                ]
            ]
        }
        with open(collage_path, 'rb') as photo_file:
            files = {'photo': ('collage.jpg', photo_file, 'image/jpeg')}
            payload = {
                "chat_id": ADMIN_ID,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": json.dumps(reply_markup)
            }
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto", data=payload, files=files, timeout=25)
    except Exception as e:
        logger.error(f"❌ Ошибка send_chip_set_moderation_card: {e}")


def publish_chip_set_to_group(set_data: dict):
    """Публикация сета в группу @the_raccoon_times_group"""
    group_chat_id = "@the_raccoon_times_group"
    preview_path = PROJECT_DIR / "webapp" / set_data['preview_collage'].lstrip('/')
    caption = (
        f"🎨 <b>Новая коллекция фишек в Raccoon Life!</b>\n\n"
        f"🏆 <b>«{html.escape(set_data['title'])}»</b>\n"
        f"👤 <b>Автор:</b> {html.escape(set_data.get('author_name') or 'Енот-мастер')}\n"
        f"🔢 <b>Фишек в сете:</b> {set_data['chips_count']} шт.\n"
    )
    if set_data.get('description'):
        caption += f"📝 <i>{html.escape(set_data['description'])}</i>\n\n"
    else:
        caption += "\n"
    caption += "🦝 Заходите в игру, чтобы оценить и проголосовать за лучший сет!"

    try:
        if preview_path.exists():
            with open(preview_path, 'rb') as photo_file:
                files = {'photo': ('set_collage.jpg', photo_file, 'image/jpeg')}
                payload = {
                    "chat_id": group_chat_id,
                    "caption": caption,
                    "parse_mode": "HTML"
                }
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto", data=payload, files=files, timeout=25)
        else:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": group_chat_id, "text": caption, "parse_mode": "HTML"},
                timeout=10
            )
    except Exception as e:
        logger.error(f"❌ Ошибка публикации сета в группу: {e}")


def create_custom_chip_set_db(author_id: int, author_name: str, title: str, description: str, chips_count: int, bg_bytes: bytes, chip_bytes_list: list) -> dict:
    """Сохраняет сет в БД, файлы на диск и отправляет модерацию админу."""
    ts = int(time.time())
    set_folder = SETS_IMG_DIR / f"set_{author_id}_{ts}"
    set_folder.mkdir(parents=True, exist_ok=True)

    # 1. Фон
    bg_filename = f"bg_{ts}.jpg"
    bg_path = set_folder / bg_filename
    bg_img = Image.open(BytesIO(bg_bytes)).convert("RGB")
    bg_img.save(bg_path, format="JPEG", quality=90)
    bg_rel_path = f"/images/sets/set_{author_id}_{ts}/{bg_filename}"

    # 2. Фишки
    chips_rel_paths = []
    for idx, raw_chip in enumerate(chip_bytes_list):
        chip_img = generate_round_chip(raw_chip, diameter=320)
        chip_fname = f"chip_{idx+1}_{ts}.png"
        chip_path = set_folder / chip_fname
        chip_img.save(chip_path, format="PNG")
        chips_rel_paths.append(f"/images/sets/set_{author_id}_{ts}/{chip_fname}")

    # 3. Превью коллаж
    collage_img = generate_set_collage(bg_bytes, chip_bytes_list, chips_count)
    collage_fname = f"collage_{ts}.jpg"
    collage_path = set_folder / collage_fname
    collage_img.convert("RGB").save(collage_path, format="JPEG", quality=92)
    collage_rel_path = f"/images/sets/set_{author_id}_{ts}/{collage_fname}"

    # 4. БД
    ensure_user_exists(author_id)
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            INSERT INTO custom_chip_sets (
                author_id, author_name, title, description, chips_count,
                background_image, chips_json, preview_collage, status, votes_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0)
        ''', (
            author_id, author_name, title, description, chips_count,
            bg_rel_path, json.dumps(chips_rel_paths), collage_rel_path
        ))
        set_id = cursor.lastrowid
        conn.commit()

        # Отправляем карточку модерации (не прерывая создание при сбое Telegram)
        try:
            send_chip_set_moderation_card(set_id, author_id, author_name, title, description, chips_count, collage_path)
        except Exception as err:
            logger.error(f"⚠️ Ошибка отправки карточки модерации сета #{set_id}: {err}")

        return {'status': 'ok', 'set_id': set_id}
    except Exception as e:
        logger.error(f"❌ Ошибка create_custom_chip_set_db: {e}")
        return {'status': 'error', 'message': str(e)}
    finally:
        conn.close()


def approve_chip_set_db(set_id: int) -> dict:
    """Одобряет сет, публикует в группу и уведомляет автора."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM custom_chip_sets WHERE id = ?", (set_id,))
        row = cursor.fetchone()
        if not row:
            return {'status': 'error', 'message': 'Сет не найден'}
        
        now_moscow = get_moscow_now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("UPDATE custom_chip_sets SET status = 'approved', approved_at = ? WHERE id = ?", (now_moscow, set_id))
        conn.commit()

        publish_chip_set_to_group(dict(row))

        try:
            author_id = row['author_id']
            msg = (
                f"🎉 <b>Ваш сет фишек одобрен!</b>\n\n"
                f"Коллекция: <b>{html.escape(row['title'])}</b> ({row['chips_count']} фишек)\n"
                f"Теперь сет доступен в разделе «Сеты фишек» игры и опубликован в группе!\n"
                f"Другие игроки уже могут голосовать за ваш сет ❤️"
            )
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": author_id, "text": msg, "parse_mode": "HTML"},
                timeout=5
            )
        except Exception as e:
            logger.error(f"Ошибка уведомления автора сета: {e}")

        return {'status': 'ok'}
    except Exception as e:
        logger.error(f"❌ Ошибка approve_chip_set_db: {e}")
        return {'status': 'error', 'message': str(e)}
    finally:
        conn.close()


def reject_chip_set_db(set_id: int, reason: str = "") -> dict:
    """Отклоняет сет фишек и уведомляет автора (с указанием причины или без)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM custom_chip_sets WHERE id = ?", (set_id,))
        row = cursor.fetchone()
        if not row:
            return {'status': 'error', 'message': 'Сет не найден'}
        cursor.execute("UPDATE custom_chip_sets SET status = 'rejected' WHERE id = ?", (set_id,))
        conn.commit()

        try:
            author_id = row['author_id']
            msg = f"😔 <b>Ваш сет фишек «{html.escape(row['title'])}» не прошёл модерацию</b>\n"
            clean_reason = reason.strip() if reason else ""
            if clean_reason:
                msg += f"\n📝 <b>Причина:</b>\n<i>{html.escape(clean_reason)}</i>\n"
            msg += "\nПопробуйте создать новый сет, соблюдая правила сообщества."

            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": author_id, "text": msg, "parse_mode": "HTML"},
                timeout=5
            )
        except Exception as e:
            logger.error(f"Ошибка уведомления автора об отклонении: {e}")

        return {'status': 'ok'}
    except Exception as e:
        logger.error(f"❌ Ошибка reject_chip_set_db: {e}")
        return {'status': 'error', 'message': str(e)}
    finally:
        conn.close()


def get_custom_chip_sets_list(sort: str = 'top', user_id: int = 0) -> list:
    """Возвращает список одобренных сетов."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        order_by = "s.votes_count DESC, s.id DESC" if sort == 'top' else "s.approved_at DESC, s.id DESC"
        cursor.execute(f"""
            SELECT s.*, 
                   (SELECT COUNT(*) FROM custom_chip_votes v WHERE v.set_id = s.id AND v.user_id = ?) as has_voted
            FROM custom_chip_sets s
            WHERE s.status = 'approved'
            ORDER BY {order_by}
            LIMIT 100
        """, (user_id,))
        rows = cursor.fetchall()
        sets = []
        for r in rows:
            d = dict(r)
            try:
                d['chips'] = json.loads(d['chips_json']) if d.get('chips_json') else []
            except Exception:
                d['chips'] = []
            d['has_voted'] = bool(d.get('has_voted'))
            sets.append(d)
        return sets
    except Exception as e:
        logger.error(f"❌ Ошибка get_custom_chip_sets_list: {e}")
        return []
    finally:
        conn.close()


def get_user_chip_sets_db(user_id: int) -> list:
    """Возвращает сеты, созданные пользователем."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT * FROM custom_chip_sets 
            WHERE author_id = ? 
            ORDER BY id DESC
        """, (user_id,))
        rows = cursor.fetchall()
        sets = []
        for r in rows:
            d = dict(r)
            try:
                d['chips'] = json.loads(d['chips_json']) if d.get('chips_json') else []
            except Exception:
                d['chips'] = []
            sets.append(d)
        return sets
    except Exception as e:
        logger.error(f"❌ Ошибка get_user_chip_sets_db: {e}")
        return []
    finally:
        conn.close()


def toggle_chip_set_vote_db(set_id: int, user_id: int) -> dict:
    """Переключает голос пользователя за сет фишек."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM custom_chip_votes WHERE set_id = ? AND user_id = ?", (set_id, user_id))
        exists = cursor.fetchone()
        if exists:
            cursor.execute("DELETE FROM custom_chip_votes WHERE set_id = ? AND user_id = ?", (set_id, user_id))
            cursor.execute("UPDATE custom_chip_sets SET votes_count = MAX(0, votes_count - 1) WHERE id = ?", (set_id,))
            has_voted = False
        else:
            cursor.execute("INSERT INTO custom_chip_votes (set_id, user_id) VALUES (?, ?)", (set_id, user_id))
            cursor.execute("UPDATE custom_chip_sets SET votes_count = votes_count + 1 WHERE id = ?", (set_id,))
            has_voted = True
        conn.commit()

        cursor.execute("SELECT votes_count FROM custom_chip_sets WHERE id = ?", (set_id,))
        row = cursor.fetchone()
        votes_count = row['votes_count'] if row else 0

        return {'status': 'ok', 'has_voted': has_voted, 'votes_count': votes_count}
    except Exception as e:
        logger.error(f"❌ Ошибка toggle_chip_set_vote_db: {e}")
        return {'status': 'error', 'message': str(e)}
    finally:
        conn.close()


# ---------- Flask API Endpoints для сетов фишек ----------

@app.route('/api/chip_sets/create', methods=['POST'])
@limiter.limit("20 per minute")
def api_chip_sets_create():
    """Создание сета фишек и отправка на модерацию"""
    try:
        if not request.is_json:
            return jsonify({'error': 'JSON required'}), 400

        data = request.get_json()
        user_id = int(data.get('userId') or data.get('user_id') or 0)
        author_name = "Игрок"

        # Авторизация через init_data если передана
        init_data = request.headers.get('X-Telegram-Init-Data')
        if init_data:
            auth_user = validate_webapp_data(init_data)
            if auth_user:
                user_id = int(auth_user.get('id', user_id))
                author_name = auth_user.get('username') or f"{auth_user.get('first_name', '')} {auth_user.get('last_name', '')}".strip() or f"User {user_id}"

        if user_id <= 0:
            return jsonify({'error': 'Некорректный ID пользователя'}), 400

        # Пытаемся получить актуальное имя автора из базы
        try:
            ensure_user_exists(user_id)
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT username, first_name, last_name FROM users WHERE user_id = ?", (user_id,))
            u_row = cursor.fetchone()
            if u_row:
                if u_row['username']:
                    author_name = f"@{u_row['username']}"
                elif u_row['first_name'] or u_row['last_name']:
                    author_name = f"{u_row['first_name'] or ''} {u_row['last_name'] or ''}".strip()
            conn.close()
        except Exception as err:
            logger.warning(f"Ошибка получения имени автора #{user_id}: {err}")

        title = str(data.get('title', '')).strip()
        description = str(data.get('description', '')).strip()
        chips_count = int(data.get('chips_count', 3))
        bg_b64 = data.get('background_image')
        chips_b64_list = data.get('chips', [])

        if not title:
            return jsonify({'error': 'Введите название сета'}), 400

        if chips_count not in (3, 6, 9):
            return jsonify({'error': 'Количество фишек должно быть 3, 6 или 9'}), 400

        if not bg_b64:
            return jsonify({'error': 'Загрузите фоновое изображение для сета'}), 400

        if len(chips_b64_list) != chips_count:
            return jsonify({'error': f'Необходимо загрузить ровно {chips_count} изображений для фишек'}), 400

        bg_bytes = decode_base64_image(bg_b64)
        if not bg_bytes:
            return jsonify({'error': 'Некорректный формат фонового изображения'}), 400

        chip_bytes_list = []
        for idx, chip_b64 in enumerate(chips_b64_list):
            cb = decode_base64_image(chip_b64)
            if not cb:
                return jsonify({'error': f'Некорректное изображение фишки #{idx + 1}'}), 400
            chip_bytes_list.append(cb)

        res = create_custom_chip_set_db(
            author_id=user_id,
            author_name=author_name,
            title=title,
            description=description,
            chips_count=chips_count,
            bg_bytes=bg_bytes,
            chip_bytes_list=chip_bytes_list
        )

        if res.get('status') == 'ok':
            return jsonify({'status': 'ok', 'set_id': res.get('set_id'), 'message': 'Сет отправлен на модерацию!'})
        else:
            return jsonify({'error': res.get('message', 'Ошибка создания сета')}), 500

    except Exception as e:
        logger.error(f"❌ Ошибка api_chip_sets_create: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/chip_sets/list', methods=['GET'])
def api_chip_sets_list():
    """Получение списка одобренных сетов фишек"""
    try:
        sort = request.args.get('sort', 'top')
        user_id = int(request.args.get('userId', 0))
        sets = get_custom_chip_sets_list(sort=sort, user_id=user_id)
        is_admin = (user_id == ADMIN_ID)
        return jsonify({'status': 'ok', 'sets': sets, 'is_admin': is_admin})
    except Exception as e:
        logger.error(f"❌ Ошибка api_chip_sets_list: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/chip_sets/my', methods=['GET'])
def api_chip_sets_my():
    """Получение сетов текущего пользователя"""
    try:
        user_id = int(request.args.get('userId', 0))
        sets = get_user_chip_sets_db(user_id=user_id)
        return jsonify({'status': 'ok', 'sets': sets})
    except Exception as e:
        logger.error(f"❌ Ошибка api_chip_sets_my: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/chip_sets/vote', methods=['POST'])
@limiter.limit("60 per minute")
def api_chip_sets_vote():
    """Голосование за сет фишек"""
    try:
        if not request.is_json:
            return jsonify({'error': 'JSON required'}), 400
        data = request.get_json()
        set_id = int(data.get('setId', 0))
        user_id = int(data.get('userId', 0))

        if set_id <= 0 or user_id <= 0:
            return jsonify({'error': 'setId and userId required'}), 400

        res = toggle_chip_set_vote_db(set_id, user_id)
        return jsonify(res)
    except Exception as e:
        logger.error(f"❌ Ошибка api_chip_sets_vote: {e}")
        return jsonify({'error': str(e)}), 500


# ==================== TELEGRAM BOT ====================


def has_received_welcome_bonus(user_id: int) -> bool:
    """Проверяет получал ли пользователь приветственные шишки через БД"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # 1. Проверяем наличие флага 'welcome_bonus' в массиве квестов
        cursor.execute('SELECT quests FROM user_stats WHERE user_id = ?', (user_id,))
        row_stats = cursor.fetchone()
        if row_stats and row_stats['quests']:
            try:
                if 'welcome_bonus' in json.loads(row_stats['quests']):
                    return True
            except:
                pass
                
        # 2. Обратная совместимость: если игрок уже заработал >= 1000 шишек
        cursor.execute('SELECT total_earned FROM user_tokens WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        if row and row[0] >= 1000:
            return True
        return False
    except Exception as e:
        logger.error(f"Ошибка has_received_welcome_bonus: {e}")
        return False
    finally:
        conn.close()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user

    # Проверяем, не забанен ли пользователь
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT is_banned FROM users WHERE user_id = ?', (user.id,))
        row = cursor.fetchone()
        if row and row[0]:
            await update.message.reply_text(
                "⛔️ <b>Вы заблокированы!</b>\n\n"
                "Вы не можете использовать этого бота.",
                parse_mode=ParseMode.HTML
            )
            return
    finally:
        conn.close()

    # Сохраняем пользователя в БД
    try:
        ensure_user_exists(
            user.id,
            {
                'username': user.username,
                'first_name': user.first_name,
                'last_name': user.last_name
            }
        )
        logger.info(f"👤 User {user.id} ({user.username}) started bot")

        # Обработка реферального параметра (/start ref_123456 или /start 123456)
        if context.args and len(context.args) > 0:
            raw_ref = context.args[0].strip()
            clean_ref = raw_ref.replace('ref_', '').replace('ref', '')
            try:
                referrer_id = int(clean_ref)
                if referrer_id != user.id and referrer_id > 0:
                    registered = register_referral(user.id, referrer_id)
                    if registered:
                        logger.info(f"👥 Реферал {user.id} привязан к пригласившему {referrer_id} через /start")
            except (ValueError, TypeError):
                pass

        # Проверяем нужно ли начислить приветственные шишки
        # Начисляем только если пользователь новый (первый раз запускает бота)
        if not has_received_welcome_bonus(user.id):
            # Проверяем баланс - если 0, начисляем приветственные
            tokens = get_user_tokens(user.id)
            if tokens['balance'] == 0 and tokens['total_earned'] == 0:
                # Начисляем 1000 приветственных шишек
                result = add_tokens(user.id, 1000, 'welcome_bonus')
                if result:
                    # Записываем флаг в БД, чтобы больше не выдавать
                    conn2 = get_db_connection()
                    cursor2 = conn2.cursor()
                    try:
                        cursor2.execute('SELECT quests FROM user_stats WHERE user_id = ?', (user.id,))
                        row_q = cursor2.fetchone()
                        quests = json.loads(row_q['quests']) if row_q and row_q['quests'] else []
                        if 'welcome_bonus' not in quests:
                            quests.append('welcome_bonus')
                            cursor2.execute('UPDATE user_stats SET quests = ? WHERE user_id = ?', (json.dumps(quests), user.id))
                            conn2.commit()
                    except Exception as e:
                        logger.error(f"Ошибка сохранения флага welcome_bonus: {e}")
                    finally:
                        conn2.close()

                    logger.info(f"🎁 Приветственные шишки начислены: user_id={user.id}, balance={result['balance']}")

                    # Отправляем приветственное сообщение
                    await update.message.reply_text(
                        f"🎉 <b>Добро пожаловать в Raccoon Life!</b>\n\n"
                        f"🦝 Вы получили <b>1000 Шишек</b> приветственных шишек!\n\n"
                        f"Играйте в игры, выполняйте квесты и зарабатывайте ещё больше шишек! 💰\n\n"
                        f"Нажмите кнопку ниже, чтобы начать:",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton(text="📰 Играть!", web_app=WebAppInfo(url=WEBAPP_URL)) if WEBAPP_URL else InlineKeyboardButton(text="📰 Играть!", url="https://t.me")
                        ]]),
                        parse_mode=ParseMode.HTML
                    )
                    return  # Выходим чтобы не дублировать сообщение

    except Exception as e:
        logger.error(f"Ошибка сохранения пользователя: {e}")

    # Кнопка для запуска WebApp
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(text="📰 Играть!", web_app=WebAppInfo(url=WEBAPP_URL)) if WEBAPP_URL else InlineKeyboardButton(text="📰 Играть!", url="https://t.me")
    ]])

    await update.message.reply_text(
        "Привет! Нажми кнопку ниже, чтобы играть:",
        reply_markup=keyboard
    )


async def add_tokens_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Команда для админа: /add <username|user_id> <amount> [reason]
    Начисляет шишки пользователю и уведомляет его
    """
    # Проверка прав администратора
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ У вас нет прав для этой команды!")
        return

    # Проверка аргументов
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Использование: /add <username|user_id> <amount> [reason]\n"
            "Пример: /add @username 100 За победу в турнире\n"
            "Пример: /add 123456789 100 За победу в турнире"
        )
        return

    try:
        amount = int(context.args[1])
        reason = ' '.join(context.args[2:]) if len(context.args) > 2 else 'Начисление админом'
    except ValueError:
        await update.message.reply_text("❌ amount должен быть числом!")
        return

    if amount <= 0:
        await update.message.reply_text("❌ amount должен быть больше 0!")
        return

    # Ищем пользователя по username или ID
    identifier = context.args[0]
    logger.info(f"🔍 Поиск пользователя: {identifier}")
    user_info = get_user_by_id_or_username(identifier)

    if not user_info:
        await update.message.reply_text(
            f"❌ Пользователь '{identifier}' не найден!\n"
            f"Убедитесь что он запускал бота (@{context.bot.username})"
        )
        return

    user_id = user_info['user_id']
    user_name = f"@{user_info['username']}" if user_info['username'] else (f"{user_info['first_name']} {user_info['last_name']}".strip() or f"Игрок #{user_id}")

    logger.info(f"💰 Начисление шишек: user_id={user_id}, amount={amount}, reason={reason}")

    # Начисляем шишки
    result = add_tokens(user_id, amount, f'admin_grant:{reason}')

    if result:
        # Отправляем уведомление админу
        await update.message.reply_text(
            f"✅ Успешно!\n"
            f"👤 Пользователь: {user_name}\n"
            f"🆔 ID: {user_id}\n"
            f"💰 Начислено: {amount} Шишек\n"
            f"📝 Причина: {reason}\n"
            f"💳 Новый баланс: {result['balance']} Шишек"
        )

        # Отправляем уведомление пользователю
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"🎉 <b>Вам начислены шишки!</b>\n\n"
                    f"💰 Сумма: <b>+{amount} Шишек</b>\n"
                    f"📝 Причина: {reason}\n"
                    f"💳 Ваш баланс: {result['balance']} Шишек\n\n"
                    f"Продолжайте играть в Raccoon Life! 🦝"
                ),
                parse_mode=ParseMode.HTML
            )
            logger.info(f"📬 Уведомление отправлено пользователю {user_id}")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось отправить уведомление пользователю {user_id}: {e}")
            await update.message.reply_text(
                f"⚠️ Пользователь не найден или заблокировал бота!\n"
                f"Но шишки начислены (баланс: {result['balance']} Шишек)"
            )
    else:
        await update.message.reply_text("❌ Ошибка при начислении шишек!")


async def get_balance_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Команда для админа: /balance <username|user_id>
    Проверяет баланс пользователя
    """
    # Проверка прав администратора
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ У вас нет прав для этой команды!")
        return

    # Проверка аргументов
    if len(context.args) < 1:
        await update.message.reply_text(
            "❌ Использование: /balance <username|user_id>\n"
            "Пример: /balance @username\n"
            "Пример: /balance 123456789"
        )
        return

    # Ищем пользователя по username или ID
    identifier = context.args[0]
    user_info = get_user_by_id_or_username(identifier)

    if not user_info:
        await update.message.reply_text(
            f"❌ Пользователь '{identifier}' не найден!\n"
            f"Убедитесь что он запускал бота (@{context.bot.username})"
        )
        return

    user_id = user_info['user_id']
    user_name = f"@{user_info['username']}" if user_info['username'] else (f"{user_info['first_name']} {user_info['last_name']}".strip() or f"Игрок #{user_id}")

    # Получаем баланс
    tokens = get_user_tokens(user_id)

    await update.message.reply_text(
        f"💳 <b>Баланс пользователя</b>\n\n"
        f"👤 {user_name}\n"
        f"🆔 ID: {user_id}\n"
        f"💰 Баланс: <b>{tokens['balance']} Шишек</b>\n"
        f"📊 Всего заработано: {tokens['total_earned']} Шишек\n"
        f"💸 Всего потрачено: {tokens['total_spent']} Шишек",
        parse_mode=ParseMode.HTML
    )


async def spend_tokens_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Команда для админа: /spend <username|user_id> <amount> [reason]
    Списывает шишки у пользователя и уведомляет его
    """
    # Проверка прав администратора
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ У вас нет прав для этой команды!")
        return

    # Проверка аргументов
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Использование: /spend <username|user_id> <amount> [reason]\n"
            "Пример: /spend @username 50 Штраф за читы\n"
            "Пример: /spend 123456789 50 Штраф за читы"
        )
        return

    try:
        amount = int(context.args[1])
        reason = ' '.join(context.args[2:]) if len(context.args) > 2 else 'Списание админом'
    except ValueError:
        await update.message.reply_text("❌ amount должен быть числом!")
        return

    if amount <= 0:
        await update.message.reply_text("❌ amount должен быть больше 0!")
        return

    # Ищем пользователя по username или ID
    identifier = context.args[0]
    user_info = get_user_by_id_or_username(identifier)

    if not user_info:
        await update.message.reply_text(
            f"❌ Пользователь '{identifier}' не найден!\n"
            f"Убедитесь что он запускал бота (@{context.bot.username})"
        )
        return

    user_id = user_info['user_id']
    user_name = f"@{user_info['username']}" if user_info['username'] else (f"{user_info['first_name']} {user_info['last_name']}".strip() or f"Игрок #{user_id}")

    # Списываем шишки
    result = spend_tokens(user_id, amount, f'admin_spend:{reason}')

    if result:
        # Отправляем уведомление админу
        await update.message.reply_text(
            f"✅ Успешно!\n"
            f"👤 Пользователь: {user_name}\n"
            f"🆔 ID: {user_id}\n"
            f"💰 Списано: {amount} Шишек\n"
            f"📝 Причина: {reason}\n"
            f"💳 Остаток: {result['balance']} Шишек"
        )

        # Отправляем уведомление пользователю
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"⚠️ <b>Списание шишек</b>\n\n"
                    f"💸 Сумма: <b>-{amount} Шишек</b>\n"
                    f"📝 Причина: {reason}\n"
                    f"💳 Ваш баланс: {result['balance']} Шишек\n\n"
                    f"Обратитесь к администрации если вы не согласны с решением. 🦝"
                ),
                parse_mode=ParseMode.HTML
            )
            logger.info(f"📬 Уведомление о списании отправлено пользователю {user_id}")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось отправить уведомление пользователю {user_id}: {e}")
            await update.message.reply_text(
                f"⚠️ Пользователь не найден или заблокировал бота!\n"
                f"Но шишки списаны (баланс: {result['balance']} Шишек)"
            )
    elif result is None:
        # Проверяем текущий баланс для сообщения об ошибке
        tokens = get_user_tokens(user_id)
        await update.message.reply_text(
            f"❌ Недостаточно шишек у пользователя!\n"
            f"💰 Баланс: {tokens['balance']} Шишек (нужно {amount} Шишек)"
        )
    else:
        await update.message.reply_text("❌ Ошибка при списании шишек!")


async def ban_user_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Команда для админа: /ban <username|user_id> [reason]
    Банит пользователя и блокирует доступ к боту
    """
    # Проверка прав администратора
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ У вас нет прав для этой команды!")
        return

    # Проверка аргументов
    if len(context.args) < 1:
        await update.message.reply_text(
            "❌ Использование: /ban <username|user_id> [reason]\n"
            "Пример: /ban @username Нарушение правил\n"
            "Пример: /ban 123456789 Читерство"
        )
        return

    # Ищем пользователя по username или ID
    identifier = context.args[0]
    reason = ' '.join(context.args[1:]) if len(context.args) > 1 else 'Нарушение правил'
    
    user_info = get_user_by_id_or_username(identifier)

    if not user_info:
        await update.message.reply_text(
            f"❌ Пользователь '{identifier}' не найден!\n"
            f"Убедитесь что он запускал бота (@{context.bot.username})"
        )
        return

    user_id = user_info['user_id']
    user_name = user_info['username'] or f"{user_info['first_name']} {user_info['last_name']}" or f"Игрок #{user_id}"

    # Проверяем, не забанен ли уже
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT is_banned FROM users WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        if row and row[0]:
            await update.message.reply_text(f"⚠️ Пользователь уже забанен!")
            return
        
        # Банит пользователя
        cursor.execute('''
            UPDATE users SET
                is_banned = 1,
                banned_at = CURRENT_TIMESTAMP,
                ban_reason = ?
            WHERE user_id = ?
        ''', (reason, user_id))
        conn.commit()
        
        logger.info(f"🚫 BAN: user_id={user_id}, reason={reason}")
        
        # Отправляем уведомление админу
        await update.message.reply_text(
            f"✅ <b>Пользователь забанен!</b>\n\n"
            f"👤 {user_name} (@{user_info['username'] or 'нет'})\n"
            f"🆔 ID: {user_id}\n"
            f"📝 Причина: {reason}\n\n"
            f"Пользователь больше не сможет использовать бота.",
            parse_mode=ParseMode.HTML
        )
        
        # Отправляем уведомление пользователю
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"⛔️ <b>Вы заблокированы!</b>\n\n"
                    f"📝 Причина: {reason}\n\n"
                    f"Вы больше не можете использовать бота Raccoon Life."
                ),
                parse_mode=ParseMode.HTML
            )
            logger.info(f"📬 Уведомление о бане отправлено пользователю {user_id}")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось отправить уведомление пользователю {user_id}: {e}")
            
    except Exception as e:
        logger.error(f"Ошибка ban_user_admin: {e}")
        await update.message.reply_text(f"❌ Ошибка при бане: {e}")
    finally:
        conn.close()


async def give_tokens_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Команда для админа: /give <amount> [reason] (ответ на сообщение)
    Начисляет шишки пользователю.
    """
    # Проверка прав администратора
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ У вас нет прав для этой команды!")
        return

    # Проверка, является ли команда ответом на сообщение
    if not update.effective_message.reply_to_message:
        await update.message.reply_text("❌ Эта команда должна быть ответом на сообщение пользователя, которому вы хотите начислить шишки.")
        return

    # Получаем ID получателя из сообщения, на которое ответили
    recipient_user = update.effective_message.reply_to_message.from_user
    recipient_id = recipient_user.id
    recipient_name = f"@{recipient_user.username}" if recipient_user.username else (f"{recipient_user.first_name} {recipient_user.last_name}".strip() or f"Игрок #{recipient_id}")

    # Проверка аргументов (количество шишек)
    if not context.args or len(context.args) < 1:
        await update.message.reply_text("❌ Укажите количество шишек для начисления.")
        return

    try:
        amount = int(context.args[0])
        reason_parts = context.args[1:]
        reason = ' '.join(reason_parts) if reason_parts else 'Начисление админом'
    except ValueError:
        await update.message.reply_text("❌ Количество шишек должно быть числом.")
        return

    if amount <= 0:
        await update.message.reply_text("❌ Количество шишек должно быть больше 0.")
        return

    # Начисляем шишки получателю
    add_result = add_tokens(recipient_id, amount, reason=f'admin_grant:{reason}')

    if add_result:
        # Отправляем уведомление админу
        await update.message.reply_text(
            f"✅ Успешно!\n"
            f"💰 Начислено: {amount} Шишек\n"
            f"➡️ Получателю: {recipient_name} (ID: {recipient_id})\n"
            f"💳 Баланс получателя: {add_result['balance']} Шишек"
        )

        # Отправляем уведомление получателю
        try:
            await context.bot.send_message(
                chat_id=recipient_id,
                text=(
                    f"🎉 <b>Вам начислены шишки!</b>\n\n"
                    f"💰 Сумма: <b>+{amount} Шишек</b>\n"
                    f"📝 Причина: {reason}\n"
                    f"💳 Ваш баланс: {add_result['balance']} Шишек"
                ),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.warning(f"⚠️ Не удалось отправить уведомление получателю {recipient_id}: {e}")
    else:
        await update.message.reply_text("❌ Произошла ошибка при начислении шишек получателю. Пожалуйста, проверьте логи.")


async def unban_user_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Команда для админа: /unban <username|user_id>
    Разбанивает пользователя
    """
    # Проверка прав администратора
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ У вас нет прав для этой команды!")
        return

    # Проверка аргументов
    if len(context.args) < 1:
        await update.message.reply_text(
            "❌ Использование: /unban <username|user_id>\n"
            "Пример: /unban @username\n"
            "Пример: /unban 123456789"
        )
        return

    # Ищем пользователя по username или ID
    identifier = context.args[0]
    user_info = get_user_by_id_or_username(identifier)

    if not user_info:
        await update.message.reply_text(
            f"❌ Пользователь '{identifier}' не найден!\n"
            f"Убедитесь что он запускал бота (@{context.bot.username})"
        )
        return

    user_id = user_info['user_id']
    user_name = user_info['username'] or f"{user_info['first_name']} {user_info['last_name']}" or f"Игрок #{user_id}"

    # Разбаниваем пользователя
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT is_banned FROM users WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        if not row or not row[0]:
            await update.message.reply_text(f"⚠️ Пользователь не забанен!")
            return
        
        cursor.execute('''
            UPDATE users SET
                is_banned = 0,
                banned_at = NULL,
                ban_reason = NULL
            WHERE user_id = ?
        ''', (user_id,))
        conn.commit()
        
        logger.info(f"✅ UNBAN: user_id={user_id}")
        
        await update.message.reply_text(
            f"✅ <b>Пользователь разбанен!</b>\n\n"
            f"👤 {user_name} (@{user_info['username'] or 'нет'})\n"
            f"🆔 ID: {user_id}\n\n"
            f"Пользователь снова может использовать бота.",
            parse_mode=ParseMode.HTML
        )
        
        # Отправляем уведомление пользователю
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"✅ <b>Вы разбанены!</b>\n\n"
                    f"Вы снова можете использовать бота Raccoon Life."
                ),
                parse_mode=ParseMode.HTML
            )
            logger.info(f"📬 Уведомление о разбане отправлено пользователю {user_id}")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось отправить уведомление пользователю {user_id}: {e}")
            
    except Exception as e:
        logger.error(f"Ошибка unban_user_admin: {e}")
        await update.message.reply_text(f"❌ Ошибка при разбане: {e}")
    finally:
        conn.close()


async def broadcast_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Команда для админа: /broadcast <сообщение>
    Рассылает сообщение всем активным пользователям бота
    """
    logger.info(f"📢 [BROADCAST] Получена команда от ID: {update.effective_user.id}. Ожидаемый ADMIN_ID: {ADMIN_ID}")

    # Проверка прав администратора
    if update.effective_user.id != ADMIN_ID:
        await update.effective_message.reply_text("❌ У вас нет прав для этой команды!")
        return

    # Проверяем, есть ли текст или медиа
    message = update.effective_message
    raw_text = message.text or message.caption or ""
    parts = raw_text.split(maxsplit=1)
    
    # Проверка наличия контента (текста или медиафайла)
    if len(parts) < 2 and not message.photo and not message.video:
        await update.effective_message.reply_text(
            "❌ Использование: /broadcast <текст сообщения>\n"
            "Пример: /broadcast 📢 Всем привет!\n"
            "💡 Также можно прикрепить картинку или видео и написать команду в подписи!"
        )
        return

    message_text = parts[1] if len(parts) > 1 else ""

    # Извлекаем ID медиа, если оно есть
    photo_id = message.photo[-1].file_id if message.photo else None
    video_id = message.video.file_id if message.video else None

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Выбираем всех пользователей, которые не забанены
        cursor.execute('SELECT user_id FROM users WHERE is_banned = 0')
        users = cursor.fetchall()
        
        if not users:
            await update.effective_message.reply_text("⚠️ В базе нет активных пользователей для рассылки.")
            return

        await update.effective_message.reply_text(f"⏳ Начинаю рассылку для {len(users)} пользователей. Пожалуйста, подождите...")

        success_count = 0
        fail_count = 0

        for row in users:
            user_id = row['user_id']
            retry_count = 0
            while retry_count < 3:
                try:
                    # Отправляем медиа если есть, иначе текст
                    if photo_id:
                        await context.bot.send_photo(
                            chat_id=user_id,
                            photo=photo_id,
                            caption=message_text,
                            parse_mode=ParseMode.HTML
                        )
                    elif video_id:
                        await context.bot.send_video(
                            chat_id=user_id,
                            video=video_id,
                            caption=message_text,
                            parse_mode=ParseMode.HTML
                        )
                    else:
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=message_text,
                            parse_mode=ParseMode.HTML
                        )
                    success_count += 1
                    break  # Успешно отправлено, выходим из цикла
                except RetryAfter as e:
                    retry_count += 1
                    logger.warning(f"Лимит Telegram (FloodControl). Ждем {e.retry_after} сек...")
                    await asyncio.sleep(e.retry_after + 1)
                except Exception as e:
                    logger.warning(f"Не удалось отправить сообщение пользователю {user_id}: {e}")
                    fail_count += 1
                    break  # Другая ошибка (заблокировал бота и т.д.), пропускаем

            # Небольшая пауза, чтобы не превысить лимиты Telegram API (около 30 сообщений в секунду)
            await asyncio.sleep(0.05)

        await update.effective_message.reply_text(f"✅ <b>Рассылка завершена!</b>\n\n📤 Успешно отправлено: {success_count}\n❌ Ошибок (бот заблокирован и т.д.): {fail_count}", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Ошибка при рассылке: {e}")
        await update.effective_message.reply_text(f"❌ Произошла ошибка при рассылке: {e}")
    finally:
        conn.close()


# ==================== КОМАНДЫ ТОТАЛИЗАТОРА (TELEGRAM) ====================

async def tot_create_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    text = ' '.join(context.args)
    parts = [p.strip() for p in text.split('|')]
    if len(parts) < 6:
        await update.message.reply_text("❌ Использование: /tot_create Название | Сторона 1 | Коэф 1 | Сторона 2 | Коэф 2 | Время начала | [URL Картинки]")
        return
    try:
        image_url = parts[6] if len(parts) > 6 else None
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO tot_events (title, side1_name, side1_odds, side2_name, side2_odds, start_time, image_url, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft')", 
                       (parts[0], parts[1], float(parts[2]), parts[3], float(parts[4]), parts[5], image_url))
        event_id = cursor.lastrowid
        conn.commit()
        conn.close()
        await update.message.reply_text(f"✅ Событие #{event_id} создано (статус: draft). Активация: /tot_active {event_id}")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def tot_active_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not context.args: return
    event_id = context.args[0]
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE tot_events SET status = 'active' WHERE event_id = ?", (event_id,))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Событие #{event_id} АКТИВНО (игроки могут ставить).")

async def tot_lock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not context.args: return
    event_id = context.args[0]
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, amount, currency FROM tot_bets WHERE event_id = ? AND status = 'pending'", (event_id,))
    for b in cursor.fetchall():
        if b['currency'] == 'Шишки':
            add_tokens(b['user_id'], int(b['amount']), f"tot_refund:{event_id}")
    cursor.execute("DELETE FROM tot_bets WHERE event_id = ? AND status = 'pending'", (event_id,))
    cursor.execute("UPDATE tot_events SET status = 'locked' WHERE event_id = ?", (event_id,))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"🔒 Событие #{event_id} ЗАБЛОКИРОВАНО. Непринятые ставки удалены.")

async def tot_finish_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if len(context.args) < 2:
        await update.message.reply_text("❌ Использование: /tot_finish <event_id> <победитель 1 или 2>")
        return
    event_id, winner = int(context.args[0]), int(context.args[1])
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM tot_events WHERE event_id = ?", (event_id,))
        event = cursor.fetchone()
        if not event: return
        
        cursor.execute("UPDATE tot_events SET status = 'finished', winner = ? WHERE event_id = ?", (winner, event_id))
        cursor.execute("UPDATE tot_bets SET status = 'won' WHERE event_id = ? AND status = 'accepted' AND side = ?", (event_id, winner))
        cursor.execute("UPDATE tot_bets SET status = 'lost' WHERE event_id = ? AND status = 'accepted' AND side != ?", (event_id, winner))
        
        # Отчет для админа
        odds = event['side1_odds'] if winner == 1 else (event['side2_odds'] if winner == 2 else event.get('draw_odds', 1.0))
        cursor.execute("SELECT b.amount, b.currency, u.username, u.first_name FROM tot_bets b JOIN users u ON b.user_id = u.user_id WHERE b.event_id = ? AND b.status = 'won'", (event_id,))
        report = f"🏆 <b>Итоги события #{event_id}</b>\nПобедила сторона {winner} (x{odds})\n\nК выплате:\n"
        total_cg = 0
        total_cones = 0
        for w in cursor.fetchall():
            win_amount = int(w['amount'] * odds)
            if w['currency'] == 'Шишки':
                total_cones += win_amount
            else:
                total_cg += win_amount
            report += f"➖ @{w['username'] or w['first_name']}: {win_amount} {w['currency']}\n"
        report += f"\nВсего: {total_cg} CG, {total_cones} Шишек.\nДля выплаты введите: <code>/tot_pay {event_id}</code>"
        
        await update.message.reply_text(report, parse_mode=ParseMode.HTML)
        conn.commit()
    finally:
        conn.close()

async def tot_pay_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not context.args: return
    event_id = int(context.args[0])
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT b.bet_id, b.user_id, b.amount, b.currency, e.side1_odds, e.side2_odds, e.draw_odds, e.winner FROM tot_bets b JOIN tot_events e ON b.event_id = e.event_id WHERE b.event_id = ? AND b.status = 'won'", (event_id,))
        bets = cursor.fetchall()
        for b in bets:
            odds = b['side1_odds'] if b['winner'] == 1 else (b['side2_odds'] if b['winner'] == 2 else b.get('draw_odds', 1.0))
            win_amount = int(b['amount'] * odds)
            if b['currency'] == 'Шишки':
                add_tokens(b['user_id'], win_amount, f'tot_win:{event_id}')
            elif b['currency'] == 'CG':
                add_tokens(b['user_id'], win_amount, f'tot_win_cg:{event_id}')
                add_tokens(b['user_id'], win_amount * 10, f'tot_win_cones:{event_id}')
        cursor.execute("UPDATE tot_bets SET status = 'paid' WHERE event_id = ? AND status = 'won'", (event_id,))
        conn.commit()
        await update.message.reply_text(f"✅ Выплаты по событию #{event_id} завершены! Роздано {len(bets)} победителям.")
    finally:
        conn.close()

async def tot_bet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id != ADMIN_ID:
        await query.answer("❌ Нет прав", show_alert=True)
        return
        
    action, bet_id = query.data.split('_')[1], int(query.data.split('_')[2])
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT b.user_id, b.amount, b.currency, b.status, e.title, b.event_id FROM tot_bets b JOIN tot_events e ON b.event_id = e.event_id WHERE b.bet_id = ?", (bet_id,))
        bet = cursor.fetchone()
        if bet and bet['status'] == 'pending':
            if action == 'accept':
                cursor.execute("UPDATE tot_bets SET status = 'accepted' WHERE bet_id = ?", (bet_id,))
                await context.bot.send_message(chat_id=bet['user_id'], text=f"✅ Ваша ставка ({bet['amount']} {bet['currency']}) на <b>{bet['title']}</b> ПРИНЯТА.", parse_mode=ParseMode.HTML)
            else:
                cursor.execute("UPDATE tot_bets SET status = 'rejected' WHERE bet_id = ?", (bet_id,))
                if bet['currency'] == 'Шишки':
                    add_tokens(bet['user_id'], int(bet['amount']), f"tot_refund:{bet['event_id']}")
                await context.bot.send_message(chat_id=bet['user_id'], text=f"❌ Ваша ставка на <b>{bet['title']}</b> ОТКЛОНЕНА.", parse_mode=ParseMode.HTML)
            conn.commit()
            await query.edit_message_text(f"{query.message.text_html}\n\nСтатус изменен: <b>{'ПРИНЯТО' if action=='accept' else 'ОТКЛОНЕНО'}</b>", parse_mode=ParseMode.HTML)
        else:
            await query.answer("Ставка уже обработана", show_alert=True)
    finally:
        conn.close()


async def shend_tokens_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Команда для пользователей в группах: /shend <amount> (ответ на сообщение)
    Передает шишки от одного пользователя другому.
    """
    # Проверка, является ли команда ответом на сообщение
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Эта команда должна быть ответом на сообщение пользователя, которому вы хотите передать шишки.")
        return

    sender = update.effective_user
    recipient = update.message.reply_to_message.from_user

    # Проверка на само-перевод
    if sender.id == recipient.id:
        await update.message.reply_text("❌ Нельзя передать шишки самому себе!")
        return
    
    # Проверка, что получатель не бот
    if recipient.is_bot:
        await update.message.reply_text("❌ Нельзя передать шишки боту!")
        return

    # Парсинг суммы
    if not context.args or len(context.args) < 1:
        await update.message.reply_text("❌ Укажите количество шишек для передачи.\nПример: /shend 100")
        return

    try:
        amount = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Количество шишек должно быть целым положительным числом.")
        return

    if amount <= 0:
        await update.message.reply_text("❌ Количество шишек должно быть больше 0.")
        return

    # Выполнение транзакции
    spend_result = spend_tokens(sender.id, amount, reason=f'user_transfer_to:{recipient.id}')

    if not spend_result:
        sender_balance = get_user_tokens(sender.id).get('balance', 0)
        await update.message.reply_text(f"❌ У вас недостаточно шишек! Ваш баланс: {sender_balance} Шишек")
        return

    add_result = add_tokens(recipient.id, amount, reason=f'user_transfer_from:{sender.id}')

    if not add_result:
        # Возврат средств в случае ошибки
        add_tokens(sender.id, amount, reason=f'refund_failed_transfer_to:{recipient.id}')
        await update.message.reply_text("❌ Произошла ошибка при начислении шишек получателю. Средства возвращены вам.")
        logger.error(f"Critical error: failed to add tokens to {recipient.id}, but tokens were spent from {sender.id}. REFUNDED.")
        return

    # Успешное сообщение в группе
    sender_name = f"@{sender.username}" if sender.username else (sender.first_name or f"Игрок #{sender.id}")
    recipient_name = f"@{recipient.username}" if recipient.username else (recipient.first_name or f"Игрок #{recipient.id}")
    
    sender_name_safe = html.escape(sender_name)
    recipient_name_safe = html.escape(recipient_name)
    
    await update.message.reply_text(
        f"✅ <b>Перевод выполнен!</b>\n\n"
        f"<b>От:</b> {sender_name_safe}\n"
        f"<b>Кому:</b> {recipient_name_safe}\n"
        f"<b>Сумма:</b> {amount} Шишек\n\n"
        f"<i>Балансы обновлены.</i>",
        parse_mode=ParseMode.HTML
    )

    # Опционально: уведомления в ЛС
    try:
        await context.bot.send_message(chat_id=sender.id, text=f"💸 Вы успешно перевели <b>{amount} Шишек</b> пользователю {recipient_name_safe}.\n💳 Ваш новый баланс: {spend_result['balance']} Шишек", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"Не удалось отправить ЛС-уведомление отправителю {sender.id}: {e}")

    try:
        await context.bot.send_message(chat_id=recipient.id, text=f"🎉 Вам поступил перевод <b>{amount} Шишек</b> от пользователя {sender_name_safe}!\n💳 Ваш новый баланс: {add_result['balance']} Шишек", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"Не удалось отправить ЛС-уведомление получателю {recipient.id}: {e}")


async def track_chat_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отслеживает активность пользователей в чатах для будущих розыгрышей."""
    if not update.effective_chat or not update.effective_user or update.effective_user.is_bot:
        return

    chat_id = update.effective_chat.id
    user = update.effective_user

    # Убедимся, что пользователь есть в основной таблице
    ensure_user_exists(user.id, {'username': user.username, 'first_name': user.first_name, 'last_name': user.last_name})

    # Обновляем запись об активности
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            INSERT INTO chat_activity (chat_id, user_id, last_message_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET last_message_at = CURRENT_TIMESTAMP
        ''', (chat_id, user.id))
        conn.commit()
    finally:
        conn.close()

async def farsh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Команда для пользователей в группах: /farsh <сумма> <кол-во_пользователей>
    Списывает <сумма> шишек с отправителя и делит их случайным образом 
    между <кол-во_пользователей> случайными пользователями (макс. 10).
    Общая сумма выигрыша равна заявленной.
    """
    if not context.args or len(context.args) != 2:
        await update.message.reply_text("❌ Использование: /farsh <сумма> <кол-во_пользователей>\nПример: /farsh 1000 5")
        return

    try:
        amount = int(context.args[0])
        num_users = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Сумма и количество пользователей должны быть целыми числами.")
        return

    if amount <= 0 or num_users <= 0:
        await update.message.reply_text("❌ Сумма и количество пользователей должны быть больше 0.")
        return
        
    if num_users > 10:
        await update.message.reply_text("❌ Максимальное количество пользователей для фарша - 10.")
        return

    sender = update.effective_user
    chat_id = update.effective_chat.id

    # Проверка баланса и списание
    spend_result = spend_tokens(sender.id, amount, reason=f'farsh_initiated:{num_users}_users')
    if not spend_result:
        sender_balance = get_user_tokens(sender.id).get('balance', 0)
        await update.message.reply_text(f"❌ У вас недостаточно шишек для такого фарша! (Нужно {amount} Шишек)\nВаш баланс: {sender_balance} Шишек")
        return

    # Получение списка получателей
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT user_id, username, first_name FROM users WHERE is_banned = 0 AND user_id != ?", (sender.id,))
        potential_recipients = cursor.fetchall()
    finally:
        conn.close()

    if len(potential_recipients) < num_users:
        add_tokens(sender.id, amount, reason='farsh_refund:not_enough_users')
        await update.message.reply_text(f"❌ Недостаточно пользователей в боте для раздачи ({len(potential_recipients)}). Нужно {num_users}. Шишки возвращены.")
        return

    # Случайный выбор победителей
    winners = random.sample(potential_recipients, num_users)
    
    # --- Новая логика случайного распределения с сохранением суммы ---
    if amount < num_users:
        add_tokens(sender.id, amount, reason='farsh_refund:amount_too_small')
        await update.message.reply_text(f"❌ Сумма {amount} Шишек слишком мала для разделения на {num_users} пользователей. Шишки возвращены.")
        return

    prizes = []
    remaining_amount = amount

    for i in range(num_users - 1):
        # Оставляем как минимум 1 шишку для каждого оставшегося участника
        max_prize = remaining_amount - (num_users - 1 - i)
        # Убедимся, что можем раздать хотя бы по 1
        if max_prize < 1:
            # Эта ситуация не должна возникать при amount >= num_users, но для надежности
            win_amount = 1
        else:
            win_amount = random.randint(1, max_prize)
        
        prizes.append(win_amount)
        remaining_amount -= win_amount

    # Последний получает весь остаток
    prizes.append(remaining_amount)
    
    # Перемешиваем призы, чтобы не было предвзятости к последнему
    random.shuffle(prizes)
    # --- Конец новой логики ---
    
    total_distributed = 0
    winner_details = []

    # Начисление победителям
    for i, winner_row in enumerate(winners):
        # Пропускаем, если по какой-то причине приз оказался нулевым
        if i >= len(prizes) or prizes[i] <= 0:
            continue
        
        win_amount = prizes[i]
            
        add_tokens(winner_row['user_id'], win_amount, reason=f'farsh_win_from:{sender.id}')
        total_distributed += win_amount
        
        winner_name = html.escape(f"@{winner_row['username']}" if winner_row['username'] else (winner_row['first_name'] or f"Игрок #{winner_row['user_id']}"))
        
        winner_details.append({'id': winner_row['user_id'], 'name': winner_name, 'amount': win_amount})
        
        try:
            await context.bot.send_message(chat_id=winner_row['user_id'], text=f"🎉 Вы выиграли в фарше!\n\nВам начислено <b>{win_amount} Шишек</b> от пользователя {html.escape(sender.first_name)}!", parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning(f"Не удалось отправить ЛС-уведомление о фарше победителю {winner_row['user_id']}: {e}")

    # Возвращаем остаток, если он есть (из-за ошибок, в теории не должно быть)
    remainder = amount - total_distributed
    if remainder > 0:
        add_tokens(sender.id, remainder, reason='farsh_remainder_refund')

    # Уведомление в чат
    sender_name_safe = html.escape(sender.first_name or sender.username)
    
    winner_lines = []
    for winner in winner_details:
        winner_lines.append(f"• <a href='tg://user?id={winner['id']}'>{winner['name']}</a> получает <b>{winner['amount']} Шишек</b>")

    message = (
        f"🥩 <b>{sender_name_safe} запустил(а) ФАРШ!</b>\n\n"
        f"Общая сумма <b>{amount} Шишек</b> была случайным образом разделена между {num_users} счастливчиками!\n\n"
        f"<b>Победители:</b>\n" + "\n".join(winner_lines)
    )
    if remainder > 0: message += f"\n\n<i>(Остаток {remainder} Шишек возвращен отправителю)</i>"
    await update.message.reply_text(message, parse_mode=ParseMode.HTML)


async def delete_user_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Команда для админа: /delete <username|user_id>
    Полностью удаляет пользователя из базы данных
    """
    # Проверка прав администратора
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ У вас нет прав для этой команды!")
        return

    # Проверка аргументов
    if len(context.args) < 1:
        await update.message.reply_text(
            "❌ Использование: /delete <username|user_id>\n"
            "Пример: /delete @username\n"
            "Пример: /delete 123456789"
        )
        return

    # Ищем пользователя по username или ID
    identifier = context.args[0]
    user_info = get_user_by_id_or_username(identifier)

    if not user_info:
        await update.message.reply_text(
            f"❌ Пользователь '{identifier}' не найден!\n"
            f"Убедитесь что он запускал бота (@{context.bot.username})"
        )
        return

    user_id = user_info['user_id']
    user_name = user_info['username'] or f"{user_info['first_name']} {user_info['last_name']}" or f"Игрок #{user_id}"

    # Получаем баланс для отображения
    tokens = get_user_tokens(user_id)

    # Отправляем подтверждение
    await update.message.reply_text(
        f"⚠️ <b>Подтверждение удаления</b>\n\n"
        f"👤 {user_name} (@{user_info['username'] or 'нет'})\n"
        f"🆔 ID: {user_id}\n"
        f"💰 Баланс: {tokens['balance']} Шишек\n\n"
        f"Все данные пользователя будут безвозвратно удалены!\n"
        f"Для подтверждения отправьте: /delete_confirm {user_id}",
        parse_mode=ParseMode.HTML
    )


async def delete_user_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Команда для админа: /delete_confirm <user_id>
    Подтверждение удаления пользователя
    """
    # Проверка прав администратора
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ У вас нет прав для этой команды!")
        return

    # Проверка аргументов
    if len(context.args) < 1:
        await update.message.reply_text("❌ Укажите ID пользователя: /delete_confirm <user_id>")
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом!")
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Проверяем существует ли пользователь
        cursor.execute('SELECT username, first_name, last_name FROM users WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        
        if not row:
            await update.message.reply_text(f"❌ Пользователь {user_id} не найден!")
            return
        
        user_name = row[0] or f"{row[1]} {row[2]}" or f"Игрок #{user_id}"
        
        # Удаляем пользователя из всех таблиц
        cursor.execute('DELETE FROM boss_damage WHERE user_id = ?', (user_id,))
        cursor.execute('DELETE FROM user_stats WHERE user_id = ?', (user_id,))
        cursor.execute('DELETE FROM user_tokens WHERE user_id = ?', (user_id,))
        cursor.execute('DELETE FROM users WHERE user_id = ?', (user_id,))
        
        conn.commit()
        
        logger.info(f"🗑️ DELETE: user_id={user_id} ({user_name})")
        
        await update.message.reply_text(
            f"✅ <b>Пользователь удален!</b>\n\n"
            f"👤 {user_name}\n"
            f"🆔 ID: {user_id}\n\n"
            f"Все данные безвозвратно удалены.",
            parse_mode=ParseMode.HTML
        )
        
    except Exception as e:
        logger.error(f"Ошибка delete_user_confirm: {e}")
        await update.message.reply_text(f"❌ Ошибка при удалении: {e}")
        conn.rollback()
    finally:
        conn.close()


async def reset_news_cooldown_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Команда для админа: /notime <username|user_id>
    Сбрасывает таймер отправки новостей для пользователя
    """
    # Проверка прав администратора
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ У вас нет прав для этой команды!")
        return

    # Проверка аргументов
    if len(context.args) < 1:
        await update.message.reply_text(
            "❌ Использование: /notime <username|user_id>\n"
            "Пример: /notime @username\n"
            "Пример: /notime 123456789"
        )
        return

    # Ищем пользователя по username или ID
    identifier = context.args[0]
    user_info = get_user_by_id_or_username(identifier)

    if not user_info:
        await update.message.reply_text(f"❌ Пользователь '{identifier}' не найден!")
        return

    user_id = user_info['user_id']
    user_name = user_info['username'] or f"{user_info['first_name']} {user_info['last_name']}" or f"Игрок #{user_id}"

    # Сбрасываем лимит в БД
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('UPDATE user_stats SET last_news_submit = NULL WHERE user_id = ?', (user_id,))
        conn.commit()
        logger.info(f"⏳ Сброс таймера новостей: user_id={user_id} админом {update.effective_user.id}")
        await update.message.reply_text(f"✅ <b>Таймер отправки новостей сброшен!</b>\n\nПользователь <b>{user_name}</b> теперь может отправить новость прямо сейчас.", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Ошибка reset_news_cooldown_admin: {e}")
        await update.message.reply_text(f"❌ Ошибка при сбросе таймера: {e}")
    finally:
        conn.close()


def get_full_user_profile_admin(identifier: str) -> dict:
    """
    Получает полную сводку данных игрока со всех таблиц БД
    """
    user_info = get_user_by_id_or_username(identifier)
    if not user_info:
        return None

    user_id = user_info['user_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # 1. Данные пользователя
        cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        user_row = cursor.fetchone()

        # 2. Шишки
        cursor.execute('SELECT * FROM user_tokens WHERE user_id = ?', (user_id,))
        token_row = cursor.fetchone()

        # 3. Статистика игр
        cursor.execute('SELECT * FROM user_stats WHERE user_id = ?', (user_id,))
        stats_row = cursor.fetchone()

        # 4. Урон боссу
        cursor.execute('SELECT * FROM boss_damage WHERE user_id = ?', (user_id,))
        boss_row = cursor.fetchone()

        # 5. Ставки тотализатора
        cursor.execute('''
            SELECT 
                COUNT(*) as total_bets,
                COALESCE(SUM(amount), 0) as total_amount,
                SUM(CASE WHEN status = 'won' OR status = 'paid' THEN 1 ELSE 0 END) as won_bets,
                SUM(CASE WHEN status = 'lost' THEN 1 ELSE 0 END) as lost_bets,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending_bets,
                SUM(CASE WHEN status = 'accepted' THEN 1 ELSE 0 END) as active_bets
            FROM tot_bets WHERE user_id = ?
        ''', (user_id,))
        tot_row = cursor.fetchone()

        # 6. Совместные крафты
        cursor.execute('SELECT COUNT(*) as count FROM coop_crafts WHERE initiator_id = ?', (user_id,))
        crafts_created_row = cursor.fetchone()
        crafts_created = crafts_created_row['count'] if crafts_created_row else 0

        cursor.execute('SELECT COUNT(*) as count FROM coop_craft_stages WHERE contributor_id = ?', (user_id,))
        crafts_contributed_row = cursor.fetchone()
        crafts_contributed = crafts_contributed_row['count'] if crafts_contributed_row else 0

        # 7. Ранги в лидербордах
        rank_overall = get_user_rank_in_leaderboard(user_id, 'overall')
        rank_tokens = get_user_rank_in_leaderboard(user_id, 'tokens')
        rank_quests = get_user_rank_in_leaderboard(user_id, 'quests')

        # Квесты и газеты
        quests_list = []
        if stats_row and stats_row['quests']:
            try:
                quests_list = json.loads(stats_row['quests'])
            except Exception:
                quests_list = []

        qt2_quests = ['qt2_1', 'qt2_2', 'qt2_3', 'qt2_4', 'qt2_5', 'qt2_6', 'qt2_7']
        qt2_done = sum(1 for q in qt2_quests if q in quests_list)
        newspapers_read = sum(1 for q in quests_list if isinstance(q, str) and (q.startswith('news_') or q.startswith('caps_news')))

        combined_row = {
            'balance': token_row['balance'] if token_row else 0,
            'quests_completed': stats_row['quests_completed'] if stats_row else 0,
            'clown_games': stats_row['clown_games'] if stats_row else 0,
            'vladeos_games': stats_row['vladeos_games'] if stats_row else 0,
            'tower_total_levels': stats_row['tower_total_levels'] if stats_row else 0,
            'roulette_games': stats_row['roulette_games'] if stats_row else 0,
            'roulette_total_bets': stats_row['roulette_total_bets'] if stats_row and 'roulette_total_bets' in stats_row.keys() else 0,
            'roulette_cones_lost': stats_row['roulette_cones_lost'] if stats_row else 0,
            'quests': stats_row['quests'] if stats_row else '[]'
        }
        score_info = calculate_overall_score(combined_row)

        # 8. Рефералы
        referral_stats = get_user_referral_stats(user_id)

        return {
            'user': dict(user_row) if user_row else dict(user_info),
            'tokens': dict(token_row) if token_row else {'balance': 0, 'total_earned': 0, 'total_spent': 0, 'last_earn': None},
            'stats': dict(stats_row) if stats_row else {},
            'boss': dict(boss_row) if boss_row else {'total_damage': 0, 'hits': 0, 'last_hit': None},
            'tot': dict(tot_row) if tot_row else {},
            'crafts_created': crafts_created,
            'crafts_contributed': crafts_contributed,
            'score_info': score_info,
            'qt2_done': qt2_done,
            'newspapers_read': newspapers_read,
            'rank_overall': rank_overall.get('rank') if rank_overall else None,
            'rank_tokens': rank_tokens.get('rank') if rank_tokens else None,
            'rank_quests': rank_quests.get('rank') if rank_quests else None,
            'referral_stats': referral_stats
        }
    finally:
        conn.close()


async def user_stats_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Команда /stats (также /user, /player):
    - Для любого игрока без аргументов: показывает свою подробную статистику
    - Для админа с аргументами (@username/id) или ответом на сообщение: показывает досье указанного игрока
    """
    caller = update.effective_user
    if not caller:
        return

    caller_id = caller.id

    # Гарантируем регистрацию вызывающего пользователя в БД
    ensure_user_exists(caller_id, caller.username, caller.first_name, caller.last_name)

    is_admin = (caller_id == ADMIN_ID)
    identifier = None
    notice_text = ""

    # Проверяем аргументы или ответ на сообщение
    if context.args and len(context.args) > 0:
        target_arg = context.args[0].strip()
        if is_admin:
            identifier = target_arg
        else:
            # Обычный игрок пытается посмотреть чужую статистику
            clean_username = (caller.username or '').lower().lstrip('@')
            clean_arg = target_arg.lower().lstrip('@')
            if clean_arg != str(caller_id) and clean_arg != clean_username:
                notice_text = "ℹ️ <i>Просмотр статистики других игроков доступен только администраторам.\nНиже отображена ваша статистика:</i>\n\n"
            identifier = str(caller_id)
    elif update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_user = update.message.reply_to_message.from_user
        if is_admin:
            identifier = str(target_user.id)
        else:
            if target_user.id != caller_id:
                notice_text = "ℹ️ <i>Просмотр статистики других игроков доступен только администраторам.\nНиже отображена ваша статистика:</i>\n\n"
            identifier = str(caller_id)
    else:
        # Просмотр собственной статистики
        identifier = str(caller_id)

    profile = get_full_user_profile_admin(identifier)
    if not profile:
        await update.message.reply_text(
            f"❌ Пользователь <b>{html.escape(identifier)}</b> не найден в базе данных!",
            parse_mode=ParseMode.HTML
        )
        return

    u = profile['user']
    t = profile['tokens']
    s = profile['stats']
    b = profile['boss']
    tot = profile['tot']
    sc = profile['score_info']

    is_self = (u['user_id'] == caller_id)
    user_name = html.escape(f"@{u['username']}" if u.get('username') else (f"{u.get('first_name', '')} {u.get('last_name', '')}".strip() or f"Игрок #{u['user_id']}"))
    header_title = f"📊 <b>ВАША СТАТИСТИКА</b>: {user_name}" if is_self else f"📊 <b>ДОСЬЕ ИГРОКА</b>: {user_name}"
    
    status_str = "⛔️ <b>ЗАБАНЕН</b>" if u.get('is_banned') else "🟢 <b>Активен</b>"
    if u.get('is_banned') and u.get('ban_reason'):
        status_str += f" <i>(Причина: {html.escape(u['ban_reason'])})</i>"

    reg_date = str(u.get('registered_at') or '—').split('.')[0]

    clown_games = s.get('clown_games', 0)
    clown_wins = s.get('clown_wins', 0)
    clown_wr = f"{(clown_wins / clown_games * 100):.1f}%" if clown_games > 0 else "0%"

    vladeos_games = s.get('vladeos_games', 0)
    vladeos_wins = s.get('vladeos_wins', 0)
    vladeos_wr = f"{(vladeos_wins / vladeos_games * 100):.1f}%" if vladeos_games > 0 else "0%"

    roulette_games = s.get('roulette_games', 0)
    roulette_wins = s.get('roulette_wins', 0)
    roulette_won = s.get('roulette_cones_won', 0)
    roulette_lost = s.get('roulette_cones_lost', 0)
    roulette_bets = sc.get('roulette_bets', 0)

    r_overall = f"#{profile['rank_overall']}" if profile['rank_overall'] else "—"
    r_tokens = f"#{profile['rank_tokens']}" if profile['rank_tokens'] else "—"
    r_quests = f"#{profile['rank_quests']}" if profile['rank_quests'] else "—"

    wallet_raw = u.get('wallet_address')
    wallet_str = f"<code>{html.escape(wallet_raw)}</code>" if wallet_raw else "<i>не привязан</i>"

    msg = (
        f"{notice_text}"
        f"{header_title}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>ID:</b> <code>{u['user_id']}</code>\n"
        f"💎 <b>TON Кошелёк:</b> {wallet_str}\n"
        f"📌 <b>Статус:</b> {status_str}\n"
        f"📅 <b>Регистрация:</b> {reg_date}\n\n"

        f"🏆 <b>ОБЩИЙ РЕЙТИНГ:</b> <b>{sc['total_score']:,} очков</b> (Место: {r_overall})\n"
        f" ├ 🌰 Баланс: +{sc['balance']:,}\n"
        f" ├ 📜 Квесты (x10 000): +{sc['quests_completed'] * 10000:,} ({sc['quests_completed']} шт)\n"
        f" ├ 🎮 Игры (x100): +{sc['total_games'] * 100:,} ({sc['total_games']} игр)\n"
        f" ├ 🎰 Рулетка (x0.5): +{sc['roulette_bets_points']:,} (ставки: {roulette_bets:,})\n"
        f" └ 📰 Газеты (x500): +{sc['newspapers_read'] * 500:,} ({sc['newspapers_read']} вып)\n\n"

        f"💰 <b>ЭКОНОМИКА (ШИШКИ)</b>:\n"
        f" ├ 💳 <b>Баланс:</b> <b>{t['balance']:,}</b> Шишек (Место: {r_tokens})\n"
        f" ├ 📈 Всего заработано: {t['total_earned']:,}\n"
        f" └ 📉 Всего потрачено: {t['total_spent']:,}\n\n"

        f"🎮 <b>МИНИ-ИГРЫ</b>:\n"
        f" ├ 🤡 <b>Клоун:</b> {clown_games} игр | {clown_wins} побед (WR: {clown_wr})\n"
        f" ├ ⚔️ <b>Vladeos:</b> {vladeos_games} игр | {vladeos_wins} побед (WR: {vladeos_wr})\n"
        f" ├ 🏰 <b>Башня 3.0:</b> Макс. ур: {s.get('tower_max_level', 0)} | Пройдено ур: {s.get('tower_total_levels', 0)}\n"
        f" ├ 🎰 <b>Рулетка:</b> {roulette_games} спинов | Выиграно: +{roulette_won:,} | Проиграно: -{roulette_lost:,}\n"
        f" └ 🔺 <b>Мировой Босс:</b> Урон: {b.get('total_damage', 0):,} | Ударов: {b.get('hits', 0):,}\n\n"

        f"📜 <b>КВЕСТЫ И КОНТЕНТ</b>:\n"
        f" ├ 🗺 <b>Квесты QT2:</b> {profile['qt2_done']}/7 (Место: {r_quests})\n"
        f" ├ 📰 <b>Газет прочитано:</b> {profile['newspapers_read']} вып.\n"
        f" ├ 🛠 <b>Крафты:</b> создано {profile['crafts_created']} | помог в {profile['crafts_contributed']} эт.\n"
        f" └ 🎲 <b>Тотализатор:</b> ставок {tot.get('total_bets') or 0} на {int(tot.get('total_amount') or 0):,} (Выиграно: {tot.get('won_bets') or 0} | В игре: {int(tot.get('active_bets') or 0) + int(tot.get('pending_bets') or 0)})\n\n"

        f"👥 <b>РЕФЕРАЛЬНАЯ ПРОГРАММА</b>:\n"
        f" ├ 🤝 <b>Приглашено игроков:</b> <b>{profile.get('referral_stats', {}).get('referrals_count', 0)}</b> чел.\n"
        f" └ 👤 <b>Пригласил:</b> {('@' + profile['referral_stats']['referrer_username']) if profile.get('referral_stats', {}).get('referrer_username') else 'Прямой вход'}\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def web_app_data_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик данных от WebApp (tg.sendData)"""
    user_id = update.effective_user.id
    raw_data = update.effective_message.web_app_data.data
    
    try:
        data = json.loads(raw_data)
        data_type = data.get('type')
        
        logger.info(f"📨 WebAppData: type={data_type}, user_id={user_id}")
        
        if data_type == 'sync_stats':
            if save_user_stats(user_id, data):
                await update.message.reply_text("✨ Данные сохранены в облаке!")
            else:
                await update.message.reply_text("⚠️ Не удалось сохранить данные")
                
        elif data_type == 'boss_damage':
            damage = data.get('damage', 0)
            if damage > 0:
                boss_info = add_boss_damage(user_id, damage)
                if boss_info:
                    logger.info(f"💥 Босс: {user_id} нанес {damage} урона")
                    
    except Exception as e:
        logger.error(f"Ошибка обработки WebAppData: {e}")


async def publish_news_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатия на кнопку 'Опубликовать' под предложенной новостью"""
    query = update.callback_query

    # Проверка прав администратора
    if update.effective_user.id != ADMIN_ID:
        await query.answer("❌ У вас нет прав!", show_alert=True)
        return

    await query.answer("Публикуем...")

    try:
        # Получаем сообщение, к которому привязана кнопка
        message = query.message
        photo_id = message.photo[-1].file_id if message.photo else None
        
        # Получаем текст (из подписи или из тела сообщения)
        text_to_publish = ""
        original_text_html = ""
        if photo_id:
            original_text_html = message.caption_html
            if "➖➖➖➖➖➖" in original_text_html:
                text_to_publish = original_text_html.split("➖➖➖➖➖➖")[0].strip()
            else:
                text_to_publish = original_text_html
        else:
            original_text_html = message.text_html
            if "➖➖➖➖➖➖" in original_text_html:
                text_to_publish = original_text_html.split("➖➖➖➖➖➖")[0].strip()
            else:
                text_to_publish = original_text_html

        # Публикуем в канал/группу
        if photo_id:
            await context.bot.send_photo(
                chat_id="@the_raccoon_times_group",
                photo=photo_id,
                caption=text_to_publish,
                parse_mode=ParseMode.HTML
            )
        else:
            await context.bot.send_message(
                chat_id="@the_raccoon_times_group",
                text=text_to_publish,
                parse_mode=ParseMode.HTML
            )

        # Обновляем сообщение админа: убираем кнопку и пишем, что опубликовано
        await query.edit_message_reply_markup(reply_markup=None)
        
    except Exception as e:
        logger.error(f"Ошибка при публикации новости: {e}")
        await query.answer("❌ Ошибка при публикации!", show_alert=True)


async def chip_set_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик модерации сетов фишек админом"""
    query = update.callback_query
    if update.effective_user.id != ADMIN_ID:
        await query.answer("❌ У вас нет прав!", show_alert=True)
        return

    data = query.data  # chip_set_approve_123, chip_set_reject_123, chip_set_rejask_123, chip_set_rejquick_123, chip_set_cancel_123
    try:
        parts = data.split('_')
        action = parts[2]
        set_id = int(parts[3])

        if action == 'approve':
            await query.answer("Одобряем и публикуем...")
            res = approve_chip_set_db(set_id)
            if res.get('status') == 'ok':
                curr_caption = query.message.caption_html or ""
                await query.edit_message_caption(
                    caption=f"{curr_caption}\n\n✅ <b>СЕТ ОДОБРЕН И ОПУБЛИКОВАН В ГРУППЕ</b>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=None
                )
            else:
                await query.answer(f"❌ Ошибка: {res.get('message')}", show_alert=True)

        elif action == 'reject':
            # Предлагаем написать причину или отклонить без причины (оставить пустым)
            await query.answer("Выберите способ отклонения")
            rej_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✍️ Указать причину", callback_data=f"chip_set_rejask_{set_id}"),
                    InlineKeyboardButton("⏩ Без причины (пусто)", callback_data=f"chip_set_rejquick_{set_id}")
                ],
                [
                    InlineKeyboardButton("↩️ Отмена", callback_data=f"chip_set_cancel_{set_id}")
                ]
            ])
            await query.edit_message_reply_markup(reply_markup=rej_keyboard)

        elif action == 'rejask':
            # Админ хочет ввести причину текстом в чат
            context.user_data['pending_reject_set_id'] = set_id
            context.user_data['pending_reject_msg_id'] = query.message.message_id
            context.user_data['pending_reject_chat_id'] = query.message.chat_id

            await query.answer("Жду сообщение с причиной...")
            rej_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⏩ Отклонить без причины (пусто)", callback_data=f"chip_set_rejquick_{set_id}"),
                    InlineKeyboardButton("↩️ Отмена", callback_data=f"chip_set_cancel_{set_id}")
                ]
            ])
            await query.edit_message_reply_markup(reply_markup=rej_keyboard)
            await query.message.reply_text(
                f"✍️ <b>Отклонение сета #{set_id}:</b>\n\n"
                f"Напишите причину отклонения в ответном сообщении, или нажмите <b>«Отклонить без причины (пусто)»</b>.",
                parse_mode=ParseMode.HTML
            )

        elif action == 'rejquick':
            # Отклонение без причины
            context.user_data.pop('pending_reject_set_id', None)
            context.user_data.pop('pending_reject_msg_id', None)
            context.user_data.pop('pending_reject_chat_id', None)

            await query.answer("Отклоняем без причины...")
            res = reject_chip_set_db(set_id, reason="")
            if res.get('status') == 'ok':
                curr_caption = query.message.caption_html or ""
                await query.edit_message_caption(
                    caption=f"{curr_caption}\n\n❌ <b>СЕТ ОТКЛОНЁН</b> <i>(без причины)</i>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=None
                )
            else:
                await query.answer(f"❌ Ошибка: {res.get('message')}", show_alert=True)

        elif action == 'cancel':
            context.user_data.pop('pending_reject_set_id', None)
            context.user_data.pop('pending_reject_msg_id', None)
            context.user_data.pop('pending_reject_chat_id', None)

            await query.answer("Отменено")
            original_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Одобрить и опубликовать", callback_data=f"chip_set_approve_{set_id}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"chip_set_reject_{set_id}")
                ]
            ])
            await query.edit_message_reply_markup(reply_markup=original_keyboard)

    except Exception as e:
        logger.error(f"Ошибка обработки chip_set_callback: {e}")
        await query.answer("❌ Ошибка обработки запроса", show_alert=True)


async def handle_admin_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстовых сообщений админа в ЛС (ввод причины отклонения сета)"""
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return

    pending_set_id = context.user_data.get('pending_reject_set_id')
    if not pending_set_id:
        return

    reason = update.message.text.strip() if update.message and update.message.text else ""
    context.user_data.pop('pending_reject_set_id', None)
    card_msg_id = context.user_data.pop('pending_reject_msg_id', None)
    card_chat_id = context.user_data.pop('pending_reject_chat_id', None)

    res = reject_chip_set_db(pending_set_id, reason=reason)
    if res.get('status') == 'ok':
        reason_display = f"<i>«{html.escape(reason)}»</i>" if reason else "<i>(без причины)</i>"
        await update.message.reply_text(
            f"❌ <b>Сет #{pending_set_id} успешно отклонён.</b>\nПричина: {reason_display}\nАвтор уведомлён.",
            parse_mode=ParseMode.HTML
        )
        if card_msg_id and card_chat_id:
            try:
                await context.bot.edit_message_caption(
                    chat_id=card_chat_id,
                    message_id=card_msg_id,
                    caption=f"🎨 <b>СЕТ #{pending_set_id} ОТКЛОНЁН</b>\n\n📝 <b>Причина:</b> {html.escape(reason) if reason else 'Не указана'}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=None
                )
            except Exception as e:
                logger.error(f"Ошибка обновления карточки модерации: {e}")
    else:
        await update.message.reply_text(f"❌ Ошибка: {res.get('message')}")


async def pre_checkout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ответ на pre_checkout_query (подтверждение платежа Telegram Stars)"""
    query = update.pre_checkout_query
    if query.invoice_payload and query.invoice_payload.startswith("cones_10000_"):
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Неизвестный заказ.")


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка успешной оплаты Telegram Stars"""
    payment = update.message.successful_payment
    payload = payment.invoice_payload
    user_id = update.effective_user.id

    if payload and payload.startswith("cones_10000_"):
        # Проверяем и записываем транзакцию
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR IGNORE INTO processed_payments (payment_id, user_id, payment_type, amount, cones_amount, comment)
                VALUES (?, ?, 'stars', 100, 10000, ?)
            ''', (payload, user_id, f"telegram_stars_100:{payment.telegram_payment_charge_id}"))
            conn.commit()
        finally:
            conn.close()

        # Автоматически начисляем 10,000 шишек игроку
        result = add_tokens(user_id, 10000, reason="stars_100_purchase")
        logger.info(f"🌟 Успешная покупка 10,000 шишек за 100 Звёзд пользователем {user_id}")

        await update.message.reply_text(
            "🌟 <b>Оплата 100 Звёзд прошла успешно!</b>\n\n"
            "✨ На ваш игровой баланс зачислено <b>10,000 Шишек</b>!\n"
            f"💳 Ваш баланс: <b>{result['balance'] if result else 0:,} Шишек</b>.\n\n"
            "Приятной игры в <b>Raccoon Life</b>! 🦝",
            parse_mode=ParseMode.HTML
        )

        # Отправляем уведомление администратору
        if ADMIN_ID and context.bot:
            user = update.effective_user
            user_name = f"@{user.username}" if user.username else (f"{user.first_name or ''} {user.last_name or ''}".strip() or f"Игрок #{user_id}")
            admin_text = (
                f"🌟 <b>НОВАЯ ОПЛАТА TELEGRAM STARS!</b>\n\n"
                f"👤 <b>Игрок:</b> {html.escape(user_name)}\n"
                f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
                f"⭐ <b>Оплачено:</b> 100 Звёзд (XTR)\n"
                f"🌲 <b>Начислено игроку:</b> +10,000 Шишек\n"
                f"💳 <b>Новый баланс:</b> {result['balance'] if result else 0:,} Шишек\n"
                f"🔖 <b>Charge ID:</b> <code>{html.escape(payment.telegram_payment_charge_id)}</code>"
            )
            try:
                await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode=ParseMode.HTML)
                logger.info(f"📬 Уведомление о Stars оплате отправлено админу {ADMIN_ID}")
            except Exception as e:
                logger.error(f"⚠️ Ошибка отправки Stars уведомления админу: {e}")


async def post_init(application: Application):
    """Инициализация после запуска бота"""
    if WEBAPP_URL:
        try:
            # Устанавливаем кнопку меню
            await application.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Играть", web_app=WebAppInfo(url=WEBAPP_URL))
            )
            logger.info("✅ Menu button set")
        except Exception as e:
            logger.error(f"⚠️ Ошибка установки кнопки меню: {e}")

    try:
        commands = [
            BotCommand('start', '🚀 Запустить бота'),
            BotCommand('stats', '📊 Моя статистика и профиль'),
            BotCommand('shend', '💸 Передать шишки (в группе)'),
            BotCommand('farsh', '🥩 Разделить шишки между игроками'),
            BotCommand('add', '💰 Начислить шишки (админ)'),
            BotCommand('balance', '💳 Проверить баланс (админ)'),
            BotCommand('spend', '💸 Списать шишки (админ)'),
            BotCommand('ban', '⛔️ Забанить пользователя (админ)'),
            BotCommand('give', '💸 Передать шишки игроку (админ)'),
            BotCommand('broadcast', '📢 Рассылка всем (админ)'),
            BotCommand('unban', '✅ Разбанить пользователя (админ)'),
            BotCommand('delete', '🗑️ Удалить пользователя (админ)'),
            BotCommand('notime', '⏳ Сбросить лимит новостей (админ)')
        ]
        await application.bot.set_my_commands(commands)
        logger.info("✅ Commands menu set")
    except Exception as e:
        logger.error(f"⚠️ Ошибка установки команд: {e}")


async def debug_all_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шпион: записывает в лог всё, что видит бот (для отладки)"""
    if update.effective_user:
        logger.info(f"👀 Бот увидел от {update.effective_user.id}: {update.effective_message.text if update.effective_message else 'Не текст'}")

def run_flask():
    """Запуск Flask сервера в отдельном потоке"""
    app.run(host='0.0.0.0', port=FLASK_PORT, debug=False, use_reloader=False, threaded=True)


def main():
    """Точка входа приложения"""
    # Инициализация БД 
    init_db()

    # Запуск Flask в фоне
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"🚀 Flask API server started on port {FLASK_PORT}")

    # Запуск фонового блокчейн-верификатора входящих транзакций TON/GRAM
    watcher_thread = Thread(target=ton_blockchain_watcher_thread, daemon=True)
    watcher_thread.start()
    logger.info("💎 TON/GRAM Blockchain watcher started")

    # Запуск фонового чекера активности игроков (механика Кабана)
    boar_thread = Thread(target=boar_watcher_thread, daemon=True)
    boar_thread.start()
    logger.info("🐗 Boar activity watcher thread started")

    # Проверка наличия BOT_TOKEN
    if not BOT_TOKEN:
        logger.critical("❌ ОШИБКА: BOT_TOKEN не задан! Создайте файл .env (в корне или папке bot) и укажите BOT_TOKEN=ваш_токен_бота")
        return

    # Настройка Telegram бота
    builder = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(60.0)
        .read_timeout(60.0)
        .pool_timeout(60.0)
        .post_init(post_init)
    )
    
    proxy_url = os.getenv("PROXY_URL")
    if proxy_url:
        logger.info(f"🔌 Используется прокси: {proxy_url}")
        builder = builder.proxy_url(proxy_url).get_updates_proxy_url(proxy_url)
        
    telegram_app = builder.build()

    # Регистрируем обработчики
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("shend", shend_tokens_user, filters=filters.ChatType.GROUPS))
    telegram_app.add_handler(CommandHandler("farsh", farsh_command, filters=filters.ChatType.GROUPS))
    telegram_app.add_handler(CommandHandler("add", add_tokens_admin))
    # Обработчик для отслеживания активности в чатах, должен идти после команд
    # чтобы не срабатывать на них. group=10 для низкого приоритета.
    telegram_app.add_handler(MessageHandler(filters.ChatType.GROUPS & (~filters.COMMAND), track_chat_activity), group=10)
    telegram_app.add_handler(CommandHandler("balance", get_balance_admin))
    telegram_app.add_handler(CommandHandler("stats", user_stats_admin))
    telegram_app.add_handler(CommandHandler("user", user_stats_admin))
    telegram_app.add_handler(CommandHandler("player", user_stats_admin))
    telegram_app.add_handler(CommandHandler("spend", spend_tokens_admin))
    telegram_app.add_handler(CommandHandler("ban", ban_user_admin))
    telegram_app.add_handler(CommandHandler("give", give_tokens_admin))
    telegram_app.add_handler(CommandHandler("unban", unban_user_admin))
    telegram_app.add_handler(CommandHandler("delete", delete_user_admin))
    telegram_app.add_handler(CommandHandler("notime", reset_news_cooldown_admin))
    telegram_app.add_handler(CommandHandler("broadcast", broadcast_admin))
    telegram_app.add_handler(CommandHandler("delete_confirm", delete_user_confirm))
    telegram_app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data_handler))
    telegram_app.add_handler(CallbackQueryHandler(publish_news_callback, pattern="^publish_news$"))
    telegram_app.add_handler(CallbackQueryHandler(chip_set_callback, pattern=r"^chip_set_(approve|reject|rejask|rejquick|cancel)_\d+$"))
    telegram_app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & (~filters.COMMAND), handle_admin_private_message))

    telegram_app.add_handler(CommandHandler("tot_create", tot_create_cmd))
    telegram_app.add_handler(CommandHandler("tot_active", tot_active_cmd))
    telegram_app.add_handler(CommandHandler("tot_lock", tot_lock_cmd))
    telegram_app.add_handler(CommandHandler("tot_finish", tot_finish_cmd))
    telegram_app.add_handler(CommandHandler("tot_pay", tot_pay_cmd))
    telegram_app.add_handler(CallbackQueryHandler(tot_bet_callback, pattern="^tot_(accept|reject)_"))

    # Telegram Stars (XTR) платежи
    telegram_app.add_handler(PreCheckoutQueryHandler(pre_checkout_callback))
    telegram_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

    # Шпион работает в группе 1, чтобы читать сообщения параллельно командам
    telegram_app.add_handler(MessageHandler(filters.ALL, debug_all_updates), group=1)

    # Запуск бота
    logger.info("🤖 Starting Telegram bot...")
    telegram_app.run_polling()


if __name__ == '__main__':
    main()
