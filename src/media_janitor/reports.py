"""Library reports and statistics."""

from dataclasses import dataclass
from datetime import datetime

import structlog

from .arr_client import ArrClient, ArrType, MediaItem
from .scanner import Scanner

logger = structlog.get_logger()


@dataclass
class FileStats:
    """Statistics for a single file."""

    title: str
    file_path: str
    size_bytes: int
    size_human: str
    quality: str | None
    arr_instance: str
    arr_type: str


@dataclass
class LibraryReport:
    """Library statistics report."""

    generated_at: datetime
    total_files: int
    total_size_bytes: int
    total_size_human: str
    largest_files: list[FileStats]
    smallest_files: list[FileStats]
    files_by_quality: dict[str, int]
    files_by_instance: dict[str, int]


def bytes_to_human(size_bytes: int) -> str:
    """Convert bytes to human readable string."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(size_bytes) < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"


async def generate_library_report(
    scanner: Scanner,
    top_n: int = 50,
    source: str = "all",  # "all", "movies", "tv"
) -> LibraryReport:
    """Generate a report of library statistics."""
    log = logger.bind(component="reports")
    log.info("Generating library report", source=source)

    all_media: list[MediaItem] = []

    # Fetch from selected clients
    for client in scanner.get_all_clients():
        # Filter by source type
        if source == "movies" and client.arr_type != ArrType.RADARR:
            continue
        if source == "tv" and client.arr_type != ArrType.SONARR:
            continue

        try:
            log.info(f"Fetching from {client.instance.name}...")
            media = await client.get_all_media()
            all_media.extend(media)
            log.info(f"Fetched {len(media)} items from {client.instance.name}")
        except Exception as e:
            log.error("Failed to fetch media", client=client.instance.name, error=str(e))

    # Filter to items with files and sizes
    files_with_size = [m for m in all_media if m.file_path and m.size_bytes]

    if not files_with_size:
        return LibraryReport(
            generated_at=datetime.now(),
            total_files=0,
            total_size_bytes=0,
            total_size_human="0 B",
            largest_files=[],
            smallest_files=[],
            files_by_quality={},
            files_by_instance={},
        )

    # Calculate totals
    total_size = sum(m.size_bytes for m in files_with_size)

    # Sort by size
    sorted_by_size = sorted(files_with_size, key=lambda m: m.size_bytes, reverse=True)

    # Top N largest
    largest = [
        FileStats(
            title=m.title,
            file_path=m.file_path,
            size_bytes=m.size_bytes,
            size_human=bytes_to_human(m.size_bytes),
            quality=m.quality,
            arr_instance=m.arr_instance,
            arr_type=m.arr_type.value if m.arr_type else "unknown",
        )
        for m in sorted_by_size[:top_n]
    ]

    # Bottom N smallest (might be suspicious)
    smallest = [
        FileStats(
            title=m.title,
            file_path=m.file_path,
            size_bytes=m.size_bytes,
            size_human=bytes_to_human(m.size_bytes),
            quality=m.quality,
            arr_instance=m.arr_instance,
            arr_type=m.arr_type.value if m.arr_type else "unknown",
        )
        for m in sorted_by_size[-top_n:]
    ]

    # Group by quality
    quality_counts: dict[str, int] = {}
    for m in files_with_size:
        q = m.quality or "Unknown"
        quality_counts[q] = quality_counts.get(q, 0) + 1

    # Group by instance
    instance_counts: dict[str, int] = {}
    for m in files_with_size:
        inst = m.arr_instance or "Unknown"
        instance_counts[inst] = instance_counts.get(inst, 0) + 1

    log.info(
        "Report generated",
        total_files=len(files_with_size),
        total_size=bytes_to_human(total_size),
    )

    return LibraryReport(
        generated_at=datetime.now(),
        total_files=len(files_with_size),
        total_size_bytes=total_size,
        total_size_human=bytes_to_human(total_size),
        largest_files=largest,
        smallest_files=smallest,
        files_by_quality=quality_counts,
        files_by_instance=instance_counts,
    )


def format_report_email(report: LibraryReport) -> str:
    """Format the library report as HTML email."""
    html = f"""
    <html>
    <head>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; }}
            .stats {{ background: #f5f5f5; padding: 15px; border-radius: 5px; margin-bottom: 20px; }}
            .stat {{ display: inline-block; margin-right: 30px; }}
            .stat-value {{ font-size: 24px; font-weight: bold; color: #333; }}
            .stat-label {{ font-size: 12px; color: #666; }}
            table {{ border-collapse: collapse; width: 100%; margin-bottom: 20px; }}
            th, td {{ padding: 8px; text-align: left; border-bottom: 1px solid #ddd; }}
            th {{ background: #333; color: white; }}
            .size {{ font-family: monospace; white-space: nowrap; }}
            h2 {{ margin-top: 30px; }}
        </style>
    </head>
    <body>
        <h1>Media Library Report</h1>
        <p>{report.generated_at.strftime('%A, %B %d, %Y at %H:%M')}</p>

        <div class="stats">
            <div class="stat">
                <div class="stat-value">{report.total_files:,}</div>
                <div class="stat-label">Total Files</div>
            </div>
            <div class="stat">
                <div class="stat-value">{report.total_size_human}</div>
                <div class="stat-label">Total Size</div>
            </div>
        </div>

        <h2>Top 50 Largest Files</h2>
        <table>
            <tr>
                <th>#</th>
                <th>Title</th>
                <th>Size</th>
                <th>Quality</th>
                <th>Instance</th>
            </tr>
    """

    for i, f in enumerate(report.largest_files, 1):
        html += f"""
            <tr>
                <td>{i}</td>
                <td>{f.title}</td>
                <td class="size">{f.size_human}</td>
                <td>{f.quality or '-'}</td>
                <td>{f.arr_instance}</td>
            </tr>
        """

    html += """
        </table>

        <h2>Files by Quality</h2>
        <table>
            <tr>
                <th>Quality</th>
                <th>Count</th>
            </tr>
    """

    for quality, count in sorted(report.files_by_quality.items(), key=lambda x: -x[1]):
        html += f"""
            <tr>
                <td>{quality}</td>
                <td>{count:,}</td>
            </tr>
        """

    html += """
        </table>

        <h2>Files by Instance</h2>
        <table>
            <tr>
                <th>Instance</th>
                <th>Count</th>
            </tr>
    """

    for instance, count in sorted(report.files_by_instance.items(), key=lambda x: -x[1]):
        html += f"""
            <tr>
                <td>{instance}</td>
                <td>{count:,}</td>
            </tr>
        """

    html += """
        </table>
    </body>
    </html>
    """

    return html


def format_report_text(report: LibraryReport) -> str:
    """Format the library report as plain text."""
    lines = [
        "=" * 60,
        "MEDIA LIBRARY REPORT",
        f"Generated: {report.generated_at.strftime('%Y-%m-%d %H:%M')}",
        "=" * 60,
        "",
        f"Total Files: {report.total_files:,}",
        f"Total Size:  {report.total_size_human}",
        "",
        "-" * 60,
        "TOP 50 LARGEST FILES",
        "-" * 60,
    ]

    for i, f in enumerate(report.largest_files, 1):
        lines.append(f"{i:3}. [{f.size_human:>10}] {f.title}")

    lines.extend([
        "",
        "-" * 60,
        "FILES BY QUALITY",
        "-" * 60,
    ])

    for quality, count in sorted(report.files_by_quality.items(), key=lambda x: -x[1]):
        lines.append(f"  {quality}: {count:,}")

    lines.extend([
        "",
        "-" * 60,
        "FILES BY INSTANCE",
        "-" * 60,
    ])

    for instance, count in sorted(report.files_by_instance.items(), key=lambda x: -x[1]):
        lines.append(f"  {instance}: {count:,}")

    return "\n".join(lines)
