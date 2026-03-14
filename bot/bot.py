"""
Raccoon Life Bot - Backend API
Синхронизация игровой статистики и урона по боссу
"""
import os
import logging
import sqlite3
import json
from pathlib import Path
from threading import Thread
from flask import Flask, jsonify, request
from flask_cors import CORS
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
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

# Пути к файлам
# При exec() __file__ не работает, используем абсолютный путь
try:
    _current_file = __file__
except NameError:
    _current_file = str(Path.cwd() / 'bot' / 'bot.py')

BOT_DIR = Path(_current_file).parent
PROJECT_DIR = BOT_DIR.parent
DB_PATH = str(BOT_DIR / "users.db")
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
CORS(app)  # Разрешаем CORS для WebApp


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
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

        # Таблица токенов $ENT
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
        
        # Обновляем статистику
        cursor.execute('''
            UPDATE user_stats SET
                clown_games = ?,
                clown_wins = ?,
                vladeos_games = ?,
                vladeos_wins = ?,
                tower_max_level = ?,
                tower_total_levels = ?,
                quests = ?
            WHERE user_id = ?
        ''', (
            int(stats_data.get('clown_games', 0)),
            int(stats_data.get('clown_wins', 0)),
            int(stats_data.get('vladeos_games', 0)),
            int(stats_data.get('vladeos_wins', 0)),
            int(stats_data.get('tower_max_level', 0)),
            int(stats_data.get('tower_total_levels', 0)),
            json.dumps(stats_data.get('quests', [])),
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
                   tower_max_level, tower_total_levels, quests
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
    Получает топ игроков по максимальному урону по боссу

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

        logger.info(f"🏆 Leaderboard: получено {len(leaderboard)} игроков")
        return leaderboard

    except Exception as e:
        logger.error(f"Ошибка get_leaderboard: {e}")
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
    Получает баланс токенов пользователя

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


def add_tokens(user_id: int, amount: int, reason: str = '') -> dict:
    """
    Начисляет токены пользователю

    Args:
        user_id: ID пользователя в Telegram
        amount: Количество токенов для начисления
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

        logger.info(f"💰 +{amount} $ENT: user_id={user_id}, reason={reason}, balance={tokens_info['balance']}")
        return tokens_info

    except Exception as e:
        logger.error(f"❌ Ошибка add_tokens: {e}")
        conn.rollback()
        return None
    finally:
        conn.close()


def spend_tokens(user_id: int, amount: int, reason: str = '') -> dict:
    """
    Списывает токены у пользователя

    Args:
        user_id: ID пользователя в Telegram
        amount: Количество токенов для списания
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
            logger.warning(f"⚠️ Недостаточно токенов: user_id={user_id}, нужно={amount}, есть={row['balance'] if row else 0}")
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

        logger.info(f"💸 -{amount} $ENT: user_id={user_id}, reason={reason}, balance={tokens_info['balance']}")
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
    """Получить баланс токенов пользователя"""
    try:
        user_id = request.args.get('userId') or request.headers.get('X-Telegram-User-Id', 0)

        logger.info(f"📥 API /api/tokens: userId={user_id}")

        if not user_id:
            logger.warning("⚠️ userId не указан")
            return jsonify({'error': 'user_id required'}), 400

        user_id = int(user_id)
        logger.info(f"🔍 Запрос токенов для user_id={user_id}")
        
        tokens = get_user_tokens(user_id)

        logger.info(f"💰 Ответ: balance={tokens['balance']}")
        
        return jsonify({
            'status': 'ok',
            'tokens': tokens
        })

    except Exception as e:
        logger.error(f"Ошибка api_get_tokens: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/casino/roulette', methods=['POST'])
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
        result_segment = random.choice(segments)

        # Проверка выигрыша
        win = False
        if bet_type == 'red' and result_segment['type'] == 'red':
            win = True
        elif bet_type == 'black' and result_segment['type'] == 'black':
            win = True
        elif bet_type == 'green' and result_segment['type'] == 'green':
            win = True

        win_amount = 0
        if win:
            win_amount = int(bet_amount * multipliers.get(bet_type, 2))
            # Начисляем выигрыш
            add_tokens(user_id, win_amount, f'roulette_win:{bet_type}')
            logger.info(f"🎰 Roulette WIN: user_id={user_id}, bet={bet_amount}, win={win_amount}")
        else:
            logger.info(f"🎰 Roulette LOSE: user_id={user_id}, bet={bet_amount}")

        return jsonify({
            'status': 'ok',
            'result': {
                'number': result_segment['value'],
                'type': result_segment['type']
            },
            'win': win,
            'winAmount': win_amount
        })

    except Exception as e:
        logger.error(f"Ошибка api_casino_roulette: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/sync', methods=['POST'])
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
        
        logger.info(f"📥 API sync: type={data_type}")

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
    
    user_id = int(user_id)
    logger.info(f"👤 sync_stats: user_id={user_id}")
    
    # Извлекаем данные пользователя (если есть)
    user_data = None
    if 'username' in data or 'first_name' in data:
        user_data = {
            'username': data.get('username', ''),
            'first_name': data.get('first_name', ''),
            'last_name': data.get('last_name', '')
        }
        logger.info(f"   Данные пользователя: {user_data}")
    
    # Извлекаем данные статистики
    stats_data = {
        'clown_games': data.get('clown_games', 0),
        'clown_wins': data.get('clown_wins', 0),
        'vladeos_games': data.get('vladeos_games', 0),
        'vladeos_wins': data.get('vladeos_wins', 0),
        'tower_max_level': data.get('tower_max_level', 0),
        'tower_total_levels': data.get('tower_total_levels', 0),
        'quests': data.get('quests', [])
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

    user_id = int(user_id)

    try:
        damage = int(data.get('damage', 0))
    except (ValueError, TypeError):
        damage = 0

    if damage <= 0:
        logger.warning(f"⚠️ boss_damage: damage={damage}")
        return jsonify({'status': 'error', 'message': 'damage must be > 0'}), 400

    logger.info(f"💥 boss_damage: user_id={user_id}, damage={damage}")

    boss_info = add_boss_damage(user_id, damage)

    # Начисляем токены: 10 $ENT за каждые 10000 урона
    if boss_info:
        tokens_earned = (damage // 10000) * 10
        if tokens_earned > 0:
            add_tokens(user_id, tokens_earned, f'boss_damage:{damage}')
            logger.info(f"💰 Начислено {tokens_earned} $ENT за урон боссу")

        return jsonify({'status': 'ok', 'boss': boss_info})
    else:
        return jsonify({'status': 'error', 'message': 'Database error'}), 500


def handle_earn_tokens(data: dict):
    """Обработка начисления токенов за победы и квесты"""
    user_id = data.get('userId') or data.get('user_id')

    if not user_id:
        logger.warning("⚠️ earn_tokens без user_id")
        return jsonify({'status': 'error', 'message': 'user_id required'}), 400

    user_id = int(user_id)

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

    logger.info(f"💰 earn_tokens: user_id={user_id}, amount={amount}, reason={reason}")

    result = add_tokens(user_id, amount, reason)

    if result:
        return jsonify({'status': 'ok', 'tokens': result})
    else:
        return jsonify({'status': 'error', 'message': 'Database error'}), 500


def handle_spend_tokens(data: dict):
    """Обработка списания токенов"""
    user_id = data.get('userId') or data.get('user_id')

    if not user_id:
        logger.warning("⚠️ spend_tokens без user_id")
        return jsonify({'status': 'error', 'message': 'user_id required'}), 400

    user_id = int(user_id)

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

# Флаг для отслеживания начисления приветственных токенов (в памяти)
WELCOME_BONUS_GRANTED = {}  # user_id -> True

def has_received_welcome_bonus(user_id: int) -> bool:
    """Проверяет получал ли пользователь приветственные токены"""
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
            # Если пользователь заработал >= 1000 токенов, считаем что он уже получил бонус
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
        
        # Проверяем нужно ли начислить приветственные токены
        # Начисляем только если пользователь новый (первый раз запускает бота)
        if not has_received_welcome_bonus(user.id):
            # Проверяем баланс - если 0, начисляем приветственные
            tokens = get_user_tokens(user.id)
            if tokens['balance'] == 0 and tokens['total_earned'] == 0:
                # Начисляем 1000 приветственных токенов
                result = add_tokens(user.id, 1000, 'welcome_bonus')
                if result:
                    WELCOME_BONUS_GRANTED[user.id] = True
                    logger.info(f"🎁 Приветственные токены начислены: user_id={user.id}, balance={result['balance']}")
                    
                    # Отправляем приветственное сообщение
                    await update.message.reply_text(
                        f"🎉 <b>Добро пожаловать в Raccoon Life!</b>\n\n"
                        f"🦝 Вы получили <b>1000 $ENT</b> приветственных токенов!\n\n"
                        f"Играйте в игры, выполняйте квесты и зарабатывайте ещё больше токенов! 💰\n\n"
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
    Начисляет токены пользователю и уведомляет его
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

    logger.info(f"💰 Начисление токенов: user_id={user_id}, amount={amount}, reason={reason}")

    # Начисляем токены
    result = add_tokens(user_id, amount, f'admin_grant:{reason}')

    if result:
        # Отправляем уведомление админу
        await update.message.reply_text(
            f"✅ Успешно!\n"
            f"👤 Пользователь: {user_name} (@{user_info['username'] or 'нет'})\n"
            f"🆔 ID: {user_id}\n"
            f"💰 Начислено: {amount} $ENT\n"
            f"📝 Причина: {reason}\n"
            f"💳 Новый баланс: {result['balance']} $ENT"
        )

        # Отправляем уведомление пользователю
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"🎉 <b>Вам начислены токены!</b>\n\n"
                    f"💰 Сумма: <b>+{amount} $ENT</b>\n"
                    f"📝 Причина: {reason}\n"
                    f"💳 Ваш баланс: {result['balance']} $ENT\n\n"
                    f"Продолжайте играть в Raccoon Life! 🦝"
                ),
                parse_mode=ParseMode.HTML
            )
            logger.info(f"📬 Уведомление отправлено пользователю {user_id}")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось отправить уведомление пользователю {user_id}: {e}")
            await update.message.reply_text(
                f"⚠️ Пользователь не найден или заблокировал бота!\n"
                f"Но токены начислены (баланс: {result['balance']} $ENT)"
            )
    else:
        await update.message.reply_text("❌ Ошибка при начислении токенов!")


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
        f"💰 Баланс: <b>{tokens['balance']} $ENT</b>\n"
        f"📊 Всего заработано: {tokens['total_earned']} $ENT\n"
        f"💸 Всего потрачено: {tokens['total_spent']} $ENT",
        parse_mode=ParseMode.HTML
    )


async def spend_tokens_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Команда для админа: /spend <username|user_id> <amount> [reason]
    Списывает токены у пользователя и уведомляет его
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

    # Списываем токены
    result = spend_tokens(user_id, amount, f'admin_spend:{reason}')

    if result:
        # Отправляем уведомление админу
        await update.message.reply_text(
            f"✅ Успешно!\n"
            f"👤 Пользователь: {user_name} (@{user_info['username'] or 'нет'})\n"
            f"🆔 ID: {user_id}\n"
            f"💰 Списано: {amount} $ENT\n"
            f"📝 Причина: {reason}\n"
            f"💳 Остаток: {result['balance']} $ENT"
        )

        # Отправляем уведомление пользователю
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"⚠️ <b>Списание токенов</b>\n\n"
                    f"💸 Сумма: <b>-{amount} $ENT</b>\n"
                    f"📝 Причина: {reason}\n"
                    f"💳 Ваш баланс: {result['balance']} $ENT\n\n"
                    f"Обратитесь к администрации если вы не согласны с решением. 🦝"
                ),
                parse_mode=ParseMode.HTML
            )
            logger.info(f"📬 Уведомление о списании отправлено пользователю {user_id}")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось отправить уведомление пользователю {user_id}: {e}")
            await update.message.reply_text(
                f"⚠️ Пользователь не найден или заблокировал бота!\n"
                f"Но токены списаны (баланс: {result['balance']} $ENT)"
            )
    elif result is None:
        # Проверяем текущий баланс для сообщения об ошибке
        tokens = get_user_tokens(user_id)
        await update.message.reply_text(
            f"❌ Недостаточно токенов у пользователя!\n"
            f"💰 Баланс: {tokens['balance']} $ENT (нужно {amount} $ENT)"
        )
    else:
        await update.message.reply_text("❌ Ошибка при списании токенов!")


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
    await application.bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="Играть", web_app=WebAppInfo(url=WEBAPP_URL))
    )
    logger.info("✅ Menu button set")


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
    telegram_app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Регистрируем обработчики
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("add", add_tokens_admin))
    telegram_app.add_handler(CommandHandler("balance", get_balance_admin))
    telegram_app.add_handler(CommandHandler("spend", spend_tokens_admin))
    telegram_app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data_handler))

    # Запуск бота
    logger.info("🤖 Starting Telegram bot...")
    telegram_app.run_polling()


if __name__ == '__main__':
    main()
