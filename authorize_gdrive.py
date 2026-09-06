"""
Скрипт однократной авторизации Google Drive для Raccoon Life
Создает token.json, который позволяет боту выгружать бекапы на ваш Google Drive.
"""
import os
import sys
from pathlib import Path

def main():
    print("=" * 60)
    print("Авторизация Google Drive для бекапов Raccoon Life")
    print("=" * 60)

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Установка зависимостей...")
        os.system(f'"{sys.executable}" -m pip install google-auth-oauthlib')
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

    SCOPES = ['https://www.googleapis.com/auth/drive.file', 'https://www.googleapis.com/auth/drive']
    
    bot_dir = Path(__file__).resolve().parent / "bot"
    token_path = bot_dir / "token.json"
    credentials_path = None

    # Поиск client_secrets / credentials .json
    possible_creds = [
        bot_dir / "credentials.json",
        bot_dir / "client_secret.json",
        Path(__file__).resolve().parent / "credentials.json",
        Path(__file__).resolve().parent / "client_secret.json",
        Path.home() / "Downloads" / "credentials.json",
        Path.home() / "Downloads" / "client_secret.json",
    ]

    for p in possible_creds:
        if p.exists():
            credentials_path = p
            break

    if not credentials_path:
        for p in (Path.home() / "Downloads").glob("client_secret_*.json"):
            credentials_path = p
            break

    if not credentials_path:
        print("\nФайл 'credentials.json' не найден!")
        return

    print(f"Используется файл клиента: {credentials_path}")
    print("Сейчас в браузере откроется страница входа в Google...")
    print("Выберите ваш Google-аккаунт и нажмите 'Разрешить / Продолжить'.")

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES, redirect_uri='http://localhost:8085/')
    auth_url, _ = flow.authorization_url(prompt='consent', access_type='offline')
    
    print("\n" + "=" * 60)
    print("ССЫЛКА ДЛЯ АВТОРИЗАЦИИ В БРАУЗЕРЕ:")
    print(auth_url)
    print("=" * 60)
    
    try:
        import subprocess
        subprocess.Popen(['cmd', '/c', 'start', '', auth_url], shell=True)
    except Exception:
        pass

    creds = flow.run_local_server(port=8085, prompt='consent')


    # Сохраняем token.json в bot/ и корень проекта
    with open(token_path, 'w', encoding='utf-8') as token_file:
        token_file.write(creds.to_json())

    root_token_path = Path(__file__).resolve().parent / "token.json"
    with open(root_token_path, 'w', encoding='utf-8') as token_file:
        token_file.write(creds.to_json())

    print("\n" + "=" * 60)
    print("УСПЕШНО! Google Drive авторизован!")
    print(f"Файл токена сохранен: {token_path}")
    print("=" * 60)

if __name__ == '__main__':
    main()
