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
        
        # Инициализация босса
        cursor.execute('''
            INSERT OR IGNORE INTO boss_global (id, current_hp, max_hp, kill_count) 
            VALUES (1, 1000000000, 1000000000, 0)
        ''')
        
        conn.commit()
        
        # Миграции: добавляем недостающие колонки
        _add_missing_columns(cursor)
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
    
    if boss_info:
        return jsonify({'status': 'ok', 'boss': boss_info})
    else:
        return jsonify({'status': 'error', 'message': 'Database error'}), 500


# ==================== TELEGRAM BOT ====================

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
    telegram_app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data_handler))
    
    # Запуск бота
    logger.info("🤖 Starting Telegram bot...")
    telegram_app.run_polling()


if __name__ == '__main__':
    main()
