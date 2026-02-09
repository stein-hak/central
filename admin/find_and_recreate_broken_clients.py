#!/usr/bin/env python3
"""
Find clients with broken flow settings in local node database,
then recreate them centrally on all nodes.

Usage:
    python3 find_and_recreate_broken_clients.py <node_db_path> <central_api_url> [--dry-run]

Example:
    python3 find_and_recreate_broken_clients.py /opt/3x-ui/data/x-ui.db http://100.64.0.2:8000
    python3 find_and_recreate_broken_clients.py /opt/3x-ui/data/x-ui.db http://100.64.0.2:8000 --dry-run

Note: You'll be prompted for the admin password when running in live mode.
"""

import sys
import json
import sqlite3
import requests
from pathlib import Path


def find_broken_clients(db_path):
    """Find enabled clients with incorrect flow in local database"""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get all inbounds
    cursor.execute("SELECT id, remark, settings FROM inbounds")
    inbounds = cursor.fetchall()

    broken_clients = []

    for inbound_id, remark, settings_str in inbounds:
        try:
            settings = json.loads(settings_str)
            clients = settings.get("clients", [])

            for client in clients:
                email = client.get("email", "")
                flow = client.get("flow", "")
                enable = client.get("enable", True)
                uuid = client.get("id", "")

                # Find enabled clients with broken flow
                if flow == "xtls-rprx-vision" and enable:
                    broken_clients.append({
                        "email": email,
                        "uuid": uuid,
                        "inbound_id": inbound_id,
                        "inbound_remark": remark
                    })

        except json.JSONDecodeError:
            continue

    conn.close()
    return broken_clients


def get_client_id_from_central(api_url, session, email):
    """Get client_id from central API by email"""
    try:
        # Search for client by email
        response = session.get(
            f"{api_url}/api/clients",
            params={"search": email, "limit": 100},
            timeout=30  # Increased timeout for search
        )

        if response.status_code == 200:
            data = response.json()
            clients = data.get("clients", [])

            # Find exact match
            for client in clients:
                if client.get("email") == email:
                    return client.get("id")

        return None

    except Exception as e:
        print(f"  ⚠️  Error searching for {email}: {e}")
        return None


def recreate_client_centrally(api_url, session, client_id, email):
    """Recreate client on all nodes via central API"""
    try:
        response = session.post(
            f"{api_url}/api/clients/{client_id}/recreate",
            timeout=180  # 3 minutes - recreating on 17 nodes takes time
        )

        if response.status_code == 200:
            data = response.json()
            success_count = data.get("success_count", 0)
            total_nodes = data.get("total_nodes", 0)
            return True, f"{success_count}/{total_nodes} nodes"
        else:
            error_data = response.json() if response.headers.get('content-type') == 'application/json' else {}
            return False, f"HTTP {response.status_code}: {error_data.get('detail', 'Unknown error')}"

    except Exception as e:
        return False, str(e)


def login_to_central(api_url, password):
    """Login to central API and return session with cookie"""
    session = requests.Session()
    session.verify = False  # Disable SSL verification

    response = session.post(
        f"{api_url}/login",
        data={"password": password},
        allow_redirects=False,
        timeout=10
    )

    if response.status_code not in [200, 302]:
        raise Exception(f"Login failed: {response.status_code}")

    session_id = response.cookies.get("session_id")
    if not session_id:
        raise Exception("No session_id cookie received after login")

    return session


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    db_path = sys.argv[1]
    central_api_url = sys.argv[2].rstrip('/')
    dry_run = "--dry-run" in sys.argv

    # Check if database exists
    if not Path(db_path).exists():
        print(f"❌ Database not found: {db_path}")
        sys.exit(1)

    print("=" * 70)
    if dry_run:
        print("FIND AND RECREATE BROKEN CLIENTS - DRY RUN")
    else:
        print("FIND AND RECREATE BROKEN CLIENTS - LIVE MODE")
    print("=" * 70)
    print()

    # Find broken clients in local database
    print(f"🔍 Scanning database: {db_path}")
    broken_clients = find_broken_clients(db_path)

    if not broken_clients:
        print("✅ No clients with incorrect flow found!")
        return

    print(f"❌ Found {len(broken_clients)} clients with incorrect flow\n")

    # Show first 10 and summary
    for client in broken_clients[:10]:
        print(f"  - {client['email']} (UUID: {client['uuid']}) in {client['inbound_remark']}")

    if len(broken_clients) > 10:
        print(f"  ... and {len(broken_clients) - 10} more\n")
    else:
        print()

    if dry_run:
        print("🔸 DRY RUN - No changes will be made")
        print(f"\nWould recreate {len(broken_clients)} clients via central API")
        return

    # Get password from user
    print("🔐 Authentication required for central API")
    print(f"    API URL: {central_api_url}")
    print()

    import getpass
    password = getpass.getpass("Enter admin password: ")

    if not password:
        print("❌ Password required!")
        sys.exit(1)

    try:
        session = login_to_central(central_api_url, password)
        print("   ✅ Authenticated\n")
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        sys.exit(1)

    print("🔧 Recreating clients via central API...\n")

    success_count = 0
    failed_count = 0
    not_found_count = 0

    for idx, client in enumerate(broken_clients, 1):
        email = client['email']
        print(f"[{idx}/{len(broken_clients)}] {email}")

        # Get client_id from central
        print(f"  Looking up in central database...", end=" ")
        client_id = get_client_id_from_central(central_api_url, session, email)

        if not client_id:
            print("❌ Not found in central")
            not_found_count += 1
            continue

        print(f"✓ (ID: {client_id})")

        # Recreate via central API
        print(f"  Recreating on all nodes...", end=" ")
        success, message = recreate_client_centrally(central_api_url, session, client_id, email)

        if success:
            print(f"✅ {message}")
            success_count += 1
        else:
            print(f"❌ {message}")
            failed_count += 1

        print()

    # Summary
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Total broken clients found:    {len(broken_clients)}")
    print(f"Successfully recreated:        {success_count}")
    print(f"Failed:                        {failed_count}")
    print(f"Not found in central:          {not_found_count}")
    print()

    if success_count > 0:
        print("✅ Clients have been recreated on all nodes with correct flow settings")


if __name__ == "__main__":
    main()
