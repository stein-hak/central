# 3x-ui Central - Documentation

This directory contains comprehensive documentation for the 3x-ui central subscription service and routing implementation.

## Documents

### 📱 [HAPP_ROUTING.md](./HAPP_ROUTING.md)
**Complete guide to Happ VPN client routing**

- How Happ routing differs from v2rayTUN
- Complete routing profile format with all required fields
- QR code and deep link generation
- Common errors and troubleshooting
- Geosite/GeoIP resources

**Key Discovery:** Happ does NOT support HTTP routing headers - requires QR codes or deep links.

### 🔄 [CLIENT_AWARE_ROUTING.md](./CLIENT_AWARE_ROUTING.md)
**Implementation guide for client-aware routing in subscription service**

- Architecture and implementation details
- User-Agent detection and logging
- Format differences between Happ and v2rayTUN
- Testing procedures and debugging
- Performance considerations

## Quick Reference

### Routing Delivery Methods by Client

| Client | Method | Format | Status |
|--------|--------|--------|--------|
| **v2rayTUN** (Android/iOS) | HTTP `routing` header | V2Ray routing object | ✅ Working |
| **Happ** (iOS) | QR code / Deep link | Happ profile JSON | ✅ Working |
| **Streisand** | Unknown | Not tested | ❓ Unknown |

### Current Test Users

Routing is enabled for these clients:
- `stein`
- `Client-40337230`

### Key Files

```
/home/stein/python/3x-ui/central/
├── subscription/main.py              # Subscription service with client-aware routing
├── happ-routing-direct-russia.png    # QR code for Happ routing (6.7KB)
├── happ-routing-direct-russia.txt    # Deep link and documentation
└── docs/
    ├── README.md                      # This file
    ├── HAPP_ROUTING.md                # Happ routing complete guide
    └── CLIENT_AWARE_ROUTING.md        # Implementation documentation
```

## Common Tasks

### Generate Happ QR Code

```python
import json
import base64
import qrcode

# Complete routing profile (all fields required!)
routing_profile = {
    "Name": "Direct Russia",
    "GlobalProxy": "true",
    "RemoteDNSType": "DoH",
    "RemoteDNSDomain": "https://cloudflare-dns.com/dns-query",
    "RemoteDNSIP": "1.1.1.1",
    "DomesticDNSType": "DoH",
    "DomesticDNSDomain": "https://dns.google/dns-query",
    "DomesticDNSIP": "8.8.8.8",
    "Geoipurl": "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat",
    "Geositeurl": "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat",
    "LastUpdated": "",
    "DnsHosts": {"cloudflare-dns.com": "1.1.1.1", "dns.google": "8.8.8.8"},
    "DirectSites": ["get-myip.com", "geosite:category-ru"],
    "DirectIp": ["geoip:ru", "geoip:private"],
    "ProxySites": [],
    "ProxyIp": [],
    "BlockSites": [],
    "BlockIp": [],
    "DomainStrategy": "IPIfNonMatch",
    "FakeDNS": "false"
}

# Generate deep link
routing_json = json.dumps(routing_profile, separators=(',', ':'))
routing_b64 = base64.b64encode(routing_json.encode()).decode()
deep_link = f"happ://routing/onadd/{routing_b64}"

# Generate QR code
qr = qrcode.QRCode(version=None, box_size=10, border=4)
qr.add_data(deep_link)
qr.make(fit=True)
qr.make_image(fill_color="black", back_color="white").save("happ-routing.png")
```

### Test Subscription with Different Clients

```bash
# v2rayTUN
curl -H "User-Agent: v2raytun/1.0" https://gorillaerror.com/sub/stein -v

# Happ
curl -H "User-Agent: Happ/2.1.0" https://gorillaerror.com/sub/stein -v

# Decode routing header
echo "BASE64_STRING" | base64 -d | jq .
```

### Check Subscription Logs

```bash
# View real-time logs
docker logs -f subscription-service

# Search for specific client
docker logs subscription-service | grep "User-Agent: Happ"
```

## Format Comparison

### v2rayTUN Format (HTTP Header)

```json
{
  "domainStrategy": "AsIs",
  "id": "12345678-1234-1234-1234-123456789ABC",
  "balancers": [],
  "domainMatcher": "hybrid",
  "rules": [
    {
      "type": "field",
      "domain": ["domain:get-myip.com", "geosite:category-ru"],
      "outboundTag": "direct",
      "id": "87654321-4321-4321-4321-CBA987654321"
    }
  ],
  "name": "Direct Russia"
}
```

**Key Points:**
- Delivered via HTTP `routing` header (base64 encoded)
- Domain rules need `domain:` prefix
- All rules need unique UUIDs
- Requires: `id`, `balancers`, `domainMatcher`, `name`

### Happ Format (QR Code / Deep Link)

```json
{
  "Name": "Direct Russia",
  "GlobalProxy": "true",
  "RemoteDNSType": "DoH",
  "RemoteDNSDomain": "https://cloudflare-dns.com/dns-query",
  "RemoteDNSIP": "1.1.1.1",
  "DomesticDNSType": "DoH",
  "DomesticDNSDomain": "https://dns.google/dns-query",
  "DomesticDNSIP": "8.8.8.8",
  "Geoipurl": "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat",
  "Geositeurl": "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat",
  "LastUpdated": "",
  "DnsHosts": {"cloudflare-dns.com": "1.1.1.1"},
  "DirectSites": ["get-myip.com", "geosite:category-ru"],
  "DirectIp": ["geoip:ru"],
  "ProxySites": [],
  "ProxyIp": [],
  "BlockSites": [],
  "BlockIp": [],
  "DomainStrategy": "IPIfNonMatch",
  "FakeDNS": "false"
}
```

**Key Points:**
- Delivered via QR code: `happ://routing/onadd/{base64}`
- Plain domain names (no `domain:` prefix)
- ALL fields are required (missing fields = error)
- Boolean values as strings: `"true"`/`"false"`

## Troubleshooting

### "Invalid data for profile creation" (Happ)

Missing required fields. Ensure ALL of these are present:
```
Name, GlobalProxy, RemoteDNSType, RemoteDNSDomain, RemoteDNSIP,
DomesticDNSType, DomesticDNSDomain, DomesticDNSIP,
Geoipurl, Geositeurl, LastUpdated, DnsHosts,
DirectSites, DirectIp, ProxySites, ProxyIp,
BlockSites, BlockIp, DomainStrategy, FakeDNS
```

### Routing Not Working (v2rayTUN iOS)

Missing required fields in v2rayTUN routing object. Must have:
```
domainStrategy, id, balancers, domainMatcher, rules, name
```

Each rule must have its own `id` field.

### Happ Ignores Routing Header

Expected behavior. Happ doesn't read HTTP routing headers. Use QR codes instead.

## Git History

Key commits related to routing implementation:

```
bdaf6d1 - Change network traffic metrics from bytes/sec to bits/sec
5ef7595 - Add debug script for sync_from_sheets conflict detection
e1ecfa6 - Add v2rayTUN iOS-compatible routing format
13ca6df - Add User-Agent logging to subscription endpoint
0b5a438 - Add client-aware routing with Happ and v2rayTUN support
01891f8 - Fix Happ routing: remove domain: prefix from plain domains
```

## API Endpoints

### Subscription Service

**Base URL:** `https://gorillaerror.com`

#### `GET /sub/{client_email}`
Get subscription for client with client-aware routing.

**Headers:**
- `User-Agent` - Client type identifier

**Response Headers:**
- `profile-title` - VPN service name
- `profile-update-interval` - Update frequency (hours)
- `routing` - Base64 routing config (v2rayTUN only)
- `subscription-userinfo` - Traffic stats (future)
- `content-disposition` - Suggested filename

**Response Body:**
- Base64 encoded VLESS URLs (one per line)

**Example:**
```bash
curl -H "User-Agent: v2raytun/1.0" https://gorillaerror.com/sub/stein
```

#### `GET /health`
Health check endpoint.

**Response:**
```json
{
  "status": "ok",
  "service": "subscription"
}
```

## Resources

### Geosite/GeoIP Files

**Recommended:** Loyalsoldier's v2ray-rules-dat
- GeoIP: https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat
- Geosite: https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat
- Updated frequently, well-maintained

**Alternative:** v2fly official
- GeoIP: https://github.com/v2fly/geoip/releases/latest/download/geoip.dat
- Geosite: https://github.com/v2fly/domain-list-community/releases/latest/download/dlc.dat

### Common Geosite Categories

```
geosite:category-ru      # Russian domains
geosite:category-cn      # Chinese domains
geosite:geolocation-!cn  # Non-Chinese domains
geosite:google           # All Google services
geosite:netflix          # Netflix
geosite:telegram         # Telegram
geosite:apple            # Apple services
geosite:microsoft        # Microsoft services
```

### Common GeoIP Categories

```
geoip:ru         # Russian IP ranges
geoip:cn         # Chinese IP ranges
geoip:us         # US IP ranges
geoip:private    # Private IP ranges (RFC1918: 10.0.0.0/8, 192.168.0.0/16, 172.16.0.0/12)
```

## Testing

### Test Direct Russia Routing

1. **Setup**
   - v2rayTUN: Subscribe to `https://gorillaerror.com/sub/stein`
   - Happ: Scan QR code at `/home/stein/python/3x-ui/central/happ-routing-direct-russia.png`

2. **Test Direct Routes** (should show Russian IP)
   ```bash
   curl https://get-myip.com
   curl https://api.myip.com
   ```

3. **Test Proxy Routes** (should show VPN server IP)
   ```bash
   curl https://google.com
   curl https://api.ipify.org
   ```

4. **Test Geosite Category**
   - Visit any Russian website (.ru domain)
   - Should connect directly (fast, local IP)
   - Visit non-Russian website
   - Should go through VPN (slower, foreign IP)

## Contributing

When adding new features or fixing bugs:

1. Update relevant documentation
2. Add testing procedures
3. Document User-Agent patterns for new clients
4. Include example configurations
5. Update troubleshooting section

## Version

**Last Updated:** 2026-03-09

**Documentation Version:** 1.0

**Subscription Service Version:** See `subscription/main.py` for implementation version
