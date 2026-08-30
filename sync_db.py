import sys
import sqlite3
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

BOT_DB = Path(__file__).parent / "bot" / "users.db"
ROOT_DB = Path(__file__).parent / "users.db"

def sync_tokens():
    if not BOT_DB.exists():
        print(f"⚠️ База {BOT_DB} не найдена. Синхронизация не требуется.")
        return
        
    bot_conn = sqlite3.connect(BOT_DB)
    root_conn = sqlite3.connect(ROOT_DB)
    bot_cur = bot_conn.cursor()
    root_cur = root_conn.cursor()
    
    # Проверяем наличие таблицы user_tokens в обеих базах
    bot_cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_tokens'")
    if not bot_cur.fetchone():
        print(f"⚠️ Таблица user_tokens не найдена в {BOT_DB}")
        bot_conn.close()
        root_conn.close()
        return

    root_cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_tokens'")
    if not root_cur.fetchone():
        root_cur.execute('''
            CREATE TABLE IF NOT EXISTS user_tokens (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER DEFAULT 0,
                total_earned INTEGER DEFAULT 0,
                total_spent INTEGER DEFAULT 0,
                last_earn TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        root_conn.commit()
    
    # Получаем все токены из bot/users.db
    bot_cur.execute('SELECT user_id, balance, total_earned, total_spent FROM user_tokens')
    tokens = bot_cur.fetchall()
    
    print(f"📊 Найдено {len(tokens)} записей в bot/users.db")
    
    # Копируем в корневой users.db
    count = 0
    for t in tokens:
        root_cur.execute('''
            INSERT OR REPLACE INTO user_tokens (user_id, balance, total_earned, total_spent)
            VALUES (?, ?, ?, ?)
        ''', t)
        count += 1
    
    root_conn.commit()
    print(f"✅ Скопировано {count} записей в корневой users.db")
    
    # Проверяем результат
    root_cur.execute('SELECT COUNT(*) FROM user_tokens')
    total = root_cur.fetchone()[0]
    print(f"📊 Всего записей в корневом users.db: {total}")
    
    bot_conn.close()
    root_conn.close()

if __name__ == '__main__':
    sync_tokens()
