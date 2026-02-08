#!/usr/bin/env python3
"""
Fix all clients with xtls-rprx-vision flow in 3x-ui database
Changes flow to empty string for compatibility
"""

import sqlite3
import json
import sys
import shutil
from datetime import datetime
from pathlib import Path

def create_backup(db_path):
    """Create timestamped backup of database"""
    db_file = Path(db_path)
    if not db_file.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = db_file.parent / f"{db_file.stem}_backup_{timestamp}{db_file.suffix}"

    print(f"📦 Creating backup: {backup_path.name}")
    shutil.copy2(db_path, backup_path)
    print(f"   ✅ Backup created\n")

    return str(backup_path)

def fix_client_flows(db_path, dry_run=False):
    """Fix all clients with XTLS flow"""

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get all inbounds
    cursor.execute("SELECT id, remark, settings FROM inbounds")
    inbounds = cursor.fetchall()

    fixed_count = 0
    total_clients = 0

    print(f"\n{'='*70}")
    print(f"FIX CLIENT FLOW - {'DRY RUN' if dry_run else 'LIVE MODE'}")
    print(f"{'='*70}\n")

    for inbound_id, remark, settings_json in inbounds:
        try:
            settings = json.loads(settings_json)
            clients = settings.get("clients", [])

            if not clients:
                continue

            modified = False

            for client in clients:
                total_clients += 1
                email = client.get("email", "unknown")
                flow = client.get("flow", "")

                if flow == "xtls-rprx-vision":
                    print(f"🔧 Fixing: {email} in inbound '{remark}'")
                    print(f"   Old flow: {flow}")

                    if not dry_run:
                        client["flow"] = ""
                        modified = True

                    print(f"   New flow: (empty)")
                    fixed_count += 1

            # Update inbound if modified
            if modified:
                updated_settings = json.dumps(settings)
                cursor.execute(
                    "UPDATE inbounds SET settings = ? WHERE id = ?",
                    (updated_settings, inbound_id)
                )

        except json.JSONDecodeError as e:
            print(f"❌ Error parsing settings for inbound {inbound_id}: {e}")
            continue

    if not dry_run:
        conn.commit()

    conn.close()

    # Summary
    print(f"\n{'='*70}")
    print(f"SUMMARY")
    print(f"{'='*70}")
    print(f"Total clients: {total_clients}")
    print(f"Fixed clients: {fixed_count}")

    if dry_run:
        print(f"\n✅ DRY RUN COMPLETE - No changes made")
        print(f"   Run without --dry-run to apply changes")
    else:
        print(f"\n✅ FIXED {fixed_count} client(s)")

    print(f"{'='*70}\n")

    return fixed_count

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python fix_client_flow.py <path_to_x-ui.db> [--dry-run]")
        print("\nExample:")
        print("  python fix_client_flow.py /home/stein/python/3x-ui/x-ui.db --dry-run")
        print("  python fix_client_flow.py /home/stein/python/3x-ui/x-ui.db")
        sys.exit(1)

    db_path = sys.argv[1]
    dry_run = "--dry-run" in sys.argv

    try:
        # Create backup before making changes
        if not dry_run:
            backup_path = create_backup(db_path)

        fixed = fix_client_flows(db_path, dry_run)
        sys.exit(0 if fixed >= 0 else 1)
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
