"""Admin service for centralized 3x-ui management"""
import os
import uuid
import json
import time
import secrets
from typing import List
from fastapi import FastAPI, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
import requests

from database import get_db, Node, Client, Key, engine, Base

# Create tables
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Subscription Manager Admin")
templates = Jinja2Templates(directory="templates")

# Simple session storage (in production use Redis)
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
    if not session_id or session_id not in sessions:
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

def create_vless_url(node: Node, client_email: str, client_uuid: str, inbound_id: int, transport: str = "grpc") -> str:
    """Generate VLESS URL for client

    Args:
        node: Node configuration
        client_email: Client email
        client_uuid: Client UUID
        inbound_id: Inbound ID (unused, kept for compatibility)
        transport: Transport type ('grpc' or 'xhttp')

    Returns:
        VLESS URL string
    """
    import urllib.parse

    domain = node.domain

    if transport == "xhttp":
        # XHTTP transport: type=xhttp, path=/api
        params = {
            "encryption": "none",
            "security": "tls",
            "type": "xhttp",
            "path": "/api"
        }
        remark = urllib.parse.quote(f"{node.name}-XHTTP-{client_email}")
    else:
        # gRPC transport: type=grpc, serviceName=sync (default)
        params = {
            "encryption": "none",
            "security": "tls",
            "type": "grpc",
            "serviceName": "sync"
        }
        remark = urllib.parse.quote(f"{node.name}-gRPC-{client_email}")

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

        # Find VLESS-gRPC-Local inbound
        vless_inbound = None
        for inbound in inbounds:
            if inbound.get("remark") == "VLESS-gRPC-Local":
                vless_inbound = inbound
                break

        if not vless_inbound:
            raise Exception("VLESS-gRPC-Local inbound not found on node")

        inbound_id = vless_inbound["id"]

        # Parse existing settings to check if client already exists
        settings = json.loads(vless_inbound.get("settings", "{}"))
        clients_list = settings.get("clients", [])

        # Check if client already exists
        existing_client = None
        for c in clients_list:
            if c.get("email") == client.email:
                existing_client = c
                break

        if existing_client:
            # Delete existing client first (ignore errors if client doesn't exist)
            try:
                delete_response = session.post(
                    f"{node.url}/panel/api/inbounds/{inbound_id}/delClientByEmail/{client.email}",
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
                        "email": client.email,
                        "limitIp": 0,  # 0 = unlimited
                        "totalGB": 0,
                        "expiryTime": 0,
                        "enable": client.enabled,
                        "tgId": "",
                        "subId": client.email
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
            raise Exception(f"Failed to add client: {add_response.status_code}")

        # Generate VLESS URL
        vless_url = create_vless_url(node, client.email, str(client_uuid), inbound_id)

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

        # ============================================================================
        # Try to add client to XHTTP inbound if it exists
        # ============================================================================

        xhttp_inbound = None
        for inbound in inbounds:
            if inbound.get("remark") == "VLESS-XHTTP":
                xhttp_inbound = inbound
                break

        if xhttp_inbound:
            try:
                xhttp_inbound_id = xhttp_inbound["id"]
                xhttp_email = f"{client.email}-xhttp"

                # Check if XHTTP client already exists
                xhttp_settings = json.loads(xhttp_inbound.get("settings", "{}"))
                xhttp_clients_list = xhttp_settings.get("clients", [])

                existing_xhttp_client = None
                for c in xhttp_clients_list:
                    if c.get("email") == xhttp_email:
                        existing_xhttp_client = c
                        break

                if existing_xhttp_client:
                    # Delete existing XHTTP client first
                    try:
                        session.post(
                            f"{node.url}/panel/api/inbounds/{xhttp_inbound_id}/delClientByEmail/{xhttp_email}",
                            verify=False,
                            timeout=10
                        )
                    except Exception:
                        pass  # Ignore delete errors

                # Add client to XHTTP inbound
                xhttp_client_config = {
                    "id": xhttp_inbound_id,
                    "settings": json.dumps({
                        "clients": [
                            {
                                "id": str(client_uuid),
                                "flow": "",
                                "email": xhttp_email,
                                "limitIp": 0,  # 0 = unlimited
                                "totalGB": 0,
                                "expiryTime": 0,
                                "enable": client.enabled,
                                "tgId": "",
                                "subId": xhttp_email
                            }
                        ]
                    })
                }

                xhttp_add_response = session.post(
                    f"{node.url}/panel/api/inbounds/addClient",
                    json=xhttp_client_config,
                    verify=False,
                    timeout=10
                )

                if xhttp_add_response.status_code == 200:
                    # Generate XHTTP VLESS URL
                    xhttp_vless_url = create_vless_url(node, client.email, str(client_uuid), xhttp_inbound_id, transport="xhttp")

                    # Save XHTTP key to database
                    existing_xhttp_key = db.query(Key).filter(
                        Key.client_id == client.id,
                        Key.node_id == node.id,
                        Key.inbound_id == xhttp_inbound_id
                    ).first()

                    if existing_xhttp_key:
                        existing_xhttp_key.uuid = client_uuid
                        existing_xhttp_key.vless_url = xhttp_vless_url
                        existing_xhttp_key.manual = False
                    else:
                        xhttp_key = Key(
                            client_id=client.id,
                            node_id=node.id,
                            inbound_id=xhttp_inbound_id,
                            uuid=client_uuid,
                            vless_url=xhttp_vless_url,
                            manual=False
                        )
                        db.add(xhttp_key)

                    db.commit()

            except Exception as e:
                # XHTTP addition failed, but gRPC succeeded - continue
                print(f"Warning: Failed to add XHTTP key for {client.email} on {node.name}: {e}")
                pass

        # Clear stats cache for this node
        clear_node_stats_cache(node.id)

        return True, vless_url

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
# Web UI Routes
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Main admin page"""
    session_id = request.cookies.get("session_id")
    if not session_id or session_id not in sessions:
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
    if session_id in sessions:
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
    return [{"id": n.id, "name": n.name, "url": n.url, "domain": n.domain, "enabled": n.enabled} for n in nodes]


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
        "enabled": node.enabled
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
    db: Session = Depends(get_db)
):
    """Update node"""
    check_auth(request)

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
    node.name = name
    node.url = url.rstrip('/')
    node.domain = domain
    node.username = username
    node.password = password

    db.commit()
    db.refresh(node)

    # Regenerate all VLESS URLs for this node's keys
    # This ensures keys are always in sync with current node name/domain
    keys = db.query(Key).filter(Key.node_id == node_id, Key.manual == False).all()
    for key in keys:
        # Get client email for the key
        client = db.query(Client).filter(Client.id == key.client_id).first()
        if client:
            # Regenerate VLESS URL with current node info
            # Determine transport type from existing URL
            if "type=xhttp" in key.vless_url:
                transport = "xhttp"
            else:
                transport = "grpc"

            # UUID is stored in the key, not the client
            new_vless_url = create_vless_url(node, client.email, str(key.uuid), key.inbound_id, transport)
            key.vless_url = new_vless_url

    db.commit()

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

    # Delete all clients from the actual 3x-ui node
    clients_deleted_on_node = 0
    for client in clients_on_node.values():
        try:
            success, msg = delete_client_from_node(node, client, db)
            if success:
                clients_deleted_on_node += 1
        except Exception:
            # Continue even if deletion fails (node might be offline)
            pass

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
# API Routes - Clients
# ============================================================================

@app.get("/api/clients")
async def get_clients(request: Request, db: Session = Depends(get_db)):
    """Get all clients"""
    check_auth(request)
    clients = db.query(Client).all()
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
    return result


@app.post("/api/clients")
async def create_client(
    request: Request,
    email: str = Form(...),
    manual_keys: str = Form(default=""),
    db: Session = Depends(get_db)
):
    """Create new client and sync to all nodes"""
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

    # Sync to all enabled nodes (auto-generated keys)
    nodes = db.query(Node).filter(Node.enabled == True).all()
    results = []

    for node in nodes:
        success, message = sync_client_to_node(node, client, client_uuid, db)
        results.append({
            "node": node.name,
            "success": success,
            "message": message
        })

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
    db: Session = Depends(get_db)
):
    """Batch create clients with pattern: seed0, seed1, ..., seed(count-1)"""
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

        # Generate UUID for this client
        client_uuid = uuid.uuid4()

        # Sync to all enabled nodes
        for node in nodes:
            success, message = sync_client_to_node(node, client, client_uuid, db)
            if success:
                total_synced += 1

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

    # Resync to all nodes
    client_uuid = uuid.uuid4()
    nodes = db.query(Node).filter(Node.enabled == True).all()
    results = []

    for node in nodes:
        success, message = sync_client_to_node(node, client, client_uuid, db)
        results.append({"node": node.name, "success": success, "message": message})

    return {"message": "Client enabled", "sync_results": results}


@app.put("/api/clients/{client_id}/disable")
async def disable_client(request: Request, client_id: int, db: Session = Depends(get_db)):
    """Disable client on all nodes"""
    check_auth(request)

    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    client.enabled = False
    db.commit()

    # Resync to all nodes to disable
    client_uuid = uuid.uuid4()
    nodes = db.query(Node).filter(Node.enabled == True).all()
    results = []

    for node in nodes:
        success, message = sync_client_to_node(node, client, client_uuid, db)
        results.append({"node": node.name, "success": success, "message": message})

    return {"message": "Client disabled", "sync_results": results}


@app.get("/api/clients/{client_id}/limit")
async def get_client_limit(request: Request, client_id: int, db: Session = Depends(get_db)):
    """Get IP limit for client from nodes"""
    check_auth(request)

    try:
        # Get client
        client = db.query(Client).filter(Client.id == client_id).first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found")

        # Get first key for this client to query a node
        key = db.query(Key).filter(Key.client_id == client_id).first()
        if not key:
            return {"client_id": client_id, "email": client.email, "limit_ip": 0, "message": "No keys found, assuming unlimited"}

        node = db.query(Node).filter(Node.id == key.node_id).first()
        if not node:
            return {"client_id": client_id, "email": client.email, "limit_ip": 0, "message": "Node not found, assuming unlimited"}

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
                print(f"Login failed for node {node.name}: {login_response.status_code}")
                return {"client_id": client_id, "email": client.email, "limit_ip": 0, "message": f"Login failed, assuming unlimited"}

            # Get inbound configuration
            get_response = session.post(
                f"{node.url}/panel/api/inbounds/get/{key.inbound_id}",
                verify=False,
                timeout=30
            )

            if get_response.status_code != 200:
                print(f"Failed to get inbound {key.inbound_id} on node {node.name}: {get_response.status_code}")
                return {"client_id": client_id, "email": client.email, "limit_ip": 0, "message": f"Failed to get inbound, assuming unlimited"}

            inbound_data = get_response.json()
            if not inbound_data.get("success"):
                print(f"API returned success=false for inbound {key.inbound_id} on node {node.name}")
                return {"client_id": client_id, "email": client.email, "limit_ip": 0, "message": "API error, assuming unlimited"}

            inbound = inbound_data["obj"]
            settings = json.loads(inbound["settings"])
            clients_list = settings.get("clients", [])

            # Extract email from VLESS URL
            # Format: vless://uuid@domain:port?...#email
            try:
                email = key.vless_url.split('#')[-1]
            except:
                email = client.email

            # Find client in inbound
            for client_obj in clients_list:
                if client_obj.get("email") == email:
                    return {
                        "client_id": client_id,
                        "email": client.email,
                        "limit_ip": client_obj.get("limitIp", 0),
                        "node": node.name,
                        "inbound_id": key.inbound_id
                    }

            # Client not found in inbound - return 0 (unlimited)
            return {"client_id": client_id, "email": client.email, "limit_ip": 0, "message": "Client not found in inbound, assuming unlimited"}

        except requests.exceptions.RequestException as e:
            print(f"Request exception for client {client_id} on node {node.name}: {e}")
            return {"client_id": client_id, "email": client.email, "limit_ip": 0, "message": f"Connection error, assuming unlimited"}
        finally:
            session.close()

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

            # Get inbound configuration
            get_response = session.post(
                f"{node.url}/panel/api/inbounds/get/{inbound_id}",
                verify=False,
                timeout=30
            )

            if get_response.status_code != 200:
                results.append({
                    "node": node.name,
                    "inbound_id": inbound_id,
                    "success": False,
                    "message": f"Failed to get inbound: {get_response.status_code}"
                })
                continue

            inbound_data = get_response.json()
            if not inbound_data.get("success"):
                results.append({
                    "node": node.name,
                    "inbound_id": inbound_id,
                    "success": False,
                    "message": "API returned success=false"
                })
                continue

            inbound = inbound_data["obj"]
            settings = json.loads(inbound["settings"])
            clients_list = settings.get("clients", [])

            # Update limitIp for all client emails in this group
            updated_count = 0
            client_emails = set()
            for key in keys_group:
                # Extract email from VLESS URL
                # Format: vless://uuid@domain:port?...#email
                try:
                    email = key.vless_url.split('#')[-1]
                    client_emails.add(email)
                except:
                    pass

            for client_obj in clients_list:
                if client_obj.get("email") in client_emails:
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

    # Delete from all nodes
    nodes = db.query(Node).filter(Node.enabled == True).all()
    results = []

    for node in nodes:
        success, message = delete_client_from_node(node, client, db)
        results.append({"node": node.name, "success": success, "message": message})

    # Delete from database
    db.delete(client)
    db.commit()

    return {"message": "Client deleted", "sync_results": results}


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

    # Get all enabled nodes
    nodes = db.query(Node).filter(Node.enabled == True).all()

    for client_id in client_ids:
        client = db.query(Client).filter(Client.id == client_id).first()
        if not client:
            continue

        # Count keys before deletion
        keys_count = db.query(Key).filter(Key.client_id == client_id).count()
        keys_removed += keys_count

        # Delete from all nodes
        for node in nodes:
            try:
                delete_client_from_node(node, client, db)
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

    # Get node names for each key
    keys_details = []
    for key in keys:
        node = db.query(Node).filter(Node.id == key.node_id).first()
        keys_details.append({
            "key_id": key.id,
            "node_name": node.name if node else "Manual",
            "vless_url": key.vless_url,
            "manual": key.manual
        })

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

        # Backup all nodes via API
        nodes = db.query(Node).all()
        for node in nodes:
            try:
                session = requests.Session()
                session.verify = False

                # Login to node
                login_response = session.post(
                    f"{node.url}/login",
                    data={"username": node.username, "password": node.password},
                    timeout=10
                )

                if login_response.status_code != 200:
                    results["nodes"].append({
                        "node": node.name,
                        "success": False,
                        "error": "Login failed"
                    })
                    continue

                # Get database backup via API
                # Note: endpoint might be /panel/api/server/getDb or /server/getDb
                backup_response = session.get(
                    f"{node.url}/panel/api/server/getDb",
                    timeout=30
                )

                if backup_response.status_code == 200:
                    node_backup_file = f"{backup_path}/{node.name}.db"
                    with open(node_backup_file, 'wb') as f:
                        f.write(backup_response.content)

                    file_size = os.path.getsize(node_backup_file)
                    results["nodes"].append({
                        "node": node.name,
                        "success": True,
                        "size": file_size,
                        "file": f"{node.name}.db"
                    })
                else:
                    results["nodes"].append({
                        "node": node.name,
                        "success": False,
                        "error": f"API returned {backup_response.status_code}"
                    })

            except Exception as e:
                results["nodes"].append({
                    "node": node.name,
                    "success": False,
                    "error": str(e)
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
