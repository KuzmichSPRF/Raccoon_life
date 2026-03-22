"""
Скрипт для исправления базы данных - добавляет записи в user_tokens для всех пользователей
"""
import sqlite3
from pathlib import Path

# Путь к базе данных
DB_PATH = Path(__file__).parent / "raccoon_main.db"

def fix_user_tokens():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Получаем всех пользователей у которых нет записи в user_tokens
    cursor.execute('''
        SELECT u.user_id
        FROM users u
        LEFT JOIN user_tokens ut ON u.user_id = ut.user_id
        WHERE ut.user_id IS NULL
    ''')
    
    missing_users = cursor.fetchall()
    
    if not missing_users:
        print("✅ Все пользователи имеют записи в user_tokens")
        conn.close()
        return
    
    print(f"⚠️ Найдено {len(missing_users)} пользователей без записей в user_tokens")
    
    # Добавляем записи
    fixed_count = 0
    for (user_id,) in missing_users:
        try:
            cursor.execute('''
                INSERT OR IGNORE INTO user_tokens (user_id, balance, total_earned, total_spent)
                VALUES (?, 0, 0, 0)
            ''', (user_id,))
            fixed_count += 1
            print(f"  + Добавлен пользователь {user_id}")
        except Exception as e:
            print(f"  ❌ Ошибка для пользователя {user_id}: {e}")
    
    conn.commit()
    print(f"\n✅ Исправлено {fixed_count} записей")
    
    # Проверяем результат
    cursor.execute('SELECT COUNT(*) FROM user_tokens')
    total = cursor.fetchone()[0]
    print(f"📊 Всего записей в user_tokens: {total}")
    
    cursor.execute('SELECT COUNT(*) FROM users')
    total_users = cursor.fetchone()[0]
    print(f"📊 Всего пользователей: {total_users}")
    
    if total == total_users:
        print("✅ Все записи синхронизированы!")
    else:
        print(f"⚠️ Расхождение: {total_users - total} записей отсутствует")
    
    conn.close()

if __name__ == '__main__':
    print("🔧 Исправление записей user_tokens...")
    fix_user_tokens()
