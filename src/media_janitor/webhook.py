"""Webhook server for receiving notifications from Radarr/Sonarr."""

import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import Any

import structlog
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .arr_client import ArrClient, ArrType, MediaItem
from .config import Config, PlexConfig
from .janitor import Janitor
from .plex_client import PlexClient
from .reports import (
    format_report_email,
    format_report_text,
    generate_mismatch_report,
    find_duplicates,
    get_codec_breakdown,
    bytes_to_human,
)

logger = structlog.get_logger()

app = FastAPI(title="Media Janitor", description="Proactive media library quality monitor")

# Mount static files if they exist
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# Global references set by main.py
_config: Config | None = None
_janitor: Janitor | None = None


def _get_plex_client() -> PlexClient | None:
    """Get a Plex client, creating from fresh config if needed."""
    # If janitor has one, use it
    if _janitor and _janitor.plex:
        return _janitor.plex

    # Try to create from current config file
    try:
        from .web_ui import get_config_dict
        config = get_config_dict()
        plex_cfg = config.get("plex", {})
        if plex_cfg.get("enabled") and plex_cfg.get("url") and plex_cfg.get("token"):
            plex_config = PlexConfig(
                enabled=True,
                url=plex_cfg["url"],
                token=plex_cfg["token"],
                refresh_on_replace=plex_cfg.get("refresh_on_replace", True),
            )
            client = PlexClient(plex_config)
            # Update janitor's plex client if we created a new one
            if _janitor:
                _janitor.plex = client
                _janitor.scanner.set_plex_client(client)
            return client
    except Exception as e:
        logger.warning("Failed to create Plex client from config", error=str(e))

    return None


def init_webhook_app(config: Config, janitor: Janitor) -> FastAPI:
    """Initialize the webhook app with config and janitor."""
    global _config, _janitor
    _config = config
    _janitor = janitor

    # Include web UI router
    from .web_ui import router as ui_router
    app.include_router(ui_router)

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

    # Extract file info from payload and create MediaItem for cache
    file_path = None
    media_item = None
    instance_name = payload.get("instanceName", "")

    # Find the appropriate client for path translation
    client = None
    if _janitor:
        for c in _janitor.scanner.get_all_clients():
            if c.arr_type == arr_type and (not instance_name or c.instance.name == instance_name):
                client = c
                break

    if arr_type == ArrType.RADARR:
        movie = payload.get("movie", {})
        movie_file = payload.get("movieFile", {})
        raw_path = movie_file.get("path") or movie_file.get("relativePath")
        title = movie.get("title", "Unknown Movie")

        # Translate path using client's path mappings
        file_path = client.translate_path(raw_path) if client else raw_path

        # Create MediaItem from webhook payload
        if file_path and movie_file.get("id"):
            media_item = MediaItem(
                id=movie.get("id", 0),
                title=title,
                file_path=file_path,
                file_id=movie_file.get("id"),
                quality=movie_file.get("quality", {}).get("quality", {}).get("name"),
                size_bytes=movie_file.get("size"),
                arr_type=ArrType.RADARR,
                arr_instance=instance_name or "radarr",
                year=movie.get("year"),
                folder_path=client.translate_path(movie.get("folderPath")) if client else movie.get("folderPath"),
            )
    else:
        series = payload.get("series", {})
        episode_file = payload.get("episodeFile", {})
        episodes = payload.get("episodes", [{}])
        ep_info = episodes[0] if episodes else {}
        raw_path = episode_file.get("path") or episode_file.get("relativePath")
        title = f"{series.get('title', 'Unknown')} S{ep_info.get('seasonNumber', 0):02d}E{ep_info.get('episodeNumber', 0):02d}"

        # Translate path using client's path mappings
        file_path = client.translate_path(raw_path) if client else raw_path

        # Create MediaItem from webhook payload
        if file_path and episode_file.get("id"):
            episode_id = ep_info.get("id")  # The actual episode ID for searches
            media_item = MediaItem(
                id=episode_id or 0,
                title=title,
                file_path=file_path,
                file_id=episode_file.get("id"),
                quality=episode_file.get("quality", {}).get("quality", {}).get("name"),
                size_bytes=episode_file.get("size"),
                series_id=series.get("id"),
                season_number=ep_info.get("seasonNumber"),
                episode_number=ep_info.get("episodeNumber"),
                episode_id=episode_id,  # Explicit episode ID for Sonarr searches
                arr_type=ArrType.SONARR,
                arr_instance=instance_name or "sonarr",
            )

    if not file_path:
        log.warning("No file path in webhook payload")
        return {"status": "error", "message": "No file path found in payload"}

    log.info("Processing imported file", file_path=file_path, title=title)

    # Add to cache so validate_and_process can find it
    if _janitor and media_item:
        _janitor.scanner.add_to_cache(media_item)

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
            "quality_by_instance": report.quality_by_instance,
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


@app.get("/report/replaced")
async def get_replaced_report(
    format: str = Query(default="json", description="Output format: json or text"),
):
    """
    Get list of files that have been replaced.

    Shows files that were deleted and re-downloaded due to validation failures.
    """
    if not _janitor:
        return {"status": "error", "message": "Janitor not initialized"}

    replaced = _janitor.state.get_replaced_files()
    # Reverse to show most recent first
    replaced = list(reversed(replaced))

    if format == "text":
        lines = [
            "=" * 70,
            "REPLACED FILES REPORT",
            f"Total: {len(replaced)} files replaced",
            "=" * 70,
        ]
        for r in replaced[:100]:
            lines.append(f"\n{r.get('title', 'Unknown')} - {r.get('timestamp', '')[:10]}")
            lines.append(f"  Path: {r.get('path', 'Unknown')}")
            lines.append(f"  Reason: {r.get('reason', 'Unknown')}")
            if r.get('wrong_file'):
                lines.append("  Type: Wrong file in folder")
        return PlainTextResponse(content="\n".join(lines))
    else:
        return {
            "count": len(replaced),
            "replaced": replaced[:100],  # Limit response size
        }


@app.get("/report/missing")
async def get_missing_report(
    format: str = Query(default="json", description="Output format: json or text"),
):
    """
    Get list of files that exist in Radarr/Sonarr but not on disk.

    These are files that may have been deleted or moved outside of arr management.
    """
    if not _janitor:
        return {"status": "error", "message": "Janitor not initialized"}

    missing = _janitor.state.get_missing_files()
    # Reverse to show most recent first
    missing = list(reversed(missing))

    # Separate by media type
    movies = [m for m in missing if m.get("media_type") == "movie"]
    tv = [m for m in missing if m.get("media_type") == "tv"]

    if format == "text":
        lines = [
            "=" * 70,
            "MISSING FILES REPORT",
            f"Total: {len(missing)} files (Movies: {len(movies)}, TV: {len(tv)})",
            "=" * 70,
            "",
            "These files exist in Radarr/Sonarr but not on disk.",
            "Consider removing them from your arr apps or re-downloading.",
            "",
        ]
        if movies:
            lines.append("--- MOVIES ---")
            for m in movies[:100]:
                lines.append(f"  {m.get('path', 'Unknown')}")
        if tv:
            lines.append("\n--- TV EPISODES ---")
            for m in tv[:200]:
                lines.append(f"  {m.get('path', 'Unknown')}")
        return PlainTextResponse(content="\n".join(lines))
    else:
        return {
            "count": len(missing),
            "movies_count": len(movies),
            "tv_count": len(tv),
            "movies": movies[:100],
            "tv": tv[:200],
        }


@app.post("/report/missing/clear")
async def clear_missing_report():
    """Clear the missing files report."""
    if not _janitor:
        return {"status": "error", "message": "Janitor not initialized"}

    _janitor.state.clear_missing_files()
    return {"status": "ok", "message": "Missing files report cleared"}


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


@app.get("/report/duplicates")
async def get_duplicates_report(
    source: str = Query(default="movies", description="Source: movies (fast) or tv (slow)"),
    format: str = Query(default="json", description="Output format: json, text"),
):
    """
    Find duplicate content (same movie/show in multiple qualities).

    This helps identify wasted space from having the same content multiple times.
    """
    if not _janitor:
        return {"status": "error", "message": "Janitor not initialized"}

    duplicates = await find_duplicates(_janitor.scanner, source)

    if format == "text":
        lines = [
            "=" * 70,
            "DUPLICATE CONTENT REPORT",
            f"Found {len(duplicates)} items with multiple copies",
            "=" * 70,
            "",
        ]
        for d in duplicates:
            lines.append(f"{d.title} ({d.year or 'Unknown year'})")
            lines.append(f"  Total size: {bytes_to_human(d.total_size_bytes)}")
            lines.append(f"  Potential savings: {bytes_to_human(d.potential_savings_bytes)}")
            lines.append(f"  Copies: {len(d.files)}")
            for f in d.files:
                lines.append(f"    - [{f.size_human}] {f.quality or 'Unknown quality'}")
            lines.append("")

        return PlainTextResponse(content="\n".join(lines))
    else:
        total_savings = sum(d.potential_savings_bytes for d in duplicates)
        return {
            "count": len(duplicates),
            "total_potential_savings_bytes": total_savings,
            "total_potential_savings_human": bytes_to_human(total_savings),
            "duplicates": [
                {
                    "title": d.title,
                    "year": d.year,
                    "total_size_bytes": d.total_size_bytes,
                    "total_size_human": bytes_to_human(d.total_size_bytes),
                    "potential_savings_bytes": d.potential_savings_bytes,
                    "potential_savings_human": bytes_to_human(d.potential_savings_bytes),
                    "copies": len(d.files),
                    "files": [
                        {
                            "file_path": f.file_path,
                            "size_bytes": f.size_bytes,
                            "size_human": f.size_human,
                            "quality": f.quality,
                            "arr_instance": f.arr_instance,
                        }
                        for f in d.files
                    ],
                }
                for d in duplicates
            ],
        }


@app.get("/report/codecs")
async def get_codecs_report(
    source: str = Query(default="movies", description="Source: movies (fast) or tv (slow)"),
    format: str = Query(default="json", description="Output format: json, text"),
):
    """
    Get codec/HDR breakdown of the library.

    Shows distribution of video codecs (H.264, HEVC, AV1) and HDR types.
    """
    if not _janitor:
        return {"status": "error", "message": "Janitor not initialized"}

    stats = await get_codec_breakdown(_janitor.scanner, source)

    if format == "text":
        lines = [
            "=" * 70,
            "CODEC BREAKDOWN REPORT",
            f"Total files: {stats.total_files}",
            "=" * 70,
            "",
            "VIDEO CODECS:",
        ]
        for codec, count in sorted(stats.video_codecs.items(), key=lambda x: -x[1]):
            pct = (count / stats.total_files * 100) if stats.total_files else 0
            lines.append(f"  {codec}: {count} ({pct:.1f}%)")

        lines.append("")
        lines.append("CONTAINERS:")
        for container, count in sorted(stats.containers.items(), key=lambda x: -x[1]):
            pct = (count / stats.total_files * 100) if stats.total_files else 0
            lines.append(f"  .{container}: {count} ({pct:.1f}%)")

        lines.append("")
        lines.append("HDR TYPES:")
        for hdr, count in sorted(stats.hdr_types.items(), key=lambda x: -x[1]):
            pct = (count / stats.total_files * 100) if stats.total_files else 0
            lines.append(f"  {hdr}: {count} ({pct:.1f}%)")

        return PlainTextResponse(content="\n".join(lines))
    else:
        return {
            "total_files": stats.total_files,
            "video_codecs": stats.video_codecs,
            "audio_codecs": stats.audio_codecs,
            "containers": stats.containers,
            "hdr_types": stats.hdr_types,
        }


# =============================================================================
# Plex Reports
# =============================================================================


@app.get("/report/upgrades")
async def get_quality_upgrades(
    min_views: int = Query(default=1, ge=1, description="Minimum view count to include"),
    max_resolution: str = Query(default="720", description="Max resolution to suggest upgrade: 480, 720, 1080"),
    format: str = Query(default="json", description="Output format: json or text"),
):
    """
    Find watched content that could be upgraded to better quality.

    Requires Plex integration to be enabled.
    """
    if not _janitor:
        return {"status": "error", "message": "Janitor not initialized"}

    plex = _get_plex_client()
    if not plex:
        raise HTTPException(status_code=400, detail="Plex integration not enabled. Login with Plex first.")

    upgrades = await plex.get_quality_upgrade_candidates(min_views, max_resolution)

    if format == "text":
        lines = [
            "=" * 70,
            "QUALITY UPGRADE SUGGESTIONS",
            f"Found {len(upgrades)} watched items that could be upgraded",
            "=" * 70,
        ]
        for u in upgrades[:50]:
            lines.append(f"\n{u.title} ({u.year or 'N/A'})")
            lines.append(f"  Current: {u.current_resolution} @ {u.current_bitrate or 'N/A'} kbps")
            lines.append(f"  Suggested: {u.suggested_quality}")
            lines.append(f"  Views: {u.view_count}")
        return PlainTextResponse(content="\n".join(lines))
    else:
        return {
            "count": len(upgrades),
            "upgrades": [
                {
                    "title": u.title,
                    "year": u.year,
                    "file_path": u.file_path,
                    "current_resolution": u.current_resolution,
                    "current_bitrate": u.current_bitrate,
                    "view_count": u.view_count,
                    "last_viewed": u.last_viewed.isoformat() if u.last_viewed else None,
                    "suggested_quality": u.suggested_quality,
                }
                for u in upgrades
            ],
        }


@app.get("/report/playback-issues")
async def get_playback_issues(
    min_progress: float = Query(default=5.0, ge=0, le=100, description="Min progress % to consider"),
    max_progress: float = Query(default=90.0, ge=0, le=100, description="Max progress % (above = almost done)"),
    format: str = Query(default="json", description="Output format: json or text"),
):
    """
    Find potential playback issues by analyzing viewing patterns.

    Detects items that were started but abandoned, which could indicate
    playback problems (buffering, codec issues, corruption).

    Requires Plex integration to be enabled.
    """
    if not _janitor:
        return {"status": "error", "message": "Janitor not initialized"}

    plex = _get_plex_client()
    if not plex:
        raise HTTPException(status_code=400, detail="Plex integration not enabled. Login with Plex first.")

    issues = await plex.get_playback_issues(min_progress, max_progress)

    if format == "text":
        lines = [
            "=" * 70,
            "POTENTIAL PLAYBACK ISSUES",
            f"Found {len(issues)} items with suspicious viewing patterns",
            "=" * 70,
        ]
        for issue in issues[:50]:
            lines.append(f"\n{issue.title} ({issue.year or 'N/A'})")
            lines.append(f"  {issue.details}")
            lines.append(f"  File: {issue.file_path or 'Unknown'}")
            if issue.last_activity:
                lines.append(f"  Last activity: {issue.last_activity.strftime('%Y-%m-%d %H:%M')}")
        return PlainTextResponse(content="\n".join(lines))
    else:
        return {
            "count": len(issues),
            "issues": [
                {
                    "rating_key": i.rating_key,
                    "title": i.title,
                    "year": i.year,
                    "file_path": i.file_path,
                    "issue_type": i.issue_type,
                    "details": i.details,
                    "progress_pct": round(i.view_offset_pct, 1),
                    "last_activity": i.last_activity.isoformat() if i.last_activity else None,
                }
                for i in issues
            ],
        }


@app.get("/report/orphans")
async def get_orphan_report(
    format: str = Query(default="json", description="Output format: json or text"),
):
    """
    Find orphaned files between Plex and Radarr/Sonarr.

    - in_plex_not_arr: Files in Plex but not in Radarr/Sonarr
    - in_arr_not_plex: Files in Radarr/Sonarr but not in Plex

    Requires Plex integration to be enabled.
    """
    if not _janitor:
        return {"status": "error", "message": "Janitor not initialized"}

    plex = _get_plex_client()
    if not plex:
        raise HTTPException(status_code=400, detail="Plex integration not enabled. Login with Plex first.")

    # Get all file paths from cache only - never fetch from API
    arr_paths: set[str] = set()
    cached_media = _janitor.scanner.get_cached_media("all")
    if cached_media:
        for item in cached_media:
            if item.file_path:
                arr_paths.add(item.file_path)
    else:
        raise HTTPException(status_code=503, detail="Library cache not loaded yet. Please wait for initial scan to complete.")

    in_plex_not_arr, in_arr_not_plex = await plex.find_orphans(arr_paths)

    if format == "text":
        lines = [
            "=" * 70,
            "ORPHAN FILES REPORT",
            "=" * 70,
            f"\nIn Plex but NOT in Radarr/Sonarr ({len(in_plex_not_arr)}):",
        ]
        for p in in_plex_not_arr[:50]:
            lines.append(f"  {p}")
        if len(in_plex_not_arr) > 50:
            lines.append(f"  ... and {len(in_plex_not_arr) - 50} more")

        lines.append(f"\nIn Radarr/Sonarr but NOT in Plex ({len(in_arr_not_plex)}):")
        for p in in_arr_not_plex[:50]:
            lines.append(f"  {p}")
        if len(in_arr_not_plex) > 50:
            lines.append(f"  ... and {len(in_arr_not_plex) - 50} more")

        return PlainTextResponse(content="\n".join(lines))
    else:
        return {
            "in_plex_not_arr": {
                "count": len(in_plex_not_arr),
                "files": in_plex_not_arr[:100],  # Limit response size
            },
            "in_arr_not_plex": {
                "count": len(in_arr_not_plex),
                "files": in_arr_not_plex[:100],
            },
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
async def refresh_library(
    source: str = Query(default="movies", description="Source: movies (fast), tv (slow), or all"),
):
    """
    Refresh the library list from Radarr/Sonarr.

    Use source=movies for fast refresh (Radarr only).
    Use source=tv for TV shows (Sonarr - very slow on large libraries).
    """
    if not _janitor:
        return {"status": "error", "message": "Janitor not initialized"}

    count = await _janitor.scanner.refresh_library(source)
    return {"status": "ok", "files_to_scan": count, "source": source}


@app.post("/state/clear")
async def clear_state():
    """Clear all scan state (forces full re-scan)."""
    if not _janitor:
        return {"status": "error", "message": "Janitor not initialized"}

    _janitor.state.clear()
    _janitor.reset_replacement_count()
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
