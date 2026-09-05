"""Тест миграции BLOB для custom_chip_sets"""
import sqlite3
import json
import base64
from pathlib import Path

DB_PATH = Path('bot/users.db')
WEBAPP_DIR = Path('webapp')

conn = sqlite3.connect(str(DB_PATH))
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Проверяем наличие колонок
cols = cur.execute("PRAGMA table_info(custom_chip_sets)").fetchall()
col_names = [c['name'] for c in cols]
print("Kolonki:", col_names)
has_blob_col = 'preview_collage_blob' in col_names
has_chips_blob_col = 'chips_blobs_json' in col_names
print("preview_collage_blob:", "OK" if has_blob_col else "MISSING - need bot restart")
print("chips_blobs_json:", "OK" if has_chips_blob_col else "MISSING - need bot restart")

if not has_blob_col:
    print("\nRunning migration manually...")
    cur.execute("ALTER TABLE custom_chip_sets ADD COLUMN preview_collage_blob BLOB")
    cur.execute("ALTER TABLE custom_chip_sets ADD COLUMN chips_blobs_json TEXT")
    conn.commit()
    print("Columns added!")
    
    # Backfill
    cur.execute("SELECT id, preview_collage, chips_json FROM custom_chip_sets WHERE preview_collage_blob IS NULL")
    rows = cur.fetchall()
    for row in rows:
        sid = row['id']
        collage_rel = str(row['preview_collage'] or '').lstrip('/\\')
        collage_path = WEBAPP_DIR / collage_rel
        collage_blob = None
        if collage_path.exists():
            with open(collage_path, 'rb') as f:
                collage_blob = f.read()
            print(f"  ID {sid}: collage blob {len(collage_blob)} bytes")
        else:
            print(f"  ID {sid}: file NOT FOUND: {collage_path}")
        chips_blobs = []
        try:
            chips_paths = json.loads(row['chips_json'] or '[]')
            for cp in chips_paths:
                chip_path = WEBAPP_DIR / str(cp).lstrip('/\\')
                if chip_path.exists():
                    with open(chip_path, 'rb') as f:
                        chips_blobs.append(base64.b64encode(f.read()).decode('ascii'))
                else:
                    chips_blobs.append(None)
        except Exception as e:
            print(f"  Error chips: {e}")
        cur.execute("UPDATE custom_chip_sets SET preview_collage_blob = ?, chips_blobs_json = ? WHERE id = ?",
                    (collage_blob, json.dumps(chips_blobs), sid))
    conn.commit()
    print("Backfill done!")

# Final check
cur.execute("SELECT id, title, preview_collage_blob, chips_blobs_json, status FROM custom_chip_sets")
rows = cur.fetchall()
for r in rows:
    blob_size = len(r['preview_collage_blob']) if r['preview_collage_blob'] else 0
    chips_count = 0
    if r['chips_blobs_json']:
        blobs = json.loads(r['chips_blobs_json'])
        chips_count = len([b for b in blobs if b])
    print(f"ID:{r['id']} [{r['status']}] collage_blob={blob_size}b chips_blobs={chips_count}")

conn.close()
