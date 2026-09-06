# ☁️ Настройка ежедневного облачного бекапа в Google Drive

Бот **Raccoon Life** поддерживает автоматическое резервное копирование актуальной базы данных (users.db) в закрытую папку Google Drive:
🔗 **Папка в Google Drive:** [Открыть папку](https://drive.google.com/drive/folders/1l1IcnYsq-yBIX-q3ebf30YI8jlr8b568?usp=drive_link)  
🆔 **ID папки:** 1l1IcnYsq-yBIX-q3ebf30YI8jlr8b568

---

## 🚀 Как работает бекап

1. **⏰ Автоматически каждое утро в 06:00 МСК**:
   - Создается целостный снимок базы данных через sqlite3.backup() без блокировки игроков и остановки бота.
   - Снимок сжимается в защищенный zip-архив вида Raccoon_DB_Backup_ГГГГ-ММ-ДД_ЧЧ-ММ-СС.zip.
   - Архив загружается в вашу закрытую папку Google Drive по Google Drive API.
   - Копия архива и статус выгрузки отправляются администратору в Telegram вместе с утренним отчетом.
2. **⚡ В любой момент вручную**:
   - Администратор может вызвать команду /backup в Telegram для мгновенного снимка и выгрузки.

---

## 🔑 Инструкция по подключению Google Service Account (1 раз за 3 минуты)

Для того чтобы бот имел доступ к закрытой папке Google Drive, нужен сервисный аккаунт Google Cloud (бесплатно):

### Шаг 1: Создание проекта в Google Cloud Console
1. Перейдите в [Google Cloud Console](https://console.cloud.google.com/).
2. Создайте новый проект (например, Raccoon-Life-Backup).
3. В меню **APIs & Services** > **Library** найдите **Google Drive API** и нажмите **Enable** (Включить).

### Шаг 2: Создание сервисного аккаунта (Service Account)
1. Перейдите в **APIs & Services** > **Credentials**.
2. Нажмите **+ CREATE CREDENTIALS** > **Service account**.
3. Введите имя (например, accoon-backup-bot) и нажмите **Create and Continue**, затем **Done**.
4. В списке сервисных аккаунтов нажмите на созданный аккаунт.
5. Перейдите во вкладку **KEYS** > **ADD KEY** > **Create new key** > выберите **JSON** > нажмите **Create**.
6. Файл с ключом (.json) скачается на ваш компьютер.

### Шаг 3: Предоставление доступа к папке Google Drive
1. Откройте скачанный .json файл и скопируйте email сервисного аккаунта (поле client_email, например: accoon-backup-bot@xxxx.iam.gserviceaccount.com).
2. Откройте вашу папку в Google Drive:  
   👉 https://drive.google.com/drive/folders/1l1IcnYsq-yBIX-q3ebf30YI8jlr8b568
3. Нажмите кнопку **Поделиться** (Share) у папки.
4. Вставьте скопированный email сервисного аккаунта и выберите роль **Редактор** (Editor).
5. Снимите галочку «Уведомить пользователей» и нажмите **Отправить** / **Поделиться**.

### Шаг 4: Размещение файла ключа в проекте
Выберите один из удобных способов:

- **Способ А (простой):**  
  Переименуйте скачанный файл в service_account.json и положите его в папку ot/ (путь: ot/service_account.json).
  
- **Способ Б (через переменную .env):**  
  В файле .env укажите путь к файлу ключа:
  `env
  GDRIVE_SERVICE_ACCOUNT_PATH=bot/service_account.json
  GDRIVE_FOLDER_ID=1l1IcnYsq-yBIX-q3ebf30YI8jlr8b568
  `
  Или передайте содержимое всего JSON одной строкой:
  `env
  GDRIVE_SERVICE_ACCOUNT_JSON={type: service_account, ...}
  `

---

## 🧪 Проверка работы

После размещения ключа напишите боту в Telegram:
`
/backup
`
Бот мгновенно создаст снимок базы, выгрузит его на Google Drive и пришлет вам подтверждение с прямой ссылкой на загруженный файл и прикрепленный архив!
