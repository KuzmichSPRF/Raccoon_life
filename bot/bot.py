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
import time
import asyncio
from urllib.parse import parse_qsl
from pathlib import Path
from threading import Thread
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram import BotCommand
from telegram.error import RetryAfter
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
security_logger = logging.getLogger('security')  # Отдельный logger для security событий

# Пути к файлам
# При exec() __file__ не работает, используем абсолютный путь
try:
    _current_file = __file__
except NameError:
    _current_file = str(Path.cwd() / 'bot' / 'bot.py')

BOT_DIR = Path(_current_file).parent
PROJECT_DIR = BOT_DIR.parent
DB_PATH = str(BOT_DIR / "raccoon_main.db")
WEBAPP_DIR = PROJECT_DIR / "webapp"

logger.info(f"Database: {DB_PATH}")
logger.info(f"WebApp: {WEBAPP_DIR}")

# Flask приложение
# Указываем root_path явно для работы при exec()
app = Flask(
    __name__,
    static_folder=str(WEBAPP_DIR),
    static_url_path='',
    root_path=str(PROJECT_DIR)
)

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
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

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


# ==================== БАЗА ДАННЫХ ====================

def get_db_connection():
    """Создает подключение к базе данных"""
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
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
                ban_reason TEXT
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

        # Инициализация босса
        cursor.execute('''
            INSERT OR IGNORE INTO boss_global (id, current_hp, max_hp, kill_count)
            VALUES (1, 1000000000, 1000000000, 0)
        ''')
        
        conn.commit()

        # Миграции: добавляем недостающие колонки
        _add_missing_columns(cursor)
        conn.commit()
        
        # Миграция: создание таблицы игровых сессий
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='game_sessions'")
        if not cursor.fetchone():
            cursor.execute('''
                CREATE TABLE game_sessions (
                    user_id INTEGER PRIMARY KEY,
                    game_type TEXT,
                    state TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

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


def ensure_user_exists(user_id: int, user_data: dict = None):
    """Гарантирует существование пользователя в БД"""
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Проверяем существует ли пользователь
        cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        exists = cursor.fetchone()

        if exists:
            # Обновляем данные если есть
            if user_data:
                username = user_data.get('username', '')
                first_name = user_data.get('first_name', '')
                last_name = user_data.get('last_name', '')

                cursor.execute('''
                    UPDATE users SET
                        username = ?,
                        first_name = ?,
                        last_name = ?
                    WHERE user_id = ?
                ''', (username, first_name, last_name, user_id))
                logger.debug(f"Пользователь {user_id} обновлён")
            else:
                logger.debug(f"Пользователь {user_id} уже существует")
        else:
            # Создаем нового пользователя
            username = user_data.get('username', '') if user_data else ''
            first_name = user_data.get('first_name', '') if user_data else ''
            last_name = user_data.get('last_name', '') if user_data else ''

            cursor.execute('''
                INSERT INTO users (user_id, username, first_name, last_name)
                VALUES (?, ?, ?, ?)
            ''', (user_id, username, first_name, last_name))
            logger.debug(f"Пользователь {user_id} создан")

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
            ON CONFLICT(user_id) DO UPDATE SET
                game_type = excluded.game_type,
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
        cursor.execute('SELECT quests FROM user_stats WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        existing_quests = []
        if row and row['quests']:
            try:
                existing_quests = json.loads(row['quests'])
            except json.JSONDecodeError:
                pass
                
        incoming_quests = stats_data.get('quests', [])
        merged_quests = list(set(existing_quests + incoming_quests))

        # Обновляем статистику
        cursor.execute('''
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
                quests = ?
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
            json.dumps(merged_quests),
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
    """Получает текущее HP босса"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute('SELECT current_hp, max_hp, kill_count FROM boss_global WHERE id = 1')
        row = cursor.fetchone()
        
        if row:
            return {
                'current_hp': row['current_hp'],
                'max_hp': row['max_hp'],
                'kill_count': row['kill_count']
            }
        return {'current_hp': 1000000000, 'max_hp': 1000000000, 'kill_count': 0}
        
    except Exception as e:
        logger.error(f"Ошибка get_boss_hp: {e}")
        return {'current_hp': 1000000000, 'max_hp': 1000000000, 'kill_count': 0}
    finally:
        conn.close()


def get_player_stats(user_id: int) -> dict:
    """Получает статистику игрока"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            SELECT clown_games, clown_wins, vladeos_games, vladeos_wins,
                   tower_max_level, tower_total_levels, quests,
                   roulette_games, roulette_wins,
                   roulette_cones_won, roulette_cones_lost
            FROM user_stats WHERE user_id = ?
        ''', (user_id,))
        row = cursor.fetchone()
        
        if row:
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
                'quests': json.loads(row['quests']) if row['quests'] else []
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
            WHERE ut.balance > 0
            ORDER BY ut.balance DESC
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


# ==================== API ROUTES ====================

@app.route('/')
def index_route():
    """Главная страница - отдает index.html"""
    return app.send_static_file('index.html')


@app.route('/<path:filename>')
def static_files(filename):
    """Отдача статических файлов из webapp/"""
    return app.send_static_file(filename)


@app.route('/api/boss_hp', methods=['GET'])
def api_get_boss_hp():
    """Получить HP босса"""
    boss_info = get_boss_hp()
    response = jsonify({'status': 'ok', 'boss': boss_info})
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


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


@app.route('/api/leaderboard', methods=['GET'])
def api_get_leaderboard():
    """Получить рейтинг игроков по урону боссу"""
    try:
        limit = request.args.get('limit', 10)
        limit = int(limit) if limit else 10
        limit = min(limit, 100)  # Максимум 100 игроков

        leaderboard = get_leaderboard(limit)

        response = jsonify({
            'status': 'ok',
            'leaderboard': leaderboard
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
    """Получить баланс шишек пользователя"""
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

        logger.info(f"💰 Ответ: balance={tokens['balance']}")
        
        return jsonify({
            'status': 'ok',
            'tokens': tokens
        })

    except Exception as e:
        logger.error(f"Ошибка api_get_tokens: {e}")
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

        if bet_amount <= 0:
            return jsonify({'error': 'betAmount must be > 0'}), 400

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

        # ДЖЕКПОТ 0.1% (1 из 1000)
        is_jackpot = False
        if random.random() < 0.001:
            is_jackpot = True
            win = True
            # При джекпоте колесо останавливается на специальном секторе
            result_segment = {'type': 'jackpot', 'value': 777}

        win_amount = 0
        if win:
            if is_jackpot:
                win_amount = bet_amount * 100
                add_tokens(user_id, win_amount, f'roulette_jackpot:{bet_type}')
                logger.info(f"💎 Roulette JACKPOT: user_id={user_id}, bet={bet_amount}, win={win_amount}")
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
                    roulette_cones_lost = roulette_cones_lost + ?
                WHERE user_id = ?
            ''', (1 if win else 0, cones_won, cones_lost, user_id))
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
            'isJackpot': is_jackpot
        })

    except Exception as e:
        logger.error(f"Ошибка api_casino_roulette: {e}")
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
            return jsonify({'error': 'user_id required'}), 400

        user_id = int(user_id)

        # Криптографическая проверка авторизации
        init_data = request.headers.get('X-Telegram-Init-Data')
        auth_user = validate_webapp_data(init_data)
        if not auth_user or str(auth_user.get('id')) != str(user_id):
            security_logger.warning(f"🚨 БЛОКИРОВКА Атаки: неверная подпись! user_id={user_id}")
            logger.warning(f"🚨 БЛОКИРОВКА Атаки: неверная подпись!")
            return jsonify({'error': 'Unauthorized'}), 403

        # Проверка бана
        if is_user_banned(user_id):
            security_logger.warning(f"🚨 BANNED USER: user_id={user_id} попытался атаковать босса")
            logger.warning(f"⚠️ Забаненный пользователь попытался атаковать босса")
            return jsonify({'error': 'User is banned'}), 403

        damage = 0
        heal = 0
        is_crit = False
        energy_change = 0

        # Серверная логика урона и затрат энергии
        if action == 'basic':
            damage = random.randint(50, 100)
            energy_change = 20
            is_crit = random.random() < 0.15
        elif action == 'strong':
            damage = random.randint(150, 250)
            energy_change = -40
            is_crit = random.random() < 0.15
        elif action == 'ultimate':
            damage = random.randint(400, 700)
            energy_change = -80
            is_crit = True
        elif action == 'heal':
            heal = random.randint(30, 50)
            energy_change = -50
        else:
            logger.warning(f"⚠️ Неизвестное действие: {action}")
            return jsonify({'error': 'Invalid action'}), 400

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

        return jsonify({
            'status': 'ok',
            'damage': damage,
            'is_crit': is_crit,
            'heal': heal,
            'energy_change': energy_change,
            'boss_damage': boss_damage,
            'boss_hp': boss_info['current_hp'],
            'tokens_earned': tokens_earned
        })

    except Exception as e:
        logger.error(f"❌ Ошибка api_boss_attack: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/game/vladeos', methods=['POST'])
@limiter.limit("30 per minute")
def api_game_vladeos():
    """Логика Vladeos PvP"""
    try:
        data = request.get_json()
        user_id = int(data.get('userId'))
        auth_user = validate_webapp_data(request.headers.get('X-Telegram-Init-Data'))
        if not auth_user or str(auth_user.get('id')) != str(user_id): return jsonify({'error': 'Unauthorized'}), 403
            
        is_win = random.random() < 0.05
        if is_win:
            v_score = random.randint(1, 90)
            p_score = v_score + 1
            add_tokens(user_id, 100, 'vladeos_win')
        else:
            p_score = random.randint(1, 90)
            v_score = p_score + 1
            
        return jsonify({'status': 'ok', 'win': is_win, 'p_score': p_score, 'v_score': v_score})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/game/battleship', methods=['POST'])
@limiter.limit("30 per minute")
def api_game_battleship():
    """Античит Морского боя - кулдаун 10 сек"""
    try:
        data = request.get_json()
        user_id = int(data.get('userId'))
        auth_user = validate_webapp_data(request.headers.get('X-Telegram-Init-Data'))
        if not auth_user or str(auth_user.get('id')) != str(user_id): return jsonify({'error': 'Unauthorized'}), 403
            
        state = get_game_session(user_id, 'battleship')
        now = time.time()
        if state and (now - state.get('last_win', 0)) < 10:
            return jsonify({'error': 'Too fast'}), 400
            
        save_game_session(user_id, 'battleship', {'last_win': now})
        add_tokens(user_id, 100, 'battleship_win')
        return jsonify({'status': 'ok'})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/game/clown', methods=['POST'])
@limiter.limit("120 per minute")
def api_game_clown():
    """Логика Битвы Фишек (Клоун)"""
    try:
        data = request.get_json()
        user_id = int(data.get('userId'))
        action = data.get('action')
        auth_user = validate_webapp_data(request.headers.get('X-Telegram-Init-Data'))
        if not auth_user or str(auth_user.get('id')) != str(user_id): return jsonify({'error': 'Unauthorized'}), 403
        
        if action == 'start':
            state = {'pHP': 100, 'pNRG': 0, 'bHP': 100, 'bNRG': 0}
            save_game_session(user_id, 'clown', state)
            return jsonify({'status': 'ok', 'state': state})

        state = get_game_session(user_id, 'clown')
        if not state: return jsonify({'status': 'error', 'error': 'No active session'}), 400

        # Обработка лечения печенькой (два варианта: 'cookie' и 'cookie_heal')
        if action in ['cookie', 'cookie_heal']:
            spend_result = spend_tokens(user_id, 100, 'cookie_heal')
            if not spend_result: return jsonify({'status': 'error', 'error': 'Недостаточно токенов!'}), 400
            state['pHP'] = 100
            save_game_session(user_id, 'clown', state)
            return jsonify({'status': 'ok', 'state': state, 'tokens': spend_result})
            
        dmg, heal, cost = 0, 0, 0
        is_crit = random.random() < 0.2
        
        if action == 'attack': dmg = 10; state['pNRG'] = min(100, state['pNRG'] + 20)
        elif action == 'trash': dmg = 25; cost = 40
        elif action == 'snack': heal = 30; cost = 30
        elif action == 'rage': dmg = 50; cost = 80; is_crit = True
        
        if state['pNRG'] < cost: return jsonify({'status': 'error', 'error': 'Not enough energy'}), 400
        state['pNRG'] -= cost
        if is_crit and dmg > 0: dmg = int(dmg * 1.5)
        
        state['bHP'] -= dmg
        state['pHP'] = min(100, state['pHP'] + heal)
        
        player_log = {'dmg': dmg, 'heal': heal, 'is_crit': is_crit, 'action': action, 'pHP': state['pHP'], 'pNRG': state['pNRG'], 'bHP': state['bHP'], 'bNRG': state['bNRG']}
        
        if state['bHP'] <= 0:
            add_tokens(user_id, 10, 'clown_win')
            clear_game_session(user_id, 'clown')
            return jsonify({'status': 'ok', 'state': state, 'player_log': player_log, 'game_over': True, 'win': True})
            
        b_dmg, b_heal = 0, 0
        b_crit = random.random() < 0.15
        b_action = 'attack'
        
        if state['bNRG'] >= 70: b_dmg = 40; state['bNRG'] -= 70; b_action = 'bomb'
        elif state['bHP'] < 40 and state['bNRG'] >= 30: b_heal = 25; state['bNRG'] -= 30; b_action = 'heal'
        else: b_dmg = 12; state['bNRG'] = min(100, state['bNRG'] + 25)
        
        if b_crit and b_dmg > 0: b_dmg = int(b_dmg * 1.5)
        state['pHP'] -= b_dmg
        state['bHP'] = min(100, state['bHP'] + b_heal)
        
        bot_log = {'dmg': b_dmg, 'heal': b_heal, 'is_crit': b_crit, 'action': b_action, 'pHP': state['pHP'], 'pNRG': state['pNRG'], 'bHP': state['bHP'], 'bNRG': state['bNRG']}
        game_over = state['pHP'] <= 0
        if game_over: clear_game_session(user_id, 'clown')
        else: save_game_session(user_id, 'clown', state)
            
        return jsonify({'status': 'ok', 'state': state, 'player_log': player_log, 'bot_log': bot_log, 'game_over': game_over, 'win': False})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/game/tower', methods=['POST'])
@limiter.limit("120 per minute")
def api_game_tower():
    """Логика Башни"""
    try:
        data = request.get_json()
        user_id = int(data.get('userId'))
        action = data.get('action')
        level = data.get('level', 1)
        
        auth_user = validate_webapp_data(request.headers.get('X-Telegram-Init-Data'))
        if not auth_user or str(auth_user.get('id')) != str(user_id): return jsonify({'error': 'Unauthorized'}), 403
        
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
        if not state: return jsonify({'status': 'error', 'error': 'No active session'}), 400
        
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
            return jsonify({'status': 'ok', 'state': state, 'player_log': player_log, 'game_over': True, 'win': True})
            
        e_dmg = int(state['eDmg'] * random.uniform(0.8, 1.2))
        e_crit = random.random() < 0.1
        if e_crit: e_dmg = int(e_dmg * 1.5)
        state['pHP'] -= e_dmg
        
        bot_log = {'dmg': e_dmg, 'is_crit': e_crit, 'pHP': state['pHP'], 'pNRG': state['pNRG'], 'eHP': state['eHP']}
        game_over = state['pHP'] <= 0
        if game_over: clear_game_session(user_id, 'tower')
        else: save_game_session(user_id, 'tower', state)
            
        return jsonify({'status': 'ok', 'state': state, 'player_log': player_log, 'bot_log': bot_log, 'game_over': game_over, 'win': False})
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

            if str(auth_user.get('id')) != str(user_id):
                security_logger.critical(f"🚨 ПОДДЕЛКА ID: Заявлен {user_id}, реальный {auth_user.get('id')}")
                return jsonify({'status': 'error', 'message': 'ID mismatch'}), 403

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
        'quests': validate_list(data.get('quests', []), default=[])
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

    # АНТИЧИТ: Жесткие лимиты наград в зависимости от причины
    max_allowed = 1000  # Глобальный лимит для неизвестных причин
    if reason.startswith('clown_win'):
        max_allowed = 10
    elif reason.startswith('vladeos_win'):
        max_allowed = 100
    elif reason.startswith('battleship_win'):
        max_allowed = 100
    elif reason.startswith('tower_level:'):
        max_allowed = 100
    elif reason.startswith('read_news:') or reason == 'welcome_bonus':
        max_allowed = 1000  # welcome_bonus = 1000
    elif reason.startswith('quest_complete:'):
        max_allowed = 10000
    elif reason.startswith('find_chip_win'):
        max_allowed = 100

    if amount > max_allowed:
        logger.warning(f"🚨 АНТИЧИТ: user_id={user_id} запросил {amount} токенов за {reason}. Ограничено до {max_allowed}!")
        amount = max_allowed
        
    # АНТИЧИТ: Проверка на повторное получение награды за квесты (защита от сброса кэша)
    if reason.startswith('quest_complete:'):
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
                logger.warning(f"🚨 АНТИЧИТ: Игрок {user_id} пытается повторно получить награду за квест {quest_id}!")
                return jsonify({'status': 'error', 'message': 'Quest already completed'}), 400
            
            # Сразу помечаем квест как выполненный, чтобы заблокировать параллельные абуз-запросы
            existing_quests.append(quest_id)
            cursor.execute('UPDATE user_stats SET quests = ? WHERE user_id = ?', (json.dumps(existing_quests), user_id))
            conn.commit()
        except Exception as e:
            logger.error(f"Ошибка проверки квеста: {e}")
        finally:
            conn.close()

    logger.info(f"💰 earn_tokens: user_id={user_id}, amount={amount}, reason={reason}")

    result = add_tokens(user_id, amount, reason)

    if result:
        return jsonify({'status': 'ok', 'tokens': result})
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


# ==================== TELEGRAM BOT ====================

# Флаг для отслеживания начисления приветственных шишек (в памяти)
WELCOME_BONUS_GRANTED = {}  # user_id -> True

def has_received_welcome_bonus(user_id: int) -> bool:
    """Проверяет получал ли пользователь приветственные шишки"""
    # Проверяем в памяти
    if user_id in WELCOME_BONUS_GRANTED:
        return True
    
    # Проверяем в БД - ищем начисление welcome_bonus
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Проверяем есть ли запись о начислении welcome_bonus в total_earned
        cursor.execute('SELECT total_earned FROM user_tokens WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        if row and row[0] >= 1000:
            # Если пользователь заработал >= 1000 шишек, считаем что он уже получил бонус
            WELCOME_BONUS_GRANTED[user_id] = True
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

        # Проверяем нужно ли начислить приветственные шишки
        # Начисляем только если пользователь новый (первый раз запускает бота)
        if not has_received_welcome_bonus(user.id):
            # Проверяем баланс - если 0, начисляем приветственные
            tokens = get_user_tokens(user.id)
            if tokens['balance'] == 0 and tokens['total_earned'] == 0:
                # Начисляем 1000 приветственных шишек
                result = add_tokens(user.id, 1000, 'welcome_bonus')
                if result:
                    WELCOME_BONUS_GRANTED[user.id] = True
                    logger.info(f"🎁 Приветственные шишки начислены: user_id={user.id}, balance={result['balance']}")

                    # Отправляем приветственное сообщение
                    await update.message.reply_text(
                        f"🎉 <b>Добро пожаловать в Raccoon Life!</b>\n\n"
                        f"🦝 Вы получили <b>1000 Шишек</b> приветственных шишек!\n\n"
                        f"Играйте в игры, выполняйте квесты и зарабатывайте ещё больше шишек! 💰\n\n"
                        f"Нажмите кнопку ниже, чтобы начать:",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton(text="📰 Играть!", web_app=WebAppInfo(url=WEBAPP_URL))
                        ]]),
                        parse_mode=ParseMode.HTML
                    )
                    return  # Выходим чтобы не дублировать сообщение

    except Exception as e:
        logger.error(f"Ошибка сохранения пользователя: {e}")

    # Кнопка для запуска WebApp
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(text="📰 Играть!", web_app=WebAppInfo(url=WEBAPP_URL))
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
    user_name = user_info['username'] or f"{user_info['first_name']} {user_info['last_name']}" or f"Игрок #{user_id}"

    logger.info(f"💰 Начисление шишек: user_id={user_id}, amount={amount}, reason={reason}")

    # Начисляем шишки
    result = add_tokens(user_id, amount, f'admin_grant:{reason}')

    if result:
        # Отправляем уведомление админу
        await update.message.reply_text(
            f"✅ Успешно!\n"
            f"👤 Пользователь: {user_name} (@{user_info['username'] or 'нет'})\n"
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
    user_name = user_info['username'] or f"{user_info['first_name']} {user_info['last_name']}" or f"Игрок #{user_id}"

    # Получаем баланс
    tokens = get_user_tokens(user_id)

    await update.message.reply_text(
        f"💳 <b>Баланс пользователя</b>\n\n"
        f"👤 {user_name} (@{user_info['username'] or 'нет'})\n"
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
    user_name = user_info['username'] or f"{user_info['first_name']} {user_info['last_name']}" or f"Игрок #{user_id}"

    # Списываем шишки
    result = spend_tokens(user_id, amount, f'admin_spend:{reason}')

    if result:
        # Отправляем уведомление админу
        await update.message.reply_text(
            f"✅ Успешно!\n"
            f"👤 Пользователь: {user_name} (@{user_info['username'] or 'нет'})\n"
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


async def post_init(application: Application):
    """Инициализация после запуска бота"""
    try:
        # Устанавливаем кнопку меню
        await application.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Играть", web_app=WebAppInfo(url=WEBAPP_URL))
        )
        logger.info("✅ Menu button set")
    except Exception as e:
        logger.error(f"⚠️ Ошибка установки кнопки меню: {e}")

    try:
        # Устанавливаем список команд для меню
        commands = [
            BotCommand('start', '🚀 Запустить бота'),
            BotCommand('add', '💰 Начислить шишки (админ)'),
            BotCommand('balance', '💳 Проверить баланс (админ)'),
            BotCommand('spend', '💸 Списать шишки (админ)'),
            BotCommand('ban', '⛔️ Забанить пользователя (админ)'),
            BotCommand('broadcast', '📢 Рассылка всем (админ)'),
            BotCommand('unban', '✅ Разбанить пользователя (админ)'),
            BotCommand('delete', '🗑️ Удалить пользователя (админ)')
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
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False, threaded=True)


def main():
    """Точка входа приложения"""
    # Инициализация БД
    init_db()

    # Запуск Flask в фоне
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("🚀 Flask API server started on port 5000")

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
    telegram_app.add_handler(CommandHandler("add", add_tokens_admin))
    telegram_app.add_handler(CommandHandler("balance", get_balance_admin))
    telegram_app.add_handler(CommandHandler("spend", spend_tokens_admin))
    telegram_app.add_handler(CommandHandler("ban", ban_user_admin))
    telegram_app.add_handler(CommandHandler("unban", unban_user_admin))
    telegram_app.add_handler(CommandHandler("delete", delete_user_admin))
    telegram_app.add_handler(CommandHandler("broadcast", broadcast_admin))
    telegram_app.add_handler(CommandHandler("delete_confirm", delete_user_confirm))
    telegram_app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data_handler))

    # Шпион работает в группе 1, чтобы читать сообщения параллельно командам
    telegram_app.add_handler(MessageHandler(filters.ALL, debug_all_updates), group=1)

    # Запуск бота
    logger.info("🤖 Starting Telegram bot...")
    telegram_app.run_polling()


if __name__ == '__main__':
    main()
