"""Admin service for centralized 3x-ui management"""
import os
import uuid
import json
import time
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

def create_vless_url(node: Node, client_email: str, client_uuid: str, inbound_id: int) -> str:
    """Generate VLESS URL for client"""
    # Use public domain from node configuration
    domain = node.domain

    # Format: vless://UUID@DOMAIN:443?encryption=none&security=tls&type=grpc&serviceName=sync#EMAIL
    import urllib.parse

    params = {
        "encryption": "none",
        "security": "tls",
        "type": "grpc",
        "serviceName": "sync"
    }

    query_string = urllib.parse.urlencode(params)
    remark = urllib.parse.quote(f"{node.name}-{client_email}")

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
                        "limitIp": 0,
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
        else:
            new_key = Key(
                client_id=client.id,
                node_id=node.id,
                inbound_id=inbound_id,
                uuid=client_uuid,
                vless_url=vless_url
            )
            db.add(new_key)

        db.commit()

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
        # Find client's UUID from existing keys
        existing_key = db.query(Key).filter(Key.client_id == client.id).first()
        if existing_key:
            # Use the same UUID as other nodes
            client_uuid = existing_key.vless_url.split('://')[1].split('@')[0]

            success, vless_url = sync_client_to_node(node, client, client_uuid, db)

            if success:
                # Save key to database
                key = Key(
                    client_id=client.id,
                    node_id=node.id,
                    inbound_id=1,  # VLESS-gRPC-Local inbound
                    uuid=client_uuid,
                    vless_url=vless_url
                )
                db.add(key)
                synced_count += 1
            else:
                failed_count += 1

    db.commit()

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
    if node.name != name:
        existing = db.query(Node).filter(Node.name == name).first()
        if existing:
            raise HTTPException(status_code=400, detail="Node name already exists")

    node.name = name
    node.url = url.rstrip('/')
    node.domain = domain
    node.username = username
    node.password = password

    db.commit()
    db.refresh(node)

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
    """Delete node"""
    check_auth(request)

    node = db.query(Node).filter(Node.id == node_id).first()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    db.delete(node)
    db.commit()

    return {"message": "Node deleted"}


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

    # Sync to all enabled nodes
    nodes = db.query(Node).filter(Node.enabled == True).all()
    results = []

    for node in nodes:
        success, message = sync_client_to_node(node, client, client_uuid, db)
        results.append({
            "node": node.name,
            "success": success,
            "message": message
        })

    return {
        "id": client.id,
        "email": client.email,
        "sync_results": results
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
