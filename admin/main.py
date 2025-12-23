"""Admin service for centralized 3x-ui management"""
import os
import uuid
import json
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

        # Find first VLESS inbound
        vless_inbound = None
        for inbound in inbounds:
            if inbound.get("protocol") == "vless":
                vless_inbound = inbound
                break

        if not vless_inbound:
            raise Exception("No VLESS inbound found on node")

        inbound_id = vless_inbound["id"]

        # Parse existing settings
        settings = json.loads(vless_inbound.get("settings", "{}"))
        clients_list = settings.get("clients", [])

        # Check if client already exists
        existing_client = None
        for c in clients_list:
            if c.get("email") == client.email:
                existing_client = c
                break

        if existing_client:
            # Update UUID if different
            if existing_client.get("id") != str(client_uuid):
                existing_client["id"] = str(client_uuid)
                existing_client["enable"] = client.enabled

                # Update inbound
                update_data = {
                    "id": inbound_id,
                    "settings": json.dumps(settings)
                }

                update_response = session.post(
                    f"{node.url}/panel/api/inbounds/update/{inbound_id}",
                    json=update_data,
                    verify=False,
                    timeout=10
                )

                if update_response.status_code != 200:
                    raise Exception(f"Failed to update client: {update_response.status_code}")
        else:
            # Add new client
            new_client = {
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

            clients_list.append(new_client)
            settings["clients"] = clients_list

            # Update inbound
            update_data = {
                "id": inbound_id,
                "settings": json.dumps(settings)
            }

            update_response = session.post(
                f"{node.url}/panel/api/inbounds/update/{inbound_id}",
                json=update_data,
                verify=False,
                timeout=10
            )

            if update_response.status_code != 200:
                raise Exception(f"Failed to add client: {update_response.status_code}")

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

        # Get inbounds
        inbounds_response = session.get(
            f"{node.url}/panel/api/inbounds/list",
            verify=False,
            timeout=10
        )

        if inbounds_response.status_code != 200:
            return False, f"Failed to get inbounds: {inbounds_response.status_code}"

        inbounds_data = inbounds_response.json()
        inbounds = inbounds_data.get("obj", [])

        # Remove client from all inbounds
        for inbound in inbounds:
            if inbound.get("protocol") != "vless":
                continue

            inbound_id = inbound["id"]
            settings = json.loads(inbound.get("settings", "{}"))
            clients_list = settings.get("clients", [])

            # Filter out this client
            new_clients = [c for c in clients_list if c.get("email") != client.email]

            if len(new_clients) < len(clients_list):
                # Client was removed, update inbound
                settings["clients"] = new_clients

                update_data = {
                    "id": inbound_id,
                    "settings": json.dumps(settings)
                }

                session.post(
                    f"{node.url}/panel/api/inbounds/update/{inbound_id}",
                    json=update_data,
                    verify=False,
                    timeout=10
                )

        # Delete keys from database
        db.query(Key).filter(
            Key.client_id == client.id,
            Key.node_id == node.id
        ).delete()
        db.commit()

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

    return {"id": node.id, "name": node.name, "url": node.url, "domain": node.domain}


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
    sub_url = os.getenv("SUBSCRIPTION_URL", "http://localhost:8001")

    return {
        "subscription_url": f"{sub_url}/sub/{client.email}"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
