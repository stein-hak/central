#!/usr/bin/env python3
"""
Query enabled users from central API, then fix their clients if they have broken flow.

This script:
1. Fetches all users from central API
2. Filters for enabled users only
3. Checks each enabled user's client in the node database
4. If client has broken flow (xtls-rprx-vision), recreates it on all nodes

This ensures we only fix clients that:
- Belong to enabled users in central database
- Actually have broken flow settings on nodes
- Are not orphaned/old clients

Usage:
    python3 fix_enabled_clients_with_broken_flow.py <node_db_path> <central_api_url> [--dry-run]

Example:
    python3 fix_enabled_clients_with_broken_flow.py x-ui-serb2.db http://100.64.0.2:8000 --dry-run
    python3 fix_enabled_clients_with_broken_flow.py x-ui-serb2.db http://100.64.0.2:8000
"""

import sys
import json
import sqlite3
import requests
import getpass
from pathlib import Path


def login_to_api(api_url, password):
    """Login to central API and return session"""
    session = requests.Session()
    session.verify = False

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
        raise Exception("No session_id cookie received")

    return session


def get_all_users_from_api(api_url, session):
    """Get all users from central API (paginated)"""
    print("📥 Fetching users from central API...")

    all_users = []
    page = 1
    limit = 100  # Max per page

    while True:
        response = session.get(
            f"{api_url}/api/users",
            params={"page": page, "limit": limit},
            timeout=30
        )

        if response.status_code != 200:
            raise Exception(f"Failed to fetch users: {response.status_code}")

        data = response.json()
        users = data.get("users", [])
        total = data.get("total", 0)

        all_users.extend(users)

        print(f"   Page {page}: fetched {len(users)} users (total so far: {len(all_users)}/{total})")

        if len(all_users) >= total:
            break

        page += 1

    print(f"   ✅ Fetched {len(all_users)} total users\n")
    return all_users


def check_client_flow_in_node(db_path, client_email):
    """Check if client has broken flow in local node database"""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("SELECT id, remark, settings FROM inbounds")
    inbounds = cursor.fetchall()

    for inbound_id, remark, settings_str in inbounds:
        try:
            settings = json.loads(settings_str)
            clients = settings.get("clients", [])

            for client in clients:
                if client.get("email") == client_email:
                    flow = client.get("flow", "")
                    enable = client.get("enable", True)
                    conn.close()
                    return {
                        "found": True,
                        "flow": flow,
                        "enabled_on_node": enable,
                        "inbound": remark,
                        "has_broken_flow": flow == "xtls-rprx-vision"
                    }

        except json.JSONDecodeError:
            continue

    conn.close()
    return {"found": False}


def recreate_client(api_url, session, client_id, client_email):
    """Recreate client on all nodes via central API"""
    try:
        response = session.post(
            f"{api_url}/api/clients/{client_id}/recreate",
            timeout=180
        )

        if response.status_code == 200:
            data = response.json()
            return True, data.get("success_count", 0), data.get("total_nodes", 0)
        else:
            error_data = response.json() if response.headers.get('content-type') == 'application/json' else {}
            return False, 0, 0, error_data.get("detail", f"HTTP {response.status_code}")

    except Exception as e:
        return False, 0, 0, str(e)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    db_path = sys.argv[1]
    central_api_url = sys.argv[2].rstrip('/')
    dry_run = "--dry-run" in sys.argv

    if not Path(db_path).exists():
        print(f"❌ Database not found: {db_path}")
        sys.exit(1)

    print("=" * 70)
    if dry_run:
        print("FIX ENABLED CLIENTS WITH BROKEN FLOW - DRY RUN")
    else:
        print("FIX ENABLED CLIENTS WITH BROKEN FLOW - LIVE MODE")
    print("=" * 70)
    print(f"Node DB: {db_path}")
    print(f"Central API: {central_api_url}")
    print("=" * 70)
    print()

    # Login
    print("🔐 Logging in to central API...")
    password = getpass.getpass("Enter admin password: ")

    if not password:
        print("❌ Password required!")
        sys.exit(1)

    try:
        session = login_to_api(central_api_url, password)
        print("   ✅ Authenticated\n")
    except Exception as e:
        print(f"❌ Login failed: {e}")
        sys.exit(1)

    # Get all users from central API
    try:
        all_users = get_all_users_from_api(central_api_url, session)
    except Exception as e:
        print(f"❌ Failed to fetch users: {e}")
        sys.exit(1)

    # Filter enabled users
    enabled_users = [u for u in all_users if u.get("enabled", False)]
    print(f"📊 Total users in central: {len(all_users)}")
    print(f"   Enabled: {len(enabled_users)}")
    print(f"   Disabled: {len(all_users) - len(enabled_users)}\n")

    # Check each enabled user's client in node DB
    print(f"🔍 Checking enabled users' clients against node database...\n")

    clients_to_fix = []

    for user in enabled_users:
        telegram_id = user.get("telegram_id")
        client_email = user.get("client_email")
        client_id = user.get("client_id")  # User should have client_id

        if not client_email:
            continue  # Skip users without client

        if not client_id:
            continue  # Skip users without client_id

        node_status = check_client_flow_in_node(db_path, client_email)

        if node_status["found"] and node_status["has_broken_flow"]:
            clients_to_fix.append({
                "telegram_id": telegram_id,
                "client_id": client_id,
                "email": client_email,
                "inbound": node_status["inbound"],
                "enabled_on_node": node_status["enabled_on_node"]
            })

    print(f"📈 Analysis complete:")
    print(f"   Enabled users in central: {len(enabled_users)}")
    print(f"   Clients with broken flow on node: {len(clients_to_fix)}")
    print()

    if not clients_to_fix:
        print("✅ No enabled clients with broken flow found!")
        return

    # Show sample
    print(f"🔧 Clients to fix (first 10):")
    for i, client in enumerate(clients_to_fix[:10], 1):
        print(f"   {i}. {client['email']} (telegram_id: {client['telegram_id']}) - {client['inbound']}")

    if len(clients_to_fix) > 10:
        print(f"   ... and {len(clients_to_fix) - 10} more")
    print()

    if dry_run:
        print("🔸 DRY RUN - No changes will be made")
        print(f"\nWould recreate {len(clients_to_fix)} clients via central API")
        return

    # Confirm
    confirm = input(f"Recreate {len(clients_to_fix)} clients? [y/N]: ").strip().lower()
    if confirm != 'y':
        print("❌ Aborted by user")
        return

    print()
    print("🚀 Recreating clients...\n")

    stats = {
        "success": 0,
        "failed": 0,
        "errors": []
    }

    for idx, client in enumerate(clients_to_fix, 1):
        client_id = client["client_id"]
        client_email = client["email"]
        telegram_id = client["telegram_id"]

        print(f"[{idx}/{len(clients_to_fix)}] {client_email} (user: {telegram_id})")

        result = recreate_client(central_api_url, session, client_id, client_email)

        if result[0]:  # success
            success_count, total_nodes = result[1], result[2]
            print(f"   ✅ Recreated on {success_count}/{total_nodes} nodes\n")
            stats["success"] += 1
        else:
            error_msg = result[3] if len(result) > 3 else "Unknown error"
            print(f"   ❌ Failed: {error_msg}\n")
            stats["failed"] += 1
            stats["errors"].append(f"{client_email}: {error_msg}")

    # Summary
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Total clients processed: {len(clients_to_fix)}")
    print(f"✅ Successfully recreated: {stats['success']}")
    print(f"❌ Failed: {stats['failed']}")

    if stats["errors"]:
        print(f"\n❌ Errors ({len(stats['errors'])}):")
        for error in stats["errors"][:10]:
            print(f"   - {error}")
        if len(stats["errors"]) > 10:
            print(f"   ... and {len(stats['errors']) - 10} more")

    print()


if __name__ == "__main__":
    main()
