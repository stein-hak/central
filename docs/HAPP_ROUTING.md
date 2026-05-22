# Happ VPN Client Routing Documentation

## Overview

This document describes how to configure and deliver routing rules to the Happ VPN client, which requires a different approach than other VPN clients like v2rayTUN.

## Key Differences: Happ vs v2rayTUN

| Feature | Happ | v2rayTUN |
|---------|------|----------|
| HTTP Header Support | ❌ No | ✅ Yes |
| Routing Delivery | QR codes, Deep links | HTTP `routing` header |
| Profile Format | Happ-specific JSON | V2Ray routing object |
| Domain Format | Plain domains | `domain:` prefix required |
| Geosite/GeoIP Support | ✅ Yes | ✅ Yes |

## Happ Does NOT Support HTTP Routing Headers

**Critical Discovery:** Unlike v2rayTUN, Happ does **not** read routing configuration from subscription HTTP headers.

### What Doesn't Work
```python
# This approach works for v2rayTUN but NOT for Happ
headers = {
    "routing": base64_encoded_routing_config  # ❌ Happ ignores this
}
```

### What Works
Happ requires routing to be imported via:
1. **QR Codes** - Scan with Happ camera
2. **Deep Links** - `happ://routing/onadd/{base64}`
3. **Manual Import** - Copy deep link to clipboard and open in Happ

## Happ Routing Profile Format

### Complete Required Fields

Happ requires **ALL** of these fields in the routing profile JSON:

```json
{
  "Name": "string - Profile name displayed in app",
  "GlobalProxy": "true" | "false" - Enable global proxy mode,

  "RemoteDNSType": "DoH" | "DoU" | "UDP",
  "RemoteDNSDomain": "string - DNS server URL for DoH/DoU",
  "RemoteDNSIP": "string - IP address of DNS server",

  "DomesticDNSType": "DoH" | "DoU" | "UDP",
  "DomesticDNSDomain": "string - Domestic DNS URL",
  "DomesticDNSIP": "string - Domestic DNS IP",

  "Geoipurl": "string - URL to geoip.dat file",
  "Geositeurl": "string - URL to geosite.dat file",
  "LastUpdated": "string - Unix timestamp or empty string",

  "DnsHosts": {
    "domain.com": "IP address"
  },

  "DirectSites": [
    "plain-domain.com",
    "geosite:category-name"
  ],
  "DirectIp": [
    "1.2.3.4/24",
    "geoip:country-code"
  ],

  "ProxySites": ["domains to force through proxy"],
  "ProxyIp": ["IPs to force through proxy"],

  "BlockSites": ["domains to block"],
  "BlockIp": ["IPs to block"],

  "DomainStrategy": "IPIfNonMatch" | "AsIs" | "IPOnDemand",
  "FakeDNS": "true" | "false"
}
```

### Critical Notes

1. **All fields are required** - Missing fields cause "invalid data for profile creation" error
2. **Boolean values are strings** - Use `"true"`/`"false"`, not bare booleans
3. **Domain format** - Use plain domains (`example.com`), NOT `domain:example.com`
4. **Geosite format** - Use `geosite:category-name` (e.g., `geosite:category-ru`)
5. **GeoIP format** - Use `geoip:country-code` (e.g., `geoip:ru`)

## Example: Direct Russia Routing Profile

This profile routes Russian traffic directly (bypassing VPN) and proxies everything else:

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
  "DnsHosts": {
    "cloudflare-dns.com": "1.1.1.1",
    "dns.google": "8.8.8.8"
  },
  "DirectSites": [
    "get-myip.com",
    "www.get-myip.com",
    "geosite:category-ru"
  ],
  "DirectIp": [
    "geoip:ru",
    "geoip:private"
  ],
  "ProxySites": [],
  "ProxyIp": [],
  "BlockSites": [],
  "BlockIp": [],
  "DomainStrategy": "IPIfNonMatch",
  "FakeDNS": "false"
}
```

### What This Does

- **Direct Routes** (bypass VPN):
  - `get-myip.com` - For testing IP location
  - `geosite:category-ru` - All Russian websites
  - `geoip:ru` - All Russian IP addresses
  - `geoip:private` - Private/local IP ranges (192.168.x.x, 10.x.x.x, etc.)

- **Proxy Routes** (through VPN):
  - Everything else (GlobalProxy = true)

- **DNS Configuration**:
  - Remote DNS: Cloudflare DoH (1.1.1.1) - for proxied domains
  - Domestic DNS: Google DoH (8.8.8.8) - for direct domains

## Generating QR Codes and Deep Links

### Python Script

```python
import json
import base64
import qrcode

# 1. Create complete routing profile
routing_profile = {
    "Name": "Direct Russia",
    "GlobalProxy": "true",
    # ... (complete profile as shown above)
}

# 2. Convert to compact JSON (no spaces)
routing_json = json.dumps(routing_profile, separators=(',', ':'))

# 3. Base64 encode
routing_encoded = base64.b64encode(routing_json.encode()).decode()

# 4. Create deep link
deep_link = f"happ://routing/onadd/{routing_encoded}"

# 5. Generate QR code
qr = qrcode.QRCode(version=None, box_size=10, border=4)
qr.add_data(deep_link)
qr.make(fit=True)

# Save as image
img = qr.make_image(fill_color="black", back_color="white")
img.save("happ-routing-qr.png")
```

### Deep Link Formats

- **Add profile:** `happ://routing/add/{base64}` - Adds but doesn't activate
- **Add and activate:** `happ://routing/onadd/{base64}` - Adds and activates immediately ✅ Recommended
- **Disable routing:** `happ://routing/off` - Turns off routing

## Current Implementation: Subscription Service

### File: `/home/stein/python/3x-ui/central/subscription/main.py`

The subscription service at `https://gorillaerror.com/sub/{client_email}` implements **client-aware routing** that detects the VPN client type from the User-Agent header and sends appropriate routing formats.

### Client Detection Logic

```python
# Line 310-396 in main.py

user_agent = request.headers.get("User-Agent", "")
is_happ_client = "Happ/" in user_agent
is_v2raytun_client = "v2raytun/" in user_agent.lower()

if is_happ_client:
    # Happ format: DirectSites/DirectIp with plain domains
    routing_config = {
        "DirectSites": ["get-myip.com", "geosite:category-ru"],
        "DirectIp": ["geoip:ru"],
        # ... (partial format)
    }
elif is_v2raytun_client:
    # v2rayTUN format: V2Ray routing object with domain: prefix
    routing_config = {
        "domainStrategy": "AsIs",
        "id": str(uuid.uuid4()).upper(),
        "rules": [
            {
                "type": "field",
                "domain": ["domain:get-myip.com", "geosite:category-ru"],
                "outboundTag": "direct"
            }
        ]
    }

# Base64 encode and add to HTTP header
routing_encoded = base64.b64encode(json.dumps(routing_config).encode()).decode()
headers["routing"] = routing_encoded  # ✅ Works for v2rayTUN, ❌ Ignored by Happ
```

### Current Status

- **v2rayTUN**: ✅ Working - Receives routing via HTTP `routing` header
- **Happ**: ⚠️ Partial - HTTP header approach doesn't work, requires QR codes/deep links

### Tested Clients

| Client | User-Agent | Routing Method | Status |
|--------|-----------|----------------|--------|
| v2rayTUN Android | `v2raytun/...` | HTTP header | ✅ Working |
| v2rayTUN iOS | `v2rayTUN/...` | HTTP header | ✅ Working |
| Happ iOS | `Happ/...` | QR code/Deep link | ✅ Working |
| Streisand | `Streisand/...` | Unknown | ❓ Not tested |

## Testing Routing Configurations

### Test with curl (v2rayTUN format)

```bash
# v2rayTUN client
curl -H "User-Agent: v2raytun/1.0" https://gorillaerror.com/sub/stein -v | head -20

# Check for routing header in response
# HTTP/1.1 200 OK
# routing: eyJkb21haW5TdHJhdGVneSI6IkFzSXMiLCAicnVsZXMiOiBbLi4uXX0=
```

### Test with curl (Happ format)

```bash
# Happ client - routing header is sent but ignored by client
curl -H "User-Agent: Happ/1.0" https://gorillaerror.com/sub/stein -v | head -20

# Happ ignores the routing header - QR code needed instead
```

### Verify Routing Profile

```bash
# Decode the base64 routing header
echo "BASE64_STRING" | base64 -d | jq .
```

## Distribution Methods for Happ

Since Happ doesn't support HTTP headers, routing profiles must be distributed via:

### 1. QR Codes (Recommended)
- Generate QR code with `happ://routing/onadd/{base64}` deep link
- User scans with Happ camera
- Profile imports and activates automatically
- **File:** `/home/stein/python/3x-ui/central/happ-routing-direct-russia.png`

### 2. Deep Links
- Share link: `happ://routing/onadd/{base64}`
- User clicks link on iOS device
- Happ app opens and imports profile
- Works via messaging apps, email, web pages

### 3. Website Import
- Could create web page at `https://gorillaerror.com/routing/happ`
- Display QR code and deep link
- User scans or clicks to import

### 4. Manual Configuration
- User manually enters routing rules in Happ settings
- Not recommended - too complex for users

## Geosite and GeoIP Resources

### Recommended Source: Loyalsoldier's v2ray-rules-dat

Best maintained geosite/geoip database with frequent updates:

- **GeoIP:** `https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat`
- **Geosite:** `https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat`

### Common Categories

**Geosite:**
- `geosite:category-ru` - Russian domains
- `geosite:category-cn` - Chinese domains
- `geosite:geolocation-!cn` - Non-Chinese domains
- `geosite:google` - All Google services
- `geosite:netflix` - Netflix domains
- `geosite:telegram` - Telegram domains

**GeoIP:**
- `geoip:ru` - Russian IP ranges
- `geoip:cn` - Chinese IP ranges
- `geoip:us` - US IP ranges
- `geoip:private` - Private IP ranges (RFC1918)

### Alternative Source: v2fly

Official v2ray project geosite/geoip files:

- **GeoIP:** `https://github.com/v2fly/geoip/releases/latest/download/geoip.dat`
- **Geosite:** `https://github.com/v2fly/domain-list-community/releases/latest/download/dlc.dat`

## Troubleshooting

### Error: "Invalid data for profile creation"

**Cause:** Missing required fields in routing profile JSON

**Solution:** Ensure ALL required fields are present:
```python
required_fields = [
    "Name", "GlobalProxy",
    "RemoteDNSType", "RemoteDNSDomain", "RemoteDNSIP",
    "DomesticDNSType", "DomesticDNSDomain", "DomesticDNSIP",
    "Geoipurl", "Geositeurl", "LastUpdated",
    "DnsHosts", "DirectSites", "DirectIp",
    "ProxySites", "ProxyIp", "BlockSites", "BlockIp",
    "DomainStrategy", "FakeDNS"
]
```

### Routing Not Applied (Happ)

**Symptom:** QR code scans successfully but routing doesn't work

**Causes:**
1. Geosite/GeoIP files not downloaded yet
2. Profile not activated (use `/onadd` instead of `/add`)
3. Domain format incorrect (use plain domains, not `domain:` prefix)

**Solution:**
- Wait for geosite/geoip download to complete
- Use `happ://routing/onadd/{base64}` to auto-activate
- Check domain format in DirectSites array

### Routing Header Ignored (Happ)

**Symptom:** Subscription works but routing from HTTP header not applied

**Cause:** Happ doesn't support HTTP routing headers

**Solution:** Use QR codes or deep links instead

## Future Enhancements

### Potential Improvements

1. **Dynamic QR Code Endpoint**
   ```
   GET /routing/happ/{client_email}
   Returns: PNG QR code with routing profile
   ```

2. **Web-based Routing Generator**
   ```
   https://gorillaerror.com/routing/configure
   - Web form to create custom routing rules
   - Generates QR code on the fly
   ```

3. **Multiple Routing Profiles**
   - Direct Russia
   - Direct China
   - Block Ads
   - Proxy All
   - Custom

4. **Routing Profile Management API**
   ```
   POST /api/routing/profiles
   GET /api/routing/profiles/{id}
   DELETE /api/routing/profiles/{id}
   ```

## References

- **Happ Documentation:** https://www.happ.su/main/dev-docs/routing
- **Happ Routing Site:** https://routing.happ.su/en
- **V2Ray Routing Docs:** https://xtls.github.io/en/config/routing.html
- **Loyalsoldier Geofiles:** https://github.com/Loyalsoldier/v2ray-rules-dat
- **Current QR Code:** `/home/stein/python/3x-ui/central/happ-routing-direct-russia.png`
- **Deep Link Example:** `/home/stein/python/3x-ui/central/happ-routing-direct-russia.txt`

## Version History

- **2026-03-09**: Initial documentation
  - Discovered Happ doesn't support HTTP routing headers
  - Documented complete Happ routing profile format
  - Created working QR code generation script
  - Documented all required fields and common errors
