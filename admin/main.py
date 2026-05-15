"""Admin service for centralized 3x-ui management"""
import os
import uuid
import json
import time
import secrets
from typing import List, Optional
from urllib.parse import unquote
from datetime import datetime, timedelta, date
from fastapi import FastAPI, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import cast, String, or_
import requests
import httpx
import asyncio

from database import get_db, Node, Client, Key, User, PaymentStatus, Domain, NodeDomain, engine, Base

# Create tables
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Subscription Manager Admin")
templates = Jinja2Templates(directory="templates")

# Redis session storage for multi-worker support
import redis as redis_lib

redis_url = os.getenv("REDIS_URL", "redis://:redis123@localhost:6379/0")
try:
    redis_client = redis_lib.from_url(redis_url, decode_responses=True)
    redis_client.ping()
    print(f"✅ Connected to Redis: {redis_url}")
except Exception as e:
    print(f"⚠️  Redis connection failed: {e}")
    print("⚠️  Falling back to in-memory sessions (single worker only)")
    redis_client = None

# Fallback in-memory storage if Redis unavailable
sessions = {}

# Stats cache with TTL (cache stats for 30 seconds to avoid hammering nodes)
stats_cache = {}
STATS_CACHE_TTL = 30  # seconds

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")


# ============================================================================
# Authentication
# ============================================================================

def check_auth(request: Request):
    """Check if user is authenticated"""
    session_id = request.cookies.get("session_id")
    if not session_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Check Redis first, fall back to in-memory
    if redis_client:
        try:
            session_exists = redis_client.exists(f"session:{session_id}")
            if not session_exists:
                raise HTTPException(status_code=401, detail="Not authenticated")
        except Exception as e:
            print(f"Redis error in check_auth: {e}")
            if session_id not in sessions:
                raise HTTPException(status_code=401, detail="Not authenticated")
    else:
        if session_id not in sessions:
            raise HTTPException(status_code=401, detail="Not authenticated")

    return True


# ============================================================================
# Cache Management
# ============================================================================

def clear_node_stats_cache(node_id: int):
    """Clear cached stats for a node"""
    cache_key = f"stats_{node_id}"
    if cache_key in stats_cache:
        del stats_cache[cache_key]


# ============================================================================
# 3x-ui API Integration
# ============================================================================

def create_vless_url(node: Node, client_email: str, client_uuid: str, inbound_id: int, transport: str = "grpc", stream_settings: dict = None, domain_override: str = None, domain_label: str = None) -> str:
    """Generate VLESS URL for client (supports both TLS and Reality, multi-domain)

    Args:
        node: Node configuration
        client_email: Client email
        client_uuid: Client UUID
        inbound_id: Inbound ID (unused, kept for compatibility)
        transport: Transport type ('grpc' or 'xhttp')
        stream_settings: Optional stream settings dict from inbound (for Reality support)
        domain_override: Override domain (for multi-domain support)
        domain_label: Label for this domain (e.g., 'cdn', 'backup') - shows in server name

    Returns:
        VLESS URL string
    """
    import urllib.parse

    # Use override domain or fallback to node.domain
    base_domain = domain_override if domain_override else node.domain

    # Determine domain based on upgraded status and transport
    if node.upgraded:
        # Upgraded nodes: use subdomain for HAProxy SNI routing
        if transport == "xhttp":
            domain = f"app.{base_domain}"  # app.domain -> xhttp-HA (20001)
        else:
            domain = f"api.{base_domain}"  # api.domain -> grpc-HA (20000)
    else:
        # Legacy nodes: use main domain with nginx
        domain = base_domain

    # Detect security type from stream settings
    # Default to "tls" for non-Reality transports
    security = "tls"
    if stream_settings:
        detected_security = stream_settings.get("security", "tls")
        # Only override to Reality if explicitly configured
        if detected_security == "reality":
            security = "reality"
        # Otherwise keep as "tls"

    if transport == "xhttp":
        # XHTTP transport: type=xhttp, path=/api
        params = {
            "encryption": "none",
            "security": security,
            "type": "xhttp",
            "path": "/api"
        }
        protocol_label = "XHTTP"
    else:
        # gRPC transport: type=grpc, serviceName from settings or default
        grpc_settings = stream_settings.get("grpcSettings", {}) if stream_settings else {}
        service_name = grpc_settings.get("serviceName", "sync")
        authority = grpc_settings.get("authority", "")

        params = {
            "type": "grpc",
            "encryption": "none",
            "serviceName": service_name,
            "authority": authority,
            "security": security
        }
        protocol_label = "gRPC"

    # Build remark with domain label
    # Format: node-name[-domain-label]-protocol-email
    if domain_label and domain_label != "primary":
        # Non-primary domain: add label to distinguish in subscription
        remark = urllib.parse.quote(f"{node.name}-{domain_label}-{protocol_label}-{client_email}")
    else:
        # Primary domain: keep original format for backwards compatibility
        remark = urllib.parse.quote(f"{node.name}-{protocol_label}-{client_email}")

    # Add Reality-specific parameters if using Reality
    if security == "reality" and stream_settings:
        reality_settings = stream_settings.get("realitySettings", {})

        # Extract Reality parameters
        public_key = reality_settings.get("settings", {}).get("publicKey", "")
        fingerprint = reality_settings.get("settings", {}).get("fingerprint", "chrome")
        spider_x = reality_settings.get("settings", {}).get("spiderX", "/")

        # Server names (SNI)
        server_names = reality_settings.get("serverNames", [])
        sni = server_names[0] if server_names else domain

        # Short IDs
        short_ids = reality_settings.get("shortIds", [])
        short_id = short_ids[0] if short_ids else ""

        # Add Reality params to URL
        params["pbk"] = public_key
        params["fp"] = fingerprint
        params["sni"] = sni
        params["sid"] = short_id
        params["spx"] = spider_x

    # Build query string (order matters for some clients)
    if security == "reality":
        # Reality: specific order for compatibility
        query_parts = []
        for key in ["type", "encryption", "serviceName", "authority", "security", "pbk", "fp", "sni", "sid", "spx"]:
            if key in params and params[key]:
                query_parts.append(f"{key}={urllib.parse.quote(str(params[key]))}")
        query_string = "&".join(query_parts)
    else:
        # TLS: standard encoding
        query_string = urllib.parse.urlencode(params)

    vless_url = f"vless://{client_uuid}@{domain}:443?{query_string}#{remark}"

    return vless_url


def sync_client_to_node(node: Node, client: Client, client_uuid: str, db: Session):
    """Create client on a 3x-ui node"""
    try:
        session = requests.Session()

        # Login to 3x-ui
        login_response = session.post(
            f"{node.url}/login",
            data={"username": node.username, "password": node.password},
            verify=False,
            timeout=10
        )

        if login_response.status_code != 200:
            raise Exception(f"Login failed: {login_response.status_code}")

        # Get inbounds to find first VLESS inbound
        inbounds_response = session.get(
            f"{node.url}/panel/api/inbounds/list",
            verify=False,
            timeout=10
        )

        if inbounds_response.status_code != 200:
            raise Exception(f"Failed to get inbounds: {inbounds_response.status_code}")

        inbounds_data = inbounds_response.json()
        inbounds = inbounds_data.get("obj", [])

        # Find ALL VLESS inbounds
        vless_inbounds = [inbound for inbound in inbounds if inbound.get("protocol") == "vless"]

        if not vless_inbounds:
            raise Exception("No VLESS inbounds found on node")

        # Add client to ALL VLESS inbounds
        first_vless_url = None
        for idx, vless_inbound in enumerate(vless_inbounds):
            inbound_id = vless_inbound["id"]
            inbound_remark = vless_inbound.get("remark", "unknown")

            # Create unique email per inbound using _0, _1, _2 suffix
            client_email = f"{client.email}_{idx}"

            # Parse existing settings to check if client already exists
            settings = json.loads(vless_inbound.get("settings", "{}"))
            clients_list = settings.get("clients", [])

            # Check if client already exists
            existing_client = None
            for c in clients_list:
                if c.get("email") == client_email:
                    existing_client = c
                    break

            if existing_client:
                # Delete existing client first (ignore errors if client doesn't exist)
                try:
                    delete_response = session.post(
                        f"{node.url}/panel/api/inbounds/{inbound_id}/delClientByEmail/{client_email}",
                        verify=False,
                        timeout=10
                    )
                    # Continue even if delete fails (client might be gone already)
                except Exception:
                    pass  # Continue to add client anyway

            # Add client using addClient endpoint (safe, doesn't override inbound)
            client_config = {
                "id": inbound_id,
                "settings": json.dumps({
                    "clients": [
                        {
                            "id": str(client_uuid),
                            "flow": "",
                            "email": client_email,
                            "limitIp": 0,  # 0 = unlimited
                            "totalGB": 0,
                            "expiryTime": 0,
                            "enable": client.enabled,
                            "tgId": "",
                            "subId": client_email
                        }
                    ]
                })
            }

            add_response = session.post(
                f"{node.url}/panel/api/inbounds/addClient",
                json=client_config,
                verify=False,
                timeout=10
            )

            if add_response.status_code != 200:
                print(f"   Warning: Failed to add client to {inbound_remark}: {add_response.status_code}")
                continue

            # Parse stream settings for Reality support
            stream_settings = None
            if vless_inbound.get("streamSettings"):
                try:
                    stream_settings = json.loads(vless_inbound["streamSettings"])
                except:
                    stream_settings = None

            # Determine transport type from inbound remark or streamSettings
            transport = "grpc"  # default
            if stream_settings:
                transport = stream_settings.get("network", "grpc")
            elif "xhttp" in inbound_remark.lower():
                transport = "xhttp"
            elif "tcp" in inbound_remark.lower():
                transport = "tcp"

            # Generate VLESS URL (with Reality support if present in stream_settings)
            vless_url = create_vless_url(node, client.email, str(client_uuid), inbound_id, transport=transport, stream_settings=stream_settings)

            # Save first URL for return value
            if first_vless_url is None:
                first_vless_url = vless_url

            # Save key to database
            existing_key = db.query(Key).filter(
                Key.client_id == client.id,
                Key.node_id == node.id,
                Key.inbound_id == inbound_id
            ).first()

            if existing_key:
                existing_key.uuid = client_uuid
                existing_key.vless_url = vless_url
                existing_key.manual = False  # Mark as auto-generated
            else:
                new_key = Key(
                    client_id=client.id,
                    node_id=node.id,
                    inbound_id=inbound_id,
                    uuid=client_uuid,
                    vless_url=vless_url,
                    manual=False  # Mark as auto-generated
                )
                db.add(new_key)

            db.commit()

        # Clear stats cache for this node
        clear_node_stats_cache(node.id)

        return True, first_vless_url

    except Exception as e:
        return False, str(e)


def delete_client_from_node(node: Node, client: Client, db: Session):
    """Delete client from a 3x-ui node"""
    try:
        session = requests.Session()

        # Login
        login_response = session.post(
            f"{node.url}/login",
            data={"username": node.username, "password": node.password},
            verify=False,
            timeout=10
        )

        if login_response.status_code != 200:
            return False, f"Login failed: {login_response.status_code}"

        # Get inbounds to find VLESS-gRPC-Local
        inbounds_response = session.get(
            f"{node.url}/panel/api/inbounds/list",
            verify=False,
            timeout=10
        )

        if inbounds_response.status_code != 200:
            return False, f"Failed to get inbounds: {inbounds_response.status_code}"

        inbounds_data = inbounds_response.json()
        inbounds = inbounds_data.get("obj", [])

        # Find VLESS-gRPC-Local inbound
        vless_inbound = None
        for inbound in inbounds:
            if inbound.get("remark") == "VLESS-gRPC-Local":
                vless_inbound = inbound
                break

        if vless_inbound:
            inbound_id = vless_inbound["id"]

            # Try to delete client using delClientByEmail endpoint
            # Don't fail if client doesn't exist on server (could be manually deleted)
            try:
                delete_response = session.post(
                    f"{node.url}/panel/api/inbounds/{inbound_id}/delClientByEmail/{client.email}",
                    verify=False,
                    timeout=10
                )

                # Ignore 404-like errors (client already gone from server)
                if delete_response.status_code != 200:
                    result = delete_response.json()
                    # If client not found, continue anyway to clean database
                    if not result.get('success'):
                        pass  # Client might not exist, that's okay
            except Exception as e:
                # Even if delete fails, continue to clean database
                pass

        # Try to delete from XHTTP inbound if it exists
        xhttp_inbound = None
        for inbound in inbounds:
            if inbound.get("remark") == "VLESS-XHTTP":
                xhttp_inbound = inbound
                break

        if xhttp_inbound:
            xhttp_inbound_id = xhttp_inbound["id"]
            xhttp_email = f"{client.email}-xhttp"

            try:
                session.post(
                    f"{node.url}/panel/api/inbounds/{xhttp_inbound_id}/delClientByEmail/{xhttp_email}",
                    verify=False,
                    timeout=10
                )
                # Ignore response - client might not exist in XHTTP inbound
            except Exception:
                # Ignore XHTTP delete errors
                pass

        # Always delete keys from database (even if server delete failed)
        db.query(Key).filter(
            Key.client_id == client.id,
            Key.node_id == node.id
        ).delete()
        db.commit()

        # Clear stats cache for this node
        clear_node_stats_cache(node.id)

        return True, "Deleted successfully"

    except Exception as e:
        return False, str(e)


# ============================================================================
# Async Node Operations (Parallel)
# ============================================================================

async def async_create_keys_on_node(node: Node, client_email: str, client_uuid: str, db: Session, reality_only: bool = False) -> dict:
    """
    Create keys for a client on a single node (async version).

    Args:
        node: Node to create keys on
        client_email: Client email
        client_uuid: Client UUID (shared across all inbounds)
        db: Database session
        reality_only: If True, only create keys on Reality inbounds. If False (default), create on legacy non-Reality inbounds only (gRPC+TLS, XHTTP)

    Returns: dict with node info, success status, keys created, and any errors
    """
    start_time = time.time()
    filter_msg = " (Reality-only)" if reality_only else " (Legacy non-Reality)"
    print(f"  ⏱️  [{node.name}] Starting key creation{filter_msg}...")

    result = {
        "node_id": node.id,
        "node_name": node.name,
        "success": False,
        "keys": [],
        "errors": []
    }

    try:
        async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
            # Login to node
            login_response = await client.post(
                f"{node.url}/login",
                data={"username": node.username, "password": node.password}
            )

            if login_response.status_code != 200:
                result["errors"].append(f"Login failed: {login_response.status_code}")
                return result

            # Get cookies from login
            cookies = login_response.cookies

            # Get inbounds list
            inbounds_response = await client.get(
                f"{node.url}/panel/api/inbounds/list",
                cookies=cookies
            )

            if inbounds_response.status_code != 200:
                result["errors"].append("Failed to get inbounds")
                return result

            inbounds = inbounds_response.json().get("obj", [])

            # Find ALL VLESS inbounds
            vless_inbounds = [inbound for inbound in inbounds if inbound.get("protocol") == "vless"]

            # Filter by inbound type based on reality_only flag
            filtered_inbounds = []
            for inbound in vless_inbounds:
                try:
                    stream_settings = json.loads(inbound.get("streamSettings", "{}"))
                    is_reality = stream_settings.get("security") == "reality"

                    # If reality_only=True, only include Reality inbounds
                    # If reality_only=False, only include non-Reality inbounds (legacy TLS/none)
                    if reality_only and is_reality:
                        filtered_inbounds.append(inbound)
                    elif not reality_only and not is_reality:
                        filtered_inbounds.append(inbound)
                except:
                    # If can't parse stream settings, treat as legacy (non-Reality)
                    if not reality_only:
                        filtered_inbounds.append(inbound)

            vless_inbounds = filtered_inbounds

            if not vless_inbounds:
                error_msg = "No Reality VLESS inbounds found" if reality_only else "No legacy VLESS inbounds found"
                result["errors"].append(error_msg)
                return result

            # Prepare all inbound updates in parallel
            update_tasks = []

            for idx, inbound in enumerate(vless_inbounds):
                # Use the shared UUID for all inbounds (same client, same UUID everywhere)
                key_uuid = client_uuid

                # Create unique email per inbound using _0, _1, _2 suffix
                full_email = f"{client_email}_{idx}"

                # Determine transport type from inbound remark
                inbound_remark = inbound.get("remark", "").lower()
                if "xhttp" in inbound_remark:
                    transport = "xhttp"
                elif "grpc" in inbound_remark:
                    transport = "grpc"
                elif "tcp" in inbound_remark:
                    transport = "tcp"
                else:
                    transport = "grpc"  # default

                # Prepare client data
                settings = json.loads(inbound["settings"])
                clients_list = settings.get("clients", [])

                new_client = {
                    "id": str(key_uuid),
                    "flow": "",  # Empty for both gRPC and XHTTP (no XTLS)
                    "email": full_email,
                    "limitIp": 0,
                    "totalGB": 0,
                    "expiryTime": 0,
                    "enable": True,
                    "tgId": "",
                    "subId": ""
                }

                clients_list.append(new_client)
                settings["clients"] = clients_list

                # Update inbound data
                update_data = {
                    "up": inbound["up"],
                    "down": inbound["down"],
                    "total": inbound["total"],
                    "remark": inbound["remark"],
                    "enable": inbound["enable"],
                    "expiryTime": inbound["expiryTime"],
                    "listen": inbound.get("listen", ""),
                    "port": inbound["port"],
                    "protocol": inbound["protocol"],
                    "settings": json.dumps(settings),
                    "streamSettings": inbound["streamSettings"],
                    "sniffing": inbound["sniffing"]
                }

                # Parse stream settings for Reality support
                stream_settings_dict = None
                try:
                    stream_settings_dict = json.loads(inbound["streamSettings"]) if inbound.get("streamSettings") else None
                except:
                    stream_settings_dict = None

                # Create update task (will execute in parallel)
                update_tasks.append({
                    "task": client.post(
                        f"{node.url}/panel/api/inbounds/update/{inbound['id']}",
                        json=update_data,
                        cookies=cookies
                    ),
                    "uuid": str(key_uuid),
                    "transport": transport.upper(),
                    "email": full_email,
                    "inbound_id": inbound["id"],
                    "stream_settings": stream_settings_dict
                })

            # Execute all inbound updates in parallel
            if update_tasks:
                update_responses = await asyncio.gather(*[t["task"] for t in update_tasks], return_exceptions=True)

                for i, response in enumerate(update_responses):
                    task_info = update_tasks[i]
                    if isinstance(response, Exception):
                        result["errors"].append(f"{task_info['transport']} update exception: {str(response)}")
                    elif response.status_code == 200:
                        result["keys"].append({
                            "uuid": task_info["uuid"],
                            "transport": task_info["transport"],
                            "email": task_info["email"],
                            "inbound_id": task_info["inbound_id"],
                            "stream_settings": task_info.get("stream_settings")
                        })
                    else:
                        result["errors"].append(f"{task_info['transport']} update failed: {response.status_code}")

            result["success"] = len(result["keys"]) > 0

    except Exception as e:
        result["errors"].append(f"Exception: {str(e)}")

    elapsed = time.time() - start_time
    status = "✅" if result["success"] else "❌"
    print(f"  {status} [{node.name}] Completed in {elapsed:.2f}s - {len(result['keys'])} keys created")

    return result


async def async_create_keys_on_all_nodes(nodes: List[Node], client_email: str, db: Session, reality_only: bool = False) -> List[dict]:
    """
    Create keys for a client on all nodes in parallel.
    Generates ONE UUID for the client to use across ALL nodes and inbounds.

    Args:
        nodes: List of nodes to create keys on
        client_email: Client email
        db: Database session
        reality_only: If True, only create keys on Reality inbounds. If False (default), create on legacy non-Reality inbounds only

    Returns: List of results from each node
    """
    start_time = time.time()

    # Generate ONE UUID for this client (shared across all nodes and inbounds)
    client_uuid = str(uuid.uuid4())
    filter_msg = " (Reality-only)" if reality_only else " (Legacy non-Reality)"
    print(f"\n🚀 Creating keys for '{client_email}' (UUID: {client_uuid[:8]}...) on {len(nodes)} nodes IN PARALLEL{filter_msg}...")

    tasks = [async_create_keys_on_node(node, client_email, client_uuid, db, reality_only) for node in nodes]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Convert exceptions to error results
    processed_results = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            processed_results.append({
                "node_id": nodes[i].id,
                "node_name": nodes[i].name,
                "success": False,
                "keys": [],
                "errors": [f"Task exception: {str(result)}"]
            })
        else:
            processed_results.append(result)

    elapsed = time.time() - start_time
    success_count = sum(1 for r in processed_results if r["success"])
    total_keys = sum(len(r["keys"]) for r in processed_results)
    print(f"✨ TOTAL: {success_count}/{len(nodes)} nodes succeeded, {total_keys} keys created in {elapsed:.2f}s\n")

    return processed_results


async def async_delete_client_from_node(node: Node, client: Client, db: Session) -> dict:
    """
    Delete client from a single node (async version).
    Returns: dict with node info, success status, and any errors
    """
    start_time = time.time()
    print(f"  ⏱️  [{node.name}] Starting client deletion...")

    result = {
        "node_id": node.id,
        "node_name": node.name,
        "success": False,
        "message": ""
    }

    try:
        async with httpx.AsyncClient(verify=False, timeout=30.0) as http_client:
            # Login
            login_response = await http_client.post(
                f"{node.url}/login",
                data={"username": node.username, "password": node.password}
            )

            if login_response.status_code != 200:
                result["message"] = f"Login failed: {login_response.status_code}"
                return result

            cookies = login_response.cookies

            # Get inbounds to find gRPC and XHTTP IDs
            inbounds_response = await http_client.get(
                f"{node.url}/panel/api/inbounds/list",
                cookies=cookies
            )

            if inbounds_response.status_code != 200:
                result["message"] = "Failed to get inbounds"
                return result

            inbounds = inbounds_response.json().get("obj", [])

            # Find ALL VLESS inbounds and check for matching clients
            vless_inbounds = [inbound for inbound in inbounds if inbound.get("protocol") == "vless"]

            # Delete from all VLESS inbounds IN PARALLEL
            delete_tasks = []

            for inbound in vless_inbounds:
                # Parse existing clients in this inbound
                settings = json.loads(inbound.get("settings", "{}"))
                clients_list = settings.get("clients", [])

                # Find all clients that match the base email (handles both old and new formats)
                for existing_client in clients_list:
                    existing_email = existing_client.get("email", "")
                    # Match if email equals base email or starts with base email + separator
                    if existing_email == client.email or existing_email.startswith(f"{client.email}_") or existing_email.startswith(f"{client.email}-"):
                        delete_tasks.append(
                            http_client.post(
                                f"{node.url}/panel/api/inbounds/{inbound['id']}/delClientByEmail/{existing_email}",
                                cookies=cookies
                            )
                        )

            # Execute all deletes in parallel
            deleted_count = 0
            if delete_tasks:
                delete_responses = await asyncio.gather(*delete_tasks, return_exceptions=True)
                for response in delete_responses:
                    if not isinstance(response, Exception):
                        deleted_count += 1

            result["success"] = True
            result["message"] = f"Deleted from {deleted_count} inbounds"

    except Exception as e:
        result["message"] = f"Exception: {str(e)}"

    elapsed = time.time() - start_time
    status = "✅" if result["success"] else "❌"
    print(f"  {status} [{node.name}] Completed in {elapsed:.2f}s - {result['message']}")

    return result


async def async_delete_client_from_all_nodes(nodes: List[Node], client: Client, db: Session) -> List[dict]:
    """
    Delete client from all nodes in parallel.
    Returns: List of results from each node
    """
    start_time = time.time()
    print(f"\n🚀 Deleting client '{client.email}' from {len(nodes)} nodes IN PARALLEL...")

    tasks = [async_delete_client_from_node(node, client, db) for node in nodes]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Convert exceptions to error results
    processed_results = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            processed_results.append({
                "node_id": nodes[i].id,
                "node_name": nodes[i].name,
                "success": False,
                "message": f"Task exception: {str(result)}"
            })
        else:
            processed_results.append(result)

    elapsed = time.time() - start_time
    success_count = sum(1 for r in processed_results if r["success"])
    print(f"✨ TOTAL: {success_count}/{len(nodes)} nodes succeeded in {elapsed:.2f}s\n")

    return processed_results


async def async_toggle_client_on_node(node: Node, client_email: str, enabled: bool, db: Session) -> dict:
    """
    Toggle client enable/disable on a single node (async version).
    Returns: dict with node info, success status, and any errors
    """
    start_time = time.time()
    print(f"  ⏱️  [{node.name}] Starting toggle...")

    result = {
        "node_id": node.id,
        "node_name": node.name,
        "success": False,
        "message": ""
    }

    try:
        async with httpx.AsyncClient(verify=False, timeout=30.0) as http_client:
            # Login
            login_response = await http_client.post(
                f"{node.url}/login",
                data={"username": node.username, "password": node.password}
            )

            if login_response.status_code != 200:
                result["message"] = f"Login failed: {login_response.status_code}"
                return result

            cookies = login_response.cookies

            # Get inbounds
            inbounds_response = await http_client.get(
                f"{node.url}/panel/api/inbounds/list",
                cookies=cookies
            )

            if inbounds_response.status_code != 200:
                result["message"] = "Failed to get inbounds"
                return result

            inbounds = inbounds_response.json().get("obj", [])

            # Find inbounds and clients
            grpc_inbound = None
            xhttp_inbound = None

            for inbound in inbounds:
                remark = inbound.get("remark", "").lower()
                if "grpc" in remark:
                    grpc_inbound = inbound
                elif "xhttp" in remark:
                    xhttp_inbound = inbound

            # Toggle both inbounds IN PARALLEL
            toggle_tasks = []

            # Toggle gRPC inbound
            if grpc_inbound:
                settings = json.loads(grpc_inbound.get("settings", "{}"))
                clients = settings.get("clients", [])

                # Find client and update enable field
                client_found = False
                for client in clients:
                    if client.get("email") == client_email:
                        client["enable"] = enabled
                        client_uuid = client.get("id")
                        client_found = True

                        # Prepare update payload
                        update_payload = {
                            "id": grpc_inbound["id"],
                            "settings": json.dumps({"clients": [client]})
                        }

                        toggle_tasks.append(
                            http_client.post(
                                f"{node.url}/panel/api/inbounds/updateClient/{client_uuid}",
                                json=update_payload,
                                cookies=cookies
                            )
                        )
                        break

                if not client_found:
                    result["message"] = f"Client {client_email} not found in gRPC inbound"

            # Toggle XHTTP inbound
            if xhttp_inbound:
                xhttp_email = f"{client_email}-xhttp"
                settings = json.loads(xhttp_inbound.get("settings", "{}"))
                clients = settings.get("clients", [])

                # Find client and update enable field
                for client in clients:
                    if client.get("email") == xhttp_email:
                        client["enable"] = enabled
                        client_uuid = client.get("id")

                        # Prepare update payload
                        update_payload = {
                            "id": xhttp_inbound["id"],
                            "settings": json.dumps({"clients": [client]})
                        }

                        toggle_tasks.append(
                            http_client.post(
                                f"{node.url}/panel/api/inbounds/updateClient/{client_uuid}",
                                json=update_payload,
                                cookies=cookies
                            )
                        )
                        break

            # Execute all toggles in parallel
            toggled_count = 0
            if toggle_tasks:
                toggle_responses = await asyncio.gather(*toggle_tasks, return_exceptions=True)
                for response in toggle_responses:
                    if not isinstance(response, Exception):
                        if response.status_code == 200:
                            response_data = response.json()
                            if response_data.get("success"):
                                toggled_count += 1

            result["success"] = toggled_count > 0
            result["message"] = f"Toggled {toggled_count} inbounds to {'enabled' if enabled else 'disabled'}"

    except Exception as e:
        result["message"] = f"Exception: {str(e)}"

    elapsed = time.time() - start_time
    status = "✅" if result["success"] else "❌"
    print(f"  {status} [{node.name}] Completed in {elapsed:.2f}s - {result['message']}")

    return result


async def async_toggle_client_on_all_nodes(nodes: List[Node], client_email: str, enabled: bool, db: Session) -> List[dict]:
    """
    Toggle client on all nodes in parallel.
    Returns: List of results from each node
    """
    start_time = time.time()
    action = "Enabling" if enabled else "Disabling"
    print(f"\n🚀 {action} client '{client_email}' on {len(nodes)} nodes IN PARALLEL...")

    tasks = [async_toggle_client_on_node(node, client_email, enabled, db) for node in nodes]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Convert exceptions to error results
    processed_results = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            processed_results.append({
                "node_id": nodes[i].id,
                "node_name": nodes[i].name,
                "success": False,
                "message": f"Task exception: {str(result)}"
            })
        else:
            processed_results.append(result)

    elapsed = time.time() - start_time
    success_count = sum(1 for r in processed_results if r["success"])
    print(f"✨ TOTAL: {success_count}/{len(nodes)} nodes succeeded in {elapsed:.2f}s\n")

    return processed_results


async def async_get_node_stats(node: Node) -> dict:
    """
    Get stats from a single node (async version).
    Returns: dict with node stats
    """
    result = {
        "node_id": node.id,
        "node_name": node.name,
        "online": False,
        "total_clients": 0,
        "enabled_clients": 0,
        "online_clients": 0,
        "traffic_up": 0,
        "traffic_down": 0,
        "traffic_total": 0
    }

    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            # Login
            login_response = await client.post(
                f"{node.url}/login",
                data={"username": node.username, "password": node.password}
            )

            if login_response.status_code != 200:
                return result

            cookies = login_response.cookies

            # Get inbounds
            inbounds_response = await client.get(
                f"{node.url}/panel/api/inbounds/list",
                cookies=cookies
            )

            if inbounds_response.status_code != 200:
                return result

            inbounds_data = inbounds_response.json()
            inbounds = inbounds_data.get("obj", [])

            # Sum traffic across ALL inbounds
            total_up = 0
            total_down = 0

            # Find gRPC inbound for client counts
            grpc_inbound = None
            for inbound in inbounds:
                # Sum traffic from all inbounds
                if "up" in inbound:
                    total_up += inbound.get("up", 0)
                if "down" in inbound:
                    total_down += inbound.get("down", 0)

                # Find gRPC inbound for client stats
                remark = inbound.get("remark", "").lower()
                if "grpc" in remark:
                    grpc_inbound = inbound

            result["online"] = True
            result["traffic_up"] = total_up
            result["traffic_down"] = total_down
            result["traffic_total"] = total_up + total_down

            if not grpc_inbound:
                return result

            # Count clients
            settings = json.loads(grpc_inbound.get("settings", "{}"))
            clients = settings.get("clients", [])
            total_clients = len(clients)

            # Get client stats to count truly online clients
            client_stats = grpc_inbound.get("clientStats", [])

            # Count clients that are ACTUALLY online (lastOnline within 2 minutes)
            current_time_ms = time.time() * 1000
            online_threshold_ms = 2 * 60 * 1000  # 2 minutes in milliseconds

            online_client_emails = set()
            for stat in client_stats:
                last_online = stat.get("lastOnline", 0)
                email = stat.get("email")
                if email and last_online and (current_time_ms - last_online) < online_threshold_ms:
                    online_client_emails.add(email)

            # Count enabled vs online
            enabled_clients = sum(1 for c in clients if c.get("enable", True))
            online_clients = len(online_client_emails)

            result["total_clients"] = total_clients
            result["enabled_clients"] = enabled_clients
            result["online_clients"] = online_clients

    except Exception:
        pass

    return result


async def async_get_all_nodes_stats(nodes: List[Node]) -> List[dict]:
    """
    Get stats from all nodes in parallel.
    Returns: List of stats from each node
    """
    tasks = [async_get_node_stats(node) for node in nodes]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Convert exceptions to offline results
    processed_results = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            processed_results.append({
                "node_id": nodes[i].id,
                "node_name": nodes[i].name,
                "online": False,
                "total_clients": 0,
                "enabled_clients": 0,
                "online_clients": 0,
                "traffic_up": 0,
                "traffic_down": 0,
                "traffic_total": 0,
                "error": str(result)
            })
        else:
            processed_results.append(result)

    return processed_results


async def async_backup_node(node: Node, backup_path: str) -> dict:
    """
    Backup a single node's database (async version).
    Returns: dict with node backup info
    """
    result = {
        "node": node.name,
        "success": False,
        "error": None,
        "file_size": 0
    }

    try:
        async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
            # Login
            login_response = await client.post(
                f"{node.url}/login",
                data={"username": node.username, "password": node.password}
            )

            if login_response.status_code != 200:
                result["error"] = "Login failed"
                return result

            cookies = login_response.cookies

            # Get database backup via API
            backup_response = await client.get(
                f"{node.url}/panel/api/server/getDb",
                cookies=cookies
            )

            if backup_response.status_code == 200:
                node_backup_file = f"{backup_path}/{node.name}.db"

                # Write backup file
                with open(node_backup_file, 'wb') as f:
                    f.write(backup_response.content)

                file_size = os.path.getsize(node_backup_file)
                result["success"] = True
                result["file_size"] = file_size
            else:
                result["error"] = f"Backup API returned {backup_response.status_code}"

    except Exception as e:
        result["error"] = str(e)

    return result


async def async_backup_all_nodes(nodes: List[Node], backup_path: str) -> List[dict]:
    """
    Backup all nodes' databases in parallel.
    Returns: List of backup results from each node
    """
    tasks = [async_backup_node(node, backup_path) for node in nodes]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Convert exceptions to error results
    processed_results = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            processed_results.append({
                "node": nodes[i].name,
                "success": False,
                "error": str(result),
                "file_size": 0
            })
        else:
            processed_results.append(result)

    return processed_results


# ============================================================================
# Web UI Routes
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Main admin page"""
    session_id = request.cookies.get("session_id")
    if not session_id:
        return RedirectResponse(url="/login")

    # Check Redis first, fall back to in-memory
    session_valid = False
    if redis_client:
        try:
            session_valid = redis_client.exists(f"session:{session_id}")
        except Exception as e:
            print(f"Redis error in home: {e}")
            session_valid = session_id in sessions
    else:
        session_valid = session_id in sessions

    if not session_valid:
        return RedirectResponse(url="/login")

    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Login page"""
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
async def login(password: str = Form(...)):
    """Handle login"""
    if password == ADMIN_PASSWORD:
        session_id = str(uuid.uuid4())

        # Store session in Redis with 24h expiry, or in-memory as fallback
        if redis_client:
            try:
                redis_client.setex(f"session:{session_id}", 86400, "authenticated")  # 24 hours
            except Exception as e:
                print(f"Redis error in login: {e}")
                sessions[session_id] = {"authenticated": True}
        else:
            sessions[session_id] = {"authenticated": True}

        response = RedirectResponse(url="/", status_code=302)
        response.set_cookie("session_id", session_id)
        return response
    else:
        raise HTTPException(status_code=401, detail="Invalid password")


@app.get("/logout")
async def logout(request: Request):
    """Handle logout"""
    session_id = request.cookies.get("session_id")

    # Delete from Redis or in-memory
    if redis_client and session_id:
        try:
            redis_client.delete(f"session:{session_id}")
        except Exception as e:
            print(f"Redis error in logout: {e}")
            if session_id in sessions:
                del sessions[session_id]
    elif session_id in sessions:
        del sessions[session_id]

    response = RedirectResponse(url="/login")
    response.delete_cookie("session_id")
    return response


# ============================================================================
# API Routes - Nodes
# ============================================================================

@app.get("/api/nodes")
async def get_nodes(request: Request, db: Session = Depends(get_db)):
    """Get all nodes"""
    check_auth(request)
    nodes = db.query(Node).all()
    return [{"id": n.id, "name": n.name, "url": n.url, "domain": n.domain, "enabled": n.enabled, "upgraded": n.upgraded} for n in nodes]


@app.get("/api/nodes/stats/all")
async def get_all_nodes_stats(request: Request, db: Session = Depends(get_db)):
    """Get statistics from ALL nodes in parallel - MUCH faster than calling /api/nodes/{id}/stats for each node"""
    check_auth(request)

    nodes = db.query(Node).filter(Node.enabled == True).all()

    if not nodes:
        return []

    # Fetch stats from all nodes in parallel
    stats = await async_get_all_nodes_stats(nodes)

    return stats


@app.get("/api/nodes/{node_id}/stats")
async def get_node_stats(request: Request, node_id: int, db: Session = Depends(get_db)):
    """Get node statistics (client counts) - cached for 30s"""
    check_auth(request)

    # Check cache first
    cache_key = f"stats_{node_id}"
    if cache_key in stats_cache:
        cached_data, cached_time = stats_cache[cache_key]
        if time.time() - cached_time < STATS_CACHE_TTL:
            return cached_data

    node = db.query(Node).filter(Node.id == node_id).first()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    try:
        session = requests.Session()

        # Login
        login_response = session.post(
            f"{node.url}/login",
            data={"username": node.username, "password": node.password},
            verify=False,
            timeout=10
        )

        if login_response.status_code != 200:
            return {"online": False, "total_clients": 0, "enabled_clients": 0, "online_clients": 0, "traffic_up": 0, "traffic_down": 0, "traffic_total": 0}

        # Get inbounds
        inbounds_response = session.get(
            f"{node.url}/panel/api/inbounds/list",
            verify=False,
            timeout=10
        )

        if inbounds_response.status_code != 200:
            return {"online": False, "total_clients": 0, "enabled_clients": 0, "online_clients": 0, "traffic_up": 0, "traffic_down": 0, "traffic_total": 0}

        inbounds_data = inbounds_response.json()
        inbounds = inbounds_data.get("obj", [])

        # Sum traffic across ALL inbounds
        total_up = 0
        total_down = 0

        # Find VLESS-gRPC-Local inbound for client counts
        vless_inbound = None
        for inbound in inbounds:
            # Sum traffic from all inbounds
            if "up" in inbound:
                total_up += inbound.get("up", 0)
            if "down" in inbound:
                total_down += inbound.get("down", 0)

            # Find VLESS-gRPC-Local for client stats
            if inbound.get("remark") == "VLESS-gRPC-Local":
                vless_inbound = inbound

        if not vless_inbound:
            return {
                "online": True,
                "total_clients": 0,
                "enabled_clients": 0,
                "online_clients": 0,
                "traffic_up": total_up,
                "traffic_down": total_down,
                "traffic_total": total_up + total_down
            }

        # Count clients
        settings = json.loads(vless_inbound.get("settings", "{}"))
        clients = settings.get("clients", [])
        total_clients = len(clients)

        # Get client stats to count truly online clients
        # clientStats shows clients with traffic history
        client_stats = vless_inbound.get("clientStats", [])

        # Count clients that are ACTUALLY online (lastOnline within 2 minutes)
        # lastOnline is in milliseconds
        current_time_ms = time.time() * 1000
        online_threshold_ms = 2 * 60 * 1000  # 2 minutes in milliseconds

        online_client_emails = set()
        for stat in client_stats:
            last_online = stat.get("lastOnline", 0)
            email = stat.get("email")
            if email and last_online and (current_time_ms - last_online) < online_threshold_ms:
                online_client_emails.add(email)

        # Count enabled vs online
        enabled_clients = sum(1 for c in clients if c.get("enable", True))
        online_clients = len(online_client_emails)

        result = {
            "online": True,
            "total_clients": total_clients,
            "enabled_clients": enabled_clients,
            "online_clients": online_clients,
            "traffic_up": total_up,
            "traffic_down": total_down,
            "traffic_total": total_up + total_down
        }

        # Cache the result
        stats_cache[cache_key] = (result, time.time())
        return result

    except Exception:
        result = {"online": False, "total_clients": 0, "enabled_clients": 0, "online_clients": 0, "traffic_up": 0, "traffic_down": 0, "traffic_total": 0}
        # Cache failures too (avoid repeated failed requests)
        stats_cache[cache_key] = (result, time.time())
        return result


@app.get("/api/nodes/{node_id}")
async def get_node(request: Request, node_id: int, db: Session = Depends(get_db)):
    """Get single node details"""
    check_auth(request)

    node = db.query(Node).filter(Node.id == node_id).first()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    return {
        "id": node.id,
        "name": node.name,
        "url": node.url,
        "domain": node.domain,
        "username": node.username,
        "password": node.password,
        "enabled": node.enabled,
        "upgraded": node.upgraded
    }


@app.post("/api/nodes")
async def create_node(
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    domain: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    """Create new node"""
    check_auth(request)

    # Check if exists
    existing = db.query(Node).filter(Node.name == name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Node already exists")

    node = Node(name=name, url=url.rstrip('/'), domain=domain, username=username, password=password)
    db.add(node)
    db.commit()
    db.refresh(node)

    # Create domain and primary mapping in multi-domain tables
    # Get or create domain
    domain_obj = db.query(Domain).filter(Domain.domain == domain).first()
    if not domain_obj:
        domain_obj = Domain(domain=domain, enabled=True)
        db.add(domain_obj)
        db.flush()

    # Create primary node-domain mapping
    node_domain = NodeDomain(
        node_id=node.id,
        domain_id=domain_obj.id,
        is_primary=True,
        enabled=True
    )
    db.add(node_domain)
    db.commit()

    # Sync all existing clients to this new node
    clients = db.query(Client).all()
    synced_count = 0
    failed_count = 0

    for client in clients:
        # Find client's UUID from existing auto-generated keys (skip manual keys)
        existing_key = db.query(Key).filter(
            Key.client_id == client.id,
            Key.manual == False
        ).first()
        if existing_key:
            # Use the same UUID as other nodes
            client_uuid = existing_key.vless_url.split('://')[1].split('@')[0]

            # sync_client_to_node already saves the key to database
            success, vless_url = sync_client_to_node(node, client, client_uuid, db)

            if success:
                synced_count += 1
            else:
                failed_count += 1

    return {
        "id": node.id,
        "name": node.name,
        "url": node.url,
        "domain": node.domain,
        "synced_clients": synced_count,
        "failed_clients": failed_count
    }


@app.put("/api/nodes/{node_id}")
async def update_node(
    request: Request,
    node_id: int,
    name: str = Form(...),
    url: str = Form(...),
    domain: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    upgraded: str = Form("false"),
    db: Session = Depends(get_db)
):
    """Update node"""
    check_auth(request)

    print(f"[UPDATE NODE] ID: {node_id}, upgraded: '{upgraded}' (type: {type(upgraded)})")

    node = db.query(Node).filter(Node.id == node_id).first()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    # Check if name is being changed and conflicts
    name_changed = (node.name != name)
    if name_changed:
        existing = db.query(Node).filter(Node.name == name).first()
        if existing:
            raise HTTPException(status_code=400, detail="Node name already exists")

    old_name = node.name
    old_upgraded = node.upgraded
    old_domain = node.domain

    node.name = name
    node.url = url.rstrip('/')
    node.domain = domain
    node.username = username
    node.password = password
    node.upgraded = upgraded.lower() in ('true', '1', 'yes', 'on')

    print(f"[UPDATE NODE] Changed upgraded: {old_upgraded} -> {node.upgraded}")

    # Handle domain change in multi-domain tables
    if old_domain != domain:
        # Get or create new domain
        new_domain_obj = db.query(Domain).filter(Domain.domain == domain).first()
        if not new_domain_obj:
            new_domain_obj = Domain(domain=domain, enabled=True)
            db.add(new_domain_obj)
            db.flush()

        # Update primary mapping
        primary_mapping = db.query(NodeDomain).filter(
            NodeDomain.node_id == node_id,
            NodeDomain.is_primary == True
        ).first()

        if primary_mapping:
            # Update existing primary mapping to new domain
            primary_mapping.domain_id = new_domain_obj.id
        else:
            # Create new primary mapping if doesn't exist
            new_mapping = NodeDomain(
                node_id=node_id,
                domain_id=new_domain_obj.id,
                is_primary=True,
                enabled=True
            )
            db.add(new_mapping)

    db.commit()
    db.refresh(node)

    # NOTE: We don't regenerate stored vless_url here because:
    # 1. For nodes with many keys (10k+), this blocks the UI for too long
    # 2. The subscription service regenerates URLs on-the-fly using current node.domain
    # 3. Stored vless_url is only used as template for multi-domain generation
    # 4. URLs will be regenerated when keys are synced or clients are recreated

    return {"id": node.id, "name": node.name, "url": node.url, "domain": node.domain}


@app.post("/api/nodes/{node_id}/test")
async def test_node(request: Request, node_id: int, db: Session = Depends(get_db)):
    """Test node connection and credentials"""
    check_auth(request)

    node = db.query(Node).filter(Node.id == node_id).first()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    try:
        session = requests.Session()

        # Test login
        login_response = session.post(
            f"{node.url}/login",
            data={"username": node.username, "password": node.password},
            verify=False,
            timeout=10
        )

        if login_response.status_code != 200:
            return {
                "success": False,
                "message": f"Login failed (HTTP {login_response.status_code})"
            }

        # Get inbounds
        inbounds_response = session.get(
            f"{node.url}/panel/api/inbounds/list",
            verify=False,
            timeout=10
        )

        if inbounds_response.status_code != 200:
            return {
                "success": False,
                "message": f"Failed to get inbounds (HTTP {inbounds_response.status_code})"
            }

        inbounds_data = inbounds_response.json()
        inbounds = inbounds_data.get("obj", [])

        # Find VLESS-gRPC-Local inbound
        vless_inbound = None
        for inbound in inbounds:
            if inbound.get("remark") == "VLESS-gRPC-Local":
                vless_inbound = inbound
                break

        if not vless_inbound:
            return {
                "success": False,
                "message": "VLESS-gRPC-Local inbound not found"
            }

        # Count clients
        settings = json.loads(vless_inbound.get("settings", "{}"))
        clients_count = len(settings.get("clients", []))

        return {
            "success": True,
            "message": f"Connection successful! Found {clients_count} clients"
        }

    except requests.exceptions.Timeout:
        return {
            "success": False,
            "message": "Connection timeout"
        }
    except requests.exceptions.ConnectionError:
        return {
            "success": False,
            "message": "Connection refused"
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Error: {str(e)}"
        }


@app.delete("/api/nodes/{node_id}")
async def delete_node(request: Request, node_id: int, db: Session = Depends(get_db)):
    """Delete node, remove all clients from it, and clean up database"""
    check_auth(request)

    node = db.query(Node).filter(Node.id == node_id).first()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    # Get all clients that have keys on this node
    keys = db.query(Key).filter(Key.node_id == node_id).all()
    clients_on_node = {}  # {client_id: client}
    for key in keys:
        if key.client_id not in clients_on_node:
            client = db.query(Client).filter(Client.id == key.client_id).first()
            if client:
                clients_on_node[key.client_id] = client

    # Check if node is online (quick 2-second timeout)
    node_online = False
    try:
        async with httpx.AsyncClient(verify=False, timeout=2.0) as client:
            response = await client.get(f"{node.url}/login")
            node_online = response.status_code in [200, 302, 405]
    except Exception:
        node_online = False

    # Delete all clients from the actual 3x-ui node (only if online)
    clients_deleted_on_node = 0
    if node_online:
        print(f"Node {node.name} is online, deleting {len(clients_on_node)} clients from node...")
        for client in clients_on_node.values():
            try:
                success, msg = delete_client_from_node(node, client, db)
                if success:
                    clients_deleted_on_node += 1
            except Exception:
                # Continue even if deletion fails
                pass
    else:
        print(f"Node {node.name} is offline, skipping API calls and just cleaning database...")

    # Delete all keys associated with this node from database
    keys_deleted = db.query(Key).filter(Key.node_id == node_id).delete()

    # Clear stats cache for this node
    clear_node_stats_cache(node_id)

    # Delete the node itself
    db.delete(node)
    db.commit()

    return {
        "message": "Node deleted",
        "keys_removed": keys_deleted,
        "clients_deleted_on_node": clients_deleted_on_node
    }


# ============================================================================
# API Routes - Domains (Multi-Domain Support)
# ============================================================================

@app.get("/api/domains")
async def get_domains(request: Request, db: Session = Depends(get_db)):
    """Get all domains"""
    check_auth(request)

    domains = db.query(Domain).order_by(Domain.domain).all()
    return [{"id": d.id, "domain": d.domain, "enabled": d.enabled} for d in domains]


@app.post("/api/domains")
async def create_domain(request: Request, db: Session = Depends(get_db)):
    """Create new domain"""
    check_auth(request)

    form = await request.form()
    domain_name = form.get("domain")

    if not domain_name:
        raise HTTPException(status_code=400, detail="Domain name required")

    # Check if domain already exists
    existing = db.query(Domain).filter(Domain.domain == domain_name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Domain already exists")

    domain = Domain(domain=domain_name, enabled=True)
    db.add(domain)
    db.commit()
    db.refresh(domain)

    return {"id": domain.id, "domain": domain.domain, "enabled": domain.enabled}


@app.get("/api/nodes/{node_id}/domains")
async def get_node_domains(request: Request, node_id: int, db: Session = Depends(get_db)):
    """Get all domains configured for a node"""
    check_auth(request)

    # Check node exists
    node = db.query(Node).filter(Node.id == node_id).first()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    # Get node-domain mappings
    mappings = db.query(NodeDomain, Domain).join(
        Domain, NodeDomain.domain_id == Domain.id
    ).filter(
        NodeDomain.node_id == node_id
    ).order_by(NodeDomain.is_primary.desc(), Domain.domain).all()

    result = []
    for nd, domain in mappings:
        result.append({
            "id": nd.id,
            "domain_id": domain.id,
            "domain": domain.domain,
            "is_primary": nd.is_primary,
            "enabled": nd.enabled,
            "display_name": nd.display_name
        })

    return result


@app.post("/api/nodes/{node_id}/domains")
async def add_domain_to_node(request: Request, node_id: int, db: Session = Depends(get_db)):
    """Add domain to node"""
    check_auth(request)

    # Check node exists
    node = db.query(Node).filter(Node.id == node_id).first()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    form = await request.form()
    domain_id = int(form.get("domain_id"))
    display_name = form.get("display_name", "").strip() or None
    is_primary = form.get("is_primary") == "true"

    # Check domain exists
    domain = db.query(Domain).filter(Domain.id == domain_id).first()
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")

    # Check if mapping already exists
    existing = db.query(NodeDomain).filter(
        NodeDomain.node_id == node_id,
        NodeDomain.domain_id == domain_id
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Domain already added to this node")

    # Create mapping
    node_domain = NodeDomain(
        node_id=node_id,
        domain_id=domain_id,
        is_primary=is_primary,
        enabled=True,
        display_name=display_name
    )
    db.add(node_domain)
    db.commit()
    db.refresh(node_domain)

    return {
        "id": node_domain.id,
        "domain_id": domain.id,
        "domain": domain.domain,
        "is_primary": node_domain.is_primary,
        "enabled": node_domain.enabled,
        "display_name": node_domain.display_name
    }


@app.put("/api/nodes/{node_id}/domains/{mapping_id}")
async def update_node_domain(request: Request, node_id: int, mapping_id: int, db: Session = Depends(get_db)):
    """Update node-domain mapping"""
    check_auth(request)

    mapping = db.query(NodeDomain).filter(
        NodeDomain.id == mapping_id,
        NodeDomain.node_id == node_id
    ).first()
    if not mapping:
        raise HTTPException(status_code=404, detail="Mapping not found")

    form = await request.form()

    if "display_name" in form:
        mapping.display_name = form.get("display_name").strip() or None

    if "enabled" in form:
        mapping.enabled = form.get("enabled") == "true"

    db.commit()
    db.refresh(mapping)

    return {"success": True}


@app.delete("/api/nodes/{node_id}/domains/{mapping_id}")
async def remove_domain_from_node(request: Request, node_id: int, mapping_id: int, db: Session = Depends(get_db)):
    """Remove domain from node"""
    check_auth(request)

    mapping = db.query(NodeDomain).filter(
        NodeDomain.id == mapping_id,
        NodeDomain.node_id == node_id
    ).first()
    if not mapping:
        raise HTTPException(status_code=404, detail="Mapping not found")

    # Don't allow deleting primary domain
    if mapping.is_primary:
        raise HTTPException(status_code=400, detail="Cannot delete primary domain mapping")

    db.delete(mapping)
    db.commit()

    return {"success": True}


# ============================================================================
# API Routes - Clients
# ============================================================================

@app.get("/api/clients")
async def get_clients(request: Request, page: int = 1, limit: int = 50, search: str = None, db: Session = Depends(get_db)):
    """Get clients with pagination and search"""
    check_auth(request)

    # Build base query
    query = db.query(Client)

    # Apply search filter if provided
    if search:
        search_term = f"%{search}%"
        query = query.filter(Client.email.like(search_term))

    # Get total count
    total = query.count()

    # Calculate offset
    offset = (page - 1) * limit

    # Get paginated clients, ordered by created_at DESC (newest first)
    clients = query.order_by(Client.created_at.desc()).offset(offset).limit(limit).all()

    result = []
    for c in clients:
        keys_count = db.query(Key).filter(Key.client_id == c.id).count()
        result.append({
            "id": c.id,
            "email": c.email,
            "enabled": c.enabled,
            "keys_count": keys_count,
            "created_at": c.created_at.isoformat()
        })

    return {
        "clients": result,
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": (total + limit - 1) // limit
    }


@app.post("/api/clients")
async def create_client(
    request: Request,
    email: str = Form(...),
    manual_keys: str = Form(default=""),
    reality_only: bool = Form(default=False),
    db: Session = Depends(get_db)
):
    """Create new client and sync to all nodes

    Args:
        email: Client email
        manual_keys: Optional manual VLESS URLs (newline separated)
        reality_only: If True, create keys only on Reality inbounds. If False (default), create on legacy non-Reality inbounds
    """
    check_auth(request)

    # Check if exists
    existing = db.query(Client).filter(Client.email == email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Client already exists")

    # Create client
    client = Client(email=email, enabled=True)
    db.add(client)
    db.commit()
    db.refresh(client)

    # Generate UUID for this client
    client_uuid = uuid.uuid4()

    # Sync to all enabled nodes IN PARALLEL (auto-generated keys)
    nodes = db.query(Node).filter(Node.enabled == True).all()

    if nodes:
        # Use async parallel key creation with reality_only flag
        node_results = await async_create_keys_on_all_nodes(nodes, email, db, reality_only=reality_only)

        # Save keys to database
        for result in node_results:
            if result["success"]:
                for key_info in result["keys"]:
                    key = Key(
                        client_id=client.id,
                        node_id=result["node_id"],
                        inbound_id=key_info["inbound_id"],
                        uuid=key_info["uuid"],
                        vless_url=create_vless_url(
                            db.query(Node).get(result["node_id"]),
                            email,
                            key_info["uuid"],
                            key_info["inbound_id"],
                            key_info["transport"].lower(),
                            stream_settings=key_info.get("stream_settings")
                        ),
                        manual=False,
                        created_at=datetime.utcnow()
                    )
                    db.add(key)

        db.commit()

        # Format results for response
        results = [{
            "node": r["node_name"],
            "success": r["success"],
            "message": f"Created {len(r['keys'])} keys" if r["success"] else ", ".join(r["errors"])
        } for r in node_results]
    else:
        results = []

    # Process manual keys if provided
    manual_results = []
    if manual_keys and manual_keys.strip():
        lines = [line.strip() for line in manual_keys.strip().split('\n') if line.strip()]
        for line in lines:
            # Validate VLESS URL format
            if not line.startswith('vless://'):
                manual_results.append({
                    "key": line[:50] + "...",
                    "success": False,
                    "message": "Invalid VLESS URL format"
                })
                continue

            # Extract node name from URL if possible (after @ symbol and before :)
            try:
                # Parse node name from VLESS URL (e.g., vless://uuid@domain:port)
                node_name = "Manual"
                if '@' in line:
                    parts = line.split('@')[1].split(':')[0].split('?')[0]
                    node_name = parts
            except:
                node_name = "Manual"

            # Create manual key entry with dummy node_id (0 for manual keys)
            key = Key(
                client_id=client.id,
                node_id=0,  # 0 indicates manual key (no associated node)
                inbound_id=0,
                uuid=client_uuid,
                vless_url=line,
                manual=True
            )
            db.add(key)
            manual_results.append({
                "key": node_name,
                "success": True,
                "message": "Manual key added"
            })

        db.commit()

    return {
        "id": client.id,
        "email": client.email,
        "sync_results": results,
        "manual_keys_added": len(manual_results),
        "manual_results": manual_results
    }


@app.post("/api/clients/batch")
async def batch_create_clients(
    request: Request,
    seed: str = Form(...),
    count: int = Form(...),
    reality_only: bool = Form(default=False),
    db: Session = Depends(get_db)
):
    """Batch create clients with pattern: seed-{random_hex}

    Args:
        seed: Base name for clients (e.g., "client")
        count: Number of clients to create (1-100)
        reality_only: If True, create keys only on Reality inbounds. If False (default), create on legacy non-Reality inbounds
    """
    check_auth(request)

    if count < 1 or count > 100:
        raise HTTPException(status_code=400, detail="Count must be between 1 and 100")

    created_count = 0
    failed_count = 0
    total_synced = 0
    subscription_urls = []

    # Get all enabled nodes
    nodes = db.query(Node).filter(Node.enabled == True).all()
    subscription_base_url = os.getenv("SUBSCRIPTION_URL", "http://localhost:8001")

    for i in range(count):
        # Generate random hex suffix to prevent account enumeration
        # Example: client-a3f9b2e1, client-7d2c8f4a
        random_suffix = secrets.token_hex(4)  # 8 hex characters
        email = f"{seed}-{random_suffix}"

        # Skip if already exists (very unlikely with random hex)
        existing = db.query(Client).filter(Client.email == email).first()
        if existing:
            failed_count += 1
            continue

        # Create client
        client = Client(email=email, enabled=True)
        db.add(client)
        db.commit()
        db.refresh(client)

        # Sync to all enabled nodes IN PARALLEL with reality_only flag
        if nodes:
            node_results = await async_create_keys_on_all_nodes(nodes, email, db, reality_only=reality_only)

            # Save keys to database
            for result in node_results:
                if result["success"]:
                    for key_info in result["keys"]:
                        key = Key(
                            client_id=client.id,
                            node_id=result["node_id"],
                            inbound_id=key_info["inbound_id"],
                            uuid=key_info["uuid"],
                            vless_url=create_vless_url(
                                db.query(Node).get(result["node_id"]),
                                email,
                                key_info["uuid"],
                                key_info["inbound_id"],
                                key_info["transport"].lower(),
                                stream_settings=key_info.get("stream_settings")
                            ),
                            manual=False,
                            created_at=datetime.utcnow()
                        )
                        db.add(key)
                        total_synced += 1

            db.commit()

        # Add subscription URL
        subscription_url = f"{subscription_base_url}/{email}"
        subscription_urls.append({
            "email": email,
            "url": subscription_url
        })

        created_count += 1

    return {
        "created": created_count,
        "failed": failed_count,
        "total_synced": total_synced,
        "subscriptions": subscription_urls
    }


@app.put("/api/clients/{client_id}/enable")
async def enable_client(request: Request, client_id: int, db: Session = Depends(get_db)):
    """Enable client on all nodes"""
    check_auth(request)

    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    client.enabled = True
    db.commit()

    # Toggle on all nodes IN PARALLEL
    nodes = db.query(Node).filter(Node.enabled == True).all()

    if nodes:
        results = await async_toggle_client_on_all_nodes(nodes, client.email, True, db)
        formatted_results = [{
            "node": r["node_name"],
            "success": r["success"],
            "message": r["message"]
        } for r in results]
    else:
        formatted_results = []

    return {"message": "Client enabled", "sync_results": formatted_results}


@app.put("/api/clients/{client_id}/disable")
async def disable_client(request: Request, client_id: int, db: Session = Depends(get_db)):
    """Disable client on all nodes"""
    check_auth(request)

    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    client.enabled = False
    db.commit()

    # Toggle on all nodes IN PARALLEL
    nodes = db.query(Node).filter(Node.enabled == True).all()

    if nodes:
        results = await async_toggle_client_on_all_nodes(nodes, client.email, False, db)
        formatted_results = [{
            "node": r["node_name"],
            "success": r["success"],
            "message": r["message"]
        } for r in results]
    else:
        formatted_results = []

    return {"message": "Client disabled", "sync_results": formatted_results}


@app.get("/api/clients/{client_id}/limit")
async def get_client_limit(request: Request, client_id: int, db: Session = Depends(get_db)):
    """Get IP limit for client from all nodes"""
    check_auth(request)

    try:
        # Get client
        client = db.query(Client).filter(Client.id == client_id).first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found")

        # Get all keys for this client
        keys = db.query(Key).filter(Key.client_id == client_id).all()
        if not keys:
            return {"client_id": client_id, "email": client.email, "limit_ip": 0, "nodes": {}, "message": "No keys found"}

        # Collect limitIp from each node/inbound
        limit_values = {}  # {node_name: limit_ip}

        for key in keys:
            node = db.query(Node).filter(Node.id == key.node_id).first()
            if not node:
                continue

            session = requests.Session()
            try:
                # Login to node
                login_response = session.post(
                    f"{node.url}/login",
                    data={"username": node.username, "password": node.password},
                    verify=False,
                    timeout=10
                )

                if login_response.status_code != 200:
                    print(f"[IP Limit Check] Login failed for node {node.name}")
                    continue

                # Get all inbounds
                get_response = session.get(
                    f"{node.url}/panel/api/inbounds/list",
                    verify=False,
                    timeout=30
                )

                if get_response.status_code != 200:
                    print(f"[IP Limit Check] Failed to get inbounds on node {node.name}")
                    continue

                inbounds_data = get_response.json()
                if not inbounds_data.get("success"):
                    continue

                # Find the specific inbound
                inbound = None
                for ib in inbounds_data.get("obj", []):
                    if ib.get("id") == key.inbound_id:
                        inbound = ib
                        break

                if not inbound:
                    continue

                settings = json.loads(inbound["settings"])
                clients_list = settings.get("clients", [])

                # Determine email based on inbound type
                inbound_remark = inbound.get("remark", "").lower()
                if "xhttp" in inbound_remark:
                    search_email = f"{client.email}-xhttp"
                    transport = "XHTTP"
                else:
                    search_email = client.email
                    transport = "gRPC"

                # Find client and get limitIp
                for client_obj in clients_list:
                    if client_obj.get("email") == search_email:
                        limit_ip = client_obj.get("limitIp", 0)
                        node_key = f"{node.name} ({transport})"
                        limit_values[node_key] = limit_ip
                        break

            except requests.exceptions.RequestException as e:
                print(f"[IP Limit Check] Connection error for node {node.name}: {e}")
            finally:
                session.close()

        # Check for mismatches
        unique_limits = set(limit_values.values())
        has_mismatch = len(unique_limits) > 1

        if has_mismatch:
            print(f"⚠️  WARNING: IP limit MISMATCH for client {client.email}:")
            for node_name, limit in limit_values.items():
                print(f"   - {node_name}: {limit}")

        # Determine consensus value (most common)
        if limit_values:
            from collections import Counter
            limit_counter = Counter(limit_values.values())
            consensus_limit = limit_counter.most_common(1)[0][0]
        else:
            consensus_limit = 0

        return {
            "client_id": client_id,
            "email": client.email,
            "limit_ip": consensus_limit,
            "nodes": limit_values,
            "mismatch": has_mismatch
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error getting IP limit for client {client_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")


@app.put("/api/clients/{client_id}/limit")
async def update_client_limit(request: Request, client_id: int, db: Session = Depends(get_db)):
    """Update IP limit for client on all nodes"""
    check_auth(request)

    # Get request body
    body = await request.json()
    limit_ip = body.get("limit_ip")

    if limit_ip is None:
        raise HTTPException(status_code=400, detail="limit_ip is required")

    if not isinstance(limit_ip, int) or limit_ip < 0:
        raise HTTPException(status_code=400, detail="limit_ip must be a non-negative integer")

    # Get client
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    # Get all keys for this client
    keys = db.query(Key).filter(Key.client_id == client_id).all()
    if not keys:
        return {"message": "No keys found for client", "updated_nodes": []}

    # Group keys by (node_id, inbound_id)
    node_inbound_map = {}
    for key in keys:
        key_tuple = (key.node_id, key.inbound_id)
        if key_tuple not in node_inbound_map:
            node_inbound_map[key_tuple] = []
        node_inbound_map[key_tuple].append(key)

    # Update limitIp on each node/inbound
    results = []
    requests.packages.urllib3.disable_warnings()

    for (node_id, inbound_id), keys_group in node_inbound_map.items():
        node = db.query(Node).filter(Node.id == node_id).first()
        if not node:
            results.append({
                "node_id": node_id,
                "inbound_id": inbound_id,
                "success": False,
                "message": "Node not found"
            })
            continue

        session = requests.Session()

        try:
            # Login to node
            login_response = session.post(
                f"{node.url}/login",
                data={"username": node.username, "password": node.password},
                verify=False,
                timeout=10
            )

            if login_response.status_code != 200:
                results.append({
                    "node": node.name,
                    "inbound_id": inbound_id,
                    "success": False,
                    "message": f"Login failed: {login_response.status_code}"
                })
                continue

            # Get all inbounds and find the one we need
            get_response = session.get(
                f"{node.url}/panel/api/inbounds/list",
                verify=False,
                timeout=30
            )

            if get_response.status_code != 200:
                results.append({
                    "node": node.name,
                    "inbound_id": inbound_id,
                    "success": False,
                    "message": f"Failed to get inbounds list: {get_response.status_code}"
                })
                continue

            inbounds_data = get_response.json()
            if not inbounds_data.get("success"):
                results.append({
                    "node": node.name,
                    "inbound_id": inbound_id,
                    "success": False,
                    "message": "API returned success=false for inbounds list"
                })
                continue

            # Find the specific inbound by ID
            inbound = None
            for ib in inbounds_data.get("obj", []):
                if ib.get("id") == inbound_id:
                    inbound = ib
                    break

            if not inbound:
                results.append({
                    "node": node.name,
                    "inbound_id": inbound_id,
                    "success": False,
                    "message": f"Inbound {inbound_id} not found in list"
                })
                continue
            settings = json.loads(inbound["settings"])
            clients_list = settings.get("clients", [])

            # Determine email based on inbound type
            # gRPC uses client.email, XHTTP uses client.email-xhttp
            inbound_remark = inbound.get("remark", "").lower()
            if "xhttp" in inbound_remark:
                search_email = f"{client.email}-xhttp"
            else:
                search_email = client.email

            # Update limitIp for matching client
            updated_count = 0
            for client_obj in clients_list:
                if client_obj.get("email") == search_email:
                    client_obj["limitIp"] = limit_ip
                    updated_count += 1

            # Update settings
            settings["clients"] = clients_list
            inbound["settings"] = json.dumps(settings)

            # Send update back to node
            update_response = session.post(
                f"{node.url}/panel/api/inbounds/update/{inbound_id}",
                json=inbound,
                verify=False,
                timeout=30
            )

            if update_response.status_code != 200:
                results.append({
                    "node": node.name,
                    "inbound_id": inbound_id,
                    "success": False,
                    "message": f"Failed to update inbound: {update_response.status_code}"
                })
                continue

            update_data = update_response.json()
            if not update_data.get("success"):
                results.append({
                    "node": node.name,
                    "inbound_id": inbound_id,
                    "success": False,
                    "message": "Update API returned success=false"
                })
                continue

            results.append({
                "node": node.name,
                "inbound_id": inbound_id,
                "success": True,
                "message": f"Updated {updated_count} clients"
            })

        except Exception as e:
            results.append({
                "node": node.name if node else str(node_id),
                "inbound_id": inbound_id,
                "success": False,
                "message": str(e)
            })
        finally:
            session.close()

    success_count = sum(1 for r in results if r.get("success"))
    total_count = len(results)

    return {
        "message": f"Updated IP limit to {limit_ip} on {success_count}/{total_count} node/inbound combinations",
        "limit_ip": limit_ip,
        "results": results
    }


@app.delete("/api/clients/{client_id}")
async def delete_client(request: Request, client_id: int, db: Session = Depends(get_db)):
    """Delete client from all nodes"""
    check_auth(request)

    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    # Delete from all nodes IN PARALLEL
    nodes = db.query(Node).filter(Node.enabled == True).all()

    if nodes:
        results = await async_delete_client_from_all_nodes(nodes, client, db)
        formatted_results = [{
            "node": r["node_name"],
            "success": r["success"],
            "message": r["message"]
        } for r in results]
    else:
        formatted_results = []

    # Delete from database
    db.delete(client)
    db.commit()

    return {"message": "Client deleted", "sync_results": formatted_results}


@app.post("/api/clients/batch-delete")
async def batch_delete_clients(
    request: Request,
    db: Session = Depends(get_db)
):
    """Batch delete multiple clients from all nodes"""
    check_auth(request)

    # Get JSON body
    body = await request.json()
    client_ids = body.get("client_ids", [])

    if not client_ids:
        raise HTTPException(status_code=400, detail="No client IDs provided")

    deleted_count = 0
    keys_removed = 0

    # Get all enabled nodes ONCE
    nodes = db.query(Node).filter(Node.enabled == True).all()

    for client_id in client_ids:
        client = db.query(Client).filter(Client.id == client_id).first()
        if not client:
            continue

        # Count keys before deletion
        keys_count = db.query(Key).filter(Key.client_id == client_id).count()
        keys_removed += keys_count

        # Delete from all nodes IN PARALLEL
        if nodes:
            try:
                await async_delete_client_from_all_nodes(nodes, client, db)
            except Exception:
                # Continue even if node deletion fails
                pass

        # Delete from database
        db.delete(client)
        deleted_count += 1

    db.commit()

    return {
        "deleted": deleted_count,
        "keys_removed": keys_removed
    }


@app.get("/api/clients/{client_id}/subscription")
async def get_client_subscription_link(request: Request, client_id: int, db: Session = Depends(get_db)):
    """Get subscription link for client"""
    check_auth(request)

    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    # Get subscription service URL from env
    # SUBSCRIPTION_URL should include /sub path if nginx proxies it
    # Example: SUBSCRIPTION_URL=https://sub.example.com/sub
    sub_url = os.getenv("SUBSCRIPTION_URL", "http://localhost:8001")

    return {
        "subscription_url": f"{sub_url}/{client.email}"
    }


@app.get("/api/clients/{client_id}/keys")
async def get_client_keys(request: Request, client_id: int, db: Session = Depends(get_db)):
    """Get all VLESS keys for client"""
    check_auth(request)

    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    # Get all keys for this client
    keys = db.query(Key).filter(Key.client_id == client_id).all()

    # Group keys by transport type (extract from vless_url)
    # We only want to show one key per transport type to the client
    keys_by_transport = {}

    for key in keys:
        node = db.query(Node).filter(Node.id == key.node_id).first()

        # Determine transport type from VLESS URL
        transport = "grpc"  # default
        if key.vless_url:
            if "type=xhttp" in key.vless_url or "type=splithttp" in key.vless_url:
                transport = "xhttp"
            elif "type=tcp" in key.vless_url:
                transport = "tcp"
            elif "type=grpc" in key.vless_url:
                transport = "grpc"

        # Only keep first key of each transport type
        if transport not in keys_by_transport:
            keys_by_transport[transport] = {
                "key_id": key.id,
                "node_name": node.name if node else "Manual",
                "vless_url": key.vless_url,
                "manual": key.manual,
                "transport": transport
            }

    # Convert to list
    keys_details = list(keys_by_transport.values())

    return {
        "email": client.email,
        "keys": keys_details
    }


@app.post("/api/clients/{client_id}/keys")
async def add_manual_keys(
    request: Request,
    client_id: int,
    manual_keys: str = Form(default=""),
    db: Session = Depends(get_db)
):
    """Add manual keys to an existing client"""
    check_auth(request)

    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    # Get client's UUID from existing keys
    existing_key = db.query(Key).filter(
        Key.client_id == client.id,
        Key.manual == False
    ).first()

    # Use existing UUID or generate new one
    if existing_key:
        client_uuid = existing_key.uuid
    else:
        client_uuid = uuid.uuid4()

    # Process manual keys
    manual_results = []
    added_count = 0

    if manual_keys and manual_keys.strip():
        lines = [line.strip() for line in manual_keys.strip().split('\n') if line.strip()]
        for line in lines:
            # Validate VLESS URL format
            if not line.startswith('vless://'):
                manual_results.append({
                    "key": line[:50] + "...",
                    "success": False,
                    "message": "Invalid VLESS URL format"
                })
                continue

            # Extract node name from URL if possible
            try:
                node_name = "Manual"
                if '@' in line:
                    parts = line.split('@')[1].split(':')[0].split('?')[0]
                    node_name = parts
            except:
                node_name = "Manual"

            # Create manual key entry
            key = Key(
                client_id=client.id,
                node_id=0,  # 0 indicates manual key
                inbound_id=0,
                uuid=client_uuid,
                vless_url=line,
                manual=True
            )
            db.add(key)
            added_count += 1
            manual_results.append({
                "key": node_name,
                "success": True,
                "message": "Manual key added"
            })

        db.commit()

    return {
        "client_id": client.id,
        "added_count": added_count,
        "results": manual_results
    }


@app.post("/api/clients/{client_id}/recreate")
async def recreate_client_on_nodes(
    request: Request,
    client_id: int,
    reality_only: str = Form(default=None),
    db: Session = Depends(get_db)
):
    """
    Recreate client on ALL nodes (fixes broken flow settings)
    - Deletes client from all inbounds on all nodes (gRPC, XHTTP, etc.)
    - Recreates client with same UUID on all nodes

    Args:
        client_id: Client ID to recreate
        reality_only: If specified, override inbound type. If None (default), auto-detect from existing keys
    """
    check_auth(request)

    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    # Get ALL nodes (not just nodes with existing keys)
    nodes = db.query(Node).filter(Node.enabled == True).all()

    if not nodes:
        return {"message": "No enabled nodes found", "results": []}

    # Get existing UUID from first key, or generate new one
    existing_key = db.query(Key).filter(
        Key.client_id == client_id,
        Key.manual == False
    ).first()

    if existing_key:
        client_uuid = existing_key.uuid
    else:
        # Generate new UUID if no keys exist
        client_uuid = str(uuid.uuid4())

    # Convert reality_only from string to bool (FormData sends "true"/"false" strings)
    reality_only_bool = None
    if reality_only is not None:
        reality_only_bool = reality_only.lower() in ('true', '1', 'yes')

    # Auto-detect reality_only from existing keys if not specified
    if reality_only_bool is None:
        # Check if existing keys have Reality (security=reality in URL)
        if existing_key and existing_key.vless_url:
            reality_only_bool = "security=reality" in existing_key.vless_url
        else:
            # Default to legacy (non-Reality) if no existing keys
            reality_only_bool = False

    # Delete all existing auto-generated keys for this client
    db.query(Key).filter(
        Key.client_id == client_id,
        Key.manual == False
    ).delete()
    db.commit()

    # Recreate keys on ALL nodes using async parallel method
    node_results = await async_create_keys_on_all_nodes(nodes, client.email, db, reality_only=reality_only_bool)

    # Save keys to database
    for result in node_results:
        if result["success"]:
            for key_info in result["keys"]:
                key = Key(
                    client_id=client.id,
                    node_id=result["node_id"],
                    inbound_id=key_info["inbound_id"],
                    uuid=key_info["uuid"],
                    vless_url=create_vless_url(
                        db.query(Node).get(result["node_id"]),
                        client.email,
                        key_info["uuid"],
                        key_info["inbound_id"],
                        key_info["transport"].lower(),
                        stream_settings=key_info.get("stream_settings")
                    ),
                    manual=False,
                    created_at=datetime.utcnow()
                )
                db.add(key)

    db.commit()

    # Format results for response
    results = [{
        "node": r["node_name"],
        "success": r["success"],
        "message": f"Created {len(r['keys'])} keys" if r["success"] else ", ".join(r["errors"])
    } for r in node_results]

    success_count = sum(1 for r in node_results if r["success"])

    return {
        "client_email": client.email,
        "client_uuid": client_uuid,
        "reality_only": reality_only,
        "total_nodes": len(results),
        "success_count": success_count,
        "results": results
    }


@app.delete("/api/keys/{key_id}")
async def delete_key(request: Request, key_id: int, db: Session = Depends(get_db)):
    """Delete a specific key (only manual keys can be deleted this way)"""
    check_auth(request)

    key = db.query(Key).filter(Key.id == key_id).first()
    if not key:
        raise HTTPException(status_code=404, detail="Key not found")

    # Only allow deletion of manual keys
    if not key.manual:
        raise HTTPException(status_code=400, detail="Only manual keys can be deleted individually")

    db.delete(key)
    db.commit()

    return {"message": "Key deleted successfully"}


# ============================================================================
# Backup & Restore API Routes
# ============================================================================

BACKUP_DIR = "/opt/central/backups"

@app.post("/api/admin/backup")
async def create_backup(request: Request, db: Session = Depends(get_db)):
    """Create backup of all node databases via API"""
    check_auth(request)

    from datetime import datetime
    import tarfile

    try:
        # Create backup directory if it doesn't exist
        os.makedirs(BACKUP_DIR, exist_ok=True)

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_id = f"backup_{timestamp}"
        backup_path = f"{BACKUP_DIR}/{backup_id}"
        os.makedirs(backup_path, exist_ok=True)

        results = {
            "backup_id": backup_id,
            "timestamp": datetime.now().isoformat(),
            "nodes": []
        }

        # Backup all nodes IN PARALLEL via API
        nodes = db.query(Node).all()

        if nodes:
            backup_results = await async_backup_all_nodes(nodes, backup_path)

            # Format results for response
            for r in backup_results:
                if r["success"]:
                    results["nodes"].append({
                        "node": r["node"],
                        "success": True,
                        "size": r["file_size"],
                        "file": f"{r['node']}.db"
                    })
                else:
                    results["nodes"].append({
                        "node": r["node"],
                        "success": False,
                        "error": r["error"]
                    })

        # Create metadata file
        metadata = {
            "backup_id": backup_id,
            "timestamp": results["timestamp"],
            "nodes_backed_up": len([n for n in results["nodes"] if n["success"]]),
            "total_nodes": len(nodes),
            "results": results
        }

        with open(f"{backup_path}/metadata.json", 'w') as f:
            json.dump(metadata, f, indent=2)

        # Create compressed archive
        archive_path = f"{BACKUP_DIR}/{backup_id}.tar.gz"
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(backup_path, arcname=backup_id)

        # Clean up uncompressed files
        import shutil
        shutil.rmtree(backup_path)

        # Get final archive size
        archive_size = os.path.getsize(archive_path)

        return {
            "success": True,
            "backup_id": backup_id,
            "size": archive_size,
            "timestamp": metadata["timestamp"],
            "summary": {
                "nodes": f"{metadata['nodes_backed_up']}/{metadata['total_nodes']}"
            }
        }

    except Exception as e:
        import traceback
        error_detail = traceback.format_exc()
        print(f"Backup error: {error_detail}")
        return {
            "success": False,
            "error": str(e)
        }


@app.get("/api/admin/backups")
async def list_backups(request: Request):
    """List all available backups"""
    check_auth(request)

    from datetime import datetime

    try:
        if not os.path.exists(BACKUP_DIR):
            return {"backups": []}

        backups = []
        for filename in os.listdir(BACKUP_DIR):
            if filename.endswith('.tar.gz') and filename.startswith('backup_'):
                filepath = os.path.join(BACKUP_DIR, filename)
                backup_id = filename.replace('.tar.gz', '')

                # Get file stats
                stats = os.stat(filepath)
                size = stats.st_size
                created = datetime.fromtimestamp(stats.st_mtime)

                # Try to extract metadata
                metadata = None
                try:
                    import tarfile
                    with tarfile.open(filepath, 'r:gz') as tar:
                        try:
                            metadata_file = tar.extractfile(f"{backup_id}/metadata.json")
                            if metadata_file:
                                metadata = json.load(metadata_file)
                        except:
                            pass
                except:
                    pass

                backups.append({
                    "backup_id": backup_id,
                    "filename": filename,
                    "size": size,
                    "created": created.isoformat(),
                    "metadata": metadata
                })

        # Sort by creation date (newest first)
        backups.sort(key=lambda x: x['created'], reverse=True)

        return {"backups": backups}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/backups/{backup_id}/download")
async def download_backup(request: Request, backup_id: str):
    """Download backup archive"""
    check_auth(request)

    from fastapi.responses import FileResponse

    filepath = os.path.join(BACKUP_DIR, f"{backup_id}.tar.gz")

    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Backup not found")

    return FileResponse(
        filepath,
        media_type="application/gzip",
        filename=f"{backup_id}.tar.gz"
    )


@app.delete("/api/admin/backups/{backup_id}")
async def delete_backup(request: Request, backup_id: str):
    """Delete backup archive"""
    check_auth(request)

    filepath = os.path.join(BACKUP_DIR, f"{backup_id}.tar.gz")

    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Backup not found")

    try:
        os.remove(filepath)
        return {"message": "Backup deleted successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/backups/{backup_id}/restore")
async def restore_backup(request: Request, backup_id: str, db: Session = Depends(get_db)):
    """Restore from backup (DANGEROUS - requires confirmation)"""
    check_auth(request)

    # This is a placeholder - full restore is complex and dangerous
    # Should be implemented carefully with proper safeguards

    return {
        "success": False,
        "message": "Restore functionality not yet implemented. Please restore manually for safety."
    }


# ============================================================================
# User Management API
# ============================================================================

@app.get("/api/users")
async def get_users(request: Request, page: int = 1, limit: int = 50, search: str = None, db: Session = Depends(get_db)):
    """Get users with pagination and search"""
    check_auth(request)

    # Build base query
    query = db.query(User)

    # Apply search filter if provided
    if search:
        search_term = f"%{search}%"
        query = query.join(Client, User.client_id == Client.id, isouter=True).filter(
            or_(
                cast(User.telegram_id, String).like(search_term),
                User.name.like(search_term),
                User.tag.like(search_term),
                Client.email.like(search_term)
            )
        )

    # Get total count
    total = query.count()

    # Calculate offset
    offset = (page - 1) * limit

    # Get paginated users, ordered by created_at DESC (newest first)
    users = query.order_by(User.created_at.desc()).offset(offset).limit(limit).all()

    result = []
    for user in users:
        user_data = {
            "id": user.id,
            "telegram_id": user.telegram_id,
            "name": user.name,
            "payment_status": user.payment_status,
            "limit_ip": user.limit_ip,
            "tag": user.tag,
            "payment_date": user.payment_date.isoformat() if user.payment_date else None,
            "renewal_date": user.renewal_date.isoformat() if user.renewal_date else None,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "updated_at": user.updated_at.isoformat() if user.updated_at else None,
            "subscription_url": None,
            "client_email": None,
            "client_id": None,
            "enabled": False
        }

        # Get associated client info
        if user.client:
            subscription_base = os.getenv("SUBSCRIPTION_URL", "http://localhost:8001")
            user_data["subscription_url"] = f"{subscription_base}/{user.client.email}"
            user_data["client_email"] = user.client.email
            user_data["client_id"] = user.client.id
            user_data["enabled"] = user.client.enabled

        result.append(user_data)

    return {
        "users": result,
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": (total + limit - 1) // limit  # Ceiling division
    }


@app.get("/api/users/{telegram_id}")
async def get_user(request: Request, telegram_id: int, db: Session = Depends(get_db)):
    """Get specific user by telegram_id"""
    check_auth(request)

    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user_data = {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "name": user.name,
        "payment_status": user.payment_status,
        "limit_ip": user.limit_ip,
        "tag": user.tag,
        "payment_date": user.payment_date.isoformat() if user.payment_date else None,
        "renewal_date": user.renewal_date.isoformat() if user.renewal_date else None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "updated_at": user.updated_at.isoformat() if user.updated_at else None,
        "subscription_url": None,
        "client_email": None,
        "client_id": None
    }

    # Get associated client info
    if user.client:
        subscription_base = os.getenv("SUBSCRIPTION_URL", "http://localhost:8001")
        user_data["subscription_url"] = f"{subscription_base}/{user.client.email}"
        user_data["client_email"] = user.client.email
        user_data["client_id"] = user.client.id

    return user_data


@app.post("/api/users")
async def create_user(request: Request, db: Session = Depends(get_db)):
    """Create new user with subscription and keys on all nodes

    If client_email is provided, links to existing client instead of creating new one.
    """
    check_auth(request)

    data = await request.json()

    telegram_id = data.get("telegram_id")
    name = data.get("name")
    payment_status = data.get("payment_status", PaymentStatus.TEST)
    limit_ip = data.get("limit_ip", 0)
    tag = data.get("tag")
    client_email = data.get("client_email")  # Optional: link to existing client

    if not telegram_id:
        raise HTTPException(status_code=400, detail="telegram_id is required")

    # Check if user already exists
    existing_user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="User with this telegram_id already exists")

    # Create user
    user = User(
        telegram_id=telegram_id,
        name=name,
        payment_status=payment_status,
        limit_ip=limit_ip,
        tag=tag,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )

    # Set renewal_date for TEST users (72 hours from now)
    if payment_status == PaymentStatus.TEST:
        user.renewal_date = datetime.utcnow().date() + timedelta(hours=72)

    db.add(user)
    db.flush()  # Get user.id

    # Option 1: Link to existing client
    if client_email:
        client = db.query(Client).filter(Client.email == client_email).first()
        if not client:
            db.rollback()
            raise HTTPException(status_code=404, detail=f"Client with email {client_email} not found")

        if client.user_id is not None:
            db.rollback()
            raise HTTPException(status_code=400, detail=f"Client {client_email} is already linked to another user")

        # Link existing client to new user
        client.user_id = user.id
        db.commit()
        db.refresh(user)
        db.refresh(client)

        subscription_base = os.getenv("SUBSCRIPTION_URL", "http://localhost:8001")

        return {
            "id": user.id,
            "telegram_id": user.telegram_id,
            "name": user.name,
            "payment_status": user.payment_status,
            "limit_ip": user.limit_ip,
            "subscription_url": f"{subscription_base}/{client.email}",
            "client_email": client.email,
            "keys_created": [],  # Keys already exist
            "errors": None,
            "renewal_date": user.renewal_date.isoformat() if user.renewal_date else None,
            "linked_existing": True
        }

    # Option 2: Create new client with keys on all nodes
    # Generate unique client email
    client_uuid = str(uuid.uuid4())[:8]
    client_email = f"Client-{client_uuid}"

    # Create client
    client = Client(
        email=client_email,
        enabled=True,
        user_id=user.id,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    db.add(client)
    db.flush()  # Get client.id

    # Get all enabled nodes
    nodes = db.query(Node).filter(Node.enabled == True).all()

    if not nodes:
        db.rollback()
        raise HTTPException(status_code=500, detail="No enabled nodes available")

    # Create keys on all nodes IN PARALLEL using httpx
    node_results = await async_create_keys_on_all_nodes(nodes, client_email, db)

    # Process results and save keys to database
    keys_created = []
    errors = []

    for result in node_results:
        if result["success"]:
            for key_info in result["keys"]:
                # Save key to database
                key = Key(
                    client_id=client.id,
                    node_id=result["node_id"],
                    inbound_id=key_info["inbound_id"],
                    uuid=key_info["uuid"],
                    vless_url=create_vless_url(
                        db.query(Node).get(result["node_id"]),
                        client_email,
                        key_info["uuid"],
                        key_info["inbound_id"],
                        key_info["transport"].lower(),
                        stream_settings=key_info.get("stream_settings")
                    ),
                    manual=False,
                    created_at=datetime.utcnow()
                )
                db.add(key)
                keys_created.append(f"{result['node_name']}-{key_info['transport']}")

        # Collect errors
        if result["errors"]:
            errors.extend([f"{result['node_name']}: {err}" for err in result["errors"]])

    if not keys_created:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create keys on any node. Errors: {'; '.join(errors)}"
        )

    # Commit everything
    db.commit()
    db.refresh(user)
    db.refresh(client)

    subscription_base = os.getenv("SUBSCRIPTION_URL", "http://localhost:8001")

    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "name": user.name,
        "payment_status": user.payment_status,
        "limit_ip": user.limit_ip,
        "subscription_url": f"{subscription_base}/{client.email}",
        "client_email": client.email,
        "keys_created": keys_created,
        "errors": errors if errors else None,
        "renewal_date": user.renewal_date.isoformat() if user.renewal_date else None,
        "linked_existing": False
    }


@app.post("/api/users/batch")
async def create_users_batch(request: Request, db: Session = Depends(get_db)):
    """Create multiple users in batch with MASSIVE parallelization

    Creates all users, clients, and keys in one operation.
    All keys are created on all nodes simultaneously (users × nodes parallel tasks).

    Request body:
    {
        "users": [
            {
                "telegram_id": 123,
                "client_email": "Client-abc123",
                "payment_status": 2,
                "limit_ip": 0,
                "tag": "optional",
                "payment_date": "2025-01-01",
                "renewal_date": "2025-02-01"
            },
            ...
        ],
        "reality_only": false  // Optional: default false (legacy non-Reality inbounds)
    }
    """
    check_auth(request)

    data = await request.json()
    users_data = data.get("users", [])
    reality_only = data.get("reality_only", False)  # Default to legacy non-Reality

    if not users_data:
        raise HTTPException(status_code=400, detail="No users provided")

    if len(users_data) > 100:
        raise HTTPException(status_code=400, detail="Maximum 100 users per batch")

    start_time = time.time()
    print(f"\n🚀 BATCH CREATE: Processing {len(users_data)} users...")

    # Get all enabled nodes upfront
    nodes = db.query(Node).filter(Node.enabled == True).all()
    if not nodes:
        raise HTTPException(status_code=500, detail="No enabled nodes available")

    # Step 1: Create all users and clients in database
    created_users = []
    user_client_map = {}  # {client_email: (user, client)}

    for user_data in users_data:
        telegram_id = user_data.get("telegram_id")
        client_email = user_data.get("client_email")
        payment_status = user_data.get("payment_status", PaymentStatus.TEST)
        limit_ip = user_data.get("limit_ip", 0)
        tag = user_data.get("tag")
        payment_date_str = user_data.get("payment_date")
        renewal_date_str = user_data.get("renewal_date")

        if not telegram_id or not client_email:
            continue  # Skip invalid entries

        # Check if user already exists by telegram_id
        existing_user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if existing_user:
            continue  # Skip existing users

        # Check if client_email already exists
        existing_client = db.query(Client).filter(Client.email == client_email).first()
        if existing_client:
            print(f"  ⚠️  Skipping telegram_id={telegram_id}: client_email={client_email} already exists")
            continue  # Skip if email is taken

        # Parse dates
        payment_date = None
        renewal_date = None
        if payment_date_str:
            try:
                payment_date = datetime.fromisoformat(payment_date_str).date()
            except:
                pass
        if renewal_date_str:
            try:
                renewal_date = datetime.fromisoformat(renewal_date_str).date()
            except:
                pass

        # Create user
        user = User(
            telegram_id=telegram_id,
            name=client_email,
            payment_status=payment_status,
            limit_ip=limit_ip,
            tag=tag,
            payment_date=payment_date,
            renewal_date=renewal_date,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        db.add(user)
        db.flush()

        # Create client
        client = Client(
            email=client_email,
            enabled=payment_status != PaymentStatus.NOT_PAID,
            user_id=user.id,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        db.add(client)
        db.flush()

        created_users.append(user)
        user_client_map[client_email] = (user, client)

    if not created_users:
        db.rollback()
        return {
            "message": "No new users created (all already exist or invalid)",
            "created": 0,
            "total_keys": 0,
            "elapsed": 0
        }

    print(f"  ✅ Created {len(created_users)} users/clients in database")

    # Step 2: Create ALL keys for ALL users on ALL nodes in MASSIVE PARALLEL batch
    print(f"  🚀 Creating keys on {len(nodes)} nodes for {len(created_users)} users ({len(created_users) * len(nodes)} parallel tasks)...")

    all_tasks = []
    task_metadata = []  # Track which task belongs to which user/node
    client_uuid_map = {}  # Track UUID for each client

    for client_email in user_client_map.keys():
        # Generate ONE UUID per client (shared across all nodes and inbounds)
        client_uuid = str(uuid.uuid4())
        client_uuid_map[client_email] = client_uuid

        for node in nodes:
            all_tasks.append(async_create_keys_on_node(node, client_email, client_uuid, db, reality_only=reality_only))
            task_metadata.append({
                "client_email": client_email,
                "node_id": node.id,
                "node_name": node.name
            })

    # Execute ALL tasks in parallel
    key_creation_start = time.time()
    results = await asyncio.gather(*all_tasks, return_exceptions=True)
    key_creation_elapsed = time.time() - key_creation_start

    print(f"  ✅ Key creation completed in {key_creation_elapsed:.2f}s")

    # Step 3: Process results and save keys to database
    total_keys = 0
    errors = []

    for i, result in enumerate(results):
        metadata = task_metadata[i]
        user, client = user_client_map[metadata["client_email"]]

        if isinstance(result, Exception):
            errors.append(f"{metadata['node_name']}: {str(result)}")
            continue

        if result["success"]:
            for key_info in result["keys"]:
                key = Key(
                    client_id=client.id,
                    node_id=metadata["node_id"],
                    inbound_id=key_info["inbound_id"],
                    uuid=key_info["uuid"],
                    vless_url=create_vless_url(
                        db.query(Node).get(metadata["node_id"]),
                        metadata["client_email"],
                        key_info["uuid"],
                        key_info["inbound_id"],
                        key_info["transport"].lower(),
                        stream_settings=key_info.get("stream_settings")
                    ),
                    manual=False,
                    created_at=datetime.utcnow()
                )
                db.add(key)
                total_keys += 1

        if result.get("errors"):
            errors.extend([f"{metadata['node_name']}: {err}" for err in result["errors"]])

    # Commit everything
    db.commit()

    elapsed = time.time() - start_time
    print(f"✨ BATCH COMPLETE: {len(created_users)} users, {total_keys} keys in {elapsed:.2f}s\n")

    return {
        "message": f"Batch created {len(created_users)} users",
        "created": len(created_users),
        "total_keys": total_keys,
        "elapsed": elapsed,
        "errors": errors if errors else None
    }


@app.put("/api/users/{telegram_id}")
async def update_user(request: Request, telegram_id: int, db: Session = Depends(get_db)):
    """Update user information"""
    check_auth(request)

    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    data = await request.json()

    # Update allowed fields
    if "name" in data:
        user.name = data["name"]
    if "payment_status" in data:
        user.payment_status = data["payment_status"]
    if "limit_ip" in data:
        user.limit_ip = data["limit_ip"]
    if "tag" in data:
        user.tag = data["tag"]
    if "payment_date" in data:
        if data["payment_date"]:
            user.payment_date = date.fromisoformat(data["payment_date"])
        else:
            user.payment_date = None
    if "renewal_date" in data:
        if data["renewal_date"]:
            user.renewal_date = date.fromisoformat(data["renewal_date"])
        else:
            user.renewal_date = None

    user.updated_at = datetime.utcnow()

    # Auto-enable if renewal date extended beyond today
    should_enable = False
    if user.renewal_date and user.renewal_date >= date.today():
        if user.client and not user.client.enabled:
            user.client.enabled = True
            should_enable = True

    db.commit()
    db.refresh(user)

    # If we re-enabled, sync to nodes
    if should_enable:
        client = user.client
        keys = db.query(Key).filter(Key.client_id == client.id).all()

        for key in keys:
            node = db.query(Node).filter(Node.id == key.node_id).first()
            if not node:
                continue

            try:
                # Login to node
                session = requests.Session()
                login_data = {"username": node.username, "password": node.password}
                login_response = session.post(
                    f"{node.url}/login",
                    data=login_data,
                    verify=False,
                    timeout=10
                )

                if login_response.status_code != 200:
                    continue

                # Get inbound
                inbounds_response = session.get(
                    f"{node.url}/panel/api/inbounds/list",
                    verify=False,
                    timeout=10
                )

                if inbounds_response.status_code != 200:
                    continue

                inbounds = inbounds_response.json().get("obj", [])
                inbound = next((ib for ib in inbounds if ib.get("id") == key.inbound_id), None)

                if not inbound:
                    continue

                # Update client enable status
                settings = json.loads(inbound["settings"])
                clients = settings.get("clients", [])

                # Determine email to search
                inbound_remark = inbound.get("remark", "").lower()
                if "xhttp" in inbound_remark:
                    search_email = f"{client.email}-xhttp"
                else:
                    search_email = client.email

                # Find and enable the client
                for c in clients:
                    if c.get("email") == search_email:
                        c["enable"] = True
                        break

                settings["clients"] = clients

                # Update inbound
                update_data = {
                    "up": inbound["up"],
                    "down": inbound["down"],
                    "total": inbound["total"],
                    "remark": inbound["remark"],
                    "enable": inbound["enable"],
                    "expiryTime": inbound["expiryTime"],
                    "listen": inbound.get("listen", ""),
                    "port": inbound["port"],
                    "protocol": inbound["protocol"],
                    "settings": json.dumps(settings),
                    "streamSettings": inbound["streamSettings"],
                    "sniffing": inbound["sniffing"]
                }

                session.post(
                    f"{node.url}/panel/api/inbounds/update/{inbound['id']}",
                    json=update_data,
                    verify=False,
                    timeout=10
                )

            except Exception as e:
                print(f"Failed to re-enable user on {node.name}: {str(e)}")

    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "name": user.name,
        "payment_status": user.payment_status,
        "limit_ip": user.limit_ip,
        "tag": user.tag,
        "payment_date": user.payment_date.isoformat() if user.payment_date else None,
        "renewal_date": user.renewal_date.isoformat() if user.renewal_date else None,
        "updated_at": user.updated_at.isoformat()
    }


@app.put("/api/users/{telegram_id}")
async def update_user(request: Request, telegram_id: int, db: Session = Depends(get_db)):
    """Update user data (payment status, dates, limits, etc.)

    Does NOT modify client relationship or keys - use POST/DELETE for that
    """
    check_auth(request)

    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    data = await request.json()

    # Update fields if provided
    if "name" in data:
        user.name = data["name"]

    if "payment_status" in data:
        user.payment_status = int(data["payment_status"])

    if "limit_ip" in data:
        user.limit_ip = int(data["limit_ip"])

    if "tag" in data:
        user.tag = data["tag"]

    if "payment_date" in data:
        if data["payment_date"]:
            user.payment_date = datetime.fromisoformat(data["payment_date"]).date()
        else:
            user.payment_date = None

    if "renewal_date" in data:
        if data["renewal_date"]:
            user.renewal_date = datetime.fromisoformat(data["renewal_date"]).date()
        else:
            user.renewal_date = None

    user.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(user)

    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "name": user.name,
        "payment_status": user.payment_status,
        "limit_ip": user.limit_ip,
        "tag": user.tag,
        "payment_date": user.payment_date.isoformat() if user.payment_date else None,
        "renewal_date": user.renewal_date.isoformat() if user.renewal_date else None,
        "updated_at": user.updated_at.isoformat()
    }


@app.delete("/api/users/{telegram_id}")
async def delete_user(request: Request, telegram_id: int, db: Session = Depends(get_db)):
    """Delete user (cascade deletes client and keys)"""
    check_auth(request)

    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Get client before deletion for node cleanup
    client = user.client

    if client:
        # Get unique nodes that have keys for this client
        keys = db.query(Key).filter(Key.client_id == client.id).all()
        unique_node_ids = list(set([key.node_id for key in keys]))
        nodes = [db.query(Node).get(node_id) for node_id in unique_node_ids if db.query(Node).get(node_id)]

        # Delete client from all nodes IN PARALLEL using httpx
        if nodes:
            results = await async_delete_client_from_all_nodes(nodes, client, db)
            errors = [r["message"] for r in results if not r["success"]]
        else:
            errors = []

    # Delete from database (cascade will handle client and keys)
    db.delete(user)
    db.commit()

    return {
        "message": "User deleted successfully",
        "telegram_id": telegram_id,
        "errors": errors if errors else None
    }


@app.post("/api/users/{telegram_id}/toggle")
async def toggle_user_enabled(request: Request, telegram_id: int, db: Session = Depends(get_db)):
    """Enable or disable user (syncs to all nodes)"""
    check_auth(request)

    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if not user.client:
        raise HTTPException(status_code=400, detail="User has no client")

    data = await request.json()
    enable = data.get("enabled", True)

    client = user.client
    client.enabled = enable

    # Get unique nodes that have keys for this client
    keys = db.query(Key).filter(Key.client_id == client.id).all()
    unique_node_ids = list(set([key.node_id for key in keys]))
    nodes = [db.query(Node).get(node_id) for node_id in unique_node_ids if db.query(Node).get(node_id)]

    # Toggle client on all nodes IN PARALLEL using httpx
    if nodes:
        results = await async_toggle_client_on_all_nodes(nodes, client.email, enable, db)
        errors = [r["message"] for r in results if not r["success"]]
    else:
        errors = []

    db.commit()

    action = "enabled" if enable else "disabled"
    print(f"User {user.telegram_id} ({user.name}) {action}")

    return {
        "message": f"User {action} successfully",
        "telegram_id": telegram_id,
        "enabled": enable,
        "errors": errors if errors else None
    }


@app.post("/api/admin/check-renewals")
async def check_renewals(request: Request, db: Session = Depends(get_db)):
    """Check all users and disable those past renewal_date"""
    check_auth(request)

    today = date.today()

    # Find users past renewal date with enabled clients
    expired_users = db.query(User).join(Client).filter(
        User.renewal_date != None,
        User.renewal_date < today,
        Client.enabled == True
    ).all()

    disabled_count = 0
    errors = []

    for user in expired_users:
        try:
            client = user.client

            # Disable client in database
            client.enabled = False

            # Get all keys for this client
            keys = db.query(Key).filter(Key.client_id == client.id).all()

            # Disable keys on all nodes
            for key in keys:
                node = db.query(Node).filter(Node.id == key.node_id).first()
                if not node:
                    continue

                try:
                    # Login to node
                    session = requests.Session()
                    login_data = {"username": node.username, "password": node.password}
                    login_response = session.post(
                        f"{node.url}/login",
                        data=login_data,
                        verify=False,
                        timeout=10
                    )

                    if login_response.status_code != 200:
                        errors.append(f"{user.telegram_id} - {node.name}: Login failed")
                        continue

                    # Get inbound
                    inbounds_response = session.get(
                        f"{node.url}/panel/api/inbounds/list",
                        verify=False,
                        timeout=10
                    )

                    if inbounds_response.status_code != 200:
                        errors.append(f"{user.telegram_id} - {node.name}: Failed to get inbounds")
                        continue

                    inbounds = inbounds_response.json().get("obj", [])
                    inbound = next((ib for ib in inbounds if ib.get("id") == key.inbound_id), None)

                    if not inbound:
                        errors.append(f"{user.telegram_id} - {node.name}: Inbound not found")
                        continue

                    # Update client enable status
                    settings = json.loads(inbound["settings"])
                    clients = settings.get("clients", [])

                    # Determine email to search
                    inbound_remark = inbound.get("remark", "").lower()
                    if "xhttp" in inbound_remark:
                        search_email = f"{client.email}-xhttp"
                    else:
                        search_email = client.email

                    # Find and disable the client
                    for c in clients:
                        if c.get("email") == search_email:
                            c["enable"] = False
                            break

                    settings["clients"] = clients

                    # Update inbound
                    update_data = {
                        "up": inbound["up"],
                        "down": inbound["down"],
                        "total": inbound["total"],
                        "remark": inbound["remark"],
                        "enable": inbound["enable"],
                        "expiryTime": inbound["expiryTime"],
                        "listen": inbound.get("listen", ""),
                        "port": inbound["port"],
                        "protocol": inbound["protocol"],
                        "settings": json.dumps(settings),
                        "streamSettings": inbound["streamSettings"],
                        "sniffing": inbound["sniffing"]
                    }

                    update_response = session.post(
                        f"{node.url}/panel/api/inbounds/update/{inbound['id']}",
                        json=update_data,
                        verify=False,
                        timeout=10
                    )

                    if update_response.status_code != 200:
                        errors.append(f"{user.telegram_id} - {node.name}: Update failed")

                except Exception as e:
                    errors.append(f"{user.telegram_id} - {node.name}: {str(e)}")

            disabled_count += 1
            print(f"Disabled user {user.telegram_id} ({user.name}) - renewal_date: {user.renewal_date}")

        except Exception as e:
            errors.append(f"User {user.telegram_id}: {str(e)}")

    db.commit()

    return {
        "checked": len(expired_users),
        "disabled": disabled_count,
        "errors": errors if errors else None
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
