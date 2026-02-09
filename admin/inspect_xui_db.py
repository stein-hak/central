#!/usr/bin/env python3
"""
Inspect 3x-ui database to find where Xray config is stored.
"""
import sqlite3
import json

DB_PATH = '/opt/3x-ui/data/x-ui.db'

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# List all tables
print("=== Tables in database ===")
cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cursor.fetchall()
for table in tables:
    print(f"  - {table[0]}")

print("\n=== Settings table contents ===")
cursor.execute("SELECT key FROM settings")
keys = cursor.fetchall()
for key in keys:
    print(f"  - {key[0]}")

print("\n=== Checking for Xray config ===")
# Check common key names
possible_keys = [
    'xrayTemplateConfig',
    'xrayConfig',
    'xray_config',
    'config',
    'template_config'
]

for key in possible_keys:
    cursor.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cursor.fetchone()
    if row:
        print(f"✓ Found config at key: '{key}'")
        print(f"  Value length: {len(row[0])} bytes")
        try:
            config = json.loads(row[0])
            print(f"  Config keys: {list(config.keys())}")
        except:
            print(f"  Value (not JSON): {row[0][:100]}...")

# Check inbounds table (Xray config might be per-inbound)
print("\n=== Inbounds table structure ===")
cursor.execute("PRAGMA table_info(inbounds)")
columns = cursor.fetchall()
for col in columns:
    print(f"  - {col[1]} ({col[2]})")

conn.close()
