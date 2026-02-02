"""Webhook server for receiving notifications from Radarr/Sonarr."""

import asyncio
from dataclasses import asdict
from typing import Any

import structlog
from fastapi import BackgroundTasks, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from .arr_client import ArrClient, ArrType
from .config import Config
from .janitor import Janitor
from .reports import format_report_email, format_report_text, generate_mismatch_report

logger = structlog.get_logger()

app = FastAPI(title="Media Janitor", description="Proactive media library quality monitor")


# Global references set by main.py
_config: Config | None = None
_janitor: Janitor | None = None


def init_webhook_app(config: Config, janitor: Janitor) -> FastAPI:
    """Initialize the webhook app with config and janitor."""
    global _config, _janitor
    _config = config
    _janitor = janitor
    return app


# =============================================================================
# Health and Status
# =============================================================================


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


@app.get("/status")
async def get_status():
    """Get current janitor status."""
    if not _janitor:
        return {"status": "error", "message": "Janitor not initialized"}
    return _janitor.get_status()


# =============================================================================
# Webhooks (Radarr/Sonarr)
# =============================================================================


@app.post("/webhook/radarr")
async def radarr_webhook(request: Request, background_tasks: BackgroundTasks):
    """Handle Radarr webhook notifications."""
    try:
        payload = await request.json()
        return await _handle_arr_webhook(payload, ArrType.RADARR, background_tasks)
    except Exception as e:
        logger.error("Failed to process Radarr webhook", error=str(e))
        return {"status": "error", "message": str(e)}


@app.post("/webhook/sonarr")
async def sonarr_webhook(request: Request, background_tasks: BackgroundTasks):
    """Handle Sonarr webhook notifications."""
    try:
        payload = await request.json()
        return await _handle_arr_webhook(payload, ArrType.SONARR, background_tasks)
    except Exception as e:
        logger.error("Failed to process Sonarr webhook", error=str(e))
        return {"status": "error", "message": str(e)}


async def _handle_arr_webhook(
    payload: dict[str, Any],
    arr_type: ArrType,
    background_tasks: BackgroundTasks,
) -> dict:
    """Handle webhook payload from Radarr or Sonarr."""
    event_type = payload.get("eventType", "Unknown")
    log = logger.bind(event_type=event_type, arr_type=arr_type.value)

    log.info("Received webhook")

    # Only process import/upgrade events
    if event_type not in ["Download", "MovieFileImported", "EpisodeFileImported"]:
        log.debug("Ignoring event type")
        return {"status": "ignored", "reason": f"Event type {event_type} not processed"}

    # Extract file path from payload
    file_path = None

    if arr_type == ArrType.RADARR:
        movie_file = payload.get("movieFile", {})
        file_path = movie_file.get("path") or movie_file.get("relativePath")
        title = payload.get("movie", {}).get("title", "Unknown Movie")
    else:
        episode_file = payload.get("episodeFile", {})
        file_path = episode_file.get("path") or episode_file.get("relativePath")
        series = payload.get("series", {})
        episodes = payload.get("episodes", [{}])
        ep_info = episodes[0] if episodes else {}
        title = f"{series.get('title', 'Unknown')} S{ep_info.get('seasonNumber', 0):02d}E{ep_info.get('episodeNumber', 0):02d}"

    if not file_path:
        log.warning("No file path in webhook payload")
        return {"status": "error", "message": "No file path found in payload"}

    log.info("Processing imported file", file_path=file_path, title=title)

    # Queue validation in background
    if _janitor:
        background_tasks.add_task(_janitor.validate_and_process, file_path, arr_type)
        return {"status": "queued", "file": file_path}
    else:
        return {"status": "error", "message": "Janitor not initialized"}


@app.post("/webhook/test")
async def test_webhook(request: Request):
    """Test endpoint for verifying webhook connectivity."""
    try:
        payload = await request.json()
        logger.info("Test webhook received", payload=payload)
        return {"status": "ok", "received": payload}
    except Exception as e:
        return {"status": "ok", "message": "Test endpoint working", "error": str(e)}


# =============================================================================
# Reports
# =============================================================================


@app.get("/report/library")
async def get_library_report(
    top_n: int = Query(default=50, ge=1, le=500, description="Number of largest/smallest files to include"),
    format: str = Query(default="json", description="Output format: json, html, or text"),
    source: str = Query(default="all", description="Source: all, movies (fast), or tv (slow)"),
):
    """
    Generate a library report showing largest files, quality breakdown, etc.

    Use source=movies for fast results (Radarr only).
    Use source=tv for TV shows (Sonarr - slow on large libraries).
    Use source=all for everything (slowest).
    """
    if not _janitor:
        return {"status": "error", "message": "Janitor not initialized"}

    report = await _janitor.generate_library_report(top_n, source)

    if format == "html":
        html = format_report_email(report)
        return HTMLResponse(content=html)
    elif format == "text":
        text = format_report_text(report)
        return PlainTextResponse(content=text)
    else:
        # JSON format - convert dataclasses to dicts
        return {
            "generated_at": report.generated_at.isoformat(),
            "total_files": report.total_files,
            "total_size_bytes": report.total_size_bytes,
            "total_size_human": report.total_size_human,
            "largest_files": [
                {
                    "title": f.title,
                    "file_path": f.file_path,
                    "size_bytes": f.size_bytes,
                    "size_human": f.size_human,
                    "quality": f.quality,
                    "arr_instance": f.arr_instance,
                }
                for f in report.largest_files
            ],
            "smallest_files": [
                {
                    "title": f.title,
                    "file_path": f.file_path,
                    "size_bytes": f.size_bytes,
                    "size_human": f.size_human,
                    "quality": f.quality,
                    "arr_instance": f.arr_instance,
                }
                for f in report.smallest_files
            ],
            "files_by_quality": report.files_by_quality,
            "files_by_instance": report.files_by_instance,
        }


@app.post("/report/library/email")
async def email_library_report(
    top_n: int = Query(default=50, ge=1, le=500, description="Number of largest files to include"),
):
    """Generate a library report and send it via email."""
    if not _janitor:
        return {"status": "error", "message": "Janitor not initialized"}

    success = await _janitor.send_library_report(top_n)
    if success:
        return {"status": "ok", "message": "Report sent"}
    else:
        return {"status": "error", "message": "Failed to send email"}


@app.get("/report/mismatches")
async def get_mismatch_report(
    source: str = Query(default="movies", description="Source: movies (fast) or tv (slow)"),
    format: str = Query(default="json", description="Output format: json, text"),
):
    """
    Find files where the filename doesn't match the expected movie/show title.

    This detects cases like:
    - F.R.E.D.I. folder containing "Mission Impossible - Fallout.mkv"
    - Wrong movie in wrong folder
    """
    if not _janitor:
        return {"status": "error", "message": "Janitor not initialized"}

    mismatches = await generate_mismatch_report(_janitor.scanner, source)

    if format == "text":
        lines = [
            "=" * 70,
            "PATH MISMATCH REPORT",
            f"Found {len(mismatches)} mismatches",
            "=" * 70,
            "",
        ]
        for m in mismatches:
            lines.append(f"Expected: {m.expected_folder}")
            lines.append(f"  Actual: {m.actual_filename}")
            lines.append(f"    Path: {m.file_path}")
            lines.append(f"Instance: {m.arr_instance}")
            lines.append("")

        return PlainTextResponse(content="\n".join(lines))
    else:
        return {
            "count": len(mismatches),
            "mismatches": [
                {
                    "title": m.title,
                    "year": m.year,
                    "expected_folder": m.expected_folder,
                    "actual_filename": m.actual_filename,
                    "file_path": m.file_path,
                    "folder_path": m.folder_path,
                    "arr_instance": m.arr_instance,
                    "mismatch_type": m.mismatch_type,
                }
                for m in mismatches
            ],
        }


# =============================================================================
# Manual Actions
# =============================================================================


@app.post("/scan/trigger")
async def trigger_scan(background_tasks: BackgroundTasks):
    """Manually trigger a background scan batch."""
    if not _janitor:
        return {"status": "error", "message": "Janitor not initialized"}

    background_tasks.add_task(_janitor.run_background_scan)
    return {"status": "ok", "message": "Scan triggered"}


@app.post("/scan/refresh")
async def refresh_library():
    """Refresh the library list from Radarr/Sonarr."""
    if not _janitor:
        return {"status": "error", "message": "Janitor not initialized"}

    count = await _janitor.scanner.refresh_library()
    return {"status": "ok", "files_to_scan": count}


@app.post("/state/clear")
async def clear_state():
    """Clear all scan state (forces full re-scan)."""
    if not _janitor:
        return {"status": "error", "message": "Janitor not initialized"}

    _janitor.state.clear()
    return {"status": "ok", "message": "State cleared, will re-scan all files"}


# =============================================================================
# Logs
# =============================================================================


@app.get("/logs")
async def get_logs(
    lines: int = Query(default=100, ge=1, le=1000, description="Number of log lines to return"),
    level: str = Query(default="all", description="Filter by level: all, error, warning, info"),
):
    """
    Get recent log entries.

    Reads from the log file and returns recent entries.
    """
    if not _config:
        return {"status": "error", "message": "Config not initialized"}

    from pathlib import Path
    import json

    log_file = Path(_config.logging.file)
    if not log_file.exists():
        return {"status": "ok", "logs": [], "message": "No log file yet"}

    try:
        # Read last N lines efficiently
        with open(log_file, 'rb') as f:
            # Seek to end and work backwards
            f.seek(0, 2)  # End of file
            file_size = f.tell()

            # Read chunks from end until we have enough lines
            chunk_size = 8192
            lines_found = []
            position = file_size

            while position > 0 and len(lines_found) < lines + 1:
                read_size = min(chunk_size, position)
                position -= read_size
                f.seek(position)
                chunk = f.read(read_size).decode('utf-8', errors='replace')
                lines_found = chunk.split('\n') + lines_found

            # Take last N lines
            recent_lines = [l for l in lines_found if l.strip()][-lines:]

        # Parse and filter logs
        parsed_logs = []
        for line in recent_lines:
            try:
                entry = json.loads(line)
                log_level = entry.get("level", "info").lower()

                # Filter by level
                if level != "all":
                    if level == "error" and log_level not in ["error", "critical"]:
                        continue
                    elif level == "warning" and log_level not in ["error", "critical", "warning"]:
                        continue

                parsed_logs.append({
                    "timestamp": entry.get("timestamp", ""),
                    "level": log_level,
                    "event": entry.get("event", ""),
                    "component": entry.get("component", ""),
                    "details": {k: v for k, v in entry.items()
                               if k not in ["timestamp", "level", "event", "component", "logger"]},
                })
            except json.JSONDecodeError:
                # Not JSON, include raw line
                parsed_logs.append({
                    "timestamp": "",
                    "level": "info",
                    "event": line,
                    "component": "",
                    "details": {},
                })

        return {
            "status": "ok",
            "count": len(parsed_logs),
            "logs": parsed_logs,
        }

    except Exception as e:
        logger.error("Failed to read logs", error=str(e))
        return {"status": "error", "message": str(e)}


@app.get("/logs/errors")
async def get_errors(lines: int = Query(default=50, ge=1, le=500)):
    """Get recent errors only."""
    return await get_logs(lines=lines, level="error")
