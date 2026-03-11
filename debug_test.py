"""
Debug тест API - подробные логи
"""
import sys
import importlib.util
from pathlib import Path
import sqlite3

# Импорт bot.py
bot_path = Path("bot/bot.py").absolute()
code = bot_path.read_text(encoding='utf-8')
main_idx = code.find('def main():')

if main_idx > 0:
    bot_module = type('BotModule', (), {})()
    exec(compile(code[:main_idx], str(bot_path), 'exec'), bot_module.__dict__)

print(f"DB_PATH: {bot_module.DB_PATH}")
print(f"WEBAPP_DIR: {bot_module.WEBAPP_DIR}")

# Инициализация БД
bot_module.init_db()

# Тест ensure_user_exists
print("\n=== Тест ensure_user_exists ===")
try:
    bot_module.ensure_user_exists(123456, {'username': 'test', 'first_name': 'Test', 'last_name': ''})
    print("✅ ensure_user_exists OK")
except Exception as e:
    print(f"❌ ensure_user_exists ERROR: {e}")
    import traceback
    traceback.print_exc()

# Тест save_user_stats
print("\n=== Тест save_user_stats ===")
stats = {
    'clown_games': 5,
    'clown_wins': 3,
    'vladeos_games': 2,
    'vladeos_wins': 1,
    'tower_max_level': 10,
    'tower_total_levels': 25,
    'quests': ['q1', 'q2']
}
try:
    result = bot_module.save_user_stats(123456, stats)
    print(f"✅ save_user_stats result: {result}")
except Exception as e:
    print(f"❌ save_user_stats ERROR: {e}")
    import traceback
    traceback.print_exc()

# Проверка БД
print("\n=== Проверка БД ===")
conn = sqlite3.connect(str(bot_module.DB_PATH))
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

cursor.execute("SELECT * FROM user_stats WHERE user_id = 123456")
row = cursor.fetchone()
if row:
    print(f"✅ user_stats найдена:")
    print(f"   clown_games: {row['clown_games']} (ожидалось: 5)")
    print(f"   clown_wins: {row['clown_wins']} (ожидалось: 3)")
    print(f"   tower_max_level: {row['tower_max_level']} (ожидалось: 10)")
else:
    print("❌ user_stats НЕ найдена")

conn.close()

# Тест add_boss_damage
print("\n=== Тест add_boss_damage ===")
try:
    result = bot_module.add_boss_damage(123456, 5000)
    print(f"✅ add_boss_damage result: {result}")
except Exception as e:
    print(f"❌ add_boss_damage ERROR: {e}")
    import traceback
    traceback.print_exc()

# Проверка boss_damage
print("\n=== Проверка boss_damage ===")
conn = sqlite3.connect(str(bot_module.DB_PATH))
cursor = conn.cursor()
cursor.execute("SELECT * FROM boss_damage WHERE user_id = 123456")
row = cursor.fetchone()
if row:
    print(f"✅ boss_damage найдена: total_damage={row[1]}")
else:
    print("❌ boss_damage НЕ найдена")
conn.close()
