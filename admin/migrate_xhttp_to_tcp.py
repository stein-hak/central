#!/usr/bin/env python3
"""
Migrate XHTTP inbounds from Unix sockets to TCP ports

This script:
1. Finds all nodes with XHTTP inbounds using Unix sockets
2. Updates them to use TCP ports instead
3. Optionally updates nginx configuration if needed

Usage:
    python3 migrate_xhttp_to_tcp.py [--port 10001] [--dry-run] [--node NODE_NAME]
"""

import argparse
import json
import sys
import requests
from typing import List, Dict, Optional

# Add parent directory to path for imports
sys.path.insert(0, '/home/stein/python/3x-ui/central/admin')

from database import SessionLocal, Node

def get_node_inbounds(node: Node) -> Optional[List[Dict]]:
    """
    Get inbounds from a node's 3x-ui API

    Args:
        node: Node object with API credentials

    Returns:
        List of inbound dicts or None on error
    """
    try:
        # Login to get session cookie
        login_url = f"https://{node.ip_address}:{node.api_port}/login"
        login_data = {
            "username": node.username,
            "password": node.password
        }

        session = requests.Session()
        session.verify = False  # Skip SSL verification for self-signed certs

        response = session.post(login_url, json=login_data, timeout=10)
        if response.status_code != 200:
            print(f"  ❌ Failed to login to {node.name}: {response.status_code}")
            return None

        # Get inbounds list
        inbounds_url = f"https://{node.ip_address}:{node.api_port}/panel/api/inbounds/list"
        response = session.get(inbounds_url, timeout=10)

        if response.status_code != 200:
            print(f"  ❌ Failed to get inbounds from {node.name}: {response.status_code}")
            return None

        data = response.json()
        if not data.get('success'):
            print(f"  ❌ API returned error for {node.name}: {data.get('msg')}")
            return None

        return data.get('obj', [])

    except Exception as e:
        print(f"  ❌ Error connecting to {node.name}: {e}")
        return None


def update_inbound(node: Node, inbound_id: int, updated_settings: Dict) -> bool:
    """
    Update an inbound on a node

    Args:
        node: Node object
        inbound_id: Inbound ID to update
        updated_settings: New inbound configuration

    Returns:
        True if successful, False otherwise
    """
    try:
        # Login
        login_url = f"https://{node.ip_address}:{node.api_port}/login"
        login_data = {
            "username": node.username,
            "password": node.password
        }

        session = requests.Session()
        session.verify = False

        response = session.post(login_url, json=login_data, timeout=10)
        if response.status_code != 200:
            return False

        # Update inbound
        update_url = f"https://{node.ip_address}:{node.api_port}/panel/api/inbounds/update/{inbound_id}"
        response = session.post(update_url, json=updated_settings, timeout=10)

        if response.status_code != 200:
            print(f"  ❌ Failed to update inbound {inbound_id}: {response.status_code}")
            return False

        data = response.json()
        if not data.get('success'):
            print(f"  ❌ API returned error: {data.get('msg')}")
            return False

        return True

    except Exception as e:
        print(f"  ❌ Error updating inbound: {e}")
        return False


def migrate_xhttp_inbound(node: Node, inbound: Dict, new_port: int, dry_run: bool = False) -> bool:
    """
    Migrate a single XHTTP inbound from Unix socket to TCP port

    Args:
        node: Node object
        inbound: Inbound configuration dict
        new_port: TCP port to use
        dry_run: If True, only show what would be done

    Returns:
        True if migration successful (or would be successful in dry-run)
    """
    inbound_id = inbound.get('id')
    remark = inbound.get('remark', 'unnamed')

    # Parse stream settings
    stream_settings_str = inbound.get('streamSettings', '{}')
    try:
        stream_settings = json.loads(stream_settings_str) if isinstance(stream_settings_str, str) else stream_settings_str
    except:
        stream_settings = {}

    network = stream_settings.get('network', '')

    if network != 'xhttp':
        return False

    # Check if using Unix socket
    listen = inbound.get('listen', '')
    current_port = inbound.get('port', 0)

    is_socket = listen and (listen.startswith('/') or listen.startswith('unix:'))

    if not is_socket:
        print(f"  ℹ️  Inbound '{remark}' already using TCP port {current_port}")
        return True

    print(f"  📝 Inbound '{remark}' (ID: {inbound_id})")
    print(f"     Current: Unix socket {listen}")
    print(f"     New: TCP port {new_port}")

    if dry_run:
        print(f"     [DRY RUN] Would migrate to port {new_port}")
        return True

    # Update configuration
    updated_inbound = inbound.copy()
    updated_inbound['port'] = new_port
    updated_inbound['listen'] = '0.0.0.0'  # Listen on all interfaces (or use '127.0.0.1' for localhost only)

    # Update stream settings if listen is in there
    if 'xhttpSettings' in stream_settings and 'listen' in stream_settings['xhttpSettings']:
        stream_settings['xhttpSettings']['listen'] = ''  # Remove socket path

    updated_inbound['streamSettings'] = json.dumps(stream_settings)

    # Apply update
    if update_inbound(node, inbound_id, updated_inbound):
        print(f"     ✅ Migrated successfully")
        return True
    else:
        print(f"     ❌ Migration failed")
        return False


def main():
    parser = argparse.ArgumentParser(description='Migrate XHTTP inbounds from Unix sockets to TCP ports')
    parser.add_argument('--port', type=int, default=10001, help='TCP port to use for XHTTP (default: 10001)')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be done without making changes')
    parser.add_argument('--node', type=str, help='Migrate only specific node (by name)')
    parser.add_argument('--auto-port', action='store_true', help='Auto-assign port as gRPC_port + 1')

    args = parser.parse_args()

    print("=" * 80)
    print("XHTTP Unix Socket → TCP Port Migration")
    print("=" * 80)
    print()

    if args.dry_run:
        print("⚠️  DRY RUN MODE - No changes will be made")
        print()

    db = SessionLocal()

    try:
        # Get enabled nodes
        query = db.query(Node).filter(Node.enabled == True)
        if args.node:
            query = query.filter(Node.name == args.node)

        nodes = query.all()

        if not nodes:
            print("❌ No nodes found")
            return

        print(f"📋 Found {len(nodes)} enabled node(s)")
        print()

        migrated_count = 0
        error_count = 0
        skipped_count = 0

        for node in nodes:
            print(f"🔧 Processing {node.name} ({node.ip_address})")

            # Get inbounds from node
            inbounds = get_node_inbounds(node)

            if inbounds is None:
                print(f"  ❌ Could not retrieve inbounds")
                error_count += 1
                print()
                continue

            # Find XHTTP inbounds
            xhttp_inbounds = [ib for ib in inbounds if json.loads(ib.get('streamSettings', '{}')).get('network') == 'xhttp']

            if not xhttp_inbounds:
                print(f"  ℹ️  No XHTTP inbounds found")
                skipped_count += 1
                print()
                continue

            print(f"  Found {len(xhttp_inbounds)} XHTTP inbound(s)")

            for inbound in xhttp_inbounds:
                # Determine port to use
                if args.auto_port:
                    # Find gRPC port and add 1
                    grpc_inbounds = [ib for ib in inbounds if json.loads(ib.get('streamSettings', '{}')).get('network') == 'grpc']
                    if grpc_inbounds:
                        grpc_port = grpc_inbounds[0].get('port', 10000)
                        target_port = grpc_port + 1
                    else:
                        target_port = args.port
                else:
                    target_port = args.port

                if migrate_xhttp_inbound(node, inbound, target_port, args.dry_run):
                    if not args.dry_run:
                        migrated_count += 1
                else:
                    error_count += 1

            print()

        # Summary
        print("=" * 80)
        print("MIGRATION SUMMARY")
        print("=" * 80)

        if args.dry_run:
            print(f"Would migrate: {migrated_count} inbound(s)")
            print(f"Errors: {error_count}")
            print(f"Skipped (no XHTTP): {skipped_count}")
            print()
            print("Run without --dry-run to apply changes")
        else:
            print(f"✅ Migrated: {migrated_count} inbound(s)")
            print(f"❌ Errors: {error_count}")
            print(f"ℹ️  Skipped: {skipped_count}")

            if migrated_count > 0:
                print()
                print("⚠️  NEXT STEPS:")
                print("1. Update nginx configurations if XHTTP is behind nginx")
                print("   - Change: grpc_pass grpc://unix:/path/to/socket")
                print(f"   - To: grpc_pass grpc://127.0.0.1:{args.port}")
                print("2. Restart nginx: systemctl reload nginx")
                print("3. Test connections with updated port")

        print()

    finally:
        db.close()


if __name__ == "__main__":
    main()
