#!/bin/bash
# Скрипт запуска бота Raccoon Life

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
BOT_DIR="${BOT_DIR:-$SCRIPT_DIR}"
LOG_FILE="${LOG_FILE:-$BOT_DIR/bot.log}"
PID_FILE="${PID_FILE:-$BOT_DIR/bot.pid}"

FLASK_PORT="${FLASK_PORT:-5000}"

echo "🦝 Raccoon Life Bot - Запуск"

# 1. Остановить старых ботов
echo "🛑 Остановка старых процессов..."
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if ps -p "$OLD_PID" > /dev/null 2>&1; then
        kill "$OLD_PID"
        echo "   Бот PID $OLD_PID остановлен"
    fi
    rm -f "$PID_FILE"
fi

# Убить все процессы bot.py
pkill -f "python.*bot.py" 2>/dev/null
sleep 2

# 2. Проверить что порт свободен
if netstat -tuln 2>/dev/null | grep -q ":$FLASK_PORT"; then
    echo "⚠️ Порт $FLASK_PORT занят! Освобождаем..."
    fuser -k $FLASK_PORT/tcp 2>/dev/null
    sleep 2
fi

# 3. Запустить нового бота
echo "🚀 Запуск бота..."
cd "$BOT_DIR"
nohup python3 bot.py > "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"

echo "   Бот запущен с PID $NEW_PID"

# 4. Подождать запуска
sleep 5

# 5. Проверить что работает
echo "🔍 Проверка..."
if ps -p "$NEW_PID" > /dev/null 2>&1; then
    echo "✅ Бот работает (PID $NEW_PID)"
    
    # Проверка Flask
    if curl -s "http://localhost:$FLASK_PORT/api/boss_hp" | grep -q "status"; then
        echo "✅ Flask API работает"
    else
        echo "❌ Flask API не отвечает"
    fi
else
    echo "❌ Бот не запустился! Смотрите лог:"
    tail -20 "$LOG_FILE"
fi

echo ""
echo "📋 Логи: tail -f $LOG_FILE"
echo "🛑 Остановка: kill $NEW_PID"
