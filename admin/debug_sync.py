#!/usr/bin/env python3
"""
Debug script for sync_from_sheets.py
Shows exactly what would happen without making changes
"""

import csv
import sys
import os
import requests
import re
from datetime import datetime
from io import StringIO
from collections import Counter

# Import from sync_from_sheets
PAYMENT_STATUS_MAP = {
    "Оплатил": 2,
    "Не оплатил": 3,
    "Тест": 1,
    "Промокод": 4,
    "Отписался": 3,
    "Вернулся": 2,
}

def extract_client_email(url):
    """Extract client email from subscription URL"""
    if not url:
        return None
    match = re.search(r'/sub/([^/\s]+)', url)
    if match:
        return match.group(1)
    return None

def parse_date(date_str):
    """Parse date from various formats"""
    if not date_str or date_str.strip() in ["", "—", "-"]:
        return None

    date_str = date_str.strip()

    if "." in date_str:
        parts = date_str.split(".")
        if len(parts) == 3:
            day, month, year = parts
            day = day.zfill(2)
            month = month.zfill(2)
            if len(year) == 2:
                year = "20" + year
            date_str = f"{day}.{month}.{year}"

    formats = ["%d.%m.%Y", "%d/%m/%Y"]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).date().isoformat()
        except ValueError:
            continue
    return None

def fetch_csv_from_url(url):
    """Download CSV from Google Sheets"""
    print(f"📥 Fetching CSV from Google Sheets...")
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    response.encoding = 'utf-8'
    print(f"   Downloaded {len(response.text)} bytes\n")
    return response.text

def parse_users_from_csv(csv_content):
    """Parse users from CSV"""
    users = []
    reader = csv.DictReader(StringIO(csv_content))

    for row in reader:
        subscription_url = row.get("Ключ", "")
        if "gorillaerror" not in subscription_url.lower():
            continue

        client_email = extract_client_email(subscription_url)
        telegram_id_str = row.get("телеграм айди", "").strip()
        payment_status_str = row.get("Оплата", "").strip()

        if not telegram_id_str or telegram_id_str in ["", "—", "-"]:
            continue

        if not client_email:
            continue

        try:
            telegram_id = int(telegram_id_str)
        except ValueError:
            continue

        payment_status = PAYMENT_STATUS_MAP.get(payment_status_str, 1)

        users.append({
            "telegram_id": telegram_id,
            "client_email": client_email,
            "payment_status": payment_status,
            "payment_status_str": payment_status_str,
        })

    return users

def login_to_api(api_url, admin_password):
    """Login to API"""
    response = requests.post(
        f"{api_url}/login",
        data={"password": admin_password},
        allow_redirects=False
    )
    if response.status_code not in [200, 302]:
        raise Exception(f"Login failed: {response.status_code}")

    session_id = response.cookies.get("session_id")
    if not session_id:
        raise Exception("No session_id cookie received")

    return session_id

def get_existing_users(api_url, session_id):
    """Get all users from API"""
    print(f"📋 Fetching existing users from API...")
    response = requests.get(
        f"{api_url}/api/users",
        cookies={"session_id": session_id}
    )
    response.raise_for_status()
    data = response.json()
    users = data.get("users", [])
    print(f"   Found {len(users)} existing users\n")
    return users

def main():
    if len(sys.argv) < 3:
        print("Usage: python debug_sync.py <sheets_url> <api_url> [--password <pwd>]")
        print("\nExample:")
        print('  python debug_sync.py "https://docs.google.com/.../pub?output=csv" http://localhost:8000')
        sys.exit(1)

    sheets_url = sys.argv[1]
    api_url = sys.argv[2]
    admin_password = os.getenv("ADMIN_PASSWORD", "admin123")

    # Check for --password
    for i, arg in enumerate(sys.argv):
        if arg == "--password" and i + 1 < len(sys.argv):
            admin_password = sys.argv[i + 1]

    print(f"\n{'='*80}")
    print(f"SYNC DEBUG - Analyzing Google Sheets vs API")
    print(f"{'='*80}\n")

    # 1. Fetch from Sheets
    csv_content = fetch_csv_from_url(sheets_url)
    sheet_users = parse_users_from_csv(csv_content)

    print(f"📊 Google Sheets: {len(sheet_users)} valid users\n")

    # 2. Login and fetch from API
    print(f"🔐 Logging in to API...")
    session_id = login_to_api(api_url, admin_password)
    print(f"   ✅ Authenticated\n")

    api_users = get_existing_users(api_url, session_id)

    # 3. Build lookups
    print(f"{'='*80}")
    print(f"TYPE ANALYSIS")
    print(f"{'='*80}\n")

    # Check types from API
    if api_users:
        sample_api = api_users[0]
        print(f"API user sample:")
        print(f"  telegram_id: {sample_api.get('telegram_id')} (type: {type(sample_api.get('telegram_id')).__name__})")
        print(f"  client_email: {sample_api.get('client_email')} (type: {type(sample_api.get('client_email')).__name__})")

    # Check types from Sheets
    if sheet_users:
        sample_sheet = sheet_users[0]
        print(f"\nSheets user sample:")
        print(f"  telegram_id: {sample_sheet['telegram_id']} (type: {type(sample_sheet['telegram_id']).__name__})")
        print(f"  client_email: {sample_sheet['client_email']} (type: {type(sample_sheet['client_email']).__name__})")

    # 4. Create lookups
    api_by_telegram = {u['telegram_id']: u for u in api_users}
    api_by_telegram_int = {int(u['telegram_id']): u for u in api_users}  # Convert to int
    api_by_email = {u['client_email']: u for u in api_users}

    sheet_by_telegram = {u['telegram_id']: u for u in sheet_users}
    sheet_by_email = {u['client_email']: u for u in sheet_users}

    # 5. Find conflicts
    print(f"\n{'='*80}")
    print(f"CONFLICT DETECTION")
    print(f"{'='*80}\n")

    # Check for duplicates in Sheets
    telegram_counts = Counter(u['telegram_id'] for u in sheet_users)
    email_counts = Counter(u['client_email'] for u in sheet_users)

    sheet_duplicates_telegram = {k: v for k, v in telegram_counts.items() if v > 1}
    sheet_duplicates_email = {k: v for k, v in email_counts.items() if v > 1}

    if sheet_duplicates_telegram:
        print(f"⚠️  DUPLICATE telegram_id in Sheets ({len(sheet_duplicates_telegram)}):")
        for tid, count in list(sheet_duplicates_telegram.items())[:5]:
            print(f"   telegram_id={tid} appears {count} times")
        if len(sheet_duplicates_telegram) > 5:
            print(f"   ... and {len(sheet_duplicates_telegram) - 5} more")
        print()

    if sheet_duplicates_email:
        print(f"⚠️  DUPLICATE client_email in Sheets ({len(sheet_duplicates_email)}):")
        for email, count in list(sheet_duplicates_email.items())[:5]:
            print(f"   {email} appears {count} times")
        if len(sheet_duplicates_email) > 5:
            print(f"   ... and {len(sheet_duplicates_email) - 5} more")
        print()

    # 6. Analyze what would happen
    print(f"{'='*80}")
    print(f"SYNC PLAN ANALYSIS")
    print(f"{'='*80}\n")

    would_create = []
    would_update = []
    conflicts = []

    for sheet_user in sheet_users:
        tid = sheet_user['telegram_id']
        email = sheet_user['client_email']

        # Check if exists in API by telegram_id (try both types)
        exists_by_tid = tid in api_by_telegram or tid in api_by_telegram_int
        exists_by_email = email in api_by_email

        if exists_by_tid:
            would_update.append(sheet_user)
        elif exists_by_email:
            # Conflict: same email, different telegram_id
            api_user = api_by_email[email]
            conflicts.append({
                'sheet': sheet_user,
                'api': api_user,
                'reason': f"Email {email} exists with different telegram_id: API={api_user['telegram_id']}, Sheet={tid}"
            })
        else:
            would_create.append(sheet_user)

    print(f"📊 Summary:")
    print(f"   Would UPDATE: {len(would_update)} users (already exist by telegram_id)")
    print(f"   Would CREATE: {len(would_create)} users (new)")
    print(f"   ❌ CONFLICTS: {len(conflicts)} users (same email, different telegram_id)\n")

    # Show conflicts
    if conflicts:
        print(f"{'='*80}")
        print(f"⚠️  CONFLICTS - These will cause API errors!")
        print(f"{'='*80}\n")

        for i, conflict in enumerate(conflicts[:10], 1):
            print(f"{i}. {conflict['reason']}")
            print(f"   Sheet: telegram_id={conflict['sheet']['telegram_id']}, email={conflict['sheet']['client_email']}")
            print(f"   API:   telegram_id={conflict['api']['telegram_id']}, email={conflict['api']['client_email']}")
            print()

        if len(conflicts) > 10:
            print(f"   ... and {len(conflicts) - 10} more conflicts\n")

    # Show samples of what would be created
    if would_create:
        print(f"{'='*80}")
        print(f"WOULD CREATE (first 10 of {len(would_create)})")
        print(f"{'='*80}\n")

        for user in would_create[:10]:
            print(f"  telegram_id={user['telegram_id']}, email={user['client_email']}, status={user['payment_status_str']}")

        if len(would_create) > 10:
            print(f"  ... and {len(would_create) - 10} more\n")

    # Show samples of what would be updated
    if would_update:
        print(f"{'='*80}")
        print(f"WOULD UPDATE (first 10 of {len(would_update)})")
        print(f"{'='*80}\n")

        for sheet_user in would_update[:10]:
            tid = sheet_user['telegram_id']
            api_user = api_by_telegram.get(tid) or api_by_telegram_int.get(tid)

            changes = []
            if api_user['payment_status'] != sheet_user['payment_status']:
                changes.append(f"status: {api_user['payment_status']}→{sheet_user['payment_status']}")

            if changes:
                print(f"  telegram_id={tid}: {', '.join(changes)}")
            else:
                print(f"  telegram_id={tid}: (no changes)")

        if len(would_update) > 10:
            print(f"  ... and {len(would_update) - 10} more\n")

    # Final summary
    print(f"{'='*80}")
    print(f"RECOMMENDATIONS")
    print(f"{'='*80}\n")

    if conflicts:
        print(f"❌ FIX CONFLICTS FIRST!")
        print(f"   You have {len(conflicts)} users with same email but different telegram_id")
        print(f"   These will cause 'duplicate email' errors from the API\n")
        print(f"   Options:")
        print(f"   1. Update telegram_id in Sheets to match API")
        print(f"   2. Delete conflicting users from API first")
        print(f"   3. Use different client_email in Sheets\n")

    if sheet_duplicates_telegram or sheet_duplicates_email:
        print(f"⚠️  FIX DUPLICATES IN SHEETS!")
        print(f"   Remove duplicate rows from Google Sheets\n")

    if not conflicts and not sheet_duplicates_telegram and not sheet_duplicates_email:
        print(f"✅ NO CONFLICTS DETECTED")
        print(f"   Safe to run sync with:")
        print(f'   python sync_from_sheets.py "{sheets_url}" --api-url {api_url} --dry-run\n')

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
