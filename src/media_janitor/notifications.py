"""Email notification system."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import aiosmtplib
import structlog

from .config import EmailConfig

logger = structlog.get_logger()


@dataclass
class ScanResult:
    """Result of scanning a file."""

    file_path: str
    title: str
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    action_taken: str | None = None  # "replaced", "flagged", None
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
    errors: list[str] = field(default_factory=list)
    invalid_files: list[ScanResult] = field(default_factory=list)


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
            if result.valid:
                summary.files_valid += 1
            else:
                summary.files_invalid += 1
                summary.invalid_files.append(result)

            if result.action_taken == "replaced":
                summary.files_replaced += 1
            elif result.action_taken == "flagged":
                summary.files_flagged += 1

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

    def _format_summary_email(self, summary: DailySummary) -> str:
        """Format the summary as an HTML email."""
        html = f"""
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; }}
                .stats {{ background: #f5f5f5; padding: 15px; border-radius: 5px; margin-bottom: 20px; }}
                .stat {{ display: inline-block; margin-right: 30px; }}
                .stat-value {{ font-size: 24px; font-weight: bold; color: #333; }}
                .stat-label {{ font-size: 12px; color: #666; }}
                .good {{ color: #28a745; }}
                .bad {{ color: #dc3545; }}
                .warning {{ color: #ffc107; }}
                table {{ border-collapse: collapse; width: 100%; }}
                th, td {{ padding: 8px; text-align: left; border-bottom: 1px solid #ddd; }}
                th {{ background: #333; color: white; }}
                .error {{ color: #dc3545; font-size: 12px; }}
            </style>
        </head>
        <body>
            <h1>Media Janitor Daily Report</h1>
            <p>{summary.date.strftime('%A, %B %d, %Y')}</p>

            <div class="stats">
                <div class="stat">
                    <div class="stat-value">{summary.files_scanned}</div>
                    <div class="stat-label">Files Scanned</div>
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
                    <div class="stat-value">{summary.files_replaced}</div>
                    <div class="stat-label">Replaced</div>
                </div>
            </div>
        """

        if summary.invalid_files:
            html += """
            <h2>Invalid Files</h2>
            <table>
                <tr>
                    <th>Title</th>
                    <th>Errors</th>
                    <th>Action</th>
                </tr>
            """
            for result in summary.invalid_files[:50]:  # Limit to 50 entries
                errors_html = "<br>".join(result.errors[:3])  # Limit to 3 errors
                action = result.action_taken or "none"
                html += f"""
                <tr>
                    <td>{result.title}</td>
                    <td class="error">{errors_html}</td>
                    <td>{action}</td>
                </tr>
                """
            html += "</table>"

            if len(summary.invalid_files) > 50:
                html += f"<p>...and {len(summary.invalid_files) - 50} more</p>"

        html += """
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
