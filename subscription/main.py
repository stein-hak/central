"""Subscription service - public read-only endpoint"""
import base64
import logging
import os
import random
import re
import urllib.parse
from fastapi import FastAPI, Depends, HTTPException, Response, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from database import get_db, Client, Key, Node, Domain, NodeDomain

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Subscription Service")

# Get profile title from environment
PROFILE_TITLE = os.getenv("PROFILE_TITLE", "VPN Service")

# Get profile update interval from environment (in hours)
PROFILE_UPDATE_INTERVAL = os.getenv("PROFILE_UPDATE_INTERVAL", "3")

# Enable auto-ping on app open (Happ feature)
ENABLE_PING_ON_OPEN = os.getenv("ENABLE_PING_ON_OPEN", "false").lower() in ("true", "1", "yes")


def get_client_ip(request: Request) -> str:
    """
    Get real client IP from request, considering proxy headers

    Checks in order:
    1. X-Real-IP (set by nginx)
    2. X-Forwarded-For (first IP in chain)
    3. request.client.host (fallback for direct connections)
    """
    # Check X-Real-IP header (preferred)
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip

    # Check X-Forwarded-For header
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # X-Forwarded-For can contain multiple IPs, get the first (client)
        return forwarded_for.split(",")[0].strip()

    # Fallback to direct connection IP
    return request.client.host if request.client else "Unknown"


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


def regenerate_url_with_domain(original_url: str, new_domain: str, node_upgraded: bool, display_name: str = None) -> str:
    """
    Regenerate VLESS URL with different domain (for multi-domain support).
    Preserves ALL parameters including Reality params - only changes domain and node name.

    Args:
        original_url: Original VLESS URL
        new_domain: New domain to use
        node_upgraded: If True, adds subdomain prefix (api./app.)
        display_name: Override node name to appear as different server (e.g., "node-france", "node-cloudflare")

    Returns:
        New URL with different domain and optionally different node name
    """
    if not original_url.startswith('vless://'):
        return original_url

    # Extract components: vless://UUID@DOMAIN:PORT?PARAMS#REMARK
    try:
        # Extract UUID
        uuid = original_url.split('://')[1].split('@')[0]

        # Extract query params and remark
        after_at = original_url.split('@', 1)[1]

        # Determine transport from params
        transport = "grpc"
        if 'type=xhttp' in original_url:
            transport = "xhttp"

        # Build new domain with subdomain if upgraded
        if node_upgraded:
            final_domain = f"{'app' if transport == 'xhttp' else 'api'}.{new_domain}"
        else:
            final_domain = new_domain

        # Extract query string and old remark
        if '?' in after_at:
            query_and_remark = after_at.split('?', 1)[1]
            if '#' in query_and_remark:
                query_string, old_remark = query_and_remark.split('#', 1)
            else:
                query_string = query_and_remark
                old_remark = ""
        else:
            query_string = ""
            old_remark = after_at.split('#')[1] if '#' in after_at else ""

        # Build new remark - replace node name if display_name provided
        if display_name:
            # Replace node name with display_name to appear as different server
            # Old format: node-name-gRPC-email or node-name-XHTTP-email
            # New format: display-name-gRPC-email or display-name-XHTTP-email
            if '-gRPC-' in old_remark:
                _, email_part = old_remark.split('-gRPC-', 1)
                new_remark = f"{display_name}-gRPC-{email_part}"
            elif '-XHTTP-' in old_remark:
                _, email_part = old_remark.split('-XHTTP-', 1)
                new_remark = f"{display_name}-XHTTP-{email_part}"
            else:
                new_remark = display_name
        else:
            # No display name - keep original remark
            new_remark = old_remark

        # Build new URL
        new_url = f"vless://{uuid}@{final_domain}:443"
        if query_string:
            new_url += f"?{query_string}"
        if new_remark:
            new_url += f"#{new_remark}"

        return new_url

    except Exception as e:
        logger.error(f"Failed to regenerate URL: {e}")
        return original_url  # Return original if parsing fails


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
    # Log User-Agent for client behavior analysis
    user_agent = request.headers.get("User-Agent", "Unknown")
    client_ip = get_client_ip(request)
    logger.info(f"Subscription request - Client: {client_email}, IP: {client_ip}, User-Agent: {user_agent}")

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

    # Generate multi-domain URLs for each key
    # Check if nodes have multiple domains configured and generate additional URLs
    all_vless_urls = []

    for key in keys:
        # Get ALL domains for this node (including primary)
        # IMPORTANT: Only include enabled nodes to filter out deleted/disabled nodes
        node_domains = db.query(NodeDomain, Domain, Node).join(
            Domain, NodeDomain.domain_id == Domain.id
        ).join(
            Node, NodeDomain.node_id == Node.id
        ).filter(
            NodeDomain.node_id == key.node_id,
            NodeDomain.enabled == True,
            Domain.enabled == True,
            Node.enabled == True  # Filter out disabled nodes
        ).order_by(NodeDomain.is_primary.desc()).all()  # Primary first

        # Skip this key if node is disabled/deleted (no domains found)
        if not node_domains:
            # Don't include URLs for disabled/deleted nodes
            continue

        # Generate URLs for all configured domains (primary + additional)
        for nd, domain, node in node_domains:
            regenerated_url = regenerate_url_with_domain(
                original_url=key.vless_url,
                new_domain=domain.domain,
                node_upgraded=node.upgraded or False,
                display_name=nd.display_name
            )
            all_vless_urls.append(regenerated_url)

    # Build subscription content (one URL per line)
    # Group URLs by country, XHTTP first within each group, then randomize groups

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

    # Group URLs by country (includes multi-domain URLs)
    country_groups = {}
    for vless_url in all_vless_urls:
        remark = get_remark_from_url(vless_url)
        country = get_country_from_remark(remark)

        if country not in country_groups:
            country_groups[country] = []
        country_groups[country].append(vless_url)

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

    subscription_content = "\n".join(vless_urls)

    # Encode in base64
    encoded = base64.b64encode(subscription_content.encode()).decode()

    # Build response with custom headers for VPN clients
    headers = {
        # Profile info - brand name from env
        "profile-title": PROFILE_TITLE,
        "profile-update-interval": PROFILE_UPDATE_INTERVAL,  # Update interval in hours (configurable)

        # Usage info (TODO: add real traffic stats from nodes)
        # Format: upload=bytes; download=bytes; total=bytes; expire=timestamp
        "subscription-userinfo": "upload=0; download=0; total=0; expire=0",

        # Suggest filename for download
        "content-disposition": f'attachment; filename="{client_email}.txt"'
    }

    # Add optional Happ auto-ping header if enabled
    if ENABLE_PING_ON_OPEN:
        headers["subscription-ping-onopen-enabled"] = "1"

    # Add routing rules for specific test users (client-aware routing)
    if client_email in ["stein", "Client-40337230"]:
        import json
        import uuid

        # Detect client type from User-Agent
        is_happ_client = "Happ/" in user_agent
        is_v2raytun_client = "v2raytun/" in user_agent.lower()

        if is_happ_client:
            # Happ client format: DirectSites/DirectIp with geosite support
            # Plain domains without "domain:" prefix, geosite/geoip with prefix
            logger.info(f"Client {client_email}: Detected Happ, sending DirectSites routing")
            routing_config = {
                "DirectSites": [
                    "get-myip.com",
                    "www.get-myip.com",
                    "geosite:category-ru"
                ],
                "DirectIp": [
                    "geoip:ru"
                ],
                "ProxySites": [],
                "BlockSites": []
            }
        elif is_v2raytun_client:
            # v2rayTUN format: V2Ray routing object with all required fields
            logger.info(f"Client {client_email}: Detected v2rayTUN, sending V2Ray routing")
            routing_config = {
                "domainStrategy": "AsIs",
                "id": str(uuid.uuid4()).upper(),
                "balancers": [],
                "domainMatcher": "hybrid",
                "rules": [
                    {
                        "type": "field",
                        "domain": [
                            "domain:get-myip.com",
                            "domain:www.get-myip.com"
                        ],
                        "outboundTag": "direct",
                        "id": str(uuid.uuid4()).upper()
                    },
                    {
                        "type": "field",
                        "domain": [
                            "geosite:category-ru"
                        ],
                        "outboundTag": "direct",
                        "id": str(uuid.uuid4()).upper()
                    }
                ],
                "name": "Direct Russia"
            }
        else:
            # Unknown client - default to v2rayTUN format (widely compatible)
            logger.info(f"Client {client_email}: Unknown client type ({user_agent}), defaulting to v2rayTUN routing")
            routing_config = {
                "domainStrategy": "AsIs",
                "id": str(uuid.uuid4()).upper(),
                "balancers": [],
                "domainMatcher": "hybrid",
                "rules": [
                    {
                        "type": "field",
                        "domain": [
                            "domain:get-myip.com",
                            "domain:www.get-myip.com"
                        ],
                        "outboundTag": "direct",
                        "id": str(uuid.uuid4()).upper()
                    },
                    {
                        "type": "field",
                        "domain": [
                            "geosite:category-ru"
                        ],
                        "outboundTag": "direct",
                        "id": str(uuid.uuid4()).upper()
                    }
                ],
                "name": "Direct Russia"
            }

        # Base64 encode routing config and add to headers
        routing_json = json.dumps(routing_config, separators=(',', ':'))
        routing_encoded = base64.b64encode(routing_json.encode()).decode()
        headers["routing"] = routing_encoded

    return Response(content=encoded, media_type="text/plain", headers=headers)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
