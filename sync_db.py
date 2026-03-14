"""
Синхронизация user_tokens между bot/users.db и корневым users.db
"""
import sqlite3
from pathlib import Path

BOT_DB = Path(__file__).parent / "bot" / "users.db"
ROOT_DB = Path(__file__).parent / "users.db"

def sync_tokens():
    bot_conn = sqlite3.connect(BOT_DB)
    root_conn = sqlite3.connect(ROOT_DB)
    bot_cur = bot_conn.cursor()
    root_cur = root_conn.cursor()
    
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
