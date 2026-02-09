#!/usr/bin/env python3
"""
Update Xray bufferSize in 3x-ui database to handle large VPN packets.
For nodes with 1GB RAM and ~30 max concurrent clients.
"""
import sqlite3
import json
import sys
import os

DB_PATH = '/opt/3x-ui/data/x-ui.db'
BUFFER_SIZE_KB = 2048  # 2MB - safe for 30 clients on 1GB RAM

def update_xray_config():
    """Update Xray configuration with optimized bufferSize."""

    # Check if database exists
    if not os.path.exists(DB_PATH):
        print(f"ERROR: Database not found at {DB_PATH}")
        print("This script should be run on the node server, not locally.")
        sys.exit(1)

    # Backup database
    backup_path = f"{DB_PATH}.backup"
    print(f"Creating backup at {backup_path}...")
    os.system(f"cp {DB_PATH} {backup_path}")

    try:
        # Connect to database
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Get current config
        cursor.execute("SELECT value FROM settings WHERE key='xrayTemplateConfig'")
        row = cursor.fetchone()

        if not row:
            print("ERROR: xrayTemplateConfig not found in database")
            sys.exit(1)

        config = json.loads(row[0])
        print(f"Current config keys: {list(config.keys())}")

        # Add/update policy configuration
        if 'policy' not in config:
            config['policy'] = {}

        if 'levels' not in config['policy']:
            config['policy']['levels'] = {}

        config['policy']['levels']['0'] = {
            'bufferSize': BUFFER_SIZE_KB,  # 2MB for handling large packets
            'connIdle': 300,
            'statsUserUplink': True,
            'statsUserDownlink': True
        }

        # Ensure log configuration exists
        if 'log' not in config:
            config['log'] = {
                'access': '/var/log/xray/access.log',
                'error': '/var/log/xray/error.log',
                'loglevel': 'warning'
            }

        # Update database
        new_config_str = json.dumps(config, indent=2)
        cursor.execute(
            "UPDATE settings SET value=? WHERE key='xrayTemplateConfig'",
            (new_config_str,)
        )
        conn.commit()

        print(f"✓ Successfully updated bufferSize to {BUFFER_SIZE_KB}KB (2MB)")
        print(f"✓ Memory usage with 30 clients: ~60MB")
        print(f"✓ Can handle packets up to ~2.8MB")
        print("\nNext steps:")
        print("1. Restart 3x-ui: systemctl restart x-ui")
        print("2. Monitor logs: tail -f /var/log/xray/error.log")
        print("3. Check for 'message too large' warnings (should be gone)")

    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    finally:
        conn.close()

if __name__ == '__main__':
    update_xray_config()
