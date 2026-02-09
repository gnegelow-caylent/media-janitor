"""Background scanner for existing media library."""

import asyncio
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from .arr_client import ArrClient, ArrType, MediaItem
from .config import Config
from .state import StateManager

if TYPE_CHECKING:
    from .plex_client import PlexClient

logger = structlog.get_logger()


class Scanner:
    """Background scanner for media library."""

    def __init__(self, config: Config, state: StateManager, plex_client: "PlexClient | None" = None):
        self.config = config
        self.state = state
        self.log = logger.bind(component="scanner")
        self._plex_client = plex_client

        # Track scan state (in-memory queue, persistent state in StateManager)
        self._scan_queue: list[MediaItem] = []
        self._last_full_refresh: datetime | None = None
        self._initial_scan_complete = state.get_stats()["initial_scan_done"]
        self._refreshing: bool = False
        self._refresh_source: str | None = None
        self._refresh_phase: str | None = None  # "fetching", "building_queue", "ready"
        self._refresh_current_instance: str | None = None  # Which arr instance is being fetched

        # Cache of media items for fast lookups (populated during refresh)
        # Keyed by (arr_instance, file_path) to ensure correct client routing
        self._media_cache: dict[str, MediaItem] = {}
        # Also keep a per-instance cache for accurate client lookup
        self._media_cache_by_instance: dict[str, dict[str, MediaItem]] = {}

        # Clients
        self._radarr_clients: list[ArrClient] = []
        self._sonarr_clients: list[ArrClient] = []
        self._init_lock = asyncio.Lock()  # Prevent concurrent client initialization

    def set_plex_client(self, plex_client: "PlexClient | None"):
        """Set or update the Plex client for watch-based prioritization."""
        self._plex_client = plex_client

    async def reinitialize_clients(self):
        """Reinitialize arr clients with updated config (e.g., after path mapping changes)."""
        async with self._init_lock:  # Prevent concurrent reinitialization
            self.log.info("Reinitializing arr clients")
            self._radarr_clients = []
            self._sonarr_clients = []
            self._media_cache = {}  # Clear cache on reinit
            self._media_cache_by_instance = {}

            for instance in self.config.radarr:
                client = ArrClient(instance, ArrType.RADARR)
                if await client.test_connection():
                    self._radarr_clients.append(client)
                else:
                    self.log.error("Failed to connect to Radarr", instance=instance.name)

            for instance in self.config.sonarr:
                client = ArrClient(instance, ArrType.SONARR)
                if await client.test_connection():
                    self._sonarr_clients.append(client)
                else:
                    self.log.error("Failed to connect to Sonarr", instance=instance.name)

            self.log.info(
                "Clients reinitialized",
                radarr_instances=len(self._radarr_clients),
                sonarr_instances=len(self._sonarr_clients),
            )

    async def initialize(self):
        """Initialize the scanner with arr clients."""
        async with self._init_lock:  # Prevent concurrent initialization
            # Clear any existing clients to prevent duplicates if called multiple times
            self._radarr_clients = []
            self._sonarr_clients = []

            for instance in self.config.radarr:
                client = ArrClient(instance, ArrType.RADARR)
                if await client.test_connection():
                    self._radarr_clients.append(client)
                else:
                    self.log.error("Failed to connect to Radarr", instance=instance.name)

            for instance in self.config.sonarr:
                client = ArrClient(instance, ArrType.SONARR)
                if await client.test_connection():
                    self._sonarr_clients.append(client)
                else:
                    self.log.error("Failed to connect to Sonarr", instance=instance.name)

            stats = self.state.get_stats()
            self.log.info(
                "Scanner initialized",
                radarr_instances=len(self._radarr_clients),
                sonarr_instances=len(self._sonarr_clients),
                previously_scanned=stats["total_scanned"],
                initial_scan_done=stats["initial_scan_done"],
            )

    async def refresh_library(self, source: str = "all") -> int:
        """
        Refresh the list of media to scan.

        Args:
            source: "all", "movies" (Radarr only), or "tv" (Sonarr only)
        """
        self._refreshing = True
        self._refresh_source = source
        self._refresh_phase = "fetching"
        self.log.info("Refreshing library list", source=source)
        all_media: list[MediaItem] = []
        movies_total = 0
        tv_total = 0

        try:
            if source in ("all", "movies"):
                for client in self._radarr_clients:
                    try:
                        self._refresh_current_instance = client.instance.name
                        self.log.info("Fetching from Radarr", instance=client.instance.name)
                        media = await client.get_all_media()
                        movies_total += len(media)
                        all_media.extend(media)
                    except Exception as e:
                        self.log.error("Failed to fetch from Radarr", instance=client.instance.name, error=str(e))

            if source in ("all", "tv"):
                for client in self._sonarr_clients:
                    try:
                        self._refresh_current_instance = client.instance.name
                        self.log.info("Fetching from Sonarr", instance=client.instance.name)
                        media = await client.get_all_media()
                        tv_total += len(media)
                        all_media.extend(media)
                    except Exception as e:
                        self.log.error("Failed to fetch from Sonarr", instance=client.instance.name, error=str(e))

            # Update library totals in state
            self._refresh_phase = "processing"
            self._refresh_current_instance = None
            if source == "all":
                self.state.set_library_totals(movies_total, tv_total)
            elif source == "movies":
                current_stats = self.state.get_stats()
                self.state.set_library_totals(movies_total, current_stats.get("tv_total", 0))
            elif source == "tv":
                current_stats = self.state.get_stats()
                self.state.set_library_totals(current_stats.get("movies_total", 0), tv_total)

            # Get already scanned paths from persistent state
            scanned_paths = self.state.get_scanned_paths()

            # Filter to unscanned files
            new_items = [
                item for item in all_media
                if item.file_path and item.file_path not in scanned_paths
            ]

            # Separate movies and TV, then interleave for balanced scanning
            movies = [item for item in new_items if item.arr_type == ArrType.RADARR]
            tv = [item for item in new_items if item.arr_type == ArrType.SONARR]

            # Prioritize by Plex watch count if available
            watch_counts: dict[str, int] = {}
            if self._plex_client:
                try:
                    watch_counts = await self._plex_client.get_watch_history()
                    self.log.info("Using Plex watch data for prioritization", watched_items=len(watch_counts))
                except Exception as e:
                    self.log.warning("Failed to fetch Plex watch data, using random order", error=str(e))

            if watch_counts:
                # Sort by view count descending (most watched first)
                movies.sort(key=lambda x: watch_counts.get(x.file_path or "", 0), reverse=True)
                tv.sort(key=lambda x: watch_counts.get(x.file_path or "", 0), reverse=True)
            else:
                # Fallback to random shuffle
                random.shuffle(movies)
                random.shuffle(tv)

            # For partial refreshes, preserve items from other sources still in queue
            if source == "tv":
                # Keep existing movies in queue
                existing_movies = [item for item in self._scan_queue if item.arr_type == ArrType.RADARR]
                movies = existing_movies  # Use existing movies, not newly fetched (which would be empty)
                self.log.info("Partial TV refresh, preserving movies in queue", movies_preserved=len(movies))
            elif source == "movies":
                # Keep existing TV in queue
                existing_tv = [item for item in self._scan_queue if item.arr_type == ArrType.SONARR]
                tv = existing_tv  # Use existing TV, not newly fetched (which would be empty)
                self.log.info("Partial movies refresh, preserving TV in queue", tv_preserved=len(tv))

            # Interleave: for every 1 movie, scan ~10 TV (proportional to library size)
            # This ensures both get scanned even with large TV libraries
            interleaved = []
            mi, ti = 0, 0
            while mi < len(movies) or ti < len(tv):
                # Add a movie
                if mi < len(movies):
                    interleaved.append(movies[mi])
                    mi += 1
                # Add up to 10 TV episodes
                for _ in range(10):
                    if ti < len(tv):
                        interleaved.append(tv[ti])
                        ti += 1

            self._scan_queue = interleaved
            if source == "all":
                self._last_full_refresh = datetime.now()

            # Build media cache for fast path lookups (avoid re-fetching all media)
            self._media_cache = {
                item.file_path: item
                for item in all_media
                if item.file_path
            }

            # Also build per-instance cache for accurate client routing
            self._media_cache_by_instance = {}
            # Track paths we've seen to detect duplicates
            path_to_instance: dict[str, str] = {}
            duplicate_count = 0

            for item in all_media:
                if item.file_path and item.arr_instance:
                    # Check for duplicate paths across instances
                    if item.file_path in path_to_instance:
                        existing_instance = path_to_instance[item.file_path]
                        if existing_instance != item.arr_instance:
                            duplicate_count += 1
                            if duplicate_count <= 10:  # Log first 10 duplicates
                                self.log.warning(
                                    "DUPLICATE PATH across instances!",
                                    path=item.file_path,
                                    first_instance=existing_instance,
                                    second_instance=item.arr_instance,
                                    title=item.title,
                                )
                    else:
                        path_to_instance[item.file_path] = item.arr_instance

                    if item.arr_instance not in self._media_cache_by_instance:
                        self._media_cache_by_instance[item.arr_instance] = {}
                    self._media_cache_by_instance[item.arr_instance][item.file_path] = item

            self.log.info(
                "Media cache built",
                cache_size=len(self._media_cache),
                instances=list(self._media_cache_by_instance.keys()),
                duplicate_paths=duplicate_count,
                sonarr_count=len(self._media_cache_by_instance.get("sonarr", {})),
                sonarr2_count=len(self._media_cache_by_instance.get("sonarr2", {})),
            )

            # Mark scan started if this is the first run
            if not self._initial_scan_complete and len(new_items) > 0:
                self.state.mark_scan_started()

            self.log.info(
                "Library refreshed",
                total_files=len(all_media),
                movies=movies_total,
                tv_episodes=tv_total,
                new_to_scan=len(new_items),
                already_scanned=len(scanned_paths),
            )

            return len(new_items)
        finally:
            self._refreshing = False
            self._refresh_source = None
            self._refresh_phase = None
            self._refresh_current_instance = None

    def get_next_batch(self, count: int) -> list[MediaItem]:
        """Get the next batch of items to scan."""
        batch = self._scan_queue[:count]
        self._scan_queue = self._scan_queue[count:]
        return batch

    def mark_scanned(self, file_path: str, valid: bool = True, media_type: str = "unknown"):
        """Mark a file as scanned (persisted to disk)."""
        self.state.mark_scanned(file_path, valid, media_type)

    def mark_replaced(
        self,
        file_path: str,
        wrong_file: bool = False,
        title: str = "",
        reason: str = "",
        media_type: str = "unknown",
    ):
        """Mark a file as replaced (removes from scanned list)."""
        self.state.mark_replaced(
            file_path,
            wrong_file=wrong_file,
            title=title,
            reason=reason,
            media_type=media_type,
        )

    def check_initial_scan_complete(self) -> bool:
        """Check if initial scan is complete and mark if so."""
        if self._initial_scan_complete:
            return True

        # Must have queue empty AND have done a refresh
        if len(self._scan_queue) == 0 and self._last_full_refresh is not None:
            # Verify we actually scanned all files, not just emptied the queue
            stats = self.state.get_stats()
            movies_scanned = stats.get("movies_scanned", 0)
            movies_total = stats.get("movies_total", 0)
            tv_scanned = stats.get("tv_scanned", 0)
            tv_total = stats.get("tv_total", 0)

            # Only mark complete if we've scanned at least as many as exist in the library
            # (scanned can be > total if files were deleted after scanning)
            if movies_total > 0 and movies_scanned < movies_total:
                self.log.warning(
                    "Queue empty but movies not fully scanned",
                    movies_scanned=movies_scanned,
                    movies_total=movies_total,
                )
                return False

            if tv_total > 0 and tv_scanned < tv_total:
                self.log.warning(
                    "Queue empty but TV not fully scanned",
                    tv_scanned=tv_scanned,
                    tv_total=tv_total,
                )
                return False

            self.state.mark_scan_completed()
            self._initial_scan_complete = True
            self.log.info(
                "Initial library scan completed!",
                movies_scanned=movies_scanned,
                tv_scanned=tv_scanned,
            )
            return True

        return False

    def get_status(self) -> dict:
        """Get scanner status."""
        stats = self.state.get_stats()

        # Count movies/TV in queue
        movies_in_queue = sum(1 for item in self._scan_queue if item.arr_type == ArrType.RADARR)
        tv_in_queue = sum(1 for item in self._scan_queue if item.arr_type == ArrType.SONARR)

        return {
            "queue_size": len(self._scan_queue),
            "scanned_count": stats["total_scanned"],
            "valid_count": stats["valid_files"],
            "invalid_count": stats["invalid_files"],
            "replaced_count": stats["total_replaced"],
            "wrong_files_count": stats["total_wrong_files"],
            "last_refresh": self._last_full_refresh.isoformat() if self._last_full_refresh else None,
            "initial_scan_done": stats["initial_scan_done"],
            "scan_started": stats["scan_started"],
            "scan_completed": stats["scan_completed"],
            "radarr_instances": len(self._radarr_clients),
            "sonarr_instances": len(self._sonarr_clients),
            # Separate movie/TV stats
            "movies_scanned": stats["movies_scanned"],
            "movies_total": stats["movies_total"],
            "movies_in_queue": movies_in_queue,
            "tv_scanned": stats["tv_scanned"],
            "tv_total": stats["tv_total"],
            "tv_in_queue": tv_in_queue,
            # Refresh status
            "refreshing": self._refreshing,
            "refresh_source": self._refresh_source,
            "refresh_phase": self._refresh_phase,
            "refresh_current_instance": self._refresh_current_instance,
        }

    def get_client_for_item(self, item: MediaItem) -> ArrClient | None:
        """Get the appropriate client for a media item."""
        if item.arr_type == ArrType.RADARR:
            for client in self._radarr_clients:
                if client.instance.name == item.arr_instance:
                    return client
        else:
            for client in self._sonarr_clients:
                if client.instance.name == item.arr_instance:
                    return client
        return None

    def get_all_clients(self) -> list[ArrClient]:
        """Get all configured clients."""
        return self._radarr_clients + self._sonarr_clients

    def add_to_cache(self, item: MediaItem) -> None:
        """Add a media item to the cache (used by webhook for newly imported files)."""
        if item.file_path:
            self._media_cache[item.file_path] = item
            # Also add to per-instance cache for correct client routing
            if item.arr_instance:
                if item.arr_instance not in self._media_cache_by_instance:
                    self._media_cache_by_instance[item.arr_instance] = {}
                self._media_cache_by_instance[item.arr_instance][item.file_path] = item
            self.log.debug("Added to cache", file_path=item.file_path, instance=item.arr_instance)

    def get_cached_media(self, source: str = "all") -> list[MediaItem]:
        """
        Get cached media items without fetching from arrs.

        This is much faster and uses less memory than fetching all media.
        Returns empty list if cache is not populated (refresh_library not called yet).

        Args:
            source: "all", "movies" (Radarr only), or "tv" (Sonarr only)
        """
        if not self._media_cache:
            return []

        items = list(self._media_cache.values())

        if source == "movies":
            return [item for item in items if item.arr_type == ArrType.RADARR]
        elif source == "tv":
            return [item for item in items if item.arr_type == ArrType.SONARR]
        return items

    async def find_item_by_path(self, file_path: str) -> tuple[MediaItem | None, ArrClient | None]:
        """Find a media item and its client by file path.

        Uses per-instance cache to ensure correct client routing.
        If the file is not in cache, returns None - it will be found during the next library refresh.
        """
        # Check which instances have this path (for debugging duplicates)
        found_in_instances = [
            inst for inst, cache in self._media_cache_by_instance.items()
            if file_path in cache
        ]
        if len(found_in_instances) > 1:
            self.log.warning(
                "Path found in multiple instances!",
                path=file_path,
                instances=found_in_instances,
            )

        # Search through per-instance caches to find the correct client
        for instance_name, instance_cache in self._media_cache_by_instance.items():
            if file_path in instance_cache:
                item = instance_cache[file_path]
                client = self.get_client_for_item(item)
                if client:
                    self.log.info(
                        "Found item for validation",
                        path=file_path,
                        instance=instance_name,
                        title=item.title,
                    )
                    return item, client

        # Fallback to main cache (for backwards compatibility)
        if file_path in self._media_cache:
            item = self._media_cache[file_path]
            client = self.get_client_for_item(item)
            if client:
                self.log.info(
                    "Found item in main cache",
                    path=file_path,
                    instance=item.arr_instance,
                    title=item.title,
                )
                return item, client

        # Cache miss - don't fetch from API (too expensive)
        # The file will be found during the next library refresh
        self.log.debug("Cache miss, file not found", path=file_path)
        return None, None
