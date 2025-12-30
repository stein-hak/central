"""Subscription service - public read-only endpoint"""
import base64
import os
import random
from fastapi import FastAPI, Depends, HTTPException, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from database import get_db, Client, Key

app = FastAPI(title="Subscription Service")

# Get profile title from environment
PROFILE_TITLE = os.getenv("PROFILE_TITLE", "VPN Service")


@app.get("/health")
async def health():
    """Health check"""
    return {"status": "ok", "service": "subscription"}


@app.get("/{client_email}")
async def get_subscription(client_email: str, db: Session = Depends(get_db)):
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
    keys = db.query(Key).filter(Key.client_id == client.id).all()

    if not keys:
        raise HTTPException(status_code=404, detail="No keys found for client")

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

    return Response(content=encoded, media_type="text/plain", headers=headers)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
