"""Background scanner for existing media library."""

import asyncio
import random
from datetime import datetime, timedelta
from pathlib import Path

import structlog

from .arr_client import ArrClient, ArrType, MediaItem
from .config import Config
from .state import StateManager

logger = structlog.get_logger()


class Scanner:
    """Background scanner for media library."""

    def __init__(self, config: Config, state: StateManager):
        self.config = config
        self.state = state
        self.log = logger.bind(component="scanner")

        # Track scan state (in-memory queue, persistent state in StateManager)
        self._scan_queue: list[MediaItem] = []
        self._last_full_refresh: datetime | None = None
        self._initial_scan_complete = state.get_stats()["initial_scan_done"]

        # Clients
        self._radarr_clients: list[ArrClient] = []
        self._sonarr_clients: list[ArrClient] = []

    async def initialize(self):
        """Initialize the scanner with arr clients."""
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

    async def refresh_library(self) -> int:
        """Refresh the list of media to scan."""
        self.log.info("Refreshing library list")
        all_media: list[MediaItem] = []

        for client in self._radarr_clients:
            try:
                media = await client.get_all_media()
                all_media.extend(media)
            except Exception as e:
                self.log.error("Failed to fetch from Radarr", instance=client.instance.name, error=str(e))

        for client in self._sonarr_clients:
            try:
                media = await client.get_all_media()
                all_media.extend(media)
            except Exception as e:
                self.log.error("Failed to fetch from Sonarr", instance=client.instance.name, error=str(e))

        # Get already scanned paths from persistent state
        scanned_paths = self.state.get_scanned_paths()

        # Filter to unscanned files
        new_items = [
            item for item in all_media
            if item.file_path and item.file_path not in scanned_paths
        ]

        # Shuffle to avoid always scanning in the same order
        random.shuffle(new_items)

        self._scan_queue = new_items
        self._last_full_refresh = datetime.now()

        # Mark scan started if this is the first run
        if not self._initial_scan_complete and len(new_items) > 0:
            self.state.mark_scan_started()

        self.log.info(
            "Library refreshed",
            total_files=len(all_media),
            new_to_scan=len(new_items),
            already_scanned=len(scanned_paths),
        )

        return len(new_items)

    def get_next_batch(self, count: int) -> list[MediaItem]:
        """Get the next batch of items to scan."""
        batch = self._scan_queue[:count]
        self._scan_queue = self._scan_queue[count:]
        return batch

    def mark_scanned(self, file_path: str, valid: bool = True):
        """Mark a file as scanned (persisted to disk)."""
        self.state.mark_scanned(file_path, valid)

    def mark_replaced(self, file_path: str):
        """Mark a file as replaced (removes from scanned list)."""
        self.state.mark_replaced(file_path)

    def check_initial_scan_complete(self) -> bool:
        """Check if initial scan is complete and mark if so."""
        if self._initial_scan_complete:
            return True

        if len(self._scan_queue) == 0 and self._last_full_refresh is not None:
            self.state.mark_scan_completed()
            self._initial_scan_complete = True
            self.log.info("Initial library scan completed!")
            return True

        return False

    def get_status(self) -> dict:
        """Get scanner status."""
        stats = self.state.get_stats()
        return {
            "queue_size": len(self._scan_queue),
            "scanned_count": stats["total_scanned"],
            "valid_count": stats["valid_files"],
            "invalid_count": stats["invalid_files"],
            "replaced_count": stats["total_replaced"],
            "last_refresh": self._last_full_refresh.isoformat() if self._last_full_refresh else None,
            "initial_scan_done": stats["initial_scan_done"],
            "scan_started": stats["scan_started"],
            "scan_completed": stats["scan_completed"],
            "radarr_instances": len(self._radarr_clients),
            "sonarr_instances": len(self._sonarr_clients),
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

    async def find_item_by_path(self, file_path: str) -> tuple[MediaItem | None, ArrClient | None]:
        """Find a media item and its client by file path."""
        for client in self._radarr_clients + self._sonarr_clients:
            try:
                item = await client.get_file_by_path(file_path)
                if item:
                    return item, client
            except Exception as e:
                self.log.warning("Error searching for file", client=client.instance.name, error=str(e))
        return None, None
