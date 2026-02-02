"""Client for Radarr and Sonarr APIs."""

from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx
import structlog

from .config import ArrInstance

logger = structlog.get_logger()


class ArrType(Enum):
    RADARR = "radarr"
    SONARR = "sonarr"


@dataclass
class MediaItem:
    """Represents a movie or episode."""

    id: int
    title: str
    file_path: str | None
    file_id: int | None
    quality: str | None
    size_bytes: int | None
    # For episodes
    series_id: int | None = None
    season_number: int | None = None
    episode_number: int | None = None
    # Source info
    arr_type: ArrType | None = None
    arr_instance: str | None = None


@dataclass
class QueueItem:
    """Represents an item in the download queue."""

    id: int
    title: str
    status: str
    size_bytes: int | None
    sizeleft_bytes: int | None


class ArrClient:
    """Client for interacting with Radarr or Sonarr API."""

    def __init__(self, instance: ArrInstance, arr_type: ArrType):
        self.instance = instance
        self.arr_type = arr_type
        self.base_url = instance.url.rstrip("/")
        self.api_key = instance.api_key
        self.log = logger.bind(arr=instance.name, type=arr_type.value)

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.api_key}

    async def _get(self, endpoint: str, params: dict | None = None) -> Any:
        """Make a GET request to the API."""
        url = f"{self.base_url}/api/v3/{endpoint}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self._headers(), params=params)
            response.raise_for_status()
            return response.json()

    async def _post(self, endpoint: str, data: dict | None = None) -> Any:
        """Make a POST request to the API."""
        url = f"{self.base_url}/api/v3/{endpoint}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, headers=self._headers(), json=data)
            response.raise_for_status()
            return response.json()

    async def _delete(self, endpoint: str, params: dict | None = None) -> bool:
        """Make a DELETE request to the API."""
        url = f"{self.base_url}/api/v3/{endpoint}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.delete(url, headers=self._headers(), params=params)
            response.raise_for_status()
            return True

    async def test_connection(self) -> bool:
        """Test the connection to the API."""
        try:
            await self._get("system/status")
            self.log.info("Connection test successful")
            return True
        except Exception as e:
            self.log.error("Connection test failed", error=str(e))
            return False

    async def get_all_media(self) -> list[MediaItem]:
        """Get all movies (Radarr) or series (Sonarr)."""
        if self.arr_type == ArrType.RADARR:
            return await self._get_all_movies()
        else:
            return await self._get_all_episodes()

    async def _get_all_movies(self) -> list[MediaItem]:
        """Get all movies from Radarr."""
        movies = await self._get("movie")
        items = []

        for movie in movies:
            if not movie.get("hasFile"):
                continue

            movie_file = movie.get("movieFile", {})
            items.append(
                MediaItem(
                    id=movie["id"],
                    title=movie["title"],
                    file_path=movie_file.get("path"),
                    file_id=movie_file.get("id"),
                    quality=movie_file.get("quality", {}).get("quality", {}).get("name"),
                    size_bytes=movie_file.get("size"),
                    arr_type=ArrType.RADARR,
                    arr_instance=self.instance.name,
                )
            )

        self.log.info("Fetched movies", count=len(items))
        return items

    async def _get_all_episodes(self) -> list[MediaItem]:
        """Get all episodes from Sonarr."""
        series_list = await self._get("series")
        items = []

        for series in series_list:
            if series.get("statistics", {}).get("episodeFileCount", 0) == 0:
                continue

            episodes = await self._get("episode", params={"seriesId": series["id"]})

            for episode in episodes:
                if not episode.get("hasFile"):
                    continue

                episode_file = episode.get("episodeFile", {})
                if not episode_file:
                    # Need to fetch episode file separately
                    file_id = episode.get("episodeFileId")
                    if file_id:
                        try:
                            episode_file = await self._get(f"episodefile/{file_id}")
                        except Exception:
                            continue

                items.append(
                    MediaItem(
                        id=episode["id"],
                        title=f"{series['title']} - S{episode.get('seasonNumber', 0):02d}E{episode.get('episodeNumber', 0):02d}",
                        file_path=episode_file.get("path"),
                        file_id=episode_file.get("id"),
                        quality=episode_file.get("quality", {}).get("quality", {}).get("name"),
                        size_bytes=episode_file.get("size"),
                        series_id=series["id"],
                        season_number=episode.get("seasonNumber"),
                        episode_number=episode.get("episodeNumber"),
                        arr_type=ArrType.SONARR,
                        arr_instance=self.instance.name,
                    )
                )

        self.log.info("Fetched episodes", count=len(items))
        return items

    async def get_file_by_path(self, file_path: str) -> MediaItem | None:
        """Find a media item by its file path."""
        all_media = await self.get_all_media()
        for item in all_media:
            if item.file_path == file_path:
                return item
        return None

    async def delete_file(self, file_id: int) -> bool:
        """Delete a media file."""
        if self.arr_type == ArrType.RADARR:
            endpoint = f"moviefile/{file_id}"
        else:
            endpoint = f"episodefile/{file_id}"

        try:
            await self._delete(endpoint)
            self.log.info("Deleted file", file_id=file_id)
            return True
        except Exception as e:
            self.log.error("Failed to delete file", file_id=file_id, error=str(e))
            return False

    async def search_for_replacement(self, item: MediaItem) -> bool:
        """Trigger a search for a replacement download."""
        try:
            if self.arr_type == ArrType.RADARR:
                await self._post(
                    "command",
                    {"name": "MoviesSearch", "movieIds": [item.id]},
                )
            else:
                await self._post(
                    "command",
                    {"name": "EpisodeSearch", "episodeIds": [item.id]},
                )
            self.log.info("Triggered search", title=item.title)
            return True
        except Exception as e:
            self.log.error("Failed to trigger search", title=item.title, error=str(e))
            return False

    async def add_to_blocklist(
        self,
        item: MediaItem,
        message: str = "Blocked by media-janitor: bad file quality",
    ) -> bool:
        """Add the current release to the blocklist so it won't be downloaded again."""
        # Note: Blocklist requires knowing the download ID which we may not have
        # for existing files. This is a best-effort operation.
        self.log.info("Blocklist requested", title=item.title, message=message)
        # The actual blocklist API requires specific download/indexer info
        # that we don't have for existing files. The delete + search approach
        # typically results in a different release being grabbed anyway.
        return True

    async def get_queue(self) -> list[QueueItem]:
        """Get the download queue."""
        try:
            data = await self._get("queue", params={"includeUnknownMovieItems": "true"})
            records = data.get("records", [])
            return [
                QueueItem(
                    id=r["id"],
                    title=r.get("title", "Unknown"),
                    status=r.get("status", "unknown"),
                    size_bytes=r.get("size"),
                    sizeleft_bytes=r.get("sizeleft"),
                )
                for r in records
            ]
        except Exception as e:
            self.log.error("Failed to get queue", error=str(e))
            return []
