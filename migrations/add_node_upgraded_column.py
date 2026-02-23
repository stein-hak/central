#!/usr/bin/env python3
"""
Add 'upgraded' column to nodes table
This indicates if a node has synced clients across inbounds (uses HA ports)
"""

import os
import psycopg2

# Database connection
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://xui_admin:xui_admin_password@localhost:5432/xui_central")

def run_migration():
    """Add upgraded column to nodes table"""

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    try:
        # Check if column already exists
        cursor.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name='nodes' AND column_name='upgraded'
        """)

        if cursor.fetchone():
            print("✓ Column 'upgraded' already exists in nodes table")
            return

        # Add upgraded column
        print("Adding 'upgraded' column to nodes table...")
        cursor.execute("""
            ALTER TABLE nodes
            ADD COLUMN upgraded BOOLEAN DEFAULT FALSE
        """)

        conn.commit()
        print("✓ Migration completed successfully")

        # Show current nodes
        cursor.execute("SELECT id, name, enabled, upgraded FROM nodes ORDER BY id")
        nodes = cursor.fetchall()

        print(f"\nCurrent nodes ({len(nodes)}):")
        for node_id, name, enabled, upgraded in nodes:
            status = "enabled" if enabled else "disabled"
            upgrade_status = "UPGRADED" if upgraded else "standard"
            print(f"  [{node_id}] {name:20} {status:8} {upgrade_status}")

    except Exception as e:
        conn.rollback()
        print(f"✗ Migration failed: {e}")
        raise
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    run_migration()
