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

from database import get_db, Client, Key, Node, Domain, NodeDomain, Proxy, ProxyBackend

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


def select_fake_sni(proxy: Proxy, node_id: int = None) -> str:
    """
    Select fake SNI based on proxy strategy

    Args:
        proxy: Proxy object with fake_snis and sni_strategy
        node_id: Optional node ID for consistent selection

    Returns:
        Selected fake SNI domain
    """
    if not proxy.fake_snis or len(proxy.fake_snis) == 0:
        # No fake SNIs configured - use proxy domain
        return proxy.domain

    if proxy.sni_strategy == 'fixed':
        # Always use first SNI
        return proxy.fake_snis[0]

    elif proxy.sni_strategy == 'rotate':
        # Rotate based on day of year
        from datetime import datetime
        day_of_year = datetime.now().timetuple().tm_yday
        index = day_of_year % len(proxy.fake_snis)
        return proxy.fake_snis[index]

    else:  # 'random' or default
        # Random selection
        # If node_id provided, use it for consistent randomization per node
        if node_id:
            index = node_id % len(proxy.fake_snis)
            return proxy.fake_snis[index]
        else:
            return random.choice(proxy.fake_snis)


def regenerate_url_with_proxy(original_url: str, proxy_domain: str, proxy_name: str, fake_sni: str) -> str:
    """
    Regenerate VLESS URL with proxy domain and fake SNI

    Args:
        original_url: Original VLESS URL from node
        proxy_domain: Proxy domain (e.g., phone-bliss.tech)
        proxy_name: Proxy display name (e.g., US-Server)
        fake_sni: Fake SNI domain for obfuscation (e.g., vk.com)

    Returns:
        New VLESS URL with proxy settings
    """
    if not original_url.startswith('vless://'):
        return original_url

    try:
        # Extract UUID
        uuid = original_url.split('://')[1].split('@')[0]

        # Extract query params
        after_at = original_url.split('@', 1)[1]

        # Determine transport
        transport = "gRPC"
        if 'type=xhttp' in original_url or 'type=splithttp' in original_url:
            transport = "XHTTP"
        elif 'type=tcp' in original_url:
            transport = "TCP"

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

        # Extract email from old remark
        email = ""
        if '-' in old_remark and '@' in old_remark:
            # Format: node-name-TRANSPORT-email
            parts = old_remark.split('-')
            if len(parts) >= 3:
                email = '-'.join(parts[2:])  # Everything after transport

        # Build new remark with proxy name (transport already in proxy_name)
        new_remark = proxy_name
        if email:
            new_remark += f"-{email}"

        # Replace or add SNI parameter in query string
        params = []
        sni_replaced = False

        if query_string:
            for param in query_string.split('&'):
                if param.startswith('sni='):
                    # Replace existing SNI
                    params.append(f"sni={fake_sni}")
                    sni_replaced = True
                else:
                    params.append(param)

        # Add SNI if not replaced
        if not sni_replaced:
            params.append(f"sni={fake_sni}")

        query_string = '&'.join(params)

        # Build new URL
        new_url = f"vless://{uuid}@{proxy_domain}:443"
        if query_string:
            new_url += f"?{query_string}"
        if new_remark:
            new_url += f"#{new_remark}"

        return new_url

    except Exception as e:
        logger.error(f"Failed to regenerate URL with proxy: {e}")
        return original_url


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

    # Generate URLs for each key
    # Priority: proxy URLs first, then direct URLs (if not proxy_only)
    all_vless_urls = []

    # Helper function to detect transport type from VLESS URL
    def get_transport_from_url(vless_url):
        """Extract transport type from VLESS URL name (after #)"""
        # Check the name part (after #) for transport type
        if '#' in vless_url:
            name_part = vless_url.split('#')[1]
            # URL decode the name
            import urllib.parse
            decoded_name = urllib.parse.unquote(name_part)

            # Look for transport indicators in the name (case insensitive)
            name_upper = decoded_name.upper()
            if '-GRPC-' in name_upper or name_upper.endswith('-GRPC'):
                return 'grpc'
            elif '-XHTTP-' in name_upper or name_upper.endswith('-XHTTP'):
                return 'xhttp'
            elif '-TCP-' in name_upper or name_upper.endswith('-TCP'):
                return 'tcp'

        # Fallback to URL parameters if name doesn't contain transport info
        if 'type=grpc' in vless_url or 'serviceName=' in vless_url:
            return 'grpc'
        elif 'type=xhttp' in vless_url:
            return 'xhttp'
        elif 'type=tcp' in vless_url:
            return 'tcp'

        # Default to tcp if not specified
        return 'tcp'

    # First, generate proxy URLs (one per proxy, not per node)
    # Get all enabled proxies with backends
    all_proxies = db.query(Proxy).filter(Proxy.enabled == True).all()
    generated_proxy_ids = set()

    for proxy in all_proxies:
        if proxy.id in generated_proxy_ids:
            continue

        # Get proxy's allowed transport (default to xhttp for backward compatibility)
        allowed_transport = proxy.allowed_transport or 'xhttp'

        # Get first enabled backend node for this proxy
        backend = db.query(ProxyBackend).join(
            Node, ProxyBackend.node_id == Node.id
        ).filter(
            ProxyBackend.proxy_id == proxy.id,
            ProxyBackend.enabled == True,
            Node.enabled == True
        ).first()

        if not backend:
            continue

        # Find a key for this backend node that matches the proxy's allowed transport
        matching_key = None
        for k in keys:
            if k.node_id == backend.node_id:
                # Check if key's transport matches proxy's allowed transport
                key_transport = get_transport_from_url(k.vless_url)
                if key_transport == allowed_transport:
                    matching_key = k
                    break

        if not matching_key:
            # No matching key found for this proxy's transport type
            continue

        # Generate one proxy URL
        fake_sni = select_fake_sni(proxy, backend.node_id)
        proxy_url = regenerate_url_with_proxy(
            original_url=matching_key.vless_url,
            proxy_domain=proxy.domain,
            proxy_name=proxy.name,
            fake_sni=fake_sni
        )
        all_vless_urls.append(proxy_url)
        generated_proxy_ids.add(proxy.id)

    # Then, generate direct URLs for each node (if not proxy_only)
    for key in keys:
        # Get node info
        node = db.query(Node).filter(Node.id == key.node_id, Node.enabled == True).first()

        if not node:
            # Skip disabled nodes
            continue

        # Generate direct URLs (if node is not proxy_only)
        if not node.proxy_only:
            # Get ALL domains for this node (including primary)
            node_domains = db.query(NodeDomain, Domain).join(
                Domain, NodeDomain.domain_id == Domain.id
            ).filter(
                NodeDomain.node_id == key.node_id,
                NodeDomain.enabled == True,
                Domain.enabled == True
            ).order_by(NodeDomain.is_primary.desc()).all()  # Primary first

            # Generate URLs for all configured domains (primary + additional)
            for nd, domain in node_domains:
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
