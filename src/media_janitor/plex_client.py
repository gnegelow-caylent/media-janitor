"""Plex Media Server API client."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
import structlog

from .config import PlexConfig

logger = structlog.get_logger()


@dataclass
class PlexLibrary:
    """A Plex library section."""

    key: str  # Section ID
    title: str
    type: str  # "movie", "show", "artist", etc.
    agent: str
    scanner: str
    location: list[str]  # File paths


@dataclass
class PlexMediaItem:
    """A media item from Plex."""

    rating_key: str  # Unique ID
    title: str
    year: int | None
    type: str  # "movie", "episode", "track"
    library_key: str
    file_path: str | None
    # Media info
    duration_ms: int | None
    video_resolution: str | None
    video_codec: str | None
    audio_codec: str | None
    container: str | None
    bitrate: int | None  # kbps
    # Watch info
    view_count: int = 0
    last_viewed_at: datetime | None = None
    added_at: datetime | None = None
    # For episodes
    show_title: str | None = None
    season_number: int | None = None
    episode_number: int | None = None


@dataclass
class PlexPlaybackError:
    """A playback error from Plex."""

    rating_key: str
    title: str
    file_path: str | None
    error_type: str
    error_message: str
    timestamp: datetime


@dataclass
class QualityUpgrade:
    """A suggestion for quality upgrade."""

    title: str
    year: int | None
    file_path: str
    current_resolution: str
    current_bitrate: int | None
    view_count: int
    last_viewed: datetime | None
    suggested_quality: str  # e.g., "1080p", "4K"


@dataclass
class PlaybackIssue:
    """A potential playback issue detected from viewing patterns."""

    rating_key: str
    title: str
    year: int | None
    file_path: str | None
    issue_type: str  # "abandoned", "repeated_starts", "stuck_ondeck"
    details: str
    view_offset_pct: float  # How far through the file playback stopped
    last_activity: datetime | None


class PlexClient:
    """Client for interacting with Plex Media Server API."""

    def __init__(self, config: PlexConfig):
        self.config = config
        self.log = logger.bind(component="plex_client")
        self._base_url = config.url.rstrip("/")
        self._headers = {
            "X-Plex-Token": config.token,
            "Accept": "application/json",
        }

    async def test_connection(self) -> bool:
        """Test connection to Plex server."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{self._base_url}/identity",
                    headers=self._headers,
                )
                return response.status_code == 200
        except Exception as e:
            self.log.error("Plex connection test failed", error=str(e))
            return False

    async def get_libraries(self) -> list[PlexLibrary]:
        """Get all library sections."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"{self._base_url}/library/sections",
                    headers=self._headers,
                )
                response.raise_for_status()
                data = response.json()

            libraries = []
            for section in data.get("MediaContainer", {}).get("Directory", []):
                locations = [loc.get("path", "") for loc in section.get("Location", [])]
                libraries.append(PlexLibrary(
                    key=str(section.get("key")),
                    title=section.get("title", ""),
                    type=section.get("type", ""),
                    agent=section.get("agent", ""),
                    scanner=section.get("scanner", ""),
                    location=locations,
                ))
            return libraries

        except Exception as e:
            self.log.error("Failed to get Plex libraries", error=str(e))
            return []

    async def get_library_items(
        self,
        library_key: str,
        include_watch_info: bool = True,
    ) -> list[PlexMediaItem]:
        """Get all items in a library section."""
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.get(
                    f"{self._base_url}/library/sections/{library_key}/all",
                    headers=self._headers,
                )
                response.raise_for_status()
                data = response.json()

            items = []
            container = data.get("MediaContainer", {})
            library_type = container.get("viewGroup", "movie")

            for item in container.get("Metadata", []):
                media_item = self._parse_media_item(item, library_key, library_type)
                if media_item:
                    items.append(media_item)

            self.log.info(
                "Fetched Plex library items",
                library=library_key,
                count=len(items),
            )
            return items

        except Exception as e:
            self.log.error("Failed to get Plex library items", library=library_key, error=str(e))
            return []

    async def get_recently_watched(
        self,
        library_key: str | None = None,
        limit: int = 100,
    ) -> list[PlexMediaItem]:
        """Get recently watched items."""
        try:
            url = f"{self._base_url}/library/recentlyViewed"
            params = {"X-Plex-Container-Size": str(limit)}
            if library_key:
                url = f"{self._base_url}/library/sections/{library_key}/recentlyViewed"

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=self._headers, params=params)
                response.raise_for_status()
                data = response.json()

            items = []
            for item in data.get("MediaContainer", {}).get("Metadata", []):
                media_item = self._parse_media_item(item, library_key or "", item.get("type", ""))
                if media_item:
                    items.append(media_item)

            return items

        except Exception as e:
            self.log.error("Failed to get recently watched", error=str(e))
            return []

    async def get_watch_history(
        self,
        library_key: str | None = None,
    ) -> dict[str, int]:
        """
        Get watch counts for all items.

        Returns:
            Dict mapping file_path -> view_count
        """
        watch_counts: dict[str, int] = {}

        try:
            libraries = await self.get_libraries()
            target_libraries = [l for l in libraries if l.type in ("movie", "show")]

            if library_key:
                target_libraries = [l for l in target_libraries if l.key == library_key]

            for library in target_libraries:
                items = await self.get_library_items(library.key)
                for item in items:
                    if item.file_path and item.view_count > 0:
                        watch_counts[item.file_path] = item.view_count

            self.log.info("Fetched watch history", items_with_views=len(watch_counts))
            return watch_counts

        except Exception as e:
            self.log.error("Failed to get watch history", error=str(e))
            return {}

    async def refresh_library(self, library_key: str | None = None) -> bool:
        """
        Trigger a library scan/refresh.

        Args:
            library_key: Specific library to refresh, or None for all
        """
        try:
            if library_key:
                url = f"{self._base_url}/library/sections/{library_key}/refresh"
            else:
                url = f"{self._base_url}/library/sections/all/refresh"

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=self._headers)
                # Plex returns 200 for success
                if response.status_code == 200:
                    self.log.info("Triggered Plex library refresh", library=library_key or "all")
                    return True
                else:
                    self.log.error("Failed to refresh Plex library", status=response.status_code)
                    return False

        except Exception as e:
            self.log.error("Failed to refresh Plex library", error=str(e))
            return False

    async def refresh_item(self, rating_key: str) -> bool:
        """Refresh metadata for a specific item."""
        try:
            url = f"{self._base_url}/library/metadata/{rating_key}/refresh"
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.put(url, headers=self._headers)
                return response.status_code == 200
        except Exception as e:
            self.log.error("Failed to refresh item", rating_key=rating_key, error=str(e))
            return False

    async def get_quality_upgrade_candidates(
        self,
        min_views: int = 1,
        max_resolution: str = "720",
    ) -> list[QualityUpgrade]:
        """
        Find watched content that could be upgraded.

        Args:
            min_views: Minimum view count to consider
            max_resolution: Maximum current resolution to suggest upgrade (e.g., "720", "1080")
        """
        upgrades = []
        resolution_order = ["sd", "480", "576", "720", "1080", "4k"]

        def resolution_index(res: str | None) -> int:
            if not res:
                return 0
            res_lower = res.lower().replace("p", "")
            for i, r in enumerate(resolution_order):
                if r in res_lower or res_lower in r:
                    return i
            return 0

        max_res_idx = resolution_index(max_resolution)

        try:
            libraries = await self.get_libraries()
            movie_libraries = [l for l in libraries if l.type == "movie"]

            for library in movie_libraries:
                items = await self.get_library_items(library.key)

                for item in items:
                    if item.view_count < min_views:
                        continue

                    current_res_idx = resolution_index(item.video_resolution)
                    if current_res_idx > max_res_idx:
                        continue  # Already high quality

                    # Suggest next quality tier
                    suggested = "1080p" if current_res_idx < 4 else "4K"

                    upgrades.append(QualityUpgrade(
                        title=item.title,
                        year=item.year,
                        file_path=item.file_path or "",
                        current_resolution=item.video_resolution or "Unknown",
                        current_bitrate=item.bitrate,
                        view_count=item.view_count,
                        last_viewed=item.last_viewed_at,
                        suggested_quality=suggested,
                    ))

            # Sort by view count descending
            upgrades.sort(key=lambda x: x.view_count, reverse=True)
            self.log.info("Found quality upgrade candidates", count=len(upgrades))
            return upgrades

        except Exception as e:
            self.log.error("Failed to get quality upgrade candidates", error=str(e))
            return []

    async def get_all_file_paths(self) -> set[str]:
        """Get all file paths known to Plex."""
        paths = set()
        try:
            libraries = await self.get_libraries()
            for library in libraries:
                if library.type in ("movie", "show"):
                    items = await self.get_library_items(library.key)
                    for item in items:
                        if item.file_path:
                            paths.add(item.file_path)
            return paths
        except Exception as e:
            self.log.error("Failed to get Plex file paths", error=str(e))
            return set()

    async def find_orphans(
        self,
        arr_paths: set[str],
    ) -> tuple[list[str], list[str]]:
        """
        Find orphaned files between Plex and Radarr/Sonarr.

        Args:
            arr_paths: Set of file paths from Radarr/Sonarr

        Returns:
            Tuple of (in_plex_not_arr, in_arr_not_plex)
        """
        plex_paths = await self.get_all_file_paths()

        in_plex_not_arr = [p for p in plex_paths if p not in arr_paths]
        in_arr_not_plex = [p for p in arr_paths if p not in plex_paths]

        self.log.info(
            "Orphan detection complete",
            in_plex_only=len(in_plex_not_arr),
            in_arr_only=len(in_arr_not_plex),
        )

        return in_plex_not_arr, in_arr_not_plex

    async def get_playback_issues(
        self,
        min_progress_pct: float = 5.0,
        max_progress_pct: float = 90.0,
    ) -> list[PlaybackIssue]:
        """
        Find potential playback issues by analyzing viewing patterns.

        Detects:
        - Items started but abandoned early (5-90% progress, never completed)
        - Items on "Continue Watching" for multiple users

        Args:
            min_progress_pct: Minimum progress to consider (filters out accidental plays)
            max_progress_pct: Maximum progress (above this is considered "almost finished")
        """
        issues = []

        try:
            # Get "On Deck" / Continue Watching items
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"{self._base_url}/library/onDeck",
                    headers=self._headers,
                )
                response.raise_for_status()
                data = response.json()

            for item in data.get("MediaContainer", {}).get("Metadata", []):
                view_offset = item.get("viewOffset", 0)  # ms
                duration = item.get("duration", 1)  # ms
                progress_pct = (view_offset / duration * 100) if duration > 0 else 0

                # Skip if outside our target range
                if progress_pct < min_progress_pct or progress_pct > max_progress_pct:
                    continue

                # Get file path
                file_path = None
                media_list = item.get("Media", [])
                if media_list:
                    parts = media_list[0].get("Part", [])
                    if parts:
                        file_path = parts[0].get("file")

                # Parse last viewed time
                last_viewed = None
                if item.get("lastViewedAt"):
                    last_viewed = datetime.fromtimestamp(item["lastViewedAt"])

                # Determine issue type based on progress
                if progress_pct < 20:
                    issue_type = "abandoned_early"
                    details = f"Stopped at {progress_pct:.0f}% - may have playback issues"
                elif progress_pct < 50:
                    issue_type = "abandoned_middle"
                    details = f"Stopped at {progress_pct:.0f}% - possible buffering/quality issues"
                else:
                    issue_type = "abandoned_late"
                    details = f"Stopped at {progress_pct:.0f}% - may need to check file end"

                issues.append(PlaybackIssue(
                    rating_key=str(item.get("ratingKey")),
                    title=item.get("title", "Unknown"),
                    year=item.get("year"),
                    file_path=file_path,
                    issue_type=issue_type,
                    details=details,
                    view_offset_pct=progress_pct,
                    last_activity=last_viewed,
                ))

            # Sort by progress (early abandonment first - more likely to be issues)
            issues.sort(key=lambda x: x.view_offset_pct)
            self.log.info("Found potential playback issues", count=len(issues))
            return issues

        except Exception as e:
            self.log.error("Failed to get playback issues", error=str(e))
            return []

    def _parse_media_item(
        self,
        data: dict[str, Any],
        library_key: str,
        item_type: str,
    ) -> PlexMediaItem | None:
        """Parse a media item from Plex API response."""
        try:
            # Get file path from Media -> Part
            file_path = None
            video_resolution = None
            video_codec = None
            audio_codec = None
            container = None
            bitrate = None
            duration_ms = data.get("duration")

            media_list = data.get("Media", [])
            if media_list:
                media = media_list[0]
                video_resolution = media.get("videoResolution")
                video_codec = media.get("videoCodec")
                audio_codec = media.get("audioCodec")
                container = media.get("container")
                bitrate = media.get("bitrate")

                parts = media.get("Part", [])
                if parts:
                    file_path = parts[0].get("file")

            # Parse timestamps
            last_viewed = None
            if data.get("lastViewedAt"):
                last_viewed = datetime.fromtimestamp(data["lastViewedAt"])

            added_at = None
            if data.get("addedAt"):
                added_at = datetime.fromtimestamp(data["addedAt"])

            # Episode-specific fields
            show_title = data.get("grandparentTitle")
            season_number = data.get("parentIndex")
            episode_number = data.get("index")

            return PlexMediaItem(
                rating_key=str(data.get("ratingKey")),
                title=data.get("title", "Unknown"),
                year=data.get("year"),
                type=data.get("type", item_type),
                library_key=library_key,
                file_path=file_path,
                duration_ms=duration_ms,
                video_resolution=video_resolution,
                video_codec=video_codec,
                audio_codec=audio_codec,
                container=container,
                bitrate=bitrate,
                view_count=data.get("viewCount", 0),
                last_viewed_at=last_viewed,
                added_at=added_at,
                show_title=show_title,
                season_number=season_number,
                episode_number=episode_number,
            )

        except Exception as e:
            self.log.warning("Failed to parse Plex media item", error=str(e))
            return None
