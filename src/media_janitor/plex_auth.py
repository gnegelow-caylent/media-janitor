"""Plex OAuth authentication."""

import asyncio
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

# Plex API endpoints
PLEX_API_BASE = "https://plex.tv/api/v2"
PLEX_PINS_URL = f"{PLEX_API_BASE}/pins"
PLEX_USER_URL = f"{PLEX_API_BASE}/user"

# App identification for Plex
PLEX_CLIENT_ID = "media-janitor-web"
PLEX_PRODUCT = "Media Janitor"
PLEX_VERSION = "0.1.0"

# Headers required by Plex API
PLEX_HEADERS = {
    "Accept": "application/json",
    "X-Plex-Client-Identifier": PLEX_CLIENT_ID,
    "X-Plex-Product": PLEX_PRODUCT,
    "X-Plex-Version": PLEX_VERSION,
    "X-Plex-Platform": "Web",
}


@dataclass
class PlexPin:
    """A Plex authentication PIN."""

    id: int
    code: str
    auth_url: str
    expires_at: datetime


@dataclass
class PlexUser:
    """Authenticated Plex user info."""

    id: int
    username: str
    email: str
    thumb: str  # Avatar URL
    auth_token: str


# In-memory storage for pending PINs (keyed by a session ID)
_pending_pins: dict[str, PlexPin] = {}


async def create_pin() -> tuple[str, PlexPin]:
    """
    Create a new Plex PIN for authentication.

    Returns:
        Tuple of (session_id, PlexPin) where session_id is used to track this auth attempt.
    """
    log = logger.bind(component="plex_auth")

    async with httpx.AsyncClient() as client:
        response = await client.post(
            PLEX_PINS_URL,
            headers=PLEX_HEADERS,
            data={"strong": "true"},  # Request a strong PIN
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()

    pin_id = data["id"]
    pin_code = data["code"]
    expires_at = datetime.utcnow() + timedelta(seconds=data.get("expiresIn", 900))

    # Build the auth URL
    auth_url = (
        f"https://app.plex.tv/auth#?"
        f"clientID={PLEX_CLIENT_ID}&"
        f"code={pin_code}&"
        f"context%5Bdevice%5D%5Bproduct%5D={PLEX_PRODUCT}"
    )

    pin = PlexPin(
        id=pin_id,
        code=pin_code,
        auth_url=auth_url,
        expires_at=expires_at,
    )

    # Generate a session ID to track this auth attempt
    session_id = secrets.token_urlsafe(32)
    _pending_pins[session_id] = pin

    log.info("Created Plex PIN", pin_id=pin_id, session_id=session_id[:8])
    return session_id, pin


async def check_pin(session_id: str) -> PlexUser | None:
    """
    Check if a PIN has been authorized.

    Args:
        session_id: The session ID from create_pin()

    Returns:
        PlexUser if authorized, None if still pending or expired.
    """
    log = logger.bind(component="plex_auth")

    pin = _pending_pins.get(session_id)
    if not pin:
        log.warning("Unknown session ID", session_id=session_id[:8])
        return None

    # Check if expired
    if datetime.utcnow() > pin.expires_at:
        log.info("PIN expired", session_id=session_id[:8])
        del _pending_pins[session_id]
        return None

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{PLEX_PINS_URL}/{pin.id}",
            headers=PLEX_HEADERS,
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()

    auth_token = data.get("authToken")
    if not auth_token:
        # Not yet authorized
        return None

    # Clean up the pending PIN
    del _pending_pins[session_id]

    # Fetch user info with the token
    user = await get_user_info(auth_token)
    log.info("Plex auth successful", username=user.username if user else "unknown")
    return user


async def get_user_info(auth_token: str) -> PlexUser | None:
    """
    Get user info using an auth token.

    Args:
        auth_token: Plex authentication token

    Returns:
        PlexUser if valid token, None otherwise.
    """
    headers = {**PLEX_HEADERS, "X-Plex-Token": auth_token}

    async with httpx.AsyncClient() as client:
        response = await client.get(
            PLEX_USER_URL,
            headers=headers,
            timeout=10,
        )

        if response.status_code != 200:
            return None

        data = response.json()

    return PlexUser(
        id=data["id"],
        username=data["username"],
        email=data.get("email", ""),
        thumb=data.get("thumb", ""),
        auth_token=auth_token,
    )


async def validate_token(auth_token: str) -> bool:
    """
    Validate if an auth token is still valid.

    Args:
        auth_token: Plex authentication token

    Returns:
        True if valid, False otherwise.
    """
    user = await get_user_info(auth_token)
    return user is not None


async def get_user_servers(auth_token: str) -> list[dict]:
    """
    Get the user's Plex servers.

    Args:
        auth_token: Plex authentication token

    Returns:
        List of server dicts with name, address, port, local, owned fields.
    """
    headers = {**PLEX_HEADERS, "X-Plex-Token": auth_token}

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{PLEX_API_BASE}/resources",
            headers=headers,
            params={"includeHttps": 1, "includeRelay": 0},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()

    servers = []
    for resource in data:
        if resource.get("provides") != "server":
            continue

        # Get the best connection (prefer local)
        connections = resource.get("connections", [])
        local_conn = next((c for c in connections if c.get("local")), None)
        remote_conn = next((c for c in connections if not c.get("local")), None)
        best_conn = local_conn or remote_conn

        if best_conn:
            servers.append({
                "name": resource.get("name", "Unknown"),
                "address": best_conn.get("address"),
                "port": best_conn.get("port", 32400),
                "uri": best_conn.get("uri"),
                "local": best_conn.get("local", False),
                "owned": resource.get("owned", False),
            })

    return servers


def cleanup_expired_pins():
    """Remove expired PINs from memory."""
    now = datetime.utcnow()
    expired = [sid for sid, pin in _pending_pins.items() if now > pin.expires_at]
    for sid in expired:
        del _pending_pins[sid]
