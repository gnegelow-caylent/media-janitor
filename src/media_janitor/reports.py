"""Library reports and statistics."""

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

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
class PathMismatch:
    """A file whose path doesn't match its expected title."""

    title: str
    year: int | None
    expected_folder: str  # What the folder should contain
    actual_filename: str  # What the file is actually named
    file_path: str
    folder_path: str
    arr_instance: str
    mismatch_type: str  # "wrong_movie", "wrong_folder", "naming_issue"


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


def normalize_title(title: str) -> str:
    """Normalize a title for comparison (lowercase, remove punctuation)."""
    # Remove common punctuation and normalize
    normalized = title.lower()
    normalized = re.sub(r"[':.,!?&\-\(\)]", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def extract_title_from_filename(filename: str) -> str:
    """Extract the movie/show title from a filename."""
    # Remove extension
    name = Path(filename).stem

    # Remove common patterns: year, quality, release group, etc.
    # Pattern: "Movie Name (2020)" or "Movie.Name.2020.1080p..."
    patterns = [
        r"\(\d{4}\).*$",  # (2020) and everything after
        r"\.\d{4}\.",  # .2020. (dot-separated year)
        r"\s\d{4}\s",  # space-separated year
        r"\.1080p\..*$",
        r"\.2160p\..*$",
        r"\.720p\..*$",
        r"\.480p\..*$",
        r"\.BluRay\..*$",
        r"\.WEBRip\..*$",
        r"\.WEBDL\..*$",
        r"\.Remux\..*$",
        r"\.BR-DISK.*$",
        r"-[A-Za-z0-9]+$",  # Release group at end
    ]

    for pattern in patterns:
        name = re.sub(pattern, "", name, flags=re.IGNORECASE)

    # Replace dots and underscores with spaces
    name = name.replace(".", " ").replace("_", " ")
    name = re.sub(r"\s+", " ", name).strip()

    return name


def detect_path_mismatch(item: MediaItem) -> PathMismatch | None:
    """Check if a file's path matches its expected title."""
    if not item.file_path or not item.folder_path:
        return None

    filename = Path(item.file_path).name
    folder_name = Path(item.folder_path).name

    # Extract title from filename
    filename_title = extract_title_from_filename(filename)
    normalized_filename = normalize_title(filename_title)
    normalized_expected = normalize_title(item.title)

    # Check if the filename contains the expected movie title
    # Allow for some flexibility (title should be in filename)
    if normalized_expected in normalized_filename:
        return None

    # Check if at least 60% of the expected title words are in the filename
    expected_words = set(normalized_expected.split())
    filename_words = set(normalized_filename.split())

    if expected_words and len(expected_words.intersection(filename_words)) / len(expected_words) >= 0.6:
        return None

    # This is a mismatch
    return PathMismatch(
        title=item.title,
        year=item.year,
        expected_folder=f"{item.title} ({item.year})" if item.year else item.title,
        actual_filename=filename,
        file_path=item.file_path,
        folder_path=item.folder_path,
        arr_instance=item.arr_instance or "unknown",
        mismatch_type="wrong_movie",
    )


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


async def generate_mismatch_report(
    scanner: Scanner,
    source: str = "movies",
) -> list[PathMismatch]:
    """Generate a report of files with mismatched paths."""
    log = logger.bind(component="reports")
    log.info("Generating mismatch report", source=source)

    all_media: list[MediaItem] = []

    for client in scanner.get_all_clients():
        if source == "movies" and client.arr_type != ArrType.RADARR:
            continue
        if source == "tv" and client.arr_type != ArrType.SONARR:
            continue

        try:
            log.info(f"Checking {client.instance.name} for mismatches...")
            media = await client.get_all_media()
            all_media.extend(media)
        except Exception as e:
            log.error("Failed to fetch media", client=client.instance.name, error=str(e))

    mismatches = []
    for item in all_media:
        if not item.file_path:
            continue
        mismatch = detect_path_mismatch(item)
        if mismatch:
            mismatches.append(mismatch)

    log.info("Mismatch report generated", total_checked=len(all_media), mismatches_found=len(mismatches))
    return mismatches


@dataclass
class DuplicateGroup:
    """A group of duplicate files for the same content."""

    title: str
    year: int | None
    files: list[FileStats]
    total_size_bytes: int
    potential_savings_bytes: int  # Size if we kept only the best quality


async def find_duplicates(
    scanner: Scanner,
    source: str = "movies",
) -> list[DuplicateGroup]:
    """Find duplicate content (same movie/show in multiple qualities)."""
    log = logger.bind(component="reports")
    log.info("Finding duplicates", source=source)

    all_media: list[MediaItem] = []

    for client in scanner.get_all_clients():
        if source == "movies" and client.arr_type != ArrType.RADARR:
            continue
        if source == "tv" and client.arr_type != ArrType.SONARR:
            continue

        try:
            media = await client.get_all_media()
            all_media.extend(media)
        except Exception as e:
            log.error("Failed to fetch media", client=client.instance.name, error=str(e))

    # Group by normalized title + year
    from collections import defaultdict
    groups: dict[str, list[MediaItem]] = defaultdict(list)

    for item in all_media:
        if not item.file_path or not item.size_bytes:
            continue
        # Normalize key: lowercase title + year
        key = f"{item.title.lower()}|{item.year or 'unknown'}"
        groups[key].append(item)

    # Find groups with multiple files
    duplicates = []
    for key, items in groups.items():
        if len(items) <= 1:
            continue

        # Sort by size (largest = best quality typically)
        sorted_items = sorted(items, key=lambda x: x.size_bytes or 0, reverse=True)

        files = [
            FileStats(
                title=m.title,
                file_path=m.file_path,
                size_bytes=m.size_bytes,
                size_human=bytes_to_human(m.size_bytes),
                quality=m.quality,
                arr_instance=m.arr_instance or "unknown",
                arr_type=m.arr_type.value if m.arr_type else "unknown",
            )
            for m in sorted_items
        ]

        total_size = sum(m.size_bytes for m in sorted_items)
        best_size = sorted_items[0].size_bytes
        savings = total_size - best_size

        duplicates.append(DuplicateGroup(
            title=sorted_items[0].title,
            year=sorted_items[0].year,
            files=files,
            total_size_bytes=total_size,
            potential_savings_bytes=savings,
        ))

    # Sort by potential savings
    duplicates.sort(key=lambda x: x.potential_savings_bytes, reverse=True)

    log.info("Duplicate search complete", groups_found=len(duplicates))
    return duplicates


@dataclass
class CodecStats:
    """Statistics about codecs in the library."""

    video_codecs: dict[str, int]
    audio_codecs: dict[str, int]
    containers: dict[str, int]
    hdr_types: dict[str, int]
    total_files: int


async def get_codec_breakdown(
    scanner: Scanner,
    source: str = "movies",
) -> CodecStats:
    """Get codec breakdown of the library."""
    log = logger.bind(component="reports")
    log.info("Getting codec breakdown", source=source)

    video_codecs: dict[str, int] = {}
    audio_codecs: dict[str, int] = {}
    containers: dict[str, int] = {}
    hdr_types: dict[str, int] = {}
    total = 0

    for client in scanner.get_all_clients():
        if source == "movies" and client.arr_type != ArrType.RADARR:
            continue
        if source == "tv" and client.arr_type != ArrType.SONARR:
            continue

        try:
            media = await client.get_all_media()
            for item in media:
                if not item.file_path:
                    continue

                total += 1

                # Extract container from file extension
                ext = Path(item.file_path).suffix.lower().lstrip('.')
                containers[ext] = containers.get(ext, 0) + 1

                # Quality string often contains codec info
                quality = item.quality or ""
                quality_lower = quality.lower()

                # Detect video codec from quality
                if "hevc" in quality_lower or "x265" in quality_lower or "h265" in quality_lower:
                    video_codecs["HEVC/H.265"] = video_codecs.get("HEVC/H.265", 0) + 1
                elif "av1" in quality_lower:
                    video_codecs["AV1"] = video_codecs.get("AV1", 0) + 1
                elif "x264" in quality_lower or "h264" in quality_lower or "avc" in quality_lower:
                    video_codecs["H.264/AVC"] = video_codecs.get("H.264/AVC", 0) + 1
                elif "mpeg" in quality_lower:
                    video_codecs["MPEG"] = video_codecs.get("MPEG", 0) + 1
                else:
                    video_codecs["Unknown"] = video_codecs.get("Unknown", 0) + 1

                # Detect HDR
                if "dv" in quality_lower or "dolby vision" in quality_lower:
                    hdr_types["Dolby Vision"] = hdr_types.get("Dolby Vision", 0) + 1
                elif "hdr10+" in quality_lower:
                    hdr_types["HDR10+"] = hdr_types.get("HDR10+", 0) + 1
                elif "hdr" in quality_lower or "2160p" in quality_lower:
                    hdr_types["HDR10"] = hdr_types.get("HDR10", 0) + 1
                else:
                    hdr_types["SDR"] = hdr_types.get("SDR", 0) + 1

                # Audio codec detection would require ffprobe - skip for now
                audio_codecs["Unknown"] = audio_codecs.get("Unknown", 0) + 1

        except Exception as e:
            log.error("Failed to fetch media", client=client.instance.name, error=str(e))

    return CodecStats(
        video_codecs=video_codecs,
        audio_codecs=audio_codecs,
        containers=containers,
        hdr_types=hdr_types,
        total_files=total,
    )


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
