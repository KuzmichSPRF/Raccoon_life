"""
Raccoon Life - Universal Diagnostic Script
Проверка работоспособности бота, API и базы данных

Использование:
    python diagnose.py              # Полный тест
    python diagnose.py --quick      # Быстрый тест
    python diagnose.py --db-only    # Только БД
    python diagnose.py --api-only   # Только API
"""
import os
import sys
import json
import sqlite3
import logging
from pathlib import Path
from datetime import datetime

# Настройка цветов для вывода
class Colors:
    RESET = '\033[0m'
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    WHITE = '\033[97m'
    BOLD = '\033[1m'

def cprint(text, color=Colors.RESET, bold=False):
    """Цветной вывод"""
    prefix = Colors.BOLD if bold else ''
    print(f"{prefix}{color}{text}{Colors.RESET}")

def print_header(text):
    """Заголовок раздела"""
    print("\n" + "=" * 70)
    cprint(f"  {text}", Colors.CYAN, bold=True)
    print("=" * 70)

def print_subheader(text):
    """Подзаголовок"""
    print(f"\n{Colors.YELLOW}▶ {text}{Colors.RESET}")

def print_success(text):
    """Успешный результат"""
    print(f"  {Colors.GREEN}✅{Colors.RESET} {text}")

def print_error(text):
    """Ошибка"""
    print(f"  {Colors.RED}❌{Colors.RESET} {text}")

def print_warning(text):
    """Предупреждение"""
    print(f"  {Colors.YELLOW}⚠️{Colors.RESET} {text}")

def print_info(text):
    """Информация"""
    print(f"  {Colors.BLUE}ℹ️{Colors.RESET} {text}")

# ==================== ДИАГНОСТИКА ====================

def check_python_version():
    """Проверка версии Python"""
    print_subheader("Версия Python")
    version = sys.version
    print_info(f"Python {version}")
    
    if sys.version_info >= (3, 8):
        print_success("Версия Python подходит")
        return True
    else:
        print_error("Требуется Python 3.8 или выше")
        return False

def check_environment():
    """Проверка переменных окружения"""
    print_subheader("Переменные окружения")
    
    # Загрузка .env
    try:
        from dotenv import load_dotenv
        load_dotenv()
        print_success(".env файл загружен")
    except ImportError:
        print_warning("python-dotenv не установлен: pip install python-dotenv")
    except Exception as e:
        print_error(f"Ошибка загрузки .env: {e}")
    
    # Проверка переменных
    bot_token = os.getenv("BOT_TOKEN")
    webapp_url = os.getenv("WEBAPP_URL")
    admin_id = os.getenv("ADMIN_ID")
    
    if bot_token:
        print_success(f"BOT_TOKEN: {bot_token[:10]}...{bot_token[-5:]}")
    else:
        print_error("BOT_TOKEN не установлен")
    
    if webapp_url:
        print_success(f"WEBAPP_URL: {webapp_url}")
    else:
        print_warning("WEBAPP_URL не установлен")
    
    if admin_id:
        print_success(f"ADMIN_ID: {admin_id}")
    else:
        print_warning("ADMIN_ID не установлен")
    
    return bot_token is not None

def check_dependencies():
    """Проверка установленных зависимостей"""
    print_subheader("Зависимости")
    
    required = {
        'flask': 'Flask',
        'flask_cors': 'Flask-CORS',
        'telegram': 'python-telegram-bot',
        'dotenv': 'python-dotenv',
        'requests': 'requests'
    }
    
    all_installed = True
    for module, package in required.items():
        try:
            __import__(module)
            print_success(f"{package} установлен")
        except ImportError:
            print_error(f"{package} НЕ установлен")
            all_installed = False
    
    if not all_installed:
        print_info("Установите: pip install flask flask-cors python-telegram-bot python-dotenv requests")
    
    return all_installed

def check_database_structure():
    """Проверка структуры базы данных"""
    print_subheader("Структура базы данных")
    
    db_path = Path("bot/raccoon_main.db")
    print_info(f"Путь к БД: {db_path.absolute()}")
    
    if not db_path.exists():
        print_warning("База данных не существует (будет создана при первом запуске)")
        return None
    
    print_success(f"Файл БД существует (размер: {db_path.stat().st_size} байт)")
    
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        
        # Проверка таблиц
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        
        expected_tables = ['users', 'user_stats', 'boss_damage', 'boss_global']
        
        for table in expected_tables:
            if table in tables:
                print_success(f"Таблица '{table}' существует")
            else:
                print_error(f"Таблица '{table}' НЕ найдена")
        
        # Проверка данных
        print_subheader("Данные в таблицах")
        
        cursor.execute("SELECT COUNT(*) FROM users")
        print_info(f"Пользователей в users: {cursor.fetchone()[0]}")
        
        cursor.execute("SELECT COUNT(*) FROM user_stats")
        print_info(f"Записей в user_stats: {cursor.fetchone()[0]}")
        
        cursor.execute("SELECT COUNT(*) FROM boss_damage")
        print_info(f"Записей в boss_damage: {cursor.fetchone()[0]}")
        
        cursor.execute("SELECT current_hp, max_hp, kill_count FROM boss_global WHERE id = 1")
        row = cursor.fetchone()
        if row:
            print_info(f"Босс: HP={row[0]:,} / {row[1]:,}, убит раз: {row[2]}")
        
        conn.close()
        return db_path
        
    except Exception as e:
        print_error(f"Ошибка проверки БД: {e}")
        return None

def check_bot_import():
    """Проверка импорта bot.py"""
    print_subheader("Импорт bot.py")
    
    bot_path = Path("bot/bot.py")
    if not bot_path.exists():
        print_error(f"Файл bot/bot.py не найден")
        return False
    
    print_success(f"bot.py найден: {bot_path.absolute()}")
    
    try:
        # Чтение и проверка синтаксиса
        code = bot_path.read_text(encoding='utf-8')
        
        # Проверка основных функций
        required_funcs = [
            'init_db',
            'save_user_stats',
            'add_boss_damage',
            'get_boss_hp',
            'api_sync'
        ]
        
        for func in required_funcs:
            if f'def {func}(' in code:
                print_success(f"Функция '{func}' найдена")
            else:
                print_error(f"Функция '{func}' НЕ найдена")
        
        # Попытка импорта (без запуска main)
        main_idx = code.find('def main():')
        if main_idx > 0:
            print_success("Структура bot.py корректна")
            return True
        else:
            print_error("Функция main() не найдена")
            return False
            
    except Exception as e:
        print_error(f"Ошибка проверки bot.py: {e}")
        return False

def test_api_locally():
    """Тест API локально (запуск Flask)"""
    print_subheader("Тестирование API (локальный сервер)")
    
    try:
        import requests
        from threading import Thread
        import time
        
        # Импорт bot.py через importlib для правильного доступа к функциям
        import importlib.util
        bot_path = Path("bot/bot.py").absolute()
        
        spec = importlib.util.spec_from_file_location("bot_module", bot_path)
        bot_module = importlib.util.module_from_spec(spec)
        
        # Загружаем только часть до main()
        code = bot_path.read_text(encoding='utf-8')
        main_idx = code.find('def main():')
        
        if main_idx > 0:
            # Выполняем код в контексте модуля
            exec(compile(code[:main_idx], str(bot_path), 'exec'), bot_module.__dict__)
        
        # Инициализация БД
        bot_module.init_db()
        print_success("БД инициализирована")
        
        # Запуск Flask
        print_info("Запуск Flask сервера на порту 5000...")
        flask_thread = Thread(
            target=lambda: bot_module.app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False),
            daemon=True
        )
        flask_thread.start()
        time.sleep(2)
        
        print_success("Flask сервер запущен")
        
        BASE_URL = 'http://127.0.0.1:5000'
        
        # Тест 1: sync_stats
        print("\n  Тест 1: Отправка статистики...")
        test_data = {
            'type': 'sync_stats',
            'userId': 999999,
            'clown_games': 1,
            'clown_wins': 1,
            'vladeos_games': 0,
            'vladeos_wins': 0,
            'tower_max_level': 1,
            'tower_total_levels': 1,
            'quests': ['test_quest']
        }
        
        try:
            r = requests.post(f'{BASE_URL}/api/sync', json=test_data, timeout=5)
            print(f"  Статус: {r.status_code}, Ответ: {r.text[:200]}")
            if r.status_code == 200 and r.json().get('status') == 'ok':
                print_success("✅ /api/sync (stats) работает")
            else:
                print_error(f"❌ /api/sync (stats) ошибка: {r.status_code} - {r.text[:200]}")
        except Exception as e:
            print_error(f"❌ Ошибка запроса: {e}")
        
        # Тест 2: boss_damage
        print("  Тест 2: Урон по боссу...")
        damage_data = {
            'type': 'boss_damage',
            'userId': 999999,
            'damage': 1000
        }
        
        r = requests.post(f'{BASE_URL}/api/sync', json=damage_data, timeout=5)
        if r.status_code == 200:
            boss = r.json().get('boss', {})
            print_success(f"✅ /api/sync (damage) работает, HP босса: {boss.get('current_hp'):,}")
        else:
            print_error(f"❌ /api/sync (damage) ошибка: {r.status_code}")
        
        # Тест 3: boss_hp
        print("  Тест 3: Получение HP босса...")
        r = requests.get(f'{BASE_URL}/api/boss_hp', timeout=5)
        if r.status_code == 200:
            print_success("✅ /api/boss_hp работает")
        else:
            print_error(f"❌ /api/boss_hp ошибка: {r.status_code}")
        
        # Проверка записи в БД
        print_subheader("Проверка записи в БД")
        conn = sqlite3.connect(str(bot_module.DB_PATH))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM user_stats WHERE user_id = 999999")
        row = cursor.fetchone()
        if row:
            print_success("✅ Данные записаны в user_stats")
            print_info(f"   clown_games: {row['clown_games']}, tower_max_level: {row['tower_max_level']}")
        else:
            print_error("❌ Данные НЕ записаны в user_stats")
        
        cursor.execute("SELECT * FROM boss_damage WHERE user_id = 999999")
        row = cursor.fetchone()
        if row:
            print_success("✅ Данные записаны в boss_damage")
            print_info(f"   total_damage: {row['total_damage']}")
        else:
            print_error("❌ Данные НЕ записаны в boss_damage")
        
        conn.close()
        
        return True
        
    except ImportError as e:
        print_error(f"Не установлен requests: pip install requests")
        return False
    except Exception as e:
        print_error(f"Ошибка теста API: {e}")
        import traceback
        traceback.print_exc()
        return False

def check_webapp_files():
    """Проверка файлов WebApp"""
    print_subheader("Файлы WebApp")
    
    webapp_dir = Path("webapp")
    if not webapp_dir.exists():
        print_error(f"Директория webapp/ не найдена")
        return False
    
    print_success(f"webapp/ найдена: {webapp_dir.absolute()}")
    
    required_files = ['index.html', 'game.html', 'tower_game.html', 'boss_game.html']
    
    for file in required_files:
        file_path = webapp_dir / file
        if file_path.exists():
            print_success(f"{file} существует ({file_path.stat().st_size} байт)")
        else:
            print_warning(f"{file} НЕ найден")
    
    # Проверка index.html на наличие API вызовов
    index_path = webapp_dir / 'index.html'
    if index_path.exists():
        content = index_path.read_text(encoding='utf-8')
        if 'fetch' in content and '/api/sync' in content:
            print_success("index.html содержит вызовы API")
        else:
            print_warning("index.html может не содержать вызовы API")
    
    return True

def generate_report():
    """Генерация итогового отчета"""
    print_header("ИТОГОВЫЙ ОТЧЕТ")
    
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print_info(f"Дата проверки: {timestamp}")
    print_info(f"Путь к проекту: {Path.cwd()}")
    
    # Рекомендации
    print_subheader("Рекомендации")
    
    issues = []
    
    if not os.getenv("BOT_TOKEN"):
        issues.append("❌ Установите BOT_TOKEN в .env")
    
    if not os.getenv("WEBAPP_URL"):
        issues.append("⚠️ Установите WEBAPP_URL для работы WebApp")
    
    if not Path("bot/raccoon_main.db").exists():
        issues.append("ℹ️ База данных будет создана при первом запуске бота")
    
    if issues:
        for issue in issues:
            print(f"  {issue}")
    else:
        print_success("Все настройки корректны!")
    
    print("\n" + "=" * 70)
    cprint("  Для запуска бота выполните:", Colors.GREEN, bold=True)
    print("=" * 70)
    print("\n  cd bot")
    print("  python bot.py\n")

# ==================== MAIN ====================

def main():
    print_header("RACCOON LIFE - DIAGNOSTIC TOOL")
    print_info(f"Время запуска: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Проверка аргументов
    quick_mode = '--quick' in sys.argv
    db_only = '--db-only' in sys.argv
    api_only = '--api-only' in sys.argv
    
    results = {}
    
    # Базовые проверки
    if not db_only and not api_only:
        results['python'] = check_python_version()
        results['env'] = check_environment()
        results['deps'] = check_dependencies()
        results['bot'] = check_bot_import()
        results['webapp'] = check_webapp_files()
    
    # Проверка БД
    if not api_only:
        results['db'] = check_database_structure() is not None
    
    # Тест API
    if not quick_mode and not db_only:
        print_header("ТЕСТИРОВАНИЕ")
        results['api'] = test_api_locally()
    
    # Отчет
    generate_report()
    
    # Итог
    print_header("СТАТУС")
    
    if all(results.values()):
        print_success("✅ Все проверки пройдены!")
        print_info("Бот готов к запуску")
        return 0
    else:
        print_warning("⚠️ Некоторые проверки не пройдены")
        print_info("Исправьте ошибки перед запуском")
        return 1

if __name__ == '__main__':
    sys.exit(main())
