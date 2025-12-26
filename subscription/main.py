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
    vless_urls = [key.vless_url for key in keys]

    # Randomize order for load balancing across nodes
    # This prevents all clients from defaulting to the first server
    random.shuffle(vless_urls)

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
