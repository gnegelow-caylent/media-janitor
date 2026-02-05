"""Persistent state management for scan progress."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()


class StateManager:
    """Manages persistent state for the janitor."""

    def __init__(self, state_file: str = "/data/state/state.json"):
        self.state_file = Path(state_file)
        self.log = logger.bind(component="state")
        self._state: dict[str, Any] = {
            "scanned_files": {},  # path -> {timestamp, valid, media_type}
            "replaced_files": [],  # list of {path, title, reason, timestamp, wrong_file}
            "missing_files": [],  # list of {path, arr_type, timestamp}
            "scan_started": None,
            "scan_completed": None,
            "total_scanned": 0,
            "total_invalid": 0,
            "total_replaced": 0,
            "total_wrong_files": 0,  # Files in wrong folder (path mismatch)
            # Separate counters for movies and TV
            "movies_scanned": 0,
            "movies_total": 0,
            "tv_scanned": 0,
            "tv_total": 0,
        }
        self._load()

    def _load(self):
        """Load state from disk."""
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    self._state = json.load(f)
                self.log.info(
                    "State loaded",
                    scanned_files=len(self._state.get("scanned_files", {})),
                )
            except Exception as e:
                self.log.error("Failed to load state, starting fresh", error=str(e))

    def _save(self):
        """Save state to disk."""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, "w") as f:
                json.dump(self._state, f, indent=2, default=str)
        except Exception as e:
            self.log.error("Failed to save state", error=str(e))

    def is_scanned(self, file_path: str) -> bool:
        """Check if a file has been scanned."""
        return file_path in self._state.get("scanned_files", {})

    def mark_scanned(self, file_path: str, valid: bool = True, media_type: str = "unknown"):
        """Mark a file as scanned.

        Args:
            file_path: Path to the file
            valid: Whether the file passed validation
            media_type: "movie", "tv", or "unknown"
        """
        if "scanned_files" not in self._state:
            self._state["scanned_files"] = {}

        self._state["scanned_files"][file_path] = {
            "timestamp": datetime.now().isoformat(),
            "valid": valid,
            "media_type": media_type,
        }
        self._state["total_scanned"] = self._state.get("total_scanned", 0) + 1

        if not valid:
            self._state["total_invalid"] = self._state.get("total_invalid", 0) + 1

        # Track separate counts for movies and TV
        if media_type == "movie":
            self._state["movies_scanned"] = self._state.get("movies_scanned", 0) + 1
        elif media_type == "tv":
            self._state["tv_scanned"] = self._state.get("tv_scanned", 0) + 1

        # Save periodically (every 10 scans)
        if self._state["total_scanned"] % 10 == 0:
            self._save()

    def mark_replaced(
        self,
        file_path: str,
        wrong_file: bool = False,
        title: str = "",
        reason: str = "",
        media_type: str = "unknown",
    ):
        """Mark a file as replaced (remove from scanned, increment counter).

        Args:
            file_path: Path to the file
            wrong_file: True if this was a wrong file in folder (path mismatch)
            title: Title of the media item
            reason: Reason for replacement
            media_type: "movie", "tv", or "unknown"
        """
        if file_path in self._state.get("scanned_files", {}):
            del self._state["scanned_files"][file_path]
        self._state["total_replaced"] = self._state.get("total_replaced", 0) + 1
        # Replaced files are invalid files, so count them as issues
        self._state["total_invalid"] = self._state.get("total_invalid", 0) + 1
        if wrong_file:
            self._state["total_wrong_files"] = self._state.get("total_wrong_files", 0) + 1

        # Track replaced files by media type for accurate counts
        if media_type == "movie":
            self._state["movies_replaced"] = self._state.get("movies_replaced", 0) + 1
        elif media_type == "tv":
            self._state["tv_replaced"] = self._state.get("tv_replaced", 0) + 1

        # Record the replacement details
        if "replaced_files" not in self._state:
            self._state["replaced_files"] = []
        self._state["replaced_files"].append({
            "path": file_path,
            "title": title,
            "reason": reason,
            "wrong_file": wrong_file,
            "media_type": media_type,
            "timestamp": datetime.now().isoformat(),
        })
        # Keep only last 500 replacements to avoid unbounded growth
        if len(self._state["replaced_files"]) > 500:
            self._state["replaced_files"] = self._state["replaced_files"][-500:]

        self._save()

    def mark_missing(self, file_path: str, media_type: str = "unknown"):
        """Record a missing file (exists in arr but not on disk)."""
        if "missing_files" not in self._state:
            self._state["missing_files"] = []

        # Avoid duplicates
        existing_paths = {f["path"] for f in self._state["missing_files"]}
        if file_path not in existing_paths:
            self._state["missing_files"].append({
                "path": file_path,
                "media_type": media_type,
                "timestamp": datetime.now().isoformat(),
            })
            # Keep only last 1000 missing files
            if len(self._state["missing_files"]) > 1000:
                self._state["missing_files"] = self._state["missing_files"][-1000:]

            # Save periodically
            if len(self._state["missing_files"]) % 50 == 0:
                self._save()

    def get_missing_files(self) -> list[dict]:
        """Get list of missing files."""
        return self._state.get("missing_files", [])

    def clear_missing_files(self):
        """Clear the missing files list."""
        self._state["missing_files"] = []
        self._save()

    def mark_scan_started(self):
        """Mark the start of a full library scan."""
        self._state["scan_started"] = datetime.now().isoformat()
        self._save()

    def mark_scan_completed(self):
        """Mark the completion of a full library scan."""
        self._state["scan_completed"] = datetime.now().isoformat()
        self._save()

    def get_scanned_paths(self) -> set[str]:
        """Get set of all scanned file paths."""
        return set(self._state.get("scanned_files", {}).keys())

    def get_replaced_files(self) -> list[dict]:
        """Get list of replaced files with details."""
        return self._state.get("replaced_files", [])

    def get_stats(self) -> dict:
        """Get scan statistics."""
        scanned_files = self._state.get("scanned_files", {})
        valid_count = sum(1 for f in scanned_files.values() if f.get("valid", True))
        # Use total_invalid counter (includes replaced files) not just current scanned_files
        invalid_count = self._state.get("total_invalid", 0)

        # Count movies and TV from scanned files
        movies_in_scanned = sum(1 for f in scanned_files.values() if f.get("media_type") == "movie")
        tv_in_scanned = sum(1 for f in scanned_files.values() if f.get("media_type") == "tv")

        # Add replaced files to get total scanned by type
        movies_replaced = self._state.get("movies_replaced", 0)
        tv_replaced = self._state.get("tv_replaced", 0)
        movies_scanned = movies_in_scanned + movies_replaced
        tv_scanned = tv_in_scanned + tv_replaced

        return {
            "total_scanned": len(scanned_files),
            "valid_files": valid_count,
            "invalid_files": invalid_count,
            "total_replaced": self._state.get("total_replaced", 0),
            "total_wrong_files": self._state.get("total_wrong_files", 0),
            "scan_started": self._state.get("scan_started"),
            "scan_completed": self._state.get("scan_completed"),
            "initial_scan_done": self._state.get("scan_completed") is not None,
            # Separate movie/TV counts (includes replaced)
            "movies_scanned": movies_scanned,
            "movies_total": self._state.get("movies_total", 0),
            "tv_scanned": tv_scanned,
            "tv_total": self._state.get("tv_total", 0),
        }

    def set_library_totals(self, movies_total: int, tv_total: int):
        """Set the total counts for movies and TV in the library."""
        self._state["movies_total"] = movies_total
        self._state["tv_total"] = tv_total
        self._save()

    def clear(self):
        """Clear all state (for fresh start)."""
        self._state = {
            "scanned_files": {},
            "replaced_files": [],
            "missing_files": [],
            "scan_started": None,
            "scan_completed": None,
            "total_scanned": 0,
            "total_invalid": 0,
            "total_replaced": 0,
            "total_wrong_files": 0,
            "movies_scanned": 0,
            "movies_total": 0,
            "movies_replaced": 0,
            "tv_scanned": 0,
            "tv_total": 0,
            "tv_replaced": 0,
        }
        self._save()
        self.log.info("State cleared")

    def force_save(self):
        """Force save state to disk."""
        self._save()
