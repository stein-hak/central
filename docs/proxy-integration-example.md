# Proxy Integration Example

## Overview

This guide shows how to configure proxy servers (HAProxy front-end) in the central system to provide client access to backend nodes with fake SNI obfuscation.

## Architecture

```
Client subscription contains:
  vless://uuid@phone-bliss.tech:443?sni=vk.com#US-Server-XHTTP-user@example.com
       ↓
  phone-bliss.tech (72.56.235.80) - HAProxy proxy
       ↓ balance source
  ├─ node-net0 (84.32.190.119) - backend node
  └─ node-net1 (84.32.59.99) - backend node
```

## Step 1: Run Migration

```bash
cd /opt/central
docker compose exec postgres psql -U postgres -d xui_central -f /migrations/005_add_proxies.sql
```

## Step 2: Add Proxy

```sql
INSERT INTO proxies (name, domain, fake_snis, sni_strategy, enabled, notes)
VALUES (
    'US-Server',
    'phone-bliss.tech',
    ARRAY['vk.com', 'mail.ru', 'ok.ru', 'google.com'],
    'random',
    TRUE,
    'Main US proxy with HAProxy load balancing'
);
```

## Step 3: Link Backend Nodes to Proxy

Assuming you have nodes with IDs 5 and 6 (node-net0, node-net1):

```sql
-- Add backend nodes to proxy
INSERT INTO proxy_backends (proxy_id, node_id, weight, enabled)
VALUES
    (1, 5, 1, TRUE),  -- node-net0
    (1, 6, 1, TRUE);  -- node-net1

-- Mark nodes as proxy_only (hide direct access)
UPDATE nodes SET proxy_only = TRUE WHERE id IN (5, 6);
```

## Step 4: Verify Configuration

```sql
-- Check proxy setup
SELECT
    p.name AS proxy_name,
    p.domain,
    p.fake_snis,
    p.sni_strategy,
    n.name AS backend_node,
    pb.weight,
    n.proxy_only
FROM proxies p
JOIN proxy_backends pb ON p.id = pb.proxy_id
JOIN nodes n ON pb.node_id = n.id
WHERE p.enabled = TRUE AND pb.enabled = TRUE;
```

Expected output:
```
 proxy_name |      domain       |           fake_snis           | sni_strategy | backend_node | weight | proxy_only
------------+-------------------+-------------------------------+--------------+--------------+--------+------------
 US-Server  | phone-bliss.tech  | {vk.com,mail.ru,ok.ru,...}   | random       | node-net0    | 1      | t
 US-Server  | phone-bliss.tech  | {vk.com,mail.ru,ok.ru,...}   | random       | node-net1    | 1      | t
```

## Step 5: Test Subscription

```bash
# Get subscription for a test client
curl http://localhost:8001/user@example.com | base64 -d
```

Expected output (URLs with proxy domain and fake SNI):
```
vless://uuid-net0@phone-bliss.tech:443?sni=vk.com&type=xhttp&security=reality...#US-Server-XHTTP-user@example.com
vless://uuid-net1@phone-bliss.tech:443?sni=mail.ru&type=xhttp&security=reality...#US-Server-XHTTP-user@example.com
```

Note:
- Both URLs use `phone-bliss.tech` (proxy domain)
- Different fake SNI for each backend (vk.com, mail.ru)
- Remark shows `US-Server` (proxy name, not node name)
- Backend node IPs (84.32.*) are hidden

## SNI Strategies

### random (default)
Each node gets a random fake SNI from the array. Consistent per node_id.
```sql
UPDATE proxies SET sni_strategy = 'random' WHERE id = 1;
```

### fixed
All nodes use the first fake SNI from array.
```sql
UPDATE proxies SET sni_strategy = 'fixed' WHERE id = 1;
```

### rotate
Fake SNI rotates daily based on day of year.
```sql
UPDATE proxies SET sni_strategy = 'rotate' WHERE id = 1;
```

## Multiple Proxies Example

You can configure the same backend nodes behind multiple proxies (different domains):

```sql
-- Add second proxy with different domain
INSERT INTO proxies (name, domain, fake_snis, sni_strategy)
VALUES (
    'US-Server-Alt',
    'api.phone-bliss.tech',
    ARRAY['ok.ru', 'yandex.ru'],
    'fixed'
);

-- Link same backend nodes to second proxy
INSERT INTO proxy_backends (proxy_id, node_id)
VALUES (2, 5), (2, 6);
```

Client will now receive 4 URLs:
- 2 URLs through `phone-bliss.tech` (proxy 1)
- 2 URLs through `api.phone-bliss.tech` (proxy 2)

All pointing to the same backend nodes, providing redundancy and flexibility.

## Hybrid Nodes (Proxy + Direct)

If you want a node to be accessible both through proxy AND directly:

```sql
-- Allow direct access (in addition to proxy access)
UPDATE nodes SET proxy_only = FALSE WHERE id = 7;

-- Add node to proxy
INSERT INTO proxy_backends (proxy_id, node_id) VALUES (1, 7);
```

Subscription will contain:
- Proxy URL: `vless://...@phone-bliss.tech:443?sni=vk.com#US-Server-XHTTP-...`
- Direct URL: `vless://...@node-domain.com:443#node-name-XHTTP-...`

## Troubleshooting

### No proxy URLs in subscription

Check:
1. Proxy enabled: `SELECT * FROM proxies WHERE enabled = TRUE;`
2. Proxy backends enabled: `SELECT * FROM proxy_backends WHERE enabled = TRUE;`
3. Client has keys on backend nodes: `SELECT * FROM keys WHERE client_id = X AND node_id IN (5,6);`

### Direct URLs still showing for proxy_only nodes

Check:
1. Node proxy_only flag: `SELECT id, name, proxy_only FROM nodes;`
2. Restart subscription service: `docker compose restart subscription`

### Fake SNI not appearing

Check:
1. Proxy has fake_snis: `SELECT name, fake_snis FROM proxies;`
2. Check subscription service logs: `docker compose logs -f subscription`

## Integration with HAProxy

The proxy configuration in central should match your HAProxy setup:

```haproxy
# HAProxy config on 72.56.235.80
backend remote_backend
    mode tcp
    balance source
    hash-type consistent
    server backend_net0 84.32.190.119:443 check
    server backend_net1 84.32.59.99:443 check
```

- `balance source` ensures client IP consistency (matches proxy_backends weight)
- HAProxy routes based on SNI to backend nodes
- Client sees only proxy domain, not backend IPs
