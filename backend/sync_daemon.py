import os, sqlite3, json, logging, time, requests  
from pathlib import Path  
from dotenv import load_dotenv  
load_dotenv()  
logging.basicConfig(level=logging.INFO)  
logger = logging.getLogger(__name__)  
BACKEND_URL = os.getenv(\"BACKEND_URL\", \"http://localhost:8000\")  
BOT_DB = Path(__file__).parent.parent / \"bot\" / \"users.db\"  
INTERVAL = int(os.getenv(\"SYNC_INTERVAL\", \"60\"))  
def sync(:  
    if not BOT_DB.exists(: return  
    conn = sqlite3.connect(str(BOT_DB))  
    conn.row_factory = sqlite3.Row  
    for row in conn.execute(\"SELECT * FROM users u JOIN user_stats s ON u.user_id = s.user_id\"):  
        try:  
            d = dict(row)  
            d[\"quests\"] = json.loads(d[\"quests\"] or \"[]\")  
            r = requests.post(f\"{BACKEND_URL}/api/user\", json=d, timeout=10)  
            if r.ok: logger.info(f\"OK: {d['user_id']}\")  
        except Exception as e: logger.error(e)  
        time.sleep(0.5)  
    conn.close()  
while True:  
    sync()  
    time.sleep(INTERVAL)  
