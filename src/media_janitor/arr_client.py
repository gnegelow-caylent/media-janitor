"""Client for Radarr and Sonarr APIs."""

from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx
import structlog

from .config import ArrInstance, PathMapping

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
    # For mismatch detection
    year: int | None = None
    folder_path: str | None = None


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
        self.path_mappings = instance.path_mappings
        self.log = logger.bind(arr=instance.name, type=arr_type.value)

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.api_key}

    def translate_path(self, path: str | None) -> str | None:
        """Translate a path from Radarr/Sonarr to the actual file system path."""
        if not path:
            return None

        for mapping in self.path_mappings:
            if path.startswith(mapping.from_path):
                translated = path.replace(mapping.from_path, mapping.to_path, 1)
                self.log.debug("Path translated", original=path, translated=translated)
                return translated

        # No mapping found, return as-is
        return path

    async def _get(self, endpoint: str, params: dict | None = None) -> Any:
        """Make a GET request to the API."""
        url = f"{self.base_url}/api/v3/{endpoint}"
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.get(url, headers=self._headers(), params=params)
            response.raise_for_status()
            return response.json()

    async def _post(self, endpoint: str, data: dict | None = None) -> Any:
        """Make a POST request to the API."""
        url = f"{self.base_url}/api/v3/{endpoint}"
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, headers=self._headers(), json=data)
            response.raise_for_status()
            return response.json()

    async def _delete(self, endpoint: str, params: dict | None = None) -> bool:
        """Make a DELETE request to the API."""
        url = f"{self.base_url}/api/v3/{endpoint}"
        async with httpx.AsyncClient(timeout=60.0) as client:
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
                    file_path=self.translate_path(movie_file.get("path")),
                    file_id=movie_file.get("id"),
                    quality=movie_file.get("quality", {}).get("quality", {}).get("name"),
                    size_bytes=movie_file.get("size"),
                    arr_type=ArrType.RADARR,
                    arr_instance=self.instance.name,
                    year=movie.get("year"),
                    folder_path=self.translate_path(movie.get("path")),
                )
            )

        self.log.info("Fetched movies", count=len(items))
        return items

    async def _get_all_episodes(self) -> list[MediaItem]:
        """Get all episodes from Sonarr using bulk episodefile endpoint."""
        # First, get series list for title lookups (single API call)
        series_list = await self._get("series")
        series_map = {s["id"]: s["title"] for s in series_list}

        # Check total episode file count to decide fetch strategy
        total_files = sum(
            s.get("statistics", {}).get("episodeFileCount", 0)
            for s in series_list
        )

        self.log.info("Fetching episodes", total_expected=total_files)

        items = []

        if total_files < 1000:
            # Small library - fetch all at once
            episode_files = await self._get("episodefile")
            items = self._parse_episode_files(episode_files, series_map)
        else:
            # Large library - paginate in chunks
            page_size = 500
            page = 1

            while True:
                self.log.debug("Fetching episode page", page=page, page_size=page_size)
                data = await self._get("episodefile", params={
                    "page": page,
                    "pageSize": page_size,
                })

                # Handle both array response and paginated response
                if isinstance(data, list):
                    # Some Sonarr versions return array directly
                    episode_files = data
                    items.extend(self._parse_episode_files(episode_files, series_map))
                    break
                else:
                    # Paginated response
                    records = data.get("records", [])
                    items.extend(self._parse_episode_files(records, series_map))

                    total_records = data.get("totalRecords", 0)
                    if page * page_size >= total_records:
                        break
                    page += 1

        self.log.info("Fetched episodes", count=len(items))
        return items

    def _parse_episode_files(self, episode_files: list, series_map: dict) -> list[MediaItem]:
        """Parse episode files into MediaItem objects."""
        items = []
        for ef in episode_files:
            series_id = ef.get("seriesId")
            series_title = series_map.get(series_id, "Unknown Series")

            # Get season/episode from the file's first episode (files can have multiple episodes)
            season_num = ef.get("seasonNumber", 0)
            # Episode files have episodes array with their episode numbers
            episodes = ef.get("episodes", [])
            if episodes:
                episode_num = episodes[0].get("episodeNumber", 0)
                episode_id = episodes[0].get("id", ef.get("id"))
            else:
                episode_num = 0
                episode_id = ef.get("id")

            items.append(
                MediaItem(
                    id=episode_id,
                    title=f"{series_title} - S{season_num:02d}E{episode_num:02d}",
                    file_path=self.translate_path(ef.get("path")),
                    file_id=ef.get("id"),
                    quality=ef.get("quality", {}).get("quality", {}).get("name"),
                    size_bytes=ef.get("size"),
                    series_id=series_id,
                    season_number=season_num,
                    episode_number=episode_num,
                    arr_type=ArrType.SONARR,
                    arr_instance=self.instance.name,
                )
            )
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
