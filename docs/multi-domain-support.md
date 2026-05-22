# Multi-Domain Support - Design Document

**Date:** 2026-05-03
**Status:** Implementation in progress
**Compatibility:** Fully backwards compatible

## Overview

Allow multiple public domains to point to the same physical 3x-ui node, generating separate VLESS URLs for each domain in client subscriptions. This enables better redundancy, load distribution, and domain-blocking circumvention.

## Architecture

### Key Principle: Dynamic URL Generation

- **Domain configuration stored** in database (`domains`, `node_domains` tables)
- **VLESS URLs generated on-the-fly** by subscription service
- **No URL storage** - URLs created fresh on each subscription request
- **Same client UUID** across all domains (client exists once on server)

### Database Schema

```sql
-- Domains table
CREATE TABLE domains (
    id SERIAL PRIMARY KEY,
    domain VARCHAR(255) NOT NULL UNIQUE,
    enabled BOOLEAN DEFAULT true
);

-- Node-Domain mapping (many-to-many)
CREATE TABLE node_domains (
    id SERIAL PRIMARY KEY,
    node_id INTEGER REFERENCES nodes(id),
    domain_id INTEGER REFERENCES domains(id),
    is_primary BOOLEAN DEFAULT false,
    enabled BOOLEAN DEFAULT true,
    label VARCHAR(50),  -- Display label (e.g., "cdn", "backup", "eu")
    UNIQUE(node_id, domain_id)
);
```

## How It Works

### Example Setup

**Physical Server:**
- node-germ1 at 100.64.0.10
- Client: `user@example.com`
- UUID: `550e8400-xxx` (created ONCE on server)

**Domain Configuration:**
```sql
-- Three domains pointing to same node
INSERT INTO domains (domain) VALUES
    ('myphonecloud.space'),
    ('backupcloud.online'),
    ('securestore.space');

-- Associate all three with node-germ1
INSERT INTO node_domains (node_id, domain_id, is_primary, label) VALUES
    (1, 1, true, 'primary'),      -- myphonecloud.space (primary)
    (1, 2, false, 'backup'),      -- backupcloud.online (backup)
    (1, 3, false, 'cdn');         -- securestore.space (cdn)
```

### Subscription Generation

**Step 1:** Query client keys (existing flow, unchanged)
```sql
SELECT * FROM keys WHERE client_id = 123;
-- Returns: node_id=1, uuid=550e8400-xxx, vless_url=vless://...@myphonecloud.space:443...
```

**Step 2:** For each key, check if node has multiple domains
```sql
SELECT d.domain, nd.label, nd.is_primary
FROM node_domains nd
JOIN domains d ON nd.domain_id = d.id
WHERE nd.node_id = 1 AND nd.enabled = true;
-- Returns: 3 domains
```

**Step 3:** Generate URL for each domain using `create_vless_url()`
```python
for domain_config in node_domains:
    vless_url = create_vless_url(
        node=node,
        client_uuid='550e8400-xxx',  # Same UUID!
        domain_override=domain_config['domain'],
        domain_label=domain_config['label']
    )
```

**Result:** 3 URLs in subscription (same client, different domains)
```
vless://550e8400-xxx@myphonecloud.space:443?...#node-germ1-gRPC-user@example.com
vless://550e8400-xxx@backupcloud.online:443?...#node-germ1-backup-gRPC-user@example.com
vless://550e8400-xxx@securestore.space:443?...#node-germ1-cdn-gRPC-user@example.com
```

## URL Remark Format

Server names appear differently based on label:

| Domain Type | Label | Remark Format | Client Sees |
|-------------|-------|---------------|-------------|
| Primary | `primary` or NULL | `node-name-protocol-email` | node-germ1-gRPC-user@example.com |
| Labeled | `cdn` | `node-name-label-protocol-email` | node-germ1-cdn-gRPC-user@example.com |
| Labeled | `backup` | `node-name-label-protocol-email` | node-germ1-backup-gRPC-user@example.com |

Users see **separate servers** in their client, but all connect to **same UUID** on **same physical server**.

## Backwards Compatibility

### Existing Clients (No Changes)
- Nodes with only one domain (current setup): **works exactly as before**
- Subscription service checks `node_domains`:
  - If empty: uses `node.domain` (fallback to primary domain)
  - Generates single URL (current behavior)
- **Zero impact** on existing deployments

### Migration Path
1. Run migration SQL (creates new tables, migrates existing domains)
2. Existing nodes automatically get primary domain mapping
3. No code changes needed for existing flow
4. Add additional domains via UI (opt-in feature)

## Use Cases

### 1. Domain Blocking Circumvention
```
myphonecloud.space blocked in country X
→ Client automatically tries backupcloud.online
→ Same server, different domain
```

### 2. Geographic Load Distribution
```
GeoDNS:
  myphonecloud.space → 1.2.3.4 (US users)
  backupcloud.online → 5.6.7.8 (EU users)

Both domains point to node-germ1 via different proxies
```

### 3. CDN Routing
```
cdn.example.com → Cloudflare → node-germ1
direct.example.com → Direct IP → node-germ1
```

## Reality Protocol Support

Works identically for both TLS and Reality inbounds:

```
# Reality + gRPC with 3 domains
vless://uuid@domain1.com:443?type=grpc&serviceName=/sync&security=reality&pbk=xxx&sni=m.vk.com...
vless://uuid@domain2.com:443?type=grpc&serviceName=/sync&security=reality&pbk=xxx&sni=m.vk.com...
vless://uuid@domain3.com:443?type=grpc&serviceName=/sync&security=reality&pbk=xxx&sni=m.vk.com...
```

**Key Point:** serviceName, Reality params (pbk, fp, sni, sid, spx) are **same** for all domains because they come from inbound configuration, not domain config.

## Implementation Status

### ✅ Completed
- [x] Database migration SQL
- [x] Updated `create_vless_url()` with domain_override and domain_label parameters

### 🔄 In Progress
- [ ] Helper function for multi-domain URL generation
- [ ] Subscription service updates

### 📋 To Do
- [ ] Domain management API endpoints
- [ ] Domain management UI
- [ ] Testing (backwards compatibility + multi-domain)
- [ ] Deployment

## API Endpoints (Planned)

```
GET    /api/domains                    # List all domains
POST   /api/domains                    # Create domain
DELETE /api/domains/{domain_id}        # Delete domain

GET    /api/nodes/{node_id}/domains    # List domains for node
POST   /api/nodes/{node_id}/domains    # Add domain to node
PUT    /api/nodes/{node_id}/domains/{domain_id}  # Update label/primary
DELETE /api/nodes/{node_id}/domains/{domain_id}  # Remove domain from node
```

## Testing Checklist

- [ ] Existing client with single domain → subscription unchanged
- [ ] Node with no node_domains entries → uses node.domain (fallback)
- [ ] Node with 3 domains → generates 3 URLs in subscription
- [ ] Reality inbound + multi-domain → Reality params same across URLs
- [ ] TLS inbound + multi-domain → serviceName same across URLs
- [ ] Primary domain URL has no label in remark
- [ ] Non-primary domains have label in remark
- [ ] Client connects successfully via all domain URLs

## Notes

- Port is always **443** (hardcoded)
- serviceName/path from **inbound config** (not domain-specific)
- Reality parameters from **inbound config** (not domain-specific)
- Only **domain** and **label** change per URL
- Client UUID **same** across all domains (one client on server)
