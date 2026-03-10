import sqlite3
import json
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "raccoon.db"

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_stats (
                user_id INTEGER PRIMARY KEY,
                clown_games INTEGER DEFAULT 0,
                clown_wins INTEGER DEFAULT 0,
                vladeos_games INTEGER DEFAULT 0,
                vladeos_wins INTEGER DEFAULT 0,
                tower_max_level INTEGER DEFAULT 0,
                tower_total_levels INTEGER DEFAULT 0,
                quests TEXT DEFAULT '[]',
                last_sync TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        """)
        conn.commit()
        logger.info("База данных инициализирована")

def upsert_user(user_id, username=None, first_name=None, last_name=None):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO users (user_id, username, first_name, last_name, registered_at)
            VALUES (?, ?, ?, ?, COALESCE((SELECT registered_at FROM users WHERE user_id = ?), CURRENT_TIMESTAMP))
        """, (user_id, username, first_name, last_name, user_id))
        cursor.execute('INSERT OR IGNORE INTO user_stats (user_id) VALUES (?)', (user_id,))
        conn.commit()

def update_stats(user_id, stats):
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO user_stats (user_id) VALUES (?)", (user_id,))
            cursor.execute("""
                UPDATE user_stats SET
                clown_games = ?, clown_wins = ?,
                vladeos_games = ?, vladeos_wins = ?,
                tower_max_level = ?, tower_total_levels = ?,
                quests = ?, last_sync = CURRENT_TIMESTAMP
                WHERE user_id = ?
            """, (
                stats.get('clown_games', 0), stats.get('clown_wins', 0),
                stats.get('vladeos_games', 0), stats.get('vladeos_wins', 0),
                stats.get('tower_max_level', 0), stats.get('tower_total_levels', 0),
                json.dumps(stats.get('quests', [])), user_id
            ))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        return False

def get_leaderboard(limit=10):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.first_name, u.username, u.user_id, s.tower_max_level,
                   s.clown_games, s.clown_wins, s.vladeos_games, s.vladeos_wins
            FROM user_stats s
            JOIN users u ON s.user_id = u.user_id
            WHERE s.tower_max_level > 0
            ORDER BY s.tower_max_level DESC
            LIMIT ?
        """, (limit,))
        return [dict(row) for row in cursor.fetchall()]

if __name__ == "__main__":
    init_db()
    print("БД создана!")
