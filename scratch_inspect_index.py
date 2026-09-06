import re

with open('webapp/index.html', 'r', encoding='utf-8', errors='ignore') as f:
    text = f.read()

# Let's inspect the Home screen buttons / header
lines = text.splitlines()
for i, line in enumerate(lines, 1):
    if 'home__bottom-btn' in line or 'home-header' in line or 'cover-top-bar' in line or 'openInventoryModal' in line:
        print(f"{i}: {line.strip()[:120]}")
