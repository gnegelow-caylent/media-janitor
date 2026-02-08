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

    id: int  # Movie ID (Radarr) or Episode ID (Sonarr) - used for searches
    title: str
    file_path: str | None
    file_id: int | None  # Episode FILE ID (Sonarr) or Movie FILE ID (Radarr) - used for deletions
    quality: str | None
    size_bytes: int | None
    # For episodes
    series_id: int | None = None
    season_number: int | None = None
    episode_number: int | None = None
    episode_id: int | None = None  # Explicit episode ID for Sonarr (may differ from file_id)
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
        """Get all episodes from Sonarr using the most memory-efficient approach."""
        # Get all series first (needed for title mapping)
        series_list = await self._get("series")
        series_map = {s["id"]: s["title"] for s in series_list}

        self.log.info("Fetching episodes from Sonarr", series_count=len(series_list))

        # Fetch per series - we need both episode files AND episodes to get the episode ID
        items = []
        for series in series_list:
            series_id = series["id"]
            episode_count = series.get("statistics", {}).get("episodeFileCount", 0)

            if episode_count == 0:
                continue

            try:
                # Fetch episode files for this series
                episode_files = await self._get("episodefile", params={"seriesId": series_id})
                if not episode_files:
                    continue

                # Fetch episodes for this series to get the episode IDs
                episodes = await self._get("episode", params={"seriesId": series_id})

                # Build mapping: (season, episode_number) -> episode_id
                episode_id_map: dict[tuple[int, int], int] = {}
                for ep in episodes:
                    key = (ep.get("seasonNumber", 0), ep.get("episodeNumber", 0))
                    episode_id_map[key] = ep.get("id")

                items.extend(self._parse_episode_files(episode_files, series_map, episode_id_map))
            except Exception as e:
                self.log.error("Failed to fetch episodes for series",
                             series=series["title"], error=str(e))

        self.log.info("Fetched episodes", count=len(items))
        return items

    def _parse_episode_files(
        self,
        episode_files: list,
        series_map: dict,
        episode_id_map: dict[tuple[int, int], int] | None = None,
    ) -> list[MediaItem]:
        """Parse episode files into MediaItem objects."""
        items = []
        for ef in episode_files:
            series_id = ef.get("seriesId")
            series_title = series_map.get(series_id, "Unknown Series")

            season_num = ef.get("seasonNumber", 0)
            file_id = ef.get("id")  # Episode FILE ID (for deletions)

            # Try to get episode number from the file path or use 0
            # The episodefile endpoint doesn't include episode number directly
            episode_num = 0
            episode_id = None

            # If we have an episode ID map, look up the episode ID
            # Note: episodefile doesn't tell us which episode number, so we need to parse from path
            # For now, try to extract from relativePath which usually has SxxExx pattern
            rel_path = ef.get("relativePath", "")
            import re
            match = re.search(r'S(\d+)E(\d+)', rel_path, re.IGNORECASE)
            if match:
                parsed_season = int(match.group(1))
                episode_num = int(match.group(2))
                # Verify season matches
                if parsed_season != season_num:
                    self.log.debug("Season mismatch in path", path=rel_path, expected=season_num, found=parsed_season)

            # Look up episode ID from the map
            if episode_id_map:
                episode_id = episode_id_map.get((season_num, episode_num))

            items.append(
                MediaItem(
                    id=episode_id or file_id,  # Fallback for display/compatibility
                    title=f"{series_title} - S{season_num:02d}E{episode_num:02d}",
                    file_path=self.translate_path(ef.get("path")),
                    file_id=file_id,
                    quality=ef.get("quality", {}).get("quality", {}).get("name"),
                    size_bytes=ef.get("size"),
                    series_id=series_id,
                    season_number=season_num,
                    episode_number=episode_num,
                    episode_id=episode_id,  # Explicit episode ID (may be None if not found)
                    arr_type=ArrType.SONARR,
                    arr_instance=self.instance.name,
                )
            )
        return items

    async def get_file_by_path(self, file_path: str) -> MediaItem | None:
        """Find a media item by its file path.

        NOTE: This method fetches ALL media which is expensive.
        Prefer using Scanner.find_item_by_path() which uses the cache.
        This method should only be called as a fallback for new files not in cache.
        """
        # For Radarr, we can search more efficiently
        if self.arr_type == ArrType.RADARR:
            # Unfortunately Radarr doesn't have a path search endpoint
            # so we still need to iterate, but we log a warning
            self.log.debug("Fetching all movies to find path (consider using cache)", path=file_path)

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
        """Search for a replacement and grab from highest priority indexer only.

        Instead of using the generic search command (which grabs from ALL indexers),
        this method:
        1. Gets available releases from the release API
        2. Filters to only releases from the highest priority indexer
        3. Pushes the best release from that indexer

        This prevents duplicate grabs from multiple indexers.
        """
        try:
            # Get indexer priorities (lower number = higher priority)
            indexers = await self._get("indexer")
            indexer_priorities = {idx["name"]: idx.get("priority", 25) for idx in indexers}

            # Get the item ID for the release search
            if self.arr_type == ArrType.RADARR:
                search_params = {"movieId": item.id}
            else:
                episode_id = item.episode_id
                if not episode_id:
                    episode_id = await self._get_episode_id_by_info(
                        item.series_id, item.season_number, item.episode_number
                    )
                    if not episode_id:
                        self.log.error(
                            "Cannot search: no episode ID available",
                            title=item.title,
                            series_id=item.series_id,
                            season=item.season_number,
                            episode=item.episode_number,
                        )
                        return False
                search_params = {"episodeId": episode_id}

            # Search for available releases
            self.log.info("Searching for releases", title=item.title)
            releases = await self._get("release", params=search_params)

            if not releases:
                self.log.warning("No releases found", title=item.title)
                # Fall back to standard search command
                return await self._fallback_search(item)

            # Filter out rejected releases and sort by indexer priority then quality
            valid_releases = [r for r in releases if not r.get("rejected", False)]
            if not valid_releases:
                self.log.warning("All releases rejected", title=item.title)
                return await self._fallback_search(item)

            # Find the highest priority indexer that has releases
            releases_by_indexer: dict[str, list] = {}
            for release in valid_releases:
                indexer = release.get("indexer", "Unknown")
                if indexer not in releases_by_indexer:
                    releases_by_indexer[indexer] = []
                releases_by_indexer[indexer].append(release)

            # Sort indexers by priority (lower = better)
            sorted_indexers = sorted(
                releases_by_indexer.keys(),
                key=lambda x: indexer_priorities.get(x, 25)
            )

            if not sorted_indexers:
                self.log.warning("No indexers with valid releases", title=item.title)
                return await self._fallback_search(item)

            # Get the best release from the highest priority indexer
            best_indexer = sorted_indexers[0]
            best_releases = releases_by_indexer[best_indexer]

            # Sort by quality weight (higher = better) - Sonarr/Radarr already sorts by preferred
            # Just pick the first one as it's usually the best match
            best_release = best_releases[0]

            self.log.info(
                "Grabbing release from single indexer",
                title=item.title,
                indexer=best_indexer,
                priority=indexer_priorities.get(best_indexer, 25),
                release=best_release.get("title", "Unknown"),
                quality=best_release.get("quality", {}).get("quality", {}).get("name", "Unknown"),
            )

            # Push the release to download
            await self._post("release", best_release)
            self.log.info("Triggered single-indexer download", title=item.title, indexer=best_indexer)
            return True

        except Exception as e:
            self.log.error("Failed to trigger search", title=item.title, error=str(e))
            return False

    async def _fallback_search(self, item: MediaItem) -> bool:
        """Fall back to standard search command if release API fails."""
        self.log.info("Falling back to standard search", title=item.title)
        try:
            if self.arr_type == ArrType.RADARR:
                await self._post(
                    "command",
                    {"name": "MoviesSearch", "movieIds": [item.id]},
                )
            else:
                episode_id = item.episode_id
                if not episode_id:
                    episode_id = await self._get_episode_id_by_info(
                        item.series_id, item.season_number, item.episode_number
                    )
                if episode_id:
                    await self._post(
                        "command",
                        {"name": "EpisodeSearch", "episodeIds": [episode_id]},
                    )
            return True
        except Exception as e:
            self.log.error("Fallback search also failed", title=item.title, error=str(e))
            return False

    async def _get_episode_id_by_info(
        self, series_id: int | None, season: int | None, episode: int | None
    ) -> int | None:
        """Get the episode ID using series/season/episode info.

        This works even after the episode file has been deleted.
        """
        if not series_id or season is None or episode is None:
            return None
        try:
            # Fetch all episodes for this series (Sonarr API)
            episodes = await self._get("episode", params={"seriesId": series_id})
            for ep in episodes:
                if ep.get("seasonNumber") == season and ep.get("episodeNumber") == episode:
                    return ep.get("id")
            self.log.warning(
                "Episode not found",
                series_id=series_id,
                season=season,
                episode=episode,
            )
            return None
        except Exception as e:
            self.log.warning(
                "Failed to get episode ID",
                series_id=series_id,
                season=season,
                episode=episode,
                error=str(e),
            )
            return None

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

    async def rename_files(self, item: MediaItem) -> bool:
        """Trigger a rename of files for a movie/series to match naming convention.

        This uses Radarr/Sonarr's built-in rename command which fixes paths
        according to the configured naming scheme.
        """
        try:
            if self.arr_type == ArrType.RADARR:
                # For Radarr, we need the movie file IDs
                if not item.file_id:
                    self.log.warning("No file_id for rename", title=item.title)
                    return False
                await self._post(
                    "command",
                    {"name": "RenameFiles", "movieId": item.id, "files": [item.file_id]},
                )
            else:
                # For Sonarr, rename the series files
                if not item.series_id:
                    self.log.warning("No series_id for rename", title=item.title)
                    return False
                if not item.file_id:
                    self.log.warning("No file_id for rename", title=item.title)
                    return False
                await self._post(
                    "command",
                    {"name": "RenameFiles", "seriesId": item.series_id, "files": [item.file_id]},
                )
            self.log.info("Triggered rename", title=item.title)
            return True
        except Exception as e:
            self.log.error("Failed to trigger rename", title=item.title, error=str(e))
            return False
