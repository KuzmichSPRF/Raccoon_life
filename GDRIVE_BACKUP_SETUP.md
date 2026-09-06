# ☁️ Настройка облачного бекапа в Google Drive

Бот **Raccoon Life** поддерживает надежное резервное копирование актуальной базы данных (users.db) в вашу закрытую папку Google Drive:
🔗 **Папка в Google Drive:** [Открыть папку](https://drive.google.com/drive/folders/1l1IcnYsq-yBIX-q3ebf30YI8jlr8b568?usp=drive_link)  
🆔 **ID папки:** 1l1IcnYsq-yBIX-q3ebf30YI8jlr8b568

---

## ⚠️ Важное примечание по Google Drive
У Google Cloud для личных аккаунтов (@gmail.com) сервисные аккаунты (Service Account) имеют **квоту диска 0 МБ** и блокируются Google с ошибкой storageQuotaExceeded.  
Поэтому для личного Google Drive используется **OAuth2 авторизация** — это позволяет выгружать бекапы прямо в ваше облако с использованием квоты вашего Google-диска (15 ГБ+).

---

## 🔑 Простая настройка за 2 минуты (OAuth2)

### Шаг 1: Создание OAuth Client ID в Google Cloud Console
1. Перейдите в [Google Cloud Console](https://console.cloud.google.com/).
2. Убедитесь, что вверху выбран ваш проект (где включен **Google Drive API**).
3. Перейдите в **APIs & Services** > **Credentials**.
4. Если вы еще не настраивали OAuth Consent Screen:
   - Перейдите в **OAuth consent screen** > выберите **External** > введите название приложения (например, Raccoon Life) и ваш email > сохраните.
5. Нажмите **+ CREATE CREDENTIALS** > выберите **OAuth client ID**.
6. В поле **Application type** выберите **Desktop app** (Классическое приложение).
7. Нажмите **Create**.
8. Нажмите **Download JSON** (скачать файл) и сохраните его как credentials.json в папку c:\Users\kuzmi\Downloads\Raccoon_life\ (или в ot\credentials.json).

---

### Шаг 2: Однократная авторизация через скрипт
В терминале выполните команду:
`ash
python authorize_gdrive.py
`
1. В браузере откроется страница авторизации Google.
2. Выберите ваш Google-аккаунт и нажмите **Продолжить / Разрешить** доступ к Google Drive.
3. Скрипт автоматически сохранит файл 	oken.json.

---

## 🎉 Готово! Проверка работы

Напишите боту в Telegram:
`
/backup
`
Бот мгновенно сделает снимок базы данных, выгрузит его на Google Drive и пришлет вам сообщение с подтверждением, прямой ссылкой на файл в облаке и копией архива.

Каждое утро в **06:00 МСК** бекап будет создаваться и загружаться в облако **полностью автоматически**.
