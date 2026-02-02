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
            "scanned_files": {},  # path -> timestamp of last scan
            "scan_started": None,
            "scan_completed": None,
            "total_scanned": 0,
            "total_invalid": 0,
            "total_replaced": 0,
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

    def mark_scanned(self, file_path: str, valid: bool = True):
        """Mark a file as scanned."""
        if "scanned_files" not in self._state:
            self._state["scanned_files"] = {}

        self._state["scanned_files"][file_path] = {
            "timestamp": datetime.now().isoformat(),
            "valid": valid,
        }
        self._state["total_scanned"] = self._state.get("total_scanned", 0) + 1

        if not valid:
            self._state["total_invalid"] = self._state.get("total_invalid", 0) + 1

        # Save periodically (every 10 scans)
        if self._state["total_scanned"] % 10 == 0:
            self._save()

    def mark_replaced(self, file_path: str):
        """Mark a file as replaced (remove from scanned, increment counter)."""
        if file_path in self._state.get("scanned_files", {}):
            del self._state["scanned_files"][file_path]
        self._state["total_replaced"] = self._state.get("total_replaced", 0) + 1
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

    def get_stats(self) -> dict:
        """Get scan statistics."""
        scanned_files = self._state.get("scanned_files", {})
        valid_count = sum(1 for f in scanned_files.values() if f.get("valid", True))
        invalid_count = len(scanned_files) - valid_count

        return {
            "total_scanned": len(scanned_files),
            "valid_files": valid_count,
            "invalid_files": invalid_count,
            "total_replaced": self._state.get("total_replaced", 0),
            "scan_started": self._state.get("scan_started"),
            "scan_completed": self._state.get("scan_completed"),
            "initial_scan_done": self._state.get("scan_completed") is not None,
        }

    def clear(self):
        """Clear all state (for fresh start)."""
        self._state = {
            "scanned_files": {},
            "scan_started": None,
            "scan_completed": None,
            "total_scanned": 0,
            "total_invalid": 0,
            "total_replaced": 0,
        }
        self._save()
        self.log.info("State cleared")

    def force_save(self):
        """Force save state to disk."""
        self._save()
