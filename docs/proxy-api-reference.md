# Proxy Management API Reference

API endpoints for managing proxy servers and their backend nodes.

## Authentication

All endpoints require admin authentication via session cookie.

## Base URL

```
http://localhost:8000/api/proxies
```

---

## Endpoints

### GET /api/proxies

Get all proxies with their backend nodes.

**Response:**
```json
[
  {
    "id": 1,
    "name": "US-Server",
    "domain": "phone-bliss.tech",
    "fake_snis": ["vk.com", "mail.ru", "ok.ru"],
    "sni_strategy": "random",
    "enabled": true,
    "notes": "Main US proxy",
    "backends": [
      {
        "id": 1,
        "node_id": 5,
        "node_name": "node-net0",
        "weight": 1,
        "enabled": true
      },
      {
        "id": 2,
        "node_id": 6,
        "node_name": "node-net1",
        "weight": 1,
        "enabled": true
      }
    ],
    "created_at": "2025-06-04T10:30:00"
  }
]
```

**Example:**
```bash
curl -X GET http://localhost:8000/api/proxies \
  -H "Cookie: session=your_session_cookie"
```

---

### POST /api/proxies

Create a new proxy.

**Parameters (Form Data):**
- `name` (required): Display name (e.g., "US-Server")
- `domain` (required): Public domain (e.g., "phone-bliss.tech")
- `fake_snis` (optional): Comma-separated list of fake SNI domains (e.g., "vk.com,mail.ru,ok.ru")
- `sni_strategy` (optional): Strategy for selecting SNI - "random" (default), "fixed", or "rotate"
- `enabled` (optional): Boolean, default true
- `notes` (optional): Additional notes

**Response:**
```json
{
  "success": true,
  "proxy": {
    "id": 1,
    "name": "US-Server",
    "domain": "phone-bliss.tech",
    "fake_snis": ["vk.com", "mail.ru", "ok.ru"],
    "sni_strategy": "random",
    "enabled": true
  }
}
```

**Example:**
```bash
curl -X POST http://localhost:8000/api/proxies \
  -H "Cookie: session=your_session_cookie" \
  -F "name=US-Server" \
  -F "domain=phone-bliss.tech" \
  -F "fake_snis=vk.com,mail.ru,ok.ru,google.com" \
  -F "sni_strategy=random" \
  -F "notes=Main US proxy with HAProxy"
```

---

### PUT /api/proxies/{proxy_id}

Update an existing proxy.

**Parameters (Form Data):**
- `name` (required): Display name
- `domain` (required): Public domain
- `fake_snis` (optional): Comma-separated list
- `sni_strategy` (optional): "random", "fixed", or "rotate"
- `enabled` (optional): Boolean
- `notes` (optional): Additional notes

**Response:**
```json
{
  "success": true
}
```

**Example:**
```bash
curl -X PUT http://localhost:8000/api/proxies/1 \
  -H "Cookie: session=your_session_cookie" \
  -F "name=US-Server-Updated" \
  -F "domain=phone-bliss.tech" \
  -F "fake_snis=vk.com,ok.ru" \
  -F "sni_strategy=fixed"
```

---

### DELETE /api/proxies/{proxy_id}

Delete a proxy (cascade deletes all proxy_backends).

**Response:**
```json
{
  "success": true
}
```

**Example:**
```bash
curl -X DELETE http://localhost:8000/api/proxies/1 \
  -H "Cookie: session=your_session_cookie"
```

---

### GET /api/proxies/{proxy_id}/backends

Get backend nodes for a specific proxy.

**Response:**
```json
[
  {
    "id": 1,
    "node_id": 5,
    "node_name": "node-net0",
    "weight": 1,
    "enabled": true,
    "created_at": "2025-06-04T10:30:00"
  },
  {
    "id": 2,
    "node_id": 6,
    "node_name": "node-net1",
    "weight": 1,
    "enabled": true,
    "created_at": "2025-06-04T10:31:00"
  }
]
```

**Example:**
```bash
curl -X GET http://localhost:8000/api/proxies/1/backends \
  -H "Cookie: session=your_session_cookie"
```

---

### POST /api/proxies/{proxy_id}/backends

Add a backend node to a proxy.

**Parameters (Form Data):**
- `node_id` (required): ID of the node to add
- `weight` (optional): Weight for load balancing, default 1
- `enabled` (optional): Boolean, default true

**Response:**
```json
{
  "success": true,
  "backend": {
    "id": 3,
    "proxy_id": 1,
    "node_id": 7,
    "node_name": "node-net2",
    "weight": 1,
    "enabled": true
  }
}
```

**Example:**
```bash
curl -X POST http://localhost:8000/api/proxies/1/backends \
  -H "Cookie: session=your_session_cookie" \
  -F "node_id=7" \
  -F "weight=1" \
  -F "enabled=true"
```

---

### DELETE /api/proxies/{proxy_id}/backends/{backend_id}

Remove a backend node from a proxy.

**Response:**
```json
{
  "success": true
}
```

**Example:**
```bash
curl -X DELETE http://localhost:8000/api/proxies/1/backends/3 \
  -H "Cookie: session=your_session_cookie"
```

---

## Complete Workflow Example

### 1. Create a Proxy

```bash
curl -X POST http://localhost:8000/api/proxies \
  -H "Cookie: session=$SESSION" \
  -F "name=US-Server" \
  -F "domain=phone-bliss.tech" \
  -F "fake_snis=vk.com,mail.ru,ok.ru,google.com" \
  -F "sni_strategy=random" \
  -F "notes=Main US proxy"
```

Response:
```json
{"success": true, "proxy": {"id": 1, ...}}
```

### 2. Add Backend Nodes

```bash
# Add node-net0
curl -X POST http://localhost:8000/api/proxies/1/backends \
  -H "Cookie: session=$SESSION" \
  -F "node_id=5" \
  -F "weight=1"

# Add node-net1
curl -X POST http://localhost:8000/api/proxies/1/backends \
  -H "Cookie: session=$SESSION" \
  -F "node_id=6" \
  -F "weight=1"
```

### 3. Mark Nodes as Proxy-Only

```bash
# Update node-net0
curl -X PUT http://localhost:8000/api/nodes/5 \
  -H "Cookie: session=$SESSION" \
  -F "name=node-net0" \
  -F "url=https://100.64.0.36:2053" \
  -F "domain=phone-bliss.tech" \
  -F "username=admin" \
  -F "password=pass" \
  -F "proxy_only=true"

# Update node-net1
curl -X PUT http://localhost:8000/api/nodes/6 \
  -H "Cookie: session=$SESSION" \
  -F "name=node-net1" \
  -F "url=https://100.64.0.40:2053" \
  -F "domain=phone-bliss.tech" \
  -F "username=admin" \
  -F "password=pass" \
  -F "proxy_only=true"
```

### 4. Verify Configuration

```bash
curl -X GET http://localhost:8000/api/proxies \
  -H "Cookie: session=$SESSION"
```

### 5. Test Subscription

```bash
curl http://localhost:8001/user@example.com | base64 -d
```

Expected: VLESS URLs with `phone-bliss.tech` domain and fake SNI.

---

## Error Responses

**404 Not Found:**
```json
{
  "detail": "Proxy not found"
}
```

**400 Bad Request:**
```json
{
  "detail": "Proxy with this name already exists"
}
```

**401 Unauthorized:**
```json
{
  "detail": "Not authenticated"
}
```

---

## Notes

- All timestamps are in ISO 8601 format
- Deleting a proxy cascades to all its backends
- `fake_snis` can be empty (will use proxy domain as SNI)
- `sni_strategy`:
  - **random**: Random SNI per node (consistent per node_id)
  - **fixed**: Always use first SNI from array
  - **rotate**: Daily rotation based on day of year
- Backend `weight` is used for HAProxy weighted load balancing
