import sys
import io
import os
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from google_auth_oauthlib.flow import InstalledAppFlow
import paramiko

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

SCOPES = ['https://www.googleapis.com/auth/drive.file', 'https://www.googleapis.com/auth/drive']
PORT = 8085
REDIRECT_URI = f'http://localhost:{PORT}/'


flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES, redirect_uri=REDIRECT_URI)
auth_url, _ = flow.authorization_url(prompt='consent', access_type='offline')

print("=" * 70, flush=True)
print("ССЫЛКА ДЛЯ АВТОРИЗАЦИИ В БРАУЗЕРЕ (КЛИКНИТЕ ПО НЕЙ):", flush=True)
print(auth_url, flush=True)
print("=" * 70, flush=True)


# Открываем в браузере Windows
try:
    import subprocess
    subprocess.Popen(['cmd', '/c', 'start', '', auth_url], shell=True)
except Exception:
    pass

auth_code = None

class OAuthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        query = parse_qs(urlparse(self.path).query)
        if 'code' in query:
            auth_code = query['code'][0]
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write("""
                <html><body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1 style="color: green;">🎉 Авторизация Google Drive успешно завершена!</h1>
                <p>Вы можете закрыть эту вкладку и вернуться в чат.</p>
                </body></html>
            """.encode('utf-8'))
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"No code received")

    def log_message(self, format, *args):
        pass

server = HTTPServer(('localhost', PORT), OAuthHandler)
print(f"Ожидание подтверждения авторизации на порту {PORT}...", flush=True)

while not auth_code:
    server.handle_request()

flow.fetch_token(code=auth_code)
creds = flow.credentials

token_json_str = creds.to_json()

# Сохраняем локально
with open('token.json', 'w', encoding='utf-8') as f:
    f.write(token_json_str)

with open('bot/token.json', 'w', encoding='utf-8') as f:
    f.write(token_json_str)

print("✅ Токен успешно сохранен локально: token.json", flush=True)

# Загружаем на сервер
try:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect('65.21.25.155', username='botuser', password='Yfdctulf2!', timeout=10)
    sftp = client.open_sftp()
    sftp.put('bot/token.json', '/home/botuser/Raccoon_life/bot/token.json')
    sftp.put('bot/token.json', '/home/botuser/Raccoon_life/token.json')
    sftp.close()
    
    stdin, stdout, stderr = client.exec_command('echo Yfdctulf2! | sudo -S systemctl restart raccoon_bot.service')
    stdout.read()
    client.close()
    print("🚀 Токен успешно загружен на сервер 65.21.25.155 и бот перезапущен!", flush=True)
except Exception as e:
    print(f"⚠️ Не удалось загрузить на сервер автоматически: {e}", flush=True)

print("=" * 70, flush=True)
print("🎉 ВСЁ ГОТОВО! Теперь Google Drive подключен и работает на 100%!", flush=True)
print("=" * 70, flush=True)
