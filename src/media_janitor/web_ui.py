"""Web UI routes and API endpoints."""

import secrets
import httpx
import yaml
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import Config
from . import plex_auth

logger = structlog.get_logger()

router = APIRouter()

# Setup templates
templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

# Config file path
CONFIG_PATH = Path("/data/config.yaml")

# Session storage (maps session_token -> plex_auth_token)
# In production, consider using Redis or encrypted cookies
_sessions: dict[str, str] = {}

# Cookie settings
SESSION_COOKIE_NAME = "mj_session"
AUTH_PENDING_COOKIE = "mj_auth_pending"


def get_config_dict() -> dict:
    """Read current config as dict."""
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def save_config_dict(config: dict) -> None:
    """Save config dict to file."""
    # Ensure directory exists
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    with open(CONFIG_PATH, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)


# =============================================================================
# UI Pages
# =============================================================================


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard page."""
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "active_page": "dashboard"}
    )


@router.get("/ui/library", response_class=HTMLResponse)
async def library_page(request: Request):
    """Library reports page."""
    return templates.TemplateResponse(
        "library.html",
        {"request": request, "active_page": "library"}
    )


@router.get("/ui/reports", response_class=HTMLResponse)
async def reports_page(request: Request):
    """Reports page."""
    return templates.TemplateResponse(
        "reports.html",
        {"request": request, "active_page": "reports"}
    )


@router.get("/ui/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    """Logs page."""
    return templates.TemplateResponse(
        "logs.html",
        {"request": request, "active_page": "logs"}
    )


@router.get("/ui/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """Settings page."""
    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "active_page": "settings"}
    )


# =============================================================================
# Authentication (Plex OAuth)
# =============================================================================


async def get_current_user(request: Request) -> plex_auth.PlexUser | None:
    """Get current authenticated user from session cookie."""
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_token:
        return None

    plex_token = _sessions.get(session_token)
    if not plex_token:
        return None

    # Validate the token is still valid
    try:
        return await plex_auth.get_user_info(plex_token)
    except Exception:
        # Token invalid, remove session
        del _sessions[session_token]
        return None


@router.get("/auth/plex/start")
async def plex_login_start(response: Response):
    """
    Start Plex OAuth flow.
    Returns the auth URL and stores the session ID in a cookie.
    """
    try:
        session_id, pin = await plex_auth.create_pin()

        # Store session ID in cookie for callback
        response.set_cookie(
            AUTH_PENDING_COOKIE,
            session_id,
            max_age=900,  # 15 min
            httponly=True,
            samesite="lax",
        )

        return {
            "success": True,
            "auth_url": pin.auth_url,
            "expires_in": 900,
        }

    except Exception as e:
        logger.error("Failed to start Plex auth", error=str(e))
        return {"success": False, "error": str(e)}


@router.get("/auth/plex/check")
async def plex_login_check(request: Request, response: Response):
    """
    Check if Plex auth has completed.
    Poll this endpoint after redirecting user to auth_url.
    """
    session_id = request.cookies.get(AUTH_PENDING_COOKIE)
    if not session_id:
        return {"success": False, "error": "No pending auth", "done": True}

    try:
        user = await plex_auth.check_pin(session_id)

        if user is None:
            # Still waiting for user to authorize
            return {"success": True, "done": False}

        # User authorized! Create session
        session_token = secrets.token_urlsafe(32)
        _sessions[session_token] = user.auth_token

        # Set session cookie
        response.set_cookie(
            SESSION_COOKIE_NAME,
            session_token,
            max_age=30 * 24 * 60 * 60,  # 30 days
            httponly=True,
            samesite="lax",
        )

        # Clear pending auth cookie
        response.delete_cookie(AUTH_PENDING_COOKIE)

        # Save token to config for backend services
        try:
            existing = get_config_dict()
            existing.setdefault("plex", {})
            existing["plex"]["token"] = user.auth_token
            existing["plex"]["enabled"] = True

            # Try to auto-detect Plex server URL if not set
            if not existing["plex"].get("url"):
                servers = await plex_auth.get_user_servers(user.auth_token)
                if servers:
                    # Prefer owned local server
                    owned_local = next((s for s in servers if s["owned"] and s["local"]), None)
                    owned = next((s for s in servers if s["owned"]), None)
                    best = owned_local or owned or servers[0]
                    existing["plex"]["url"] = best["uri"] or f"http://{best['address']}:{best['port']}"
                    logger.info("Auto-detected Plex server", name=best["name"], url=existing["plex"]["url"])

            save_config_dict(existing)
            logger.info("Saved Plex token to config", username=user.username)
        except Exception as e:
            logger.warning("Failed to save Plex token to config", error=str(e))

        return {
            "success": True,
            "done": True,
            "user": {
                "username": user.username,
                "email": user.email,
                "thumb": user.thumb,
            },
        }

    except Exception as e:
        logger.error("Failed to check Plex auth", error=str(e))
        return {"success": False, "error": str(e), "done": True}


@router.post("/auth/logout")
async def logout(response: Response, request: Request):
    """Log out the current user."""
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if session_token and session_token in _sessions:
        del _sessions[session_token]

    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"success": True}


@router.get("/auth/user")
async def get_user(request: Request):
    """Get the currently logged-in user."""
    user = await get_current_user(request)
    if user:
        return {
            "logged_in": True,
            "user": {
                "username": user.username,
                "email": user.email,
                "thumb": user.thumb,
            },
        }
    return {"logged_in": False}


# =============================================================================
# Config API
# =============================================================================


def mask_secret(value: str, show_chars: int = 8) -> str:
    """Mask a secret value, showing only first few chars."""
    if not value:
        return ""
    if len(value) <= show_chars:
        return "***"
    return value[:show_chars] + "..."


@router.get("/api/config")
async def get_config():
    """Get current configuration."""
    config = get_config_dict()

    # Mask sensitive fields
    if config.get("email", {}).get("smtp_password"):
        config["email"]["smtp_password"] = "***"

    # Mask Plex token
    if config.get("plex", {}).get("token"):
        config["plex"]["token"] = mask_secret(config["plex"]["token"])

    # Mask Radarr/Sonarr API keys
    for instance in config.get("radarr", []):
        if instance.get("api_key"):
            instance["api_key"] = mask_secret(instance["api_key"])

    for instance in config.get("sonarr", []):
        if instance.get("api_key"):
            instance["api_key"] = mask_secret(instance["api_key"])

    # Mask notification secrets
    notifications = config.get("notifications", {})
    if notifications.get("discord", {}).get("webhook_url"):
        notifications["discord"]["webhook_url"] = mask_secret(notifications["discord"]["webhook_url"], 40)
    if notifications.get("slack", {}).get("webhook_url"):
        notifications["slack"]["webhook_url"] = mask_secret(notifications["slack"]["webhook_url"], 40)
    if notifications.get("telegram", {}).get("bot_token"):
        notifications["telegram"]["bot_token"] = mask_secret(notifications["telegram"]["bot_token"])
    if notifications.get("pushover", {}).get("api_token"):
        notifications["pushover"]["api_token"] = mask_secret(notifications["pushover"]["api_token"])
    if notifications.get("gotify", {}).get("app_token"):
        notifications["gotify"]["app_token"] = mask_secret(notifications["gotify"]["app_token"])

    return config


def is_masked(value: str) -> bool:
    """Check if a value is masked."""
    if not value:
        return False
    return value == "***" or value.endswith("...")


@router.post("/api/config")
async def save_config(request: Request):
    """Save configuration."""
    try:
        new_config = await request.json()

        # Load existing config to preserve secrets if not changed
        existing = get_config_dict()

        # Preserve Radarr API keys if masked
        for i, inst in enumerate(new_config.get("radarr", [])):
            if is_masked(inst.get("api_key", "")):
                # Try to find existing instance by name or URL
                for existing_inst in existing.get("radarr", []):
                    if existing_inst.get("name") == inst.get("name") or existing_inst.get("url") == inst.get("url"):
                        inst["api_key"] = existing_inst.get("api_key", "")
                        break

        # Preserve Sonarr API keys if masked
        for i, inst in enumerate(new_config.get("sonarr", [])):
            if is_masked(inst.get("api_key", "")):
                for existing_inst in existing.get("sonarr", []):
                    if existing_inst.get("name") == inst.get("name") or existing_inst.get("url") == inst.get("url"):
                        inst["api_key"] = existing_inst.get("api_key", "")
                        break

        # Preserve Plex token if masked
        if is_masked(new_config.get("plex", {}).get("token", "")):
            new_config.setdefault("plex", {})["token"] = existing.get("plex", {}).get("token", "")

        # Preserve email password if masked
        if is_masked(new_config.get("email", {}).get("smtp_password", "")):
            new_config.setdefault("email", {})["smtp_password"] = existing.get("email", {}).get("smtp_password", "")

        # Preserve notification secrets if masked
        existing_notif = existing.get("notifications", {})
        new_notif = new_config.get("notifications", {})

        if is_masked(new_notif.get("discord", {}).get("webhook_url", "")):
            new_notif.setdefault("discord", {})["webhook_url"] = existing_notif.get("discord", {}).get("webhook_url", "")
        if is_masked(new_notif.get("slack", {}).get("webhook_url", "")):
            new_notif.setdefault("slack", {})["webhook_url"] = existing_notif.get("slack", {}).get("webhook_url", "")
        if is_masked(new_notif.get("telegram", {}).get("bot_token", "")):
            new_notif.setdefault("telegram", {})["bot_token"] = existing_notif.get("telegram", {}).get("bot_token", "")
        if is_masked(new_notif.get("pushover", {}).get("api_token", "")):
            new_notif.setdefault("pushover", {})["api_token"] = existing_notif.get("pushover", {}).get("api_token", "")
        if is_masked(new_notif.get("gotify", {}).get("app_token", "")):
            new_notif.setdefault("gotify", {})["app_token"] = existing_notif.get("gotify", {}).get("app_token", "")

        save_config_dict(new_config)

        # Hot-reload config into running janitor
        try:
            from .config import Config
            from . import webhook
            if webhook._janitor:
                parsed_config = Config(**new_config)
                webhook._janitor.reload_config(parsed_config)
                logger.info("Hot-reloaded config into janitor")
        except Exception as reload_err:
            logger.warning("Config saved but hot-reload failed", error=str(reload_err))

        return {"success": True}

    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/api/test-connection")
async def test_connection(request: Request):
    """Test connection to Radarr/Sonarr and fetch root folders."""
    try:
        data = await request.json()
        url = data.get("url", "").rstrip("/")
        api_key = data.get("api_key", "")

        # If API key is masked, get from config
        if api_key.endswith("..."):
            existing = get_config_dict()
            arr_type = data.get("type", "radarr")
            for inst in existing.get(arr_type, []):
                if inst.get("url", "").rstrip("/") == url:
                    api_key = inst.get("api_key", "")
                    break

        async with httpx.AsyncClient(timeout=10.0) as client:
            # Test connection
            response = await client.get(
                f"{url}/api/v3/system/status",
                headers={"X-Api-Key": api_key}
            )
            response.raise_for_status()
            status = response.json()

            # Fetch root folders
            root_response = await client.get(
                f"{url}/api/v3/rootfolder",
                headers={"X-Api-Key": api_key}
            )
            root_response.raise_for_status()
            root_folders = root_response.json()

            return {
                "success": True,
                "version": status.get("version", "unknown"),
                "root_folders": [
                    {
                        "path": rf.get("path"),
                        "free_space": rf.get("freeSpace", 0),
                        "accessible": rf.get("accessible", True),
                    }
                    for rf in root_folders
                ]
            }

    except httpx.TimeoutException:
        return {"success": False, "error": "Connection timed out"}
    except httpx.HTTPStatusError as e:
        return {"success": False, "error": f"HTTP {e.response.status_code}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/api/test-plex")
async def test_plex(request: Request):
    """Test connection to Plex server."""
    try:
        data = await request.json()
        url = data.get("url", "").rstrip("/")
        token = data.get("token", "")

        # If token is masked, get from config
        if token == "***" or token.endswith("..."):
            existing = get_config_dict()
            token = existing.get("plex", {}).get("token", "")

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{url}/identity",
                headers={"X-Plex-Token": token, "Accept": "application/json"}
            )
            response.raise_for_status()
            data = response.json()

            # Get libraries
            lib_response = await client.get(
                f"{url}/library/sections",
                headers={"X-Plex-Token": token, "Accept": "application/json"}
            )
            lib_response.raise_for_status()
            lib_data = lib_response.json()

            libraries = []
            for lib in lib_data.get("MediaContainer", {}).get("Directory", []):
                libraries.append({
                    "title": lib.get("title"),
                    "type": lib.get("type"),
                    "key": lib.get("key"),
                })

            return {
                "success": True,
                "server_name": data.get("MediaContainer", {}).get("machineIdentifier", "unknown"),
                "libraries": libraries
            }

    except httpx.TimeoutException:
        return {"success": False, "error": "Connection timed out"}
    except httpx.HTTPStatusError as e:
        return {"success": False, "error": f"HTTP {e.response.status_code}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/api/test-notification")
async def test_notification(request: Request):
    """Test a notification service."""
    try:
        data = await request.json()
        service = data.get("service")
        config = get_config_dict()

        if service == "discord":
            webhook_url = data.get("webhook_url") or config.get("notifications", {}).get("discord", {}).get("webhook_url")
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    webhook_url,
                    json={
                        "content": "Test notification from Media Janitor",
                        "embeds": [{
                            "title": "Test Successful",
                            "description": "Discord notifications are working!",
                            "color": 0xf5a623
                        }]
                    }
                )
                response.raise_for_status()
                return {"success": True}

        elif service == "slack":
            webhook_url = data.get("webhook_url") or config.get("notifications", {}).get("slack", {}).get("webhook_url")
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    webhook_url,
                    json={"text": "Test notification from Media Janitor - Slack is working!"}
                )
                response.raise_for_status()
                return {"success": True}

        elif service == "telegram":
            bot_token = data.get("bot_token") or config.get("notifications", {}).get("telegram", {}).get("bot_token")
            chat_id = data.get("chat_id") or config.get("notifications", {}).get("telegram", {}).get("chat_id")
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={"chat_id": chat_id, "text": "Test notification from Media Janitor - Telegram is working!"}
                )
                response.raise_for_status()
                return {"success": True}

        elif service == "pushover":
            user_key = data.get("user_key") or config.get("notifications", {}).get("pushover", {}).get("user_key")
            api_token = data.get("api_token") or config.get("notifications", {}).get("pushover", {}).get("api_token")
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    "https://api.pushover.net/1/messages.json",
                    data={
                        "token": api_token,
                        "user": user_key,
                        "message": "Test notification from Media Janitor - Pushover is working!"
                    }
                )
                response.raise_for_status()
                return {"success": True}

        elif service == "gotify":
            server_url = data.get("server_url") or config.get("notifications", {}).get("gotify", {}).get("server_url")
            app_token = data.get("app_token") or config.get("notifications", {}).get("gotify", {}).get("app_token")
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{server_url.rstrip('/')}/message",
                    headers={"X-Gotify-Key": app_token},
                    json={"title": "Media Janitor", "message": "Test notification - Gotify is working!"}
                )
                response.raise_for_status()
                return {"success": True}

        else:
            return {"success": False, "error": f"Unknown service: {service}"}

    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/api/test-email")
async def test_email():
    """Send a test email."""
    try:
        config = get_config_dict()
        email_config = config.get("email", {})

        if not email_config.get("enabled"):
            return {"success": False, "error": "Email not enabled"}

        import aiosmtplib
        from email.mime.text import MIMEText

        msg = MIMEText("This is a test email from Media Janitor.")
        msg["Subject"] = "Media Janitor Test Email"
        msg["From"] = email_config.get("from_address", "")
        msg["To"] = email_config.get("to_address", "")

        await aiosmtplib.send(
            msg,
            hostname=email_config.get("smtp_host", "smtp.gmail.com"),
            port=email_config.get("smtp_port", 587),
            username=email_config.get("smtp_user", ""),
            password=email_config.get("smtp_password", ""),
            start_tls=True,
        )

        return {"success": True}

    except Exception as e:
        return {"success": False, "error": str(e)}


# =============================================================================
# Additional Report APIs
# =============================================================================


@router.get("/api/library/duplicates")
async def get_duplicates():
    """Find potential duplicate files."""
    # This will be implemented to detect duplicates
    return {"duplicates": [], "message": "Coming soon"}


@router.get("/api/library/codecs")
async def get_codec_breakdown():
    """Get codec breakdown of library."""
    # This will be implemented to show codec stats
    return {"codecs": {}, "message": "Coming soon"}


@router.get("/api/library/hdr")
async def get_hdr_breakdown():
    """Get HDR breakdown of library."""
    return {"hdr": {}, "message": "Coming soon"}
