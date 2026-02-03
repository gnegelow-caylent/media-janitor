"""Email notification system."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

import aiosmtplib
import structlog

from .config import EmailConfig

if TYPE_CHECKING:
    from .reports import DuplicateGroup, PathMismatch

logger = structlog.get_logger()


@dataclass
class ScanResult:
    """Result of scanning a file."""

    file_path: str
    title: str
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    action_taken: str | None = None  # "replaced", "flagged", "queued", None
    media_type: str = "unknown"  # "movie", "tv", "unknown"
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class DailySummary:
    """Daily summary of janitor activity."""

    date: datetime
    files_scanned: int = 0
    files_valid: int = 0
    files_invalid: int = 0
    files_replaced: int = 0
    files_flagged: int = 0
    files_queued: int = 0
    # Separate tracking for movies and TV
    movies_scanned: int = 0
    movies_replaced: int = 0
    movies_queued: int = 0
    tv_scanned: int = 0
    tv_replaced: int = 0
    tv_queued: int = 0
    errors: list[str] = field(default_factory=list)
    invalid_files: list[ScanResult] = field(default_factory=list)
    # Duplicates and mismatches for reporting
    duplicates: list["DuplicateGroup"] = field(default_factory=list)
    path_mismatches: list["PathMismatch"] = field(default_factory=list)


class NotificationManager:
    """Manages email notifications."""

    def __init__(self, config: EmailConfig):
        self.config = config
        self.log = logger.bind(component="notifications")

        # Track results for daily summary
        self._results: list[ScanResult] = []
        self._last_summary_date: datetime | None = None

    def record_result(self, result: ScanResult):
        """Record a scan result for the daily summary."""
        self._results.append(result)

    def get_summary(self, clear: bool = True) -> DailySummary:
        """Generate a summary of recent activity."""
        summary = DailySummary(date=datetime.now())

        for result in self._results:
            summary.files_scanned += 1
            is_movie = result.media_type == "movie"
            is_tv = result.media_type == "tv"

            if is_movie:
                summary.movies_scanned += 1
            elif is_tv:
                summary.tv_scanned += 1

            if result.valid:
                summary.files_valid += 1
            else:
                summary.files_invalid += 1
                summary.invalid_files.append(result)

            if result.action_taken == "replaced":
                summary.files_replaced += 1
                if is_movie:
                    summary.movies_replaced += 1
                elif is_tv:
                    summary.tv_replaced += 1
            elif result.action_taken == "flagged":
                summary.files_flagged += 1
            elif result.action_taken == "queued":
                summary.files_queued += 1
                if is_movie:
                    summary.movies_queued += 1
                elif is_tv:
                    summary.tv_queued += 1

        if clear:
            self._results = []
            self._last_summary_date = datetime.now()

        return summary

    async def send_daily_summary(self) -> bool:
        """Send the daily summary email."""
        if not self.config.enabled:
            self.log.debug("Email notifications disabled")
            return False

        summary = self.get_summary(clear=True)

        if summary.files_scanned == 0:
            self.log.info("No activity to report")
            return True

        subject = f"Media Janitor Daily Report - {summary.date.strftime('%Y-%m-%d')}"
        body = self._format_summary_email(summary)

        return await self._send_email(subject, body)

    async def send_summary_with_extras(self, summary: DailySummary) -> bool:
        """Send a daily summary with pre-populated duplicates and mismatches."""
        if not self.config.enabled:
            self.log.debug("Email notifications disabled")
            return False

        if summary.files_scanned == 0 and not summary.duplicates and not summary.path_mismatches:
            self.log.info("No activity to report")
            return True

        subject = f"Media Janitor Daily Report - {summary.date.strftime('%Y-%m-%d')}"
        body = self._format_summary_email(summary)

        return await self._send_email(subject, body)

    def _format_summary_email(self, summary: DailySummary) -> str:
        """Format the summary as an HTML email."""
        # Separate invalid files by type
        invalid_movies = [r for r in summary.invalid_files if r.media_type == "movie"]
        invalid_tv = [r for r in summary.invalid_files if r.media_type == "tv"]

        html = f"""
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; background: #1a1a2e; color: #eee; }}
                h1 {{ color: #fbbf24; }}
                h2 {{ color: #fbbf24; margin-top: 30px; }}
                h3 {{ color: #9ca3af; margin-top: 20px; }}
                .stats {{ background: #16213e; padding: 20px; border-radius: 10px; margin-bottom: 20px; }}
                .stat {{ display: inline-block; margin-right: 40px; text-align: center; }}
                .stat-value {{ font-size: 28px; font-weight: bold; color: #fff; }}
                .stat-label {{ font-size: 12px; color: #9ca3af; text-transform: uppercase; }}
                .good {{ color: #10b981; }}
                .bad {{ color: #ef4444; }}
                .warning {{ color: #f59e0b; }}
                .queued {{ color: #3b82f6; }}
                .section {{ background: #16213e; padding: 15px; border-radius: 10px; margin-bottom: 20px; }}
                .section-title {{ display: flex; align-items: center; gap: 10px; margin-bottom: 15px; }}
                .movie-icon {{ color: #f97316; }}
                .tv-icon {{ color: #3b82f6; }}
                table {{ border-collapse: collapse; width: 100%; }}
                th, td {{ padding: 10px; text-align: left; border-bottom: 1px solid #374151; }}
                th {{ background: #0f172a; color: #9ca3af; font-size: 12px; text-transform: uppercase; }}
                .error {{ color: #f87171; font-size: 11px; }}
                .action-replaced {{ color: #10b981; }}
                .action-queued {{ color: #3b82f6; }}
                .action-flagged {{ color: #f59e0b; }}
            </style>
        </head>
        <body>
            <h1>Media Janitor Daily Report</h1>
            <p style="color: #9ca3af;">{summary.date.strftime('%A, %B %d, %Y')}</p>

            <div class="stats">
                <div class="stat">
                    <div class="stat-value">{summary.files_scanned}</div>
                    <div class="stat-label">Scanned</div>
                </div>
                <div class="stat">
                    <div class="stat-value good">{summary.files_valid}</div>
                    <div class="stat-label">Valid</div>
                </div>
                <div class="stat">
                    <div class="stat-value bad">{summary.files_invalid}</div>
                    <div class="stat-label">Invalid</div>
                </div>
                <div class="stat">
                    <div class="stat-value good">{summary.files_replaced}</div>
                    <div class="stat-label">Replaced</div>
                </div>
                <div class="stat">
                    <div class="stat-value queued">{summary.files_queued}</div>
                    <div class="stat-label">Queued</div>
                </div>
            </div>

            <h2>Breakdown by Type</h2>
            <div class="stats">
                <div class="stat">
                    <div class="stat-value movie-icon">{summary.movies_scanned}</div>
                    <div class="stat-label">Movies Scanned</div>
                </div>
                <div class="stat">
                    <div class="stat-value good">{summary.movies_replaced}</div>
                    <div class="stat-label">Movies Replaced</div>
                </div>
                <div class="stat">
                    <div class="stat-value queued">{summary.movies_queued}</div>
                    <div class="stat-label">Movies Queued</div>
                </div>
            </div>
            <div class="stats">
                <div class="stat">
                    <div class="stat-value tv-icon">{summary.tv_scanned}</div>
                    <div class="stat-label">TV Episodes Scanned</div>
                </div>
                <div class="stat">
                    <div class="stat-value good">{summary.tv_replaced}</div>
                    <div class="stat-label">TV Replaced</div>
                </div>
                <div class="stat">
                    <div class="stat-value queued">{summary.tv_queued}</div>
                    <div class="stat-label">TV Queued</div>
                </div>
            </div>
        """

        # Movies section
        if invalid_movies:
            html += """
            <div class="section">
                <div class="section-title">
                    <span class="movie-icon" style="font-size: 20px;">🎬</span>
                    <h3 style="margin: 0;">Movies with Issues</h3>
                </div>
                <table>
                    <tr>
                        <th>Title</th>
                        <th>Error</th>
                        <th>Action</th>
                    </tr>
            """
            for result in invalid_movies[:25]:
                errors_html = result.errors[0][:80] if result.errors else "Unknown error"
                action = result.action_taken or "none"
                action_class = f"action-{action}" if action in ["replaced", "queued", "flagged"] else ""
                html += f"""
                <tr>
                    <td>{result.title}</td>
                    <td class="error">{errors_html}</td>
                    <td class="{action_class}">{action}</td>
                </tr>
                """
            html += "</table>"
            if len(invalid_movies) > 25:
                html += f"<p style='color: #9ca3af;'>...and {len(invalid_movies) - 25} more movies</p>"
            html += "</div>"

        # TV section
        if invalid_tv:
            html += """
            <div class="section">
                <div class="section-title">
                    <span class="tv-icon" style="font-size: 20px;">📺</span>
                    <h3 style="margin: 0;">TV Episodes with Issues</h3>
                </div>
                <table>
                    <tr>
                        <th>Title</th>
                        <th>Error</th>
                        <th>Action</th>
                    </tr>
            """
            for result in invalid_tv[:25]:
                errors_html = result.errors[0][:80] if result.errors else "Unknown error"
                action = result.action_taken or "none"
                action_class = f"action-{action}" if action in ["replaced", "queued", "flagged"] else ""
                html += f"""
                <tr>
                    <td>{result.title}</td>
                    <td class="error">{errors_html}</td>
                    <td class="{action_class}">{action}</td>
                </tr>
                """
            html += "</table>"
            if len(invalid_tv) > 25:
                html += f"<p style='color: #9ca3af;'>...and {len(invalid_tv) - 25} more TV episodes</p>"
            html += "</div>"

        # Duplicates section
        if summary.duplicates:
            from .reports import bytes_to_human
            total_savings = sum(d.potential_savings_bytes for d in summary.duplicates)
            html += f"""
            <div class="section">
                <div class="section-title">
                    <span style="font-size: 20px;">📦</span>
                    <h3 style="margin: 0;">Duplicates Found</h3>
                </div>
                <p style="color: #9ca3af;">
                    Found <span class="warning">{len(summary.duplicates)}</span> items with multiple copies.
                    Potential savings: <span class="warning">{bytes_to_human(total_savings)}</span>
                </p>
                <table>
                    <tr>
                        <th>Title</th>
                        <th>Copies</th>
                        <th>Potential Savings</th>
                    </tr>
            """
            for dup in summary.duplicates[:15]:
                html += f"""
                <tr>
                    <td>{dup.title} ({dup.year or 'N/A'})</td>
                    <td>{len(dup.files)}</td>
                    <td class="warning">{bytes_to_human(dup.potential_savings_bytes)}</td>
                </tr>
                """
            html += "</table>"
            if len(summary.duplicates) > 15:
                html += f"<p style='color: #9ca3af;'>...and {len(summary.duplicates) - 15} more duplicates</p>"
            html += "</div>"

        # Path mismatches section
        if summary.path_mismatches:
            html += f"""
            <div class="section">
                <div class="section-title">
                    <span style="font-size: 20px;">⚠️</span>
                    <h3 style="margin: 0;">Path Mismatches</h3>
                </div>
                <p style="color: #9ca3af;">
                    Found <span class="warning">{len(summary.path_mismatches)}</span> files with path naming issues.
                </p>
                <table>
                    <tr>
                        <th>Expected</th>
                        <th>Actual Filename</th>
                    </tr>
            """
            for mm in summary.path_mismatches[:15]:
                html += f"""
                <tr>
                    <td>{mm.expected_folder}</td>
                    <td style="color: #f87171; font-size: 11px;">{mm.actual_filename[:60]}{'...' if len(mm.actual_filename) > 60 else ''}</td>
                </tr>
                """
            html += "</table>"
            if len(summary.path_mismatches) > 15:
                html += f"<p style='color: #9ca3af;'>...and {len(summary.path_mismatches) - 15} more mismatches</p>"
            html += "</div>"

        html += """
            <p style="color: #6b7280; font-size: 12px; margin-top: 30px;">
                Generated by Media Janitor
            </p>
        </body>
        </html>
        """

        return html

    async def _send_email(self, subject: str, html_body: str) -> bool:
        """Send an email."""
        if not self.config.enabled:
            return False

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = self.config.from_address
            msg["To"] = self.config.to_address

            # Plain text version
            plain_text = f"Media Janitor Report\n\nPlease view this email in HTML format."
            msg.attach(MIMEText(plain_text, "plain"))
            msg.attach(MIMEText(html_body, "html"))

            await aiosmtplib.send(
                msg,
                hostname=self.config.smtp_host,
                port=self.config.smtp_port,
                username=self.config.smtp_user,
                password=self.config.smtp_password,
                start_tls=True,
            )

            self.log.info("Email sent successfully", subject=subject)
            return True

        except Exception as e:
            self.log.error("Failed to send email", error=str(e))
            return False

    async def send_alert(self, title: str, message: str) -> bool:
        """Send an immediate alert email."""
        if not self.config.enabled:
            return False

        subject = f"Media Janitor Alert: {title}"
        html_body = f"""
        <html>
        <body>
            <h1>Media Janitor Alert</h1>
            <p><strong>{title}</strong></p>
            <p>{message}</p>
            <p><em>Sent at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</em></p>
        </body>
        </html>
        """

        return await self._send_email(subject, html_body)
