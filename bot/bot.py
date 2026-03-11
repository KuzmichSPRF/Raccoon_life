import os
import logging
import sqlite3
import json
from threading import Thread
from flask import Flask, jsonify, request
from flask_cors import CORS
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL")
# ID администратора, который может делать рассылки (ваш user_id в Telegram)
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_NAME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.db")
logger.info(f"Using database file at: {DB_NAME}")

# Flask app для API
app = Flask(__name__)
# Разрешаем запросы с любых доменов (важно для Telegram WebApps)
CORS(app)

@app.route('/api/boss_hp', methods=['GET'])
def api_get_boss_hp():
    """API endpoint для получения HP босса"""
    boss_info = get_boss_hp()
    # Возвращаем JSON с четкой структурой
    response = jsonify({'status': 'ok', 'boss': boss_info})
    # ВАЖНО: Отключаем кеширование, чтобы браузер всегда запрашивал актуальное здоровье
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.route('/api/player_stats', methods=['GET'])
def api_get_player_stats():
    """API endpoint для получения статистики игрока по боссу"""
    try:
        user_id = request.args.get('userId') or request.headers.get('X-Telegram-User-Id', 0)
        if not user_id:
            return jsonify({'total_damage': 0, 'hits': 0, 'crits': 0})
        
        with sqlite3.connect(DB_NAME, timeout=10.0) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT total_damage FROM boss_damage WHERE user_id = ?", (int(user_id),))
            row = cursor.fetchone()
            if row:
                return jsonify({'total_damage': row[0], 'hits': 0, 'crits': 0})
            return jsonify({'total_damage': 0, 'hits': 0, 'crits': 0})
    except (sqlite3.Error, Exception) as e:
        logger.error(f"Error getting player stats: {e}")
        return jsonify({'total_damage': 0, 'hits': 0, 'crits': 0})

@app.route('/api/sync', methods=['POST'])
def api_sync():
    """API endpoint для синхронизации данных"""
    try:
        data = request.json
        data_type = data.get('type')
        logger.info(f"📥 API sync received: type={data_type}, data={data}")

        if data_type == 'sync_stats':
            user_id = data.get('userId') or data.get('user_id') or request.headers.get('X-Telegram-User-Id', 0)
            logger.info(f"👤 sync_stats: user_id={user_id}")
            if user_id:
                if update_db_stats(user_id, data):
                    logger.info(f"✅ sync_stats успешно для user_id={user_id}")
                    return jsonify({'status': 'ok'})
                else:
                    logger.error("❌ Database update failed")
                    return jsonify({'status': 'error', 'message': 'Database update failed'}), 500
            else:
                logger.warning("⚠️ sync_stats received without user_id")

        if data_type == 'boss_damage':
            # Получаем user_id из тела запроса или заголовка
            user_id = data.get('userId') or data.get('user_id') or request.headers.get('X-Telegram-User-Id', 0)

            # Принудительно превращаем damage в число
            try:
                damage = int(data.get('damage', 0))
            except (ValueError, TypeError):
                damage = 0

            logger.info(f"💥 Boss damage: user_id={user_id}, damage={damage}")
            if damage > 0 and user_id:
                if update_boss_damage(int(user_id), damage):
                    # Возвращаем актуальное состояние босса ТОЛЬКО если обновление прошло успешно
                    boss_info = get_boss_hp()
                    return jsonify({'status': 'ok', 'boss': boss_info})
                else:
                    logger.error("❌ Update boss damage returned False")
            elif damage > 0:
                logger.warning(f"⚠️ Boss damage without user_id: {damage}")

        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f"❌ API sync error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

def run_flask():
    """Запуск Flask сервера"""
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)

def init_db():
    with sqlite3.connect(DB_NAME, timeout=10.0) as conn:
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
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS boss_damage (
                user_id INTEGER PRIMARY KEY,
                total_damage INTEGER DEFAULT 0,
                last_hit TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS boss_global (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                current_hp INTEGER DEFAULT 1000000000,
                max_hp INTEGER DEFAULT 1000000000,
                last_reset TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                kill_count INTEGER DEFAULT 0
            )
        ''')
        # Инициализируем босса если таблица пустая
        cursor.execute("INSERT OR IGNORE INTO boss_global (id, current_hp, max_hp) VALUES (1, 1000000000, 1000000000)")

        # Миграция для boss_global (на случай если таблица старая и без нужных колонок)
        cursor.execute("PRAGMA table_info(boss_global)")
        boss_cols = {info[1] for info in cursor.fetchall()}
        if 'kill_count' not in boss_cols:
            try:
                cursor.execute("ALTER TABLE boss_global ADD COLUMN kill_count INTEGER DEFAULT 0")
                logger.info("Миграция: добавлена колонка kill_count в boss_global")
            except Exception as e:
                logger.error(f"Ошибка миграции boss_global: {e}")

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
        logger.info(f"📝 update_db_stats вызвана для user_id={user_id}, данные: {data}")
        with sqlite3.connect(DB_NAME, timeout=10.0) as conn:
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
            
            # Проверяем что записалось
            cursor.execute("SELECT * FROM user_stats WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            logger.info(f"✅ После обновления в БД: {row}")
            
        logger.info(f"✅ Успешное обновление БД для {user_id}")
        return True
    except (sqlite3.Error, Exception) as e:
        logger.error(f"❌ Ошибка БД при обновлении статистики для {user_id}: {e}")
        return False

def update_boss_damage(user_id, damage):
    """Обновляет урон игрока по боссу и уменьшает HP босса"""
    try:
        with sqlite3.connect(DB_NAME, timeout=10.0) as conn:
            cursor = conn.cursor()
            # Гарантируем, что пользователь существует, чтобы не было ошибки FK (если игрок не нажал /start)
            cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))

            # Обновляем урон игрока
            cursor.execute('''
                INSERT INTO boss_damage (user_id, total_damage, last_hit)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                total_damage = total_damage + ?,
                last_hit = CURRENT_TIMESTAMP
            ''', (user_id, damage, damage))
            
            # Уменьшаем HP босса
            cursor.execute('''
                UPDATE boss_global SET current_hp = current_hp - ?, last_hit = CURRENT_TIMESTAMP
                WHERE id = 1 AND current_hp > 0
            ''', (damage,))
            
            # Проверяем не умер ли босс
            cursor.execute("SELECT current_hp FROM boss_global WHERE id = 1")
            row = cursor.fetchone()
            if row and row[0] <= 0:
                # Босс умер! Увеличиваем счетчик убийств и возрождаем
                cursor.execute('''
                    UPDATE boss_global SET 
                    current_hp = max_hp, 
                    kill_count = kill_count + 1,
                    last_reset = CURRENT_TIMESTAMP
                ''')
                logger.info(f"БОСС УБИТ! Игрок {user_id} нанес последний удар. Возрождение...")
            
            conn.commit()
        logger.info(f"Урон по боссу: user {user_id}, damage {damage}")
        return True
    except (sqlite3.Error, Exception) as e:
        logger.error(f"Ошибка обновления урона по боссу: {e}")
        return False

def get_boss_leaderboard():
    """Возвращает топ игроков по урону боссу"""
    try:
        with sqlite3.connect(DB_NAME, timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT u.first_name, u.username, b.total_damage, b.last_hit
                FROM boss_damage b
                JOIN users u ON b.user_id = u.user_id
                WHERE b.total_damage > 0
                ORDER BY b.total_damage DESC
                LIMIT 10
            ''')
            return cursor.fetchall()
    except (sqlite3.Error, Exception) as e:
        logger.error(f"Error fetching boss leaderboard: {e}")
        return []

def get_boss_hp():
    """Возвращает текущие HP босса"""
    try:
        with sqlite3.connect(DB_NAME, timeout=10.0) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT current_hp, max_hp, kill_count FROM boss_global WHERE id = 1")
            row = cursor.fetchone()
            if row:
                return {'current_hp': row[0], 'max_hp': row[1], 'kill_count': row[2]}
            return {'current_hp': 1000000000, 'max_hp': 1000000000, 'kill_count': 0}
    except (sqlite3.Error, Exception) as e:
        logger.error(f"Error getting boss HP: {e}")
        return {'current_hp': 1000000000, 'max_hp': 1000000000, 'kill_count': 0}

def get_boss_total_hp():
    """Возвращает оставшееся HP босса"""
    return get_boss_hp()['current_hp']

def get_leaderboard_text():
    """Fetches and formats the leaderboard text based on tower_max_level."""
    try:
        with sqlite3.connect(DB_NAME, timeout=10.0) as conn:
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

    except (sqlite3.Error, Exception) as e:
        logger.error(f"Error fetching leaderboard: {e}")
        return "Не удалось загрузить рейтинг. Попробуйте позже."

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the leaderboard to the chat."""
    leaderboard_text = get_leaderboard_text()
    await update.message.reply_text(leaderboard_text, parse_mode=ParseMode.HTML)

async def boss_leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Рейтинг урона по боссу"""
    try:
        leaders = get_boss_leaderboard()
        boss_info = get_boss_hp()

        message = f"🔺 <b>Босс Енотов - Секретная Организация</b>\n\n"
        message += f"💀 <b>HP Босса:</b> {boss_info['current_hp']:,} / {boss_info['max_hp']:,}\n"
        message += f"☠️ <b>Убит раз:</b> {boss_info['kill_count']}\n\n"
        
        if not leaders:
            message += "Пока никто не нанес урона боссу!\n"
        else:
            message += "<b>Топ урона:</b>\n"
            for i, leader in enumerate(leaders):
                display_name = leader['first_name'] or leader['username'] or "Аноним"
                display_name = display_name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                message += f"{i+1}. {display_name} - {leader['total_damage']:,} урона\n"

        await update.message.reply_text(message, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Error in boss leaderboard: {e}")
        await update.message.reply_text("❌ Не удалось загрузить рейтинг босса")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Рассылка сообщений всем игрокам. Использование: /broadcast <текст>"""
    user_id = update.effective_user.id
    
    # Проверка прав администратора
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ У вас нет прав для рассылки сообщений.")
        return
    
    # Получаем текст сообщения (после команды)
    if not context.args:
        await update.message.reply_text(
            "❌ Использование: /broadcast <текст сообщения>\n\n"
            "Пример: /broadcast 🎉 Новый квест доступен!"
        )
        return
    
    message_text = " ".join(context.args)
    
    # Получаем всех пользователей из БД
    try:
        with sqlite3.connect(DB_NAME, timeout=10.0) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM users")
            users = cursor.fetchall()
        
        if not users:
            await update.message.reply_text("❌ В базе данных нет пользователей.")
            return
        
        # Отправляем сообщение всем пользователям
        success_count = 0
        fail_count = 0
        
        status_message = await update.message.reply_text(f"🔄 Начинаю рассылку для {len(users)} пользователей...")
        
        for (target_user_id,) in users:
            try:
                await context.bot.send_message(
                    chat_id=target_user_id,
                    text=message_text,
                    parse_mode=ParseMode.HTML
                )
                success_count += 1
            except Exception as e:
                logger.error(f"Не удалось отправить пользователю {target_user_id}: {e}")
                fail_count += 1
            
            # Небольшая задержка чтобы избежать лимитов
            import asyncio
            await asyncio.sleep(0.05)
        
        await status_message.edit_text(
            f"✅ Рассылка завершена!\n\n"
            f"📩 Отправлено: {success_count}\n"
            f"❌ Ошибок: {fail_count}\n"
            f"📊 Всего: {len(users)}"
        )
        
    except (sqlite3.Error, Exception) as e:
        logger.error(f"Ошибка при рассылке: {e}")
        await update.message.reply_text(f"❌ Ошибка при рассылке: {e}")

async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать количество зарегистрированных пользователей"""
    user_id = update.effective_user.id
    
    # Проверка прав администратора
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ У вас нет прав для этой команды.")
        return
    
    try:
        with sqlite3.connect(DB_NAME, timeout=10.0) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users")
            total = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM user_stats WHERE tower_max_level > 0")
            active = cursor.fetchone()[0]
        
        await update.message.reply_text(
            f"📊 Статистика пользователей:\n\n"
            f"👥 Всего: {total}\n"
            f"🎮 Активных (играли в башню): {active}"
        )
    except (sqlite3.Error, Exception) as e:
        logger.error(f"Ошибка при получении статистики: {e}")
        await update.message.reply_text(f"❌ Ошибка: {e}")

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
        
        elif data_type == 'boss_damage':
            damage = data.get('damage', 0)
            if damage > 0 and update_boss_damage(user_id, damage):
                logger.info(f"Босс: игрок {user_id} нанес {damage} урона")
    except Exception as e:
        logger.error(f"Ошибка обработки WebAppData: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Код сохранения пользователя (оставляем ваш)
    user = update.effective_user
    try:
        with sqlite3.connect(DB_NAME, timeout=10.0) as conn:
            cursor = conn.cursor()
            # Используем UPSERT: если пользователь был создан заглушкой (через урон), обновляем его данные
            cursor.execute("""
                INSERT INTO users (user_id, username, first_name, last_name) VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, last_name=excluded.last_name
            """, (user.id, user.username, user.first_name, user.last_name))
            cursor.execute("INSERT OR IGNORE INTO user_stats (user_id) VALUES (?)", (user.id,))
            conn.commit()
        logger.info(f"User {user.id} ({user.username}) started the bot.")
    except (sqlite3.Error, Exception) as e:
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
    
    # Запуск Flask сервера в отдельном потоке
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Flask API server started on port 5000")
    
    telegram_app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    telegram_app.add_handler(CommandHandler("boss", boss_leaderboard_command))
    telegram_app.add_handler(CommandHandler("broadcast", broadcast_command))
    telegram_app.add_handler(CommandHandler("users", users_command))
    # ВАЖНО: Хендлер для приема данных из Mini App
    telegram_app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data_handler))

    telegram_app.run_polling()

if __name__ == '__main__':
    main()
