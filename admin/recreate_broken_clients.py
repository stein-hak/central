#!/usr/bin/env python3
"""
Recreate clients with broken flow settings using 3x-ui API
Finds clients with xtls-rprx-vision flow and recreates them with empty flow
"""

import sqlite3
import json
import sys
import requests
import os
from datetime import datetime

def get_broken_clients(db_path):
    """Find all clients with xtls-rprx-vision flow"""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("SELECT id, remark, listen, port, settings FROM inbounds")
    inbounds = cursor.fetchall()

    broken_clients = []

    for inbound_id, remark, listen, port, settings_json in inbounds:
        try:
            settings = json.loads(settings_json)
            clients = settings.get("clients", [])

            for client in clients:
                flow = client.get("flow", "")
                enable = client.get("enable", True)
                if flow == "xtls-rprx-vision" and enable:  # Only enabled clients
                    broken_clients.append({
                        "inbound_id": inbound_id,
                        "inbound_remark": remark,
                        "email": client.get("email"),
                        "uuid": client.get("id"),
                        "enable": enable,
                        "flow": flow
                    })
        except json.JSONDecodeError:
            continue

    conn.close()
    return broken_clients

def login_to_xui(api_url, username, password):
    """Login to 3x-ui and return session"""
    session = requests.Session()

    response = session.post(
        f"{api_url}/login",
        data={"username": username, "password": password},
        timeout=10
    )

    if response.status_code not in [200, 302]:
        raise Exception(f"Login failed: {response.status_code}")

    return session

def delete_client_from_inbound(session, api_url, inbound_id, email):
    """Delete client from inbound via API"""
    response = session.post(
        f"{api_url}/panel/api/inbounds/{inbound_id}/delClientByEmail/{email}",
        timeout=10
    )

    if response.status_code != 200:
        raise Exception(f"Delete failed: {response.status_code} - {response.text}")

    result = response.json()
    if not result.get("success"):
        raise Exception(f"Delete API error: {result}")

    return True

def add_client_to_inbound(session, api_url, inbound_id, email, uuid, enable=True):
    """Add client to inbound with correct flow via API"""
    client_data = {
        "id": inbound_id,
        "settings": json.dumps({
            "clients": [{
                "id": uuid,
                "email": email,
                "enable": enable,
                "flow": "",  # Empty flow - correct!
                "limitIp": 0,
                "totalGB": 0,
                "expiryTime": 0,
                "subId": "",
                "tgId": ""
            }]
        })
    }

    response = session.post(
        f"{api_url}/panel/api/inbounds/addClient",
        json=client_data,
        timeout=10
    )

    if response.status_code != 200:
        raise Exception(f"Add failed: {response.status_code} - {response.text}")

    result = response.json()
    if not result.get("success"):
        raise Exception(f"Add API error: {result}")

    return True

def read_credentials(creds_path="/opt/3x-ui/data/credentials.txt"):
    """Read username and password from credentials file"""
    username = "admin"
    password = "admin"

    try:
        with open(creds_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line.startswith("Username:"):
                    username = line.split(":", 1)[1].strip()
                elif line.startswith("Password:"):
                    password = line.split(":", 1)[1].strip()
    except Exception as e:
        print(f"Warning: Could not read credentials from {creds_path}: {e}")
        print(f"Using default: admin/admin")

    return username, password

def main():
    if len(sys.argv) < 2:
        print("Usage: python recreate_broken_clients.py <path_to_x-ui.db> [--api-url URL] [--username USER] [--password PASS] [--dry-run]")
        print("\nExample:")
        print("  python recreate_broken_clients.py /opt/3x-ui/data/x-ui.db --api-url http://localhost:2053 --dry-run")
        print("  python recreate_broken_clients.py /opt/3x-ui/data/x-ui.db --api-url http://localhost:2053")
        sys.exit(1)

    db_path = sys.argv[1]
    api_url = "http://localhost:2053"

    # Read credentials from file
    username, password = read_credentials()
    dry_run = "--dry-run" in sys.argv

    # Parse arguments (can override credentials)
    for i, arg in enumerate(sys.argv):
        if arg == "--api-url" and i + 1 < len(sys.argv):
            api_url = sys.argv[i + 1]
        if arg == "--username" and i + 1 < len(sys.argv):
            username = sys.argv[i + 1]
        if arg == "--password" and i + 1 < len(sys.argv):
            password = sys.argv[i + 1]

    print(f"\n{'='*70}")
    print(f"RECREATE BROKEN CLIENTS - {'DRY RUN' if dry_run else 'LIVE MODE'}")
    print(f"{'='*70}\n")

    # 1. Find broken clients
    print(f"🔍 Scanning database: {db_path}")
    broken_clients = get_broken_clients(db_path)

    if not broken_clients:
        print(f"✅ No broken clients found!\n")
        return 0

    print(f"❌ Found {len(broken_clients)} clients with incorrect flow\n")

    for client in broken_clients[:10]:
        print(f"  - {client['email']} (UUID: {client['uuid']}) in {client['inbound_remark']}")

    if len(broken_clients) > 10:
        print(f"  ... and {len(broken_clients) - 10} more\n")
    else:
        print()

    if dry_run:
        print(f"✅ DRY RUN COMPLETE - No changes made")
        print(f"   Run without --dry-run to recreate clients via API\n")
        return 0

    # 2. Login to API
    print(f"🔐 Logging in to 3x-ui API at {api_url}...")
    try:
        session = login_to_xui(api_url, username, password)
        print(f"   ✅ Authenticated\n")
    except Exception as e:
        print(f"   ❌ Login failed: {e}\n")
        return 1

    # 3. Recreate each client
    success_count = 0
    error_count = 0

    print(f"🔧 Recreating clients...\n")

    for client in broken_clients:
        email = client['email']
        uuid = client['uuid']
        inbound_id = client['inbound_id']
        inbound_remark = client['inbound_remark']

        try:
            # Delete old client
            print(f"  Deleting: {email} from {inbound_remark}...")
            delete_client_from_inbound(session, api_url, inbound_id, email)

            # Recreate with correct flow
            print(f"  Creating: {email} with flow=\"\" (empty)...")
            add_client_to_inbound(session, api_url, inbound_id, email, uuid, client['enable'])

            print(f"  ✅ Recreated: {email}\n")
            success_count += 1

        except Exception as e:
            print(f"  ❌ Error with {email}: {e}\n")
            error_count += 1

    # Summary
    print(f"{'='*70}")
    print(f"SUMMARY")
    print(f"{'='*70}")
    print(f"Total broken clients: {len(broken_clients)}")
    print(f"Successfully recreated: {success_count}")
    print(f"Errors: {error_count}")
    print(f"{'='*70}\n")

    return 0 if error_count == 0 else 1

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"\n❌ FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
