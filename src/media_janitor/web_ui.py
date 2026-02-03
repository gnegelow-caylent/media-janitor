"""Web UI routes and API endpoints."""

import httpx
import yaml
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .config import Config

router = APIRouter()

# Setup templates
templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

# Config file path
CONFIG_PATH = Path("/data/config.yaml")


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
# Config API
# =============================================================================


@router.get("/api/config")
async def get_config():
    """Get current configuration."""
    config = get_config_dict()

    # Mask sensitive fields
    if config.get("email", {}).get("smtp_password"):
        config["email"]["smtp_password"] = ""  # Don't send password to frontend

    for instance in config.get("radarr", []):
        if instance.get("api_key"):
            instance["api_key"] = instance["api_key"][:8] + "..." if len(instance["api_key"]) > 8 else "***"

    for instance in config.get("sonarr", []):
        if instance.get("api_key"):
            instance["api_key"] = instance["api_key"][:8] + "..." if len(instance["api_key"]) > 8 else "***"

    return config


@router.post("/api/config")
async def save_config(request: Request):
    """Save configuration."""
    try:
        new_config = await request.json()

        # Load existing config to preserve passwords if not changed
        existing = get_config_dict()

        # Preserve API keys if masked
        for i, inst in enumerate(new_config.get("radarr", [])):
            if inst.get("api_key", "").endswith("..."):
                if i < len(existing.get("radarr", [])):
                    inst["api_key"] = existing["radarr"][i].get("api_key", "")

        for i, inst in enumerate(new_config.get("sonarr", [])):
            if inst.get("api_key", "").endswith("..."):
                if i < len(existing.get("sonarr", [])):
                    inst["api_key"] = existing["sonarr"][i].get("api_key", "")

        # Preserve email password if not changed
        if not new_config.get("email", {}).get("smtp_password"):
            if existing.get("email", {}).get("smtp_password"):
                new_config.setdefault("email", {})["smtp_password"] = existing["email"]["smtp_password"]

        save_config_dict(new_config)
        return {"success": True}

    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/api/test-connection")
async def test_connection(request: Request):
    """Test connection to Radarr/Sonarr."""
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
            response = await client.get(
                f"{url}/api/v3/system/status",
                headers={"X-Api-Key": api_key}
            )
            response.raise_for_status()
            return {"success": True}

    except httpx.TimeoutException:
        return {"success": False, "error": "Connection timed out"}
    except httpx.HTTPStatusError as e:
        return {"success": False, "error": f"HTTP {e.response.status_code}"}
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
