"""Subscription service - public read-only endpoint"""
import base64
import os
import random
import re
from fastapi import FastAPI, Depends, HTTPException, Response, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from database import get_db, Client, Key

app = FastAPI(title="Subscription Service")

# Get profile title from environment
PROFILE_TITLE = os.getenv("PROFILE_TITLE", "VPN Service")

# Feature toggle: Enable device-aware fingerprint randomization
# Set to "true" to enable, "false" to disable (default: false)
ENABLE_FP_RANDOMIZATION = os.getenv("ENABLE_FP_RANDOMIZATION", "false").lower() == "true"


def get_fingerprints_for_device(user_agent: str) -> tuple[list, list]:
    """
    Return (fingerprints, weights) appropriate for device type

    Args:
        user_agent: HTTP User-Agent header

    Returns:
        Tuple of (fingerprint_list, weights_list)
    """
    ua = user_agent.lower()

    if 'iphone' in ua or 'ipad' in ua:
        # iOS devices: prefer ios/safari, include chrome (iOS can run it), random as fallback
        return (
            ['ios', 'safari', 'chrome', 'random'],
            [40, 30, 20, 10]
        )

    elif 'android' in ua:
        # Android devices: prefer android/chrome, include firefox, random as fallback
        return (
            ['android', 'chrome', 'firefox', 'random'],
            [35, 35, 20, 10]
        )

    else:
        # Desktop or unknown: all desktop browsers + random
        return (
            ['chrome', 'firefox', 'edge', 'safari', 'random'],
            [30, 25, 20, 15, 10]
        )


def get_random_fingerprint(user_agent: str = "") -> str:
    """
    Get device-appropriate random fingerprint

    Args:
        user_agent: HTTP User-Agent header (optional)

    Returns:
        TLS fingerprint string (chrome, firefox, safari, edge, ios, android, or random)
    """
    if not user_agent:
        # No User-Agent: return 'random' to let Xray client decide
        return 'random'

    fingerprints, weights = get_fingerprints_for_device(user_agent)
    return random.choices(fingerprints, weights=weights, k=1)[0]


def add_or_replace_fingerprint(vless_url: str, user_agent: str = "") -> str:
    """
    Add or replace fp= parameter in VLESS URL with device-appropriate random fingerprint

    Args:
        vless_url: Original VLESS URL (may or may not have fp= already)
        user_agent: HTTP User-Agent for device detection

    Returns:
        VLESS URL with randomized fp= parameter
    """
    # Get device-appropriate random fingerprint
    fp = get_random_fingerprint(user_agent)

    # Remove existing fp= if present (handles all positions)
    vless_url = re.sub(r'&fp=[^&#]+', '', vless_url)           # &fp=xxx
    vless_url = re.sub(r'\?fp=[^&#]+&', '?', vless_url)        # ?fp=xxx& (first param)
    vless_url = re.sub(r'\?fp=[^&#]+#', '?#', vless_url)       # ?fp=xxx# (only param with remark)
    vless_url = re.sub(r'\?fp=[^&#]+$', '', vless_url)         # ?fp=xxx (only param, no remark)

    # Clean up double separators
    vless_url = vless_url.replace('?&', '?')
    vless_url = vless_url.replace('?#', '#')

    # Add new random fp= parameter
    if '?' in vless_url:
        # Has query params - append to them
        if '#' in vless_url:
            # Has remark: insert before #
            base, remark = vless_url.rsplit('#', 1)
            vless_url = f"{base}&fp={fp}#{remark}"
        else:
            # No remark: append to end
            vless_url = f"{vless_url}&fp={fp}"
    else:
        # No query params - add them
        if '#' in vless_url:
            # Has remark: insert before #
            base, remark = vless_url.rsplit('#', 1)
            vless_url = f"{base}?fp={fp}#{remark}"
        else:
            # No remark: append to end
            vless_url = f"{vless_url}?fp={fp}"

    return vless_url


def add_standard_alpn(vless_url: str) -> str:
    """
    Add standard browser ALPN (h2,http/1.1) to VLESS URL

    All modern browsers (Chrome, Firefox, Safari, Edge) send this ALPN list
    in TLS handshakes. Adding it makes VPN traffic indistinguishable from
    real browser traffic at the TLS handshake level.

    Args:
        vless_url: Original VLESS URL

    Returns:
        VLESS URL with alpn=h2,http/1.1 parameter
    """
    # Standard browser ALPN: what Chrome/Firefox/Safari send
    alpn = 'h2,http/1.1'

    # Only add ALPN to TLS connections (not Reality, which handles it differently)
    if 'security=tls' not in vless_url:
        return vless_url

    # Skip if ALPN already present (don't override existing config)
    if 'alpn=' in vless_url:
        return vless_url

    # Add alpn parameter in correct position
    if '?' in vless_url:
        # Has query params - append to them
        if '#' in vless_url:
            # Has remark: insert before #
            base, remark = vless_url.rsplit('#', 1)
            vless_url = f"{base}&alpn={alpn}#{remark}"
        else:
            # No remark: append to end
            vless_url = f"{vless_url}&alpn={alpn}"
    else:
        # No query params - add them
        if '#' in vless_url:
            # Has remark: insert before #
            base, remark = vless_url.rsplit('#', 1)
            vless_url = f"{base}?alpn={alpn}#{remark}"
        else:
            # No remark: append to end
            vless_url = f"{vless_url}?alpn={alpn}"

    return vless_url


@app.get("/health")
async def health():
    """Health check"""
    return {"status": "ok", "service": "subscription"}


@app.get("/{client_email}")
async def get_subscription(client_email: str, request: Request, db: Session = Depends(get_db)):
    """
    Get subscription for client
    Returns base64 encoded VLESS URLs (one per line) with auto-update headers
    """
    # Find client
    client = db.query(Client).filter(Client.email == client_email).first()

    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    if not client.enabled:
        raise HTTPException(status_code=403, detail="Client is disabled")

    # Get all keys for this client
    all_keys = db.query(Key).filter(Key.client_id == client.id).all()

    if not all_keys:
        raise HTTPException(status_code=404, detail="No keys found for client")

    # Filter to one key per transport type per node
    # This prevents duplicate links when multiple inbounds of same type exist
    keys_by_node_transport = {}

    for key in all_keys:
        # Determine transport from URL
        transport = "grpc"  # default
        if key.vless_url:
            if "type=xhttp" in key.vless_url or "type=splithttp" in key.vless_url:
                transport = "xhttp"
            elif "type=tcp" in key.vless_url:
                transport = "tcp"
            elif "type=grpc" in key.vless_url:
                transport = "grpc"

        # Use (node_id, transport) as key to deduplicate
        node_transport_key = (key.node_id, transport)

        # Only keep first key of each (node, transport) combination
        if node_transport_key not in keys_by_node_transport:
            keys_by_node_transport[node_transport_key] = key

    # Get deduplicated keys
    keys = list(keys_by_node_transport.values())

    # Build subscription content (one URL per line)
    # Group keys by country, XHTTP first within each group, then randomize groups

    # Extract remark from VLESS URL for grouping
    def get_remark_from_url(vless_url):
        """Extract remark from vless://...#remark"""
        if '#' in vless_url:
            return vless_url.split('#')[-1]
        return ""

    def get_country_from_remark(remark):
        """Extract country/node name from remark (before -gRPC or -XHTTP)"""
        # Remark format: "NodeName-gRPC-email" or "NodeName-XHTTP-email"
        # Extract just the node name part
        if '-gRPC-' in remark:
            return remark.split('-gRPC-')[0]
        elif '-XHTTP-' in remark:
            return remark.split('-XHTTP-')[0]
        # Fallback: return first part before any dash
        return remark.split('-')[0] if '-' in remark else remark

    def is_xhttp(vless_url):
        """Check if URL is XHTTP transport"""
        return 'type=xhttp' in vless_url

    # Group keys by country
    country_groups = {}
    for key in keys:
        remark = get_remark_from_url(key.vless_url)
        country = get_country_from_remark(remark)

        if country not in country_groups:
            country_groups[country] = []
        country_groups[country].append(key.vless_url)

    # Sort within each country group: XHTTP first, then gRPC
    for country in country_groups:
        country_groups[country].sort(key=lambda url: (not is_xhttp(url), url))

    # Get country names and randomize group order
    countries = list(country_groups.keys())
    random.shuffle(countries)

    # Flatten groups in randomized order
    vless_urls = []
    for country in countries:
        vless_urls.extend(country_groups[country])

    # Apply fingerprint randomization if enabled
    if ENABLE_FP_RANDOMIZATION:
        user_agent = request.headers.get("User-Agent", "")
        # Apply fingerprint randomization (device-aware)
        vless_urls = [add_or_replace_fingerprint(url, user_agent) for url in vless_urls]
        # Apply standard browser ALPN to all TLS URLs
        # DISABLED: Causing connection issues, testing fingerprint only
        # vless_urls = [add_standard_alpn(url) for url in vless_urls]

    subscription_content = "\n".join(vless_urls)

    # Encode in base64
    encoded = base64.b64encode(subscription_content.encode()).decode()

    # Build response with custom headers for VPN clients
    headers = {
        # Profile info - brand name from env
        "profile-title": PROFILE_TITLE,
        "profile-update-interval": "24",  # Update every 24 hours

        # Usage info (TODO: add real traffic stats from nodes)
        # Format: upload=bytes; download=bytes; total=bytes; expire=timestamp
        "subscription-userinfo": "upload=0; download=0; total=0; expire=0",

        # Suggest filename for download
        "content-disposition": f'attachment; filename="{client_email}.txt"'
    }

    # Add routing rules for specific users (v2rayTUN support)
    if client_email == "stein":
        # V2Ray routing object: bypass VPN for specific domains and geosite
        routing_config = {
            "domainStrategy": "AsIs",
            "rules": [
                {
                    "type": "field",
                    "domain": [
                        "domain:get-myip.com",
                        "domain:www.get-myip.com"
                    ],
                    "outboundTag": "direct"
                },
                {
                    "type": "field",
                    "domain": [
                        "geosite:category-ru"
                    ],
                    "outboundTag": "direct"
                }
            ]
        }
        # Base64 encode routing config and add to headers
        import json
        routing_json = json.dumps(routing_config, separators=(',', ':'))
        routing_encoded = base64.b64encode(routing_json.encode()).decode()
        headers["routing"] = routing_encoded

    return Response(content=encoded, media_type="text/plain", headers=headers)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
