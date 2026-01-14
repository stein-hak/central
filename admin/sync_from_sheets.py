#!/usr/bin/env python3
"""
Sync users from Google Sheets to Central API

Usage:
    # Preview mode (just show summary, no API calls)
    python sync_from_sheets.py <google_sheets_csv_url>

    # Sync mode (actually create/update users)
    python sync_from_sheets.py <google_sheets_csv_url> --api-url http://localhost:8000 [--dry-run]

Example:
    python sync_from_sheets.py "https://docs.google.com/spreadsheets/d/e/2PACX-.../pub?output=csv&gid=0"
    python sync_from_sheets.py "https://docs.google.com/spreadsheets/d/e/2PACX-.../pub?output=csv&gid=0" --api-url http://localhost:8000 --dry-run
    python sync_from_sheets.py "https://docs.google.com/spreadsheets/d/e/2PACX-.../pub?output=csv&gid=0" --api-url http://localhost:8000
"""

import csv
import sys
import os
import requests
import re
from datetime import datetime
from io import StringIO
from collections import Counter

# Payment status mapping
PAYMENT_STATUS_MAP = {
    "Оплатил": 2,      # PAID
    "Не оплатил": 3,   # NOT_PAID
    "Тест": 1,         # TEST
    "Промокод": 4,     # PROMO
    "Отписался": 3,    # Unsubscribed → NOT_PAID
    "Вернулся": 2,     # Returned → PAID
}

PAYMENT_STATUS_NAMES = {
    1: "TEST",
    2: "PAID",
    3: "NOT_PAID",
    4: "PROMO"
}

def parse_date(date_str):
    """Parse date from various formats: 05.05.2023, 1.11.2023, 20.08.2024"""
    if not date_str or date_str.strip() in ["", "—", "-"]:
        return None

    date_str = date_str.strip()

    # Try different date formats
    formats = ["%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%d/%m/%y"]

    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).date().isoformat()
        except ValueError:
            continue

    return None

def extract_client_email(url):
    """Extract client email from subscription URL"""
    # https://gorillaerror.com/sub/Client-dbe898dd → Client-dbe898dd
    if not url:
        return None

    match = re.search(r'/sub/([^/\s]+)', url)
    if match:
        return match.group(1)

    return None

def fetch_csv_from_url(url):
    """Download CSV from Google Sheets URL"""
    print(f"📥 Fetching CSV from Google Sheets...")
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    # Force UTF-8 encoding
    response.encoding = 'utf-8'
    print(f"   Downloaded {len(response.text)} bytes\n")
    return response.text

def parse_users_from_csv(csv_content):
    """Parse users from CSV content"""
    users = []
    stats = {
        "total_rows": 0,
        "gorillaerror_rows": 0,
        "spare_keys": 0,
        "valid_users": 0,
        "no_client_email": 0,
        "invalid_telegram_id": 0
    }

    reader = csv.DictReader(StringIO(csv_content))

    for row in reader:
        stats["total_rows"] += 1

        # Filter only gorillaerror rows
        subscription_url = row.get("Ключ", "")
        if "gorillaerror" not in subscription_url.lower():
            continue

        stats["gorillaerror_rows"] += 1

        # Extract data
        client_email = extract_client_email(subscription_url)
        telegram_id_str = row.get("телеграм айди", "").strip()
        payment_status_str = row.get("Оплата", "").strip()
        limit_str = row.get("Лимит устройств", "").strip()
        tag = row.get("Тег", "").strip() or None
        payment_date = parse_date(row.get("дата оплаты", ""))
        renewal_date = parse_date(row.get("дата продления", ""))

        # Skip spare keys (no telegram_id)
        if not telegram_id_str or telegram_id_str in ["", "—", "-"]:
            stats["spare_keys"] += 1
            continue

        if not client_email:
            stats["no_client_email"] += 1
            continue

        try:
            telegram_id = int(telegram_id_str)
        except ValueError:
            stats["invalid_telegram_id"] += 1
            continue

        # Parse payment status
        payment_status = PAYMENT_STATUS_MAP.get(payment_status_str, 1)

        # Force limit_ip to 0 (unlimited) - device limits not enforced yet
        limit_ip = 0

        users.append({
            "telegram_id": telegram_id,
            "client_email": client_email,
            "payment_status": payment_status,
            "payment_status_str": payment_status_str,
            "limit_ip": limit_ip,
            "tag": tag,
            "payment_date": payment_date,
            "renewal_date": renewal_date
        })

        stats["valid_users"] += 1

    return users, stats

def preview_mode(sheets_url):
    """Preview what would be imported (no API calls)"""
    print(f"\n{'='*70}")
    print(f"PREVIEW MODE - Google Sheets Analysis")
    print(f"{'='*70}\n")

    # Fetch CSV
    csv_content = fetch_csv_from_url(sheets_url)

    # Parse users
    users, stats = parse_users_from_csv(csv_content)

    # Analyze data
    payment_status_counts = Counter(u["payment_status_str"] for u in users)

    # Print summary
    print(f"📊 SUMMARY")
    print(f"{'='*70}")
    print(f"Total rows in CSV: {stats['total_rows']}")
    print(f"Rows with gorillaerror: {stats['gorillaerror_rows']}")
    print(f"Spare keys (no telegram_id): {stats['spare_keys']}")
    print(f"Invalid data (skipped): {stats['no_client_email'] + stats['invalid_telegram_id']}")
    print(f"  - No client email: {stats['no_client_email']}")
    print(f"  - Invalid telegram ID: {stats['invalid_telegram_id']}")
    print(f"\n✅ Valid users to import: {stats['valid_users']}")

    print(f"\n📈 Payment Status Breakdown:")
    for status, count in sorted(payment_status_counts.items(), key=lambda x: -x[1]):
        mapped_status = PAYMENT_STATUS_MAP.get(status, 1)
        print(f"   {status:15} → {PAYMENT_STATUS_NAMES[mapped_status]:10} ({count:4} users)")

    print(f"\n📱 Device Limit:")
    print(f"   All users will have UNLIMITED devices (limit_ip=0)")

    # Show sample users
    print(f"\n👥 Sample Users (first 10):")
    print(f"{'='*70}")
    for user in users[:10]:
        print(f"  telegram_id: {user['telegram_id']}")
        print(f"  client: {user['client_email']}")
        print(f"  status: {user['payment_status_str']} → {PAYMENT_STATUS_NAMES[user['payment_status']]}")
        print(f"  limit: {user['limit_ip']}")
        print(f"  renewal: {user['renewal_date']}")
        print()

    if len(users) > 10:
        print(f"  ... and {len(users) - 10} more users")

    print(f"{'='*70}\n")
    print("💡 To sync these users to the API, run:")
    print(f"   python sync_from_sheets.py \"<url>\" --api-url http://localhost:8000\n")

def login_to_api(api_url, admin_password):
    """Login to API and get session cookie"""
    response = requests.post(
        f"{api_url}/login",
        data={"password": admin_password},
        allow_redirects=False
    )
    if response.status_code not in [200, 302]:
        raise Exception(f"Login failed: {response.status_code}")

    session_id = response.cookies.get("session_id")
    if not session_id:
        raise Exception("No session_id cookie received after login")

    return session_id

def get_existing_users(api_url, session_id):
    """Get all existing users from API"""
    print(f"📋 Fetching existing users from API...")
    response = requests.get(
        f"{api_url}/api/users",
        cookies={"session_id": session_id}
    )
    response.raise_for_status()
    data = response.json()
    users = data.get("users", [])  # API returns {"users": [...]}
    print(f"   Found {len(users)} existing users\n")

    # Create lookup by telegram_id
    return {user["telegram_id"]: user for user in users}

def create_user(api_url, session_id, user_data, dry_run=False):
    """Create new user via API"""
    if dry_run:
        return {"status": "dry_run"}

    response = requests.post(
        f"{api_url}/api/users",
        json=user_data,
        cookies={"session_id": session_id}
    )

    if response.status_code in [200, 201]:
        return response.json()
    else:
        raise Exception(f"API error {response.status_code}: {response.text}")

def update_user(api_url, session_id, telegram_id, user_data, dry_run=False):
    """Update existing user via API"""
    if dry_run:
        return {"status": "dry_run"}

    response = requests.put(
        f"{api_url}/api/users/{telegram_id}",
        json=user_data,
        cookies={"session_id": session_id}
    )

    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(f"API error {response.status_code}: {response.text}")

def sync_mode(sheets_url, api_url, admin_password, dry_run=False):
    """Sync users to Central API"""
    print(f"\n{'='*70}")
    print(f"SYNC MODE - Google Sheets → Central API")
    print(f"{'='*70}")
    print(f"Mode: {'DRY RUN (no changes)' if dry_run else 'LIVE (will modify database)'}")
    print(f"API URL: {api_url}")
    print(f"{'='*70}\n")

    # Login to API
    print(f"🔐 Logging in to API...")
    session_id = login_to_api(api_url, admin_password)
    print(f"   ✅ Authenticated\n")

    # Fetch CSV
    csv_content = fetch_csv_from_url(sheets_url)

    # Parse users
    users, parse_stats = parse_users_from_csv(csv_content)

    # Get existing users from API
    existing_users = get_existing_users(api_url, session_id)

    # Sync
    sync_stats = {
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "errors": []
    }

    for user in users:
        telegram_id = user["telegram_id"]
        client_email = user["client_email"]

        if telegram_id in existing_users:
            # Check if update needed
            existing = existing_users[telegram_id]
            changes = []

            if existing["payment_status"] != user["payment_status"]:
                changes.append(f"status:{existing['payment_status']}→{user['payment_status']}")
            # Skip limit_ip check (always 0/unlimited for now)
            if existing.get("renewal_date") != user["renewal_date"]:
                changes.append(f"renewal:{existing.get('renewal_date')}→{user['renewal_date']}")

            if changes:
                print(f"🔄 Updating telegram_id={telegram_id} ({', '.join(changes)})")
                try:
                    update_data = {
                        "payment_status": user["payment_status"],
                        "limit_ip": user["limit_ip"],
                        "tag": user["tag"],
                        "payment_date": user["payment_date"],
                        "renewal_date": user["renewal_date"]
                    }
                    update_user(api_url, session_id, telegram_id, update_data, dry_run)
                    sync_stats["updated"] += 1
                except Exception as e:
                    print(f"   ❌ Error: {e}")
                    sync_stats["errors"].append(f"Update {telegram_id}: {e}")
            else:
                sync_stats["unchanged"] += 1
        else:
            # Create new user
            print(f"✨ Creating telegram_id={telegram_id}, client={client_email}")
            try:
                create_user(api_url, session_id, user, dry_run)
                sync_stats["created"] += 1
            except Exception as e:
                print(f"   ❌ Error: {e}")
                sync_stats["errors"].append(f"Create {telegram_id}: {e}")

    # Print summary
    print(f"\n{'='*70}")
    print("SYNC SUMMARY")
    print(f"{'='*70}")
    print(f"Valid users in sheet: {len(users)}")
    print(f"✨ Created: {sync_stats['created']}")
    print(f"🔄 Updated: {sync_stats['updated']}")
    print(f"⏭️  Unchanged: {sync_stats['unchanged']}")

    if sync_stats["errors"]:
        print(f"\n❌ Errors ({len(sync_stats['errors'])}):")
        for error in sync_stats["errors"][:10]:
            print(f"   - {error}")
        if len(sync_stats["errors"]) > 10:
            print(f"   ... and {len(sync_stats['errors']) - 10} more")

    print(f"{'='*70}\n")

    if dry_run:
        print("✅ DRY RUN COMPLETE - No changes were made\n")
    else:
        print("✅ SYNC COMPLETE\n")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  # Preview mode (just show summary)")
        print("  python sync_from_sheets.py <google_sheets_csv_url>")
        print()
        print("  # Sync mode (create/update users)")
        print("  python sync_from_sheets.py <google_sheets_csv_url> --api-url <url> [--dry-run] [--password <pwd>]")
        print()
        print("Example:")
        print('  python sync_from_sheets.py "https://docs.google.com/.../pub?output=csv&gid=0"')
        print('  python sync_from_sheets.py "https://docs.google.com/.../pub?output=csv&gid=0" --api-url http://localhost:8000 --dry-run')
        sys.exit(1)

    sheets_url = sys.argv[1]

    # Parse optional arguments
    api_url = None
    admin_password = os.getenv("ADMIN_PASSWORD", "admin123")
    dry_run = "--dry-run" in sys.argv

    for i, arg in enumerate(sys.argv):
        if arg == "--api-url" and i + 1 < len(sys.argv):
            api_url = sys.argv[i + 1]
        if arg == "--password" and i + 1 < len(sys.argv):
            admin_password = sys.argv[i + 1]

    try:
        if api_url:
            # Sync mode
            sync_mode(sheets_url, api_url, admin_password, dry_run)
        else:
            # Preview mode
            preview_mode(sheets_url)
    except Exception as e:
        print(f"\n❌ FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
