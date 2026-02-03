"""Main janitor orchestration logic."""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import structlog

from .arr_client import ArrClient, ArrType, MediaItem
from .config import Config
from .notifications import NotificationManager, ScanResult
from .reports import (
    LibraryReport,
    generate_library_report,
    format_report_email,
    find_duplicates,
    generate_mismatch_report,
    detect_path_mismatch,
)
from .scanner import Scanner
from .state import StateManager
from .validation import ValidationResult, validate_file

logger = structlog.get_logger()


class Janitor:
    """Main orchestrator for media validation and replacement."""

    def __init__(self, config: Config):
        self.config = config
        self.log = logger.bind(component="janitor")

        # Initialize state manager first
        self.state = StateManager()

        self.scanner = Scanner(config, self.state)
        self.notifications = NotificationManager(config.email)

        # Rate limiting
        self._replacements_today = 0
        self._replacement_reset_date = datetime.now().date()

    async def initialize(self):
        """Initialize the janitor and all components."""
        self.log.info("Initializing janitor")
        await self.scanner.initialize()
        # Refresh both movies and TV at startup
        await self.scanner.refresh_library("all")
        self.log.info("Janitor initialized")

    def _check_rate_limit(self) -> bool:
        """Check if we can make more replacements today."""
        today = datetime.now().date()
        if today != self._replacement_reset_date:
            self._replacements_today = 0
            self._replacement_reset_date = today

        return self._replacements_today < self.config.actions.max_replacements_per_day

    def _increment_replacement_count(self):
        """Increment the replacement counter."""
        self._replacements_today += 1

    async def validate_and_process(
        self,
        file_path: str,
        arr_type: ArrType | None = None,
    ) -> ScanResult | None:
        """
        Validate a file and process the result.

        This is the main entry point for both webhook-triggered and
        background scanner validations.
        """
        log = self.log.bind(file=file_path)
        log.info("Starting validation")

        # Check file exists
        if not Path(file_path).exists():
            log.warning("File does not exist")
            return None

        # Find the media item in Radarr/Sonarr
        item, client = await self.scanner.find_item_by_path(file_path)
        if not item:
            log.warning("File not found in Radarr/Sonarr")
            return None

        title = item.title

        # Run validation
        validation_result = await validate_file(file_path, self.config.validation)

        # Create scan result
        scan_result = ScanResult(
            file_path=file_path,
            title=title,
            valid=validation_result.valid,
            errors=validation_result.errors,
            warnings=validation_result.warnings,
        )

        # Determine media type from arr_type
        media_type = "movie" if item.arr_type == ArrType.RADARR else "tv"
        scan_result.media_type = media_type

        if validation_result.valid:
            # Check for path mismatch - if filename doesn't match expected title,
            # it's likely the wrong file entirely (not just a naming issue)
            mismatch = detect_path_mismatch(item)
            if mismatch:
                log.warning(
                    "Path mismatch detected - wrong file in folder, needs replacement",
                    title=title,
                    expected=mismatch.expected_folder,
                    actual=mismatch.actual_filename,
                )
                # Treat as invalid - this file needs to be replaced, not renamed
                validation_result.valid = False
                validation_result.errors.append(
                    f"Wrong file: expected '{mismatch.expected_folder}' but found '{mismatch.actual_filename}'"
                )
                scan_result.valid = False
                scan_result.errors = validation_result.errors
            else:
                # File is valid and in correct location
                self.scanner.mark_scanned(file_path, True, media_type)
                log.info("File validated successfully", title=title)
                self.notifications.record_result(scan_result)
                return scan_result

        # File is invalid (either failed validation or path mismatch)
        log.warning("File validation failed", title=title, errors=validation_result.errors)

        # Check if auto-replace is enabled
        if not self.config.actions.auto_replace:
            # Mark as scanned since user doesn't want auto-replace
            self.scanner.mark_scanned(file_path, False, media_type)
            scan_result.action_taken = "flagged"
            self.notifications.record_result(scan_result)
            return scan_result

        # Check rate limit - DON'T mark as scanned so it retries next day
        if not self._check_rate_limit():
            log.warning("Rate limit reached, queuing for next day", title=title)
            scan_result.action_taken = "queued"
            self.notifications.record_result(scan_result)
            return scan_result

        # Attempt replacement
        replaced = await self._replace_file(item, client, validation_result)
        if replaced:
            scan_result.action_taken = "replaced"
            self._increment_replacement_count()
            # Remove from scanned list so new file will be scanned
            self.scanner.mark_replaced(file_path)
        else:
            # Mark as scanned if replacement failed (don't keep retrying)
            self.scanner.mark_scanned(file_path, False, media_type)
            scan_result.action_taken = "flagged"

        self.notifications.record_result(scan_result)
        return scan_result

    async def _replace_file(
        self,
        item: MediaItem,
        client: ArrClient,
        validation_result: ValidationResult,
    ) -> bool:
        """Delete a bad file and trigger re-download."""
        log = self.log.bind(title=item.title, file_id=item.file_id)

        if not item.file_id:
            log.error("No file ID available for deletion")
            return False

        # Delete the file
        log.info("Deleting bad file")
        deleted = await client.delete_file(item.file_id)
        if not deleted:
            log.error("Failed to delete file")
            return False

        # Add to blocklist if enabled
        if self.config.actions.blocklist_bad_releases:
            await client.add_to_blocklist(
                item,
                message=f"Blocked by media-janitor: {', '.join(validation_result.errors[:2])}",
            )

        # Trigger search for replacement
        log.info("Triggering search for replacement")
        searched = await client.search_for_replacement(item)
        if not searched:
            log.error("Failed to trigger replacement search")
            # File is deleted but search failed - this is still a partial success
            return True

        log.info("Replacement process initiated")
        return True

    async def run_background_scan(self):
        """Run a batch of background scans."""
        if not self.config.scanner.enabled:
            self.log.debug("Background scanner disabled")
            return

        # Check if initial scan is complete
        initial_done = self.scanner.check_initial_scan_complete()

        # If using "watch_only" mode and initial scan is done, skip background scanning
        if self.config.scanner.mode == "watch_only" and initial_done:
            self.log.debug("Watch-only mode, initial scan complete, skipping background scan")
            return

        # Calculate batch size based on files_per_hour
        # Assuming this method is called every minute
        batch_size = max(1, self.config.scanner.files_per_hour // 60)

        # Check if we need to refresh the library
        status = self.scanner.get_status()
        if status["queue_size"] == 0:
            self.log.info("Scan queue empty, refreshing library")
            # Refresh both movies and TV
            count = await self.scanner.refresh_library("all")
            if count == 0:
                self.log.info("All files have been scanned")
                return

        # Get next batch
        batch = self.scanner.get_next_batch(batch_size)
        if not batch:
            return

        self.log.info("Running background scan batch", count=len(batch))

        for item in batch:
            if not item.file_path:
                continue

            try:
                await self.validate_and_process(item.file_path, item.arr_type)
            except Exception as e:
                self.log.error("Error validating file", file=item.file_path, error=str(e))

            # Small delay between files
            await asyncio.sleep(1)

    async def refresh_tv_library(self):
        """Refresh TV library in background (slow operation)."""
        self.log.info("Starting scheduled TV library refresh")
        try:
            count = await self.scanner.refresh_library("tv")
            self.log.info("TV library refresh complete", new_episodes=count)
        except Exception as e:
            self.log.error("TV library refresh failed", error=str(e))

    async def generate_library_report(self, top_n: int = 50, source: str = "all") -> LibraryReport:
        """Generate a library report with largest files."""
        return await generate_library_report(self.scanner, top_n, source)

    async def send_library_report(self, top_n: int = 50) -> bool:
        """Generate and send a library report via email."""
        report = await self.generate_library_report(top_n)
        html = format_report_email(report)
        return await self.notifications._send_email(
            f"Media Library Report - {report.generated_at.strftime('%Y-%m-%d')}",
            html,
        )

    async def send_daily_summary(self):
        """Send the daily summary email with duplicates and path mismatches."""
        # Fetch duplicates and path mismatches to include in email
        try:
            duplicates = await find_duplicates(self.scanner, source="movies")
            mismatches = await generate_mismatch_report(self.scanner, source="movies")

            # Get the summary before clearing, add extras, then send
            summary = self.notifications.get_summary(clear=True)
            summary.duplicates = duplicates
            summary.path_mismatches = mismatches

            # Send the enhanced summary
            await self.notifications.send_summary_with_extras(summary)
        except Exception as e:
            self.log.error("Failed to generate full daily summary", error=str(e))
            # Fallback to basic summary
            await self.notifications.send_daily_summary()

    def get_status(self) -> dict:
        """Get current janitor status."""
        return {
            "scanner": self.scanner.get_status(),
            "replacements_today": self._replacements_today,
            "max_replacements_per_day": self.config.actions.max_replacements_per_day,
            "auto_replace_enabled": self.config.actions.auto_replace,
        }
