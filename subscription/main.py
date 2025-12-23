"""Subscription service - public read-only endpoint"""
import base64
from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from database import get_db, Client, Key

app = FastAPI(title="Subscription Service")


@app.get("/health")
async def health():
    """Health check"""
    return {"status": "ok", "service": "subscription"}


@app.get("/{client_email}", response_class=PlainTextResponse)
async def get_subscription(client_email: str, db: Session = Depends(get_db)):
    """
    Get subscription for client
    Returns base64 encoded VLESS URLs (one per line)
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
    subscription_content = "\n".join(vless_urls)

    # Encode in base64
    encoded = base64.b64encode(subscription_content.encode()).decode()

    return encoded


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
