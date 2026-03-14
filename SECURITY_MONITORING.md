# 🔒 Security Monitoring для Raccoon Life

## Обзор

Система security мониторинга отслеживает подозрительную активность в играх и отправляет логи на сервер для последующего анализа в SIEM системе.

## Архитектура

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Client (Game)  │────▶│  Server (API)    │────▶│  SIEM System    │
│  battleship.html│     │  bot.py          │     │  (external)     │
└─────────────────┘     └──────────────────┘     └─────────────────┘
```

## Типы security событий

### 1. **GAME_INIT** — Инициализация игры
```json
{
  "event_type": "GAME_INIT",
  "message": "Battleship game initialized",
  "user_id": 123456789,
  "game": "battleship",
  "details": {
    "has_init_data": true,
    "has_init_data_unsafe": true
  }
}
```

### 2. **GAME_ACTION** — Действия в игре
```json
{
  "event_type": "GAME_ACTION",
  "message": "TOKEN_EARN_REQUEST",
  "user_id": 123456789,
  "game": "battleship",
  "details": {
    "amount": 100,
    "reason": "battleship_win"
  }
}
```

### 3. **SUSPICIOUS_ACTIVITY** — Подозрительная активность
```json
{
  "event_type": "SUSPICIOUS_ACTIVITY",
  "message": "earnTokens without userId",
  "user_id": null,
  "game": "battleship",
  "details": {
    "amount": 100,
    "reason": "battleship_win"
  }
}
```

### 4. **API_ERROR** — Ошибки API
```json
{
  "event_type": "API_ERROR",
  "message": "Error calling /api/game/battleship",
  "user_id": 123456789,
  "game": "battleship",
  "details": {
    "error": "HTTP 403",
    "status": 403,
    "endpoint": "/api/game/battleship"
  }
}
```

### 5. **AUTH_ERROR** — Ошибки авторизации
```json
{
  "event_type": "AUTH_ERROR",
  "message": "Failed to parse user data",
  "user_id": null,
  "game": "battleship",
  "details": {
    "error": "Unexpected token..."
  }
}
```

## Клиентская реализация

### Функции для логирования

```javascript
// Базовое логирование security событий
logSecurityEvent(eventType, message, details = {})

// Логирование действий в игре
logGameAction(action, details = {})

// Логирование ошибок API
logApiError(endpoint, error, responseStatus)

// Логирование подозрительной активности
logSuspiciousActivity(reason, details = {})
```

### Автоматическая отправка

- Логи буферизуются (до 50 записей)
- Отправка каждые 30 секунд
- Отправка при закрытии страницы (`beforeunload`)

## Серверная реализация

### Endpoint: `/api/security/log`

**Метод:** POST  
**Content-Type:** application/json  
**Rate Limit:** 30 запросов в минуту (общий с /api/sync)

**Тело запроса:**
```json
{
  "logs": [
    {
      "timestamp": "2026-03-14T10:30:00.000Z",
      "event_type": "GAME_ACTION",
      "message": "TOKEN_EARN_REQUEST",
      "user_id": 123456789,
      "game": "battleship",
      "details": { "amount": 100 }
    }
  ]
}
```

**Ответ:**
```json
{
  "status": "ok",
  "received": 1
}
```

## Интеграция с SIEM

### Вариант 1: Логирование в файл

Логи security записываются в отдельный файл:

```python
# В bot.py добавить file handler для security_logger
import logging.handlers

security_handler = logging.handlers.RotatingFileHandler(
    'logs/security.log',
    maxBytes=10*1024*1024,  # 10MB
    backupCount=5
)
security_handler.setFormatter(
    logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
)
security_logger.addHandler(security_handler)
```

### Вариант 2: Отправка в external SIEM

```python
# Добавить handler для отправки в SIEM
class SIEMHandler(logging.Handler):
    def __init__(self, siem_url, api_key):
        super().__init__()
        self.siem_url = siem_url
        self.api_key = api_key
    
    def emit(self, record):
        import requests
        log_entry = self.format(record)
        try:
            requests.post(
                self.siem_url,
                json={'log': log_entry},
                headers={'Authorization': f'Bearer {self.api_key}'},
                timeout=5
            )
        except:
            pass  # Не блокируем работу при ошибке SIEM

security_logger.addHandler(SIEMHandler(
    siem_url='https://your-siem.com/api/logs',
    api_key='your-api-key'
))
```

### Вариант 3: CloudWatch / Azure Monitor / GCP Logging

```python
# Пример для AWS CloudWatch
import boto3

cloudwatch = boto3.client('logs')

class CloudWatchHandler(logging.Handler):
    def __init__(self, log_group, log_stream):
        super().__init__()
        self.log_group = log_group
        self.log_stream = log_stream
    
    def emit(self, record):
        log_entry = {
            'logGroupName': self.log_group,
            'logStreamName': self.log_stream,
            'logEvents': [{
                'timestamp': int(time.time() * 1000),
                'message': self.format(record)
            }]
        }
        try:
            cloudwatch.put_log_events(**log_entry)
        except:
            pass

security_logger.addHandler(CloudWatchHandler(
    log_group='/raccoon-life/security',
    log_stream='security-events'
))
```

## Мониторинг и алерты

### Рекомендуемые алерты

1. **Множественные ошибки авторизации**
   - Условие: >10 AUTH_ERROR за 5 минут от одного user_id
   - Действие: Заблокировать пользователя, отправить алерт

2. **Подозрительная активность**
   - Условие: Любой SUSPICIOUS_ACTIVITY
   - Действие: Отправить алерт security команде

3. **Аномалии в игре**
   - Условие: >5 API_ERROR за 1 минуту
   - Действие: Проверить логи на предмет атаки

### Пример SQL запроса для анализа

```sql
-- Найти пользователей с множественными ошибками авторизации
SELECT 
    user_id,
    COUNT(*) as error_count,
    MAX(timestamp) as last_error
FROM security_logs
WHERE event_type = 'AUTH_ERROR'
  AND timestamp >= NOW() - INTERVAL '1 hour'
GROUP BY user_id
HAVING COUNT(*) > 5
ORDER BY error_count DESC;
```

## Примеры использования

### Логирование победы в игре

```javascript
// Перед начислением токенов
logGameAction('GAME_WIN', {
    game: 'battleship',
    moves: movesCount,
    time_ms: Date.now() - gameStartTime
});

// После успешного начисления
logGameAction('GAME_WIN_REPORTED', {
    win: true,
    game: 'battleship'
});
```

### Логирование ошибок

```javascript
fetch('/api/game/battleship', {...})
    .then(r => {
        if (!r.ok) {
            logApiError('/api/game/battleship', `HTTP ${r.status}`, r.status);
        }
        return r.json();
    })
    .catch(err => {
        logApiError('/api/game/battleship', err, null);
    });
```

## Безопасность

### Защита от злоупотреблений

1. **Rate limiting** — 30 запросов в минуту
2. **Валидация данных** — проверка структуры логов
3. **Аутентификация** — проверка Telegram initData
4. **Ограничение размера** — макс. 1MB на запрос

### Конфиденциальность

- Не логируем персональные данные (имена, usernames)
- Логируем только user_id из Telegram
- User agent используется для детектирования ботов

## Развёртывание

1. Убедитесь что `security_logger` настроен в `bot.py`
2. Проверьте что endpoint `/api/security/log` доступен
3. Настройте SIEM handler для вашей инфраструктуры
4. Протестируйте отправку логов из клиента

## Troubleshooting

### Логи не отправляются на сервер

1. Проверьте консоль браузера на ошибки
2. Убедитесь что Telegram WebApp инициализирован
3. Проверьте network tab на наличие запросов к `/api/security/log`

### Сервер не принимает логи

1. Проверьте логи бота на ошибки `api_security_log`
2. Убедитесь что Content-Type: application/json
3. Проверьте структуру JSON (массив logs)

---

**Версия:** 1.0  
**Дата:** 2026-03-14
