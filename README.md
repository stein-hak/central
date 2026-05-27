# 3x-ui Centralized Subscription Manager

Centralized management system for multiple 3x-ui nodes with automatic client synchronization, subscription generation, and multi-domain support.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    Docker Compose Stack                       │
├──────────────────────────────────────────────────────────────┤
│  PostgreSQL:5432  │  Redis:6379  │  Admin:8000  │  Sub:8001  │
│                   │  (sessions)  │  (protected) │  (public)   │
└──────────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────────┐
│              Multiple 3x-ui Nodes (via Tailscale)             │
│    node-1    │    node-2    │    node-3   │    node-N...     │
└──────────────────────────────────────────────────────────────┘
```

## Features

### Core Management
- ✅ **Centralized Management** - Manage all nodes and clients from one web interface
- ✅ **Automatic Sync** - Clients automatically created/updated/deleted on all nodes in parallel
- ✅ **Batch Operations** - Create/enable/disable/delete multiple clients simultaneously
- ✅ **x-ui-client Library** - Unified API client with CSRF support for v2.8.x and v3.x panels

### Performance & Scalability
- 🚀 **True Batch Processing** - Create 20 clients on 13 nodes with 13 API calls (not 260!)
- 🚀 **Parallel Execution** - All node operations execute concurrently using asyncio
- 🚀 **Global Timeouts** - 15-second request timeout prevents hanging on offline nodes
- ⚡ **Redis Sessions** - Multi-worker support with Redis-backed session storage

### Subscription Management
- 📱 **Subscription Endpoint** - Standard base64 encoded subscription format
- 🌐 **Multi-Domain Support** - Rotate between multiple subscription domains (anti-block)
- 🔄 **Auto-Update** - Configurable update interval (default: 24 hours)
- 📊 **Traffic Monitoring** - Real-time client statistics across all nodes

### Security
- 🔐 **Separate Services** - Admin (protected) and subscription (public) services
- 🔐 **Read-Only DB** - Subscription service uses read-only PostgreSQL user
- 🔐 **Session Management** - Secure cookie-based authentication with Redis
- 🔐 **IP Limit Control** - Set concurrent IP limits per client across all nodes

## Quick Start

### Prerequisites

- Docker & Docker Compose
- PostgreSQL (via Docker)
- Redis (via Docker)
- Access to 3x-ui nodes (v2.8.x or v3.x)

### 1. Clone repository

```bash
git clone https://github.com/stein-hak/central.git
cd central
```

### 2. Initialize submodules

```bash
git submodule update --init --recursive
```

This will pull the `x-ui-client-lib` library into `admin/x_ui_client/`.

### 3. Create environment file

```bash
cp .env.example .env
# Edit .env and set your passwords
```

### 4. Start services

```bash
docker-compose up -d
```

### 5. Access admin panel

```
http://localhost:8000
Default password: admin123 (change in .env)
```

## Services

### Admin Service (Port 8000)

**Web Interface:**
- Login with admin password
- Manage nodes (add/delete 3x-ui panels)
- Manage clients (add/enable/disable/delete)
- Batch create clients (up to 100 at once)
- View subscription links
- Configure subscription domains
- Monitor node statistics

**API Endpoints:**

**Authentication:**
- `POST /login` - Admin login
- `GET /logout` - Logout

**Nodes:**
- `GET /api/nodes` - List all nodes
- `POST /api/nodes` - Add node
- `DELETE /api/nodes/{id}` - Delete node
- `GET /api/nodes/stats/all` - Get stats from all nodes (parallel)
- `GET /api/nodes/{id}/stats` - Get specific node stats

**Clients:**
- `GET /api/clients` - List clients
- `POST /api/clients` - Add single client (syncs to all nodes)
- `POST /api/batch/clients` - **Batch create clients** (syncs all clients to all nodes in one operation per node)
- `PUT /api/clients/{id}/enable` - Enable client on all nodes
- `PUT /api/clients/{id}/disable` - Disable client on all nodes
- `DELETE /api/clients/{id}` - Delete client from all nodes
- `GET /api/clients/{id}/subscription` - Get subscription link
- `GET /api/clients/{id}/limit` - Get client IP limit
- `PUT /api/clients/{id}/limit` - Update client IP limit on all nodes

**Subscription Domains:**
- `GET /api/subscription-domains` - List subscription domains
- `POST /api/subscription-domains` - Add domain
- `PUT /api/subscription-domains/{id}` - Update domain
- `DELETE /api/subscription-domains/{id}` - Delete domain

### Subscription Service (Port 8001)

**Public Endpoint:**
- `GET /{email}` - Get subscription (base64 encoded VLESS URLs)

**Example:**
```bash
curl http://localhost:8001/user@example.com
```

Returns base64 encoded list of VLESS URLs (one per line).

**Response Headers:**
- `profile-update-interval: 24` - Auto-update every 24 hours
- `profile-title: Your VPN Service` - Brand name in VPN clients
- `subscription-userinfo: upload=0; download=0; total=0; expire=0` - Traffic info

## Database Schema

### nodes
- id, name, url, domain, username, password, enabled, created_at

### clients
- id, email, enabled, created_at, updated_at

### keys
- id, client_id, node_id, inbound_id, uuid, vless_url, manual, created_at

### subscription_domains
- id, domain, enabled, is_primary, notes, created_at, updated_at

## Usage Workflow

### 1. Add Nodes

```
Admin UI → Nodes → Add New Node
- Name: node-vienna
- API URL: https://100.64.1.5:2053 (Tailscale IP for management)
- Public Domain: vienna.example.com (for VLESS URLs)
- Username: admin
- Password: password123
```

**Important**:
- **API URL** - Internal address for managing the node (Tailscale IP, private network, etc.)
- **Public Domain** - Public-facing domain used in generated VLESS links for clients

### 2. Configure Subscription Domains

```
Admin UI → Subscription Tab → Add Domain
- Domain: sub1.example.com
- Enabled: Yes
- Primary: Yes (used by default)
- Notes: Main subscription domain

Add backup domains:
- sub2.example.com
- sub3.example.com
```

The system will automatically use the primary domain, or fallback to any enabled domain if primary is blocked.

### 3. Add Clients (Single)

```
Admin UI → Clients → Add New Client
- Email: user@example.com
```

This automatically:
- Creates client in database
- Generates UUID
- Creates client on ALL enabled nodes via x-ui-client API (parallel)
- Stores VLESS URLs in database

### 4. Add Clients (Batch)

```
Admin UI → Clients → Batch Create
- Seed: client
- Count: 20
- Reality Only: No (for legacy gRPC+XHTTP inbounds)
```

This creates 20 clients (client-a3f9b2e1, client-7d2c8f4a, ...) and syncs ALL 20 clients to ALL nodes in ONE batch API call per node. **Much faster than single client creation!**

**Performance:**
- Old: 20 clients × 13 nodes = 260 sequential API calls (~3-5 minutes)
- New: 13 parallel batch API calls (~15-30 seconds)
- **20x faster!**

### 5. Get Subscription

```
Admin UI → Clients → Sub Link
```

Gives you: `https://sub1.example.com/user@example.com`

Clients add this URL to their VPN app (v2rayNG, Hiddify, etc.)

### 6. Enable/Disable Clients

```
Admin UI → Clients → Enable/Disable
```

This updates the client on ALL nodes simultaneously using the x-ui-client library.

### 7. Set IP Limits

```
Admin UI → Clients → Set IP Limit
- Limit: 2 (allow max 2 concurrent connections)
```

This updates the IP limit on ALL nodes simultaneously.

### 8. Delete Clients

```
Admin UI → Clients → Delete
```

This removes the client from ALL nodes and deletes all keys.

## Environment Variables

**Admin Service:**
- `DATABASE_URL` - PostgreSQL connection (full access)
- `ADMIN_PASSWORD` - Admin panel password
- `SUBSCRIPTION_URL` - Public subscription service URL (deprecated - use subscription_domains table)
- `REDIS_URL` - Redis connection for sessions

**Subscription Service:**
- `DATABASE_URL_READONLY` - PostgreSQL read-only connection
- `PROFILE_TITLE` - Brand name shown in VPN clients (default: "VPN Service")
- `ENABLE_FP_RANDOMIZATION` - Enable fingerprint randomization (default: false)

## x-ui-client Library

The system uses the `x-ui-client-lib` (git submodule) for all 3x-ui API interactions.

**Features:**
- ✅ Supports both v2.8.x and v3.x panels
- ✅ Automatic CSRF token handling
- ✅ Global 15-second timeout (prevents hanging on offline nodes)
- ✅ Batch operations (sync multiple clients in one API call)
- ✅ Session persistence

**Location:** `admin/x_ui_client/` (git submodule)

**Repository:** https://github.com/stein-hak/x-ui-client-lib

## Performance Optimizations

### Batch Client Creation

**Before (Sequential):**
```python
for i in range(20):
    create_client(i)
    sync_to_all_nodes(client_i)  # 13 API calls per iteration
# Total: 20 × 13 = 260 API calls
```

**After (True Batch):**
```python
clients = [create_client(i) for i in range(20)]
batch_sync_all_clients_to_all_nodes(clients)  # 1 API call per node
# Total: 13 API calls (ONE batch per node)
```

### Parallel Node Operations

All node operations use `asyncio.gather()` to execute in parallel:
- Client creation
- Client updates (enable/disable/IP limit)
- Client deletion
- Statistics gathering
- Batch operations

### Global Timeouts

All x-ui-client requests have a 15-second timeout, preventing the system from hanging on offline/unreachable nodes.

## Security

**Separation:**
- Admin service (port 8000) - Should be behind firewall/VPN (Tailscale recommended)
- Subscription service (port 8001) - Public facing (can be behind CDN)

**Database Access:**
- Admin service - Full read/write (postgres user)
- Subscription service - Read-only (sub_readonly user)

**Authentication:**
- Admin service - Password protected with Redis sessions
- Subscription service - Public (no authentication, read-only operations)

**Session Storage:**
- Redis-backed sessions for multi-worker support
- Secure cookie-based authentication
- Fallback to in-memory sessions if Redis unavailable

## Production Deployment

### 1. Change passwords in .env

```bash
DB_PASSWORD=strong_random_password
ADMIN_PASSWORD=strong_admin_password
REDIS_PASSWORD=strong_redis_password
```

### 2. Use HTTPS with reverse proxy

Put services behind nginx with SSL:

```nginx
# Admin (restrict access via Tailscale/VPN)
server {
    listen 443 ssl;
    server_name admin.example.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    # Only allow Tailscale IPs
    allow 100.64.0.0/10;
    deny all;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

# Subscription (public, can be behind CDN)
server {
    listen 443 ssl;
    server_name sub1.example.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://localhost:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### 3. Configure subscription domains

Add multiple domains to the subscription_domains table for rotation:
- sub1.example.com (primary)
- sub2.example.com (backup)
- sub3.example.com (backup)

### 4. Firewall rules

```bash
# Only allow admin access from Tailscale network
ufw allow from 100.64.0.0/10 to any port 8000

# Allow public subscription access (or restrict to CDN IPs)
ufw allow 8001

# Allow Redis only from localhost
ufw deny 6379
```

### 5. Monitoring

Monitor node health and subscription domain availability. Rotate domains if one gets blocked.

## Upgrading

### Update code

```bash
cd /opt/central
git pull origin main
git submodule update --remote
```

### Run migrations

```bash
# Check for new migrations
ls migrations/

# Run new migrations (example)
docker compose exec postgres psql -U postgres -d xui_central -f /migrations/004_add_subscription_domains.sql
```

### Restart services

```bash
docker compose restart admin subscription
```

### Check logs

```bash
docker compose logs -f admin
docker compose logs -f subscription
```

## Troubleshooting

### Check services

```bash
docker compose ps
docker compose logs admin
docker compose logs subscription
docker compose logs postgres
docker compose logs redis
```

### Test Redis connection

```bash
docker exec xui-central-redis redis-cli -a your_redis_password ping
# Should return: PONG
```

### Database access

```bash
docker compose exec postgres psql -U postgres -d xui_central

# List tables
\dt

# Check clients
SELECT * FROM clients;

# Check keys
SELECT * FROM keys;

# Check subscription domains
SELECT * FROM subscription_domains;
```

### Test subscription

```bash
curl http://localhost:8001/user@example.com | base64 -d
```

Should return VLESS URLs (one per line).

### Test batch creation performance

```bash
# Watch admin logs while creating batch
docker compose logs -f admin

# Should see:
# 🚀 BATCH creating 20 clients on 13 nodes IN PARALLEL...
# ⏱️  [node-1] Starting BATCH creation of 20 clients...
# ✅ [node-1] Completed in 12.5s - 40 keys created for 20 clients
```

### Check x-ui-client library

```bash
# Verify submodule is initialized
ls admin/x_ui_client/client.py

# Check library version
cd admin/x_ui_client && git log -1 --oneline
```

## File Structure

```
central/
├── docker-compose.yml           # Orchestration
├── .env                         # Configuration (create from .env.example)
├── .gitmodules                  # Git submodules configuration
├── init.sql                     # Database schema
├── migrations/                  # Database migrations
│   ├── 001_add_domain_column.sql
│   ├── 002_add_keys_manual_flag.sql
│   ├── 003_add_node_domain_table.sql
│   └── 004_add_subscription_domains.sql
├── admin/                       # Admin service
│   ├── Dockerfile
│   ├── main.py                 # FastAPI app
│   ├── database.py             # SQLAlchemy models
│   ├── requirements.txt
│   ├── x_ui_client/            # Git submodule (x-ui-client-lib)
│   │   ├── client.py           # XUIClient class
│   │   ├── exceptions.py
│   │   └── __init__.py
│   └── templates/
│       ├── login.html          # Login page
│       └── index.html          # Admin UI
├── subscription/               # Subscription service
│   ├── Dockerfile
│   ├── main.py                # Simple FastAPI app
│   ├── database.py            # Read-only models
│   └── requirements.txt
└── docs/                      # Documentation
    ├── legacy-http-calls-audit.md
    └── ...
```

## API Integration

The system uses the x-ui-client library to communicate with 3x-ui panels. It:

1. Authenticates to each node (supports v2.8.x and v3.x)
2. Gets list of VLESS inbounds
3. Uses batch operations to sync clients efficiently
4. Handles CSRF tokens automatically
5. Times out after 15 seconds on offline nodes
6. Stores VLESS URLs in database

This ensures clients work immediately on all nodes with optimal performance.

## Contributing

1. Fork the repository
2. Create your feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

## License

MIT

## Related Projects

- [x-ui-client-lib](https://github.com/stein-hak/x-ui-client-lib) - Python API client for 3x-ui
- [3x-ui](https://github.com/MHSanaei/3x-ui) - Web panel for Xray

## Support

For issues and questions:
- GitHub Issues: https://github.com/stein-hak/central/issues
- Documentation: See `docs/` directory
