# 3x-ui Centralized Subscription Manager

Centralized management system for multiple 3x-ui nodes with automatic client synchronization and subscription generation.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    Docker Compose Stack                       │
├──────────────────────────────────────────────────────────────┤
│  PostgreSQL:5432  │  Admin:8000  │  Subscription:8001        │
│                   │  (protected) │  (public read-only)        │
└──────────────────────────────────────────────────────────────┘
```

## Features

- ✅ **Centralized Management** - Manage all nodes and clients from one place
- ✅ **Automatic Sync** - Clients automatically created on all nodes
- ✅ **Subscription Endpoint** - Standard base64 encoded subscription format
- ✅ **Batch Operations** - Enable/disable/delete clients across all nodes
- ✅ **Security** - Separate admin and public services
- ✅ **Read-Only DB** - Subscription service uses read-only PostgreSQL user

## Quick Start

### 1. Create environment file

```bash
cp .env.example .env
# Edit .env and set your passwords
```

### 2. Start services

```bash
docker-compose up -d
```

### 3. Access admin panel

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
- View subscription links

**API Endpoints:**
- `POST /login` - Admin login
- `GET /api/nodes` - List nodes
- `POST /api/nodes` - Add node
- `DELETE /api/nodes/{id}` - Delete node
- `GET /api/clients` - List clients
- `POST /api/clients` - Add client (syncs to all nodes)
- `PUT /api/clients/{id}/enable` - Enable client on all nodes
- `PUT /api/clients/{id}/disable` - Disable client on all nodes
- `DELETE /api/clients/{id}` - Delete client from all nodes
- `GET /api/clients/{id}/subscription` - Get subscription link

### Subscription Service (Port 8001)

**Public Endpoint:**
- `GET /sub/{email}` - Get subscription (base64 encoded VLESS URLs)

**Example:**
```bash
curl http://localhost:8001/sub/user@example.com
```

Returns base64 encoded list of VLESS URLs (one per line).

## Database Schema

### nodes
- id, name, url, username, password, enabled, created_at

### clients
- id, email, enabled, created_at, updated_at

### keys
- id, client_id, node_id, inbound_id, uuid, vless_url, created_at

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

### 2. Add Clients

```
Admin UI → Clients → Add New Client
- Email: user@example.com
```

This automatically:
- Creates client in database
- Generates UUID
- Creates client on ALL enabled nodes via 3x-ui API
- Stores VLESS URLs in database

### 3. Get Subscription

```
Admin UI → Clients → Sub Link
```

Gives you: `http://localhost:8001/sub/user@example.com`

Clients add this URL to their VPN app (v2rayNG, Hiddify, etc.)

### 4. Enable/Disable Clients

```
Admin UI → Clients → Enable/Disable
```

This updates the client on ALL nodes simultaneously.

### 5. Delete Clients

```
Admin UI → Clients → Delete
```

This removes the client from ALL nodes and deletes all keys.

## Environment Variables

**Admin Service:**
- `DATABASE_URL` - PostgreSQL connection (full access)
- `ADMIN_PASSWORD` - Admin panel password
- `SUBSCRIPTION_URL` - Public subscription service URL

**Subscription Service:**
- `DATABASE_URL_READONLY` - PostgreSQL read-only connection

## Security

**Separation:**
- Admin service (port 8000) - Can be behind firewall/VPN
- Subscription service (port 8001) - Public facing

**Database Access:**
- Admin service - Full read/write
- Subscription service - Read-only PostgreSQL user

**Authentication:**
- Admin service - Password protected
- Subscription service - Public (no sensitive operations)

## Production Deployment

### 1. Change passwords in .env

```bash
DB_PASSWORD=strong_random_password
ADMIN_PASSWORD=strong_admin_password
```

### 2. Use HTTPS

Put admin and subscription services behind nginx with SSL:

```nginx
# Admin (restrict access)
server {
    listen 443 ssl;
    server_name admin.example.com;

    location / {
        proxy_pass http://localhost:8000;
    }
}

# Subscription (public)
server {
    listen 443 ssl;
    server_name sub.example.com;

    location / {
        proxy_pass http://localhost:8001;
    }
}
```

### 3. Update SUBSCRIPTION_URL in .env

```bash
SUBSCRIPTION_URL=https://sub.example.com
```

### 4. Firewall rules

```bash
# Only allow admin access from VPN
ufw allow from 10.0.0.0/8 to any port 8000

# Allow public subscription access
ufw allow 8001
```

## Upgrading from Previous Versions

If you're upgrading from a version without the `domain` field:

### 1. Pull latest changes

```bash
cd /opt/central
git pull
```

### 2. Run migration

```bash
docker compose exec postgres psql -U postgres -d xui_central -f /migrations/001_add_domain_column.sql
```

### 3. Verify and fix domains

```bash
docker compose exec postgres psql -U postgres -d xui_central -c "SELECT id, name, url, domain FROM nodes;"
```

Update incorrect domains:

```bash
docker compose exec postgres psql -U postgres -d xui_central -c "UPDATE nodes SET domain = 'vienna.example.com' WHERE id = 1;"
```

### 4. Restart services

```bash
docker compose restart admin subscription
```

See `migrations/README.md` for detailed migration instructions.

## Troubleshooting

### Check services

```bash
docker-compose ps
docker-compose logs admin
docker-compose logs subscription
docker-compose logs postgres
```

### Database access

```bash
docker-compose exec postgres psql -U postgres -d xui_central

# List tables
\dt

# Check clients
SELECT * FROM clients;

# Check keys
SELECT * FROM keys;
```

### Test subscription

```bash
curl http://localhost:8001/sub/user@example.com | base64 -d
```

Should return VLESS URLs (one per line).

## File Structure

```
central/
├── docker-compose.yml       # Orchestration
├── .env                     # Configuration
├── init.sql                 # Database schema
├── admin/                   # Admin service
│   ├── Dockerfile
│   ├── main.py             # FastAPI app
│   ├── database.py         # SQLAlchemy models
│   ├── requirements.txt
│   └── templates/
│       ├── login.html      # Login page
│       └── index.html      # Admin UI
└── subscription/           # Subscription service
    ├── Dockerfile
    ├── main.py            # Simple FastAPI app
    ├── database.py        # Read-only models
    └── requirements.txt
```

## API Integration

The system uses the 3x-ui panel API to sync clients. It:

1. Logs in to each node
2. Gets list of VLESS inbounds
3. Adds/updates/deletes clients via API
4. Stores VLESS URLs in database

This ensures clients work immediately on all nodes.

## License

MIT
