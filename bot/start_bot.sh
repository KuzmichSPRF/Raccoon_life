#!/bin/bash
# Скрипт запуска бота Raccoon Life

BOT_DIR="/home/botuser/my_bot/bot"
LOG_FILE="/home/botuser/my_bot/bot.log"
PID_FILE="/home/botuser/my_bot/bot.pid"

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

# 2. Проверить что порт 5000 свободен
if netstat -tuln 2>/dev/null | grep -q ":5000"; then
    echo "⚠️ Порт 5000 занят! Освобождаем..."
    fuser -k 5000/tcp 2>/dev/null
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
    if curl -s http://localhost:5000/api/boss_hp | grep -q "status"; then
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
