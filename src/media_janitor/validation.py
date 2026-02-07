"""Media file validation using ffprobe and ffmpeg."""

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from .config import ValidationConfig

logger = structlog.get_logger()


@dataclass
class ValidationResult:
    """Result of validating a media file."""

    file_path: str
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # Metadata extracted during validation
    duration_seconds: float | None = None
    video_bitrate_kbps: int | None = None
    audio_bitrate_kbps: int | None = None
    width: int | None = None
    height: int | None = None
    codec: str | None = None

    # Set to True if validation failed due to timeout (should retry later)
    timed_out: bool = False

    def __bool__(self) -> bool:
        return self.valid


@dataclass
class ProbeResult:
    """Result from ffprobe."""

    success: bool
    duration: float | None = None
    video_streams: list[dict] = field(default_factory=list)
    audio_streams: list[dict] = field(default_factory=list)
    format_info: dict = field(default_factory=dict)
    error: str | None = None


async def run_ffprobe(file_path: str) -> ProbeResult:
    """Run ffprobe on a file and extract metadata."""
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        file_path,
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)

        if process.returncode != 0:
            return ProbeResult(success=False, error=stderr.decode())

        data = json.loads(stdout.decode())

        video_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
        audio_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
        format_info = data.get("format", {})

        duration = None
        if "duration" in format_info:
            duration = float(format_info["duration"])
        elif video_streams and "duration" in video_streams[0]:
            duration = float(video_streams[0]["duration"])

        return ProbeResult(
            success=True,
            duration=duration,
            video_streams=video_streams,
            audio_streams=audio_streams,
            format_info=format_info,
        )

    except asyncio.TimeoutError:
        return ProbeResult(success=False, error="ffprobe timed out")
    except json.JSONDecodeError as e:
        return ProbeResult(success=False, error=f"Failed to parse ffprobe output: {e}")
    except Exception as e:
        return ProbeResult(success=False, error=str(e))


async def run_ffmpeg_decode_test(
    file_path: str,
    start_seconds: float | None = None,
    duration_seconds: float = 30,
    timeout_seconds: int = 30,
) -> tuple[bool, list[str], bool]:
    """
    Run ffmpeg decode test on a portion of the file.
    Returns (success, list of errors, timed_out).
    """
    cmd = ["ffmpeg", "-v", "error"]

    if start_seconds is not None:
        cmd.extend(["-ss", str(start_seconds)])

    cmd.extend(["-i", file_path])

    if duration_seconds:
        cmd.extend(["-t", str(duration_seconds)])

    cmd.extend(["-f", "null", "-"])

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Use configurable timeout (default 30s is better for network mounts)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)

        errors = []
        if stderr:
            # Parse ffmpeg error output
            error_text = stderr.decode()
            # Filter out common non-critical warnings
            for line in error_text.strip().split("\n"):
                if line and not _is_ignorable_ffmpeg_warning(line):
                    errors.append(line)

        success = process.returncode == 0 and len(errors) == 0
        # If ffmpeg failed but no error output, add a generic error
        if not success and not errors:
            errors = [f"ffmpeg exited with code {process.returncode}"]
        return success, errors, False

    except asyncio.TimeoutError:
        return False, ["ffmpeg decode test timed out"], True  # timed_out=True for retry
    except Exception as e:
        return False, [str(e)], False


def _is_ignorable_ffmpeg_warning(line: str) -> bool:
    """Check if an ffmpeg warning line can be safely ignored."""
    ignorable_patterns = [
        r"Last message repeated",
        r"Discarding ID3 tags",
        r"deprecated pixel format",
        r"Consider increasing the -probesize",
    ]
    return any(re.search(pattern, line, re.IGNORECASE) for pattern in ignorable_patterns)


# 3D filename patterns - case insensitive
_3D_FILENAME_PATTERNS = [
    r"[\.\-_ ]3D[\.\-_ ]",       # .3D. or -3D- or _3D_ or " 3D "
    r"[\.\-_ ]SBS[\.\-_ ]",      # Side-by-Side
    r"[\.\-_ ]HSBS[\.\-_ ]",     # Half Side-by-Side
    r"[\.\-_ ]H[\.\-]?SBS[\.\-_ ]",
    r"[\.\-_ ]OU[\.\-_ ]",       # Over-Under
    r"[\.\-_ ]HOU[\.\-_ ]",      # Half Over-Under
    r"[\.\-_ ]H[\.\-]?OU[\.\-_ ]",
    r"[\.\-_ ]TAB[\.\-_ ]",      # Top-and-Bottom
    r"[\.\-_ ]HTAB[\.\-_ ]",     # Half Top-and-Bottom
    r"Side[\.\-_ ]?by[\.\-_ ]?Side",
    r"Half[\.\-_ ]?SBS",
    r"Half[\.\-_ ]?OU",
    r"[\.\-_ ]MVC[\.\-_ ]",      # Multi-View Coding (Blu-ray 3D)
    r"BluRay3D",
    r"Blu[\.\-_ ]?Ray[\.\-_ ]?3D",
    r"3D[\.\-_ ]?BluRay",
]


def detect_3d_from_filename(file_path: str) -> str | None:
    """
    Detect 3D format from filename patterns.
    Returns the detected 3D format string, or None if not 3D.
    """
    filename = Path(file_path).name
    for pattern in _3D_FILENAME_PATTERNS:
        match = re.search(pattern, filename, re.IGNORECASE)
        if match:
            return match.group(0).strip(".-_ ")
    return None


def detect_3d_from_metadata(probe: "ProbeResult") -> str | None:
    """
    Detect 3D format from video stream metadata.
    Returns the detected 3D format string, or None if not 3D.
    """
    if not probe.video_streams:
        return None

    video = probe.video_streams[0]

    # Check for stereo_mode tag (MKV/MP4)
    stereo_mode = video.get("stereo_mode")
    if stereo_mode:
        return f"stereo_mode:{stereo_mode}"

    # Check tags for 3D indicators
    tags = video.get("tags", {})
    for key, value in tags.items():
        key_lower = key.lower()
        if "stereo" in key_lower or "3d" in key_lower:
            return f"{key}:{value}"

    # Check side_data for stereoscopic info
    side_data = video.get("side_data_list", [])
    for sd in side_data:
        sd_type = sd.get("side_data_type", "")
        if "stereo" in sd_type.lower() or "3d" in sd_type.lower():
            return f"side_data:{sd_type}"

    return None


def detect_3d_from_aspect_ratio(probe: "ProbeResult") -> str | None:
    """
    Detect 3D from unusual aspect ratios.
    SBS 3D typically has ~4:1 ratio (e.g., 3840x1080 for "1080p" content)
    OU 3D typically has ~1:1 ratio (e.g., 1920x2160 for "1080p" content)
    """
    if not probe.video_streams:
        return None

    video = probe.video_streams[0]
    width = video.get("width")
    height = video.get("height")

    if not width or not height:
        return None

    ratio = width / height

    # SBS: extremely wide aspect ratio (wider than 3:1 is suspicious)
    # Normal movies are 1.33 (4:3) to 2.76 (ultra panavision)
    if ratio >= 3.2:
        return f"SBS-aspect({width}x{height}, ratio={ratio:.2f})"

    # OU: nearly square or taller than wide for video content
    # (but be careful - some older content is 4:3 = 1.33)
    if ratio <= 1.0 and height >= 1080:
        return f"OU-aspect({width}x{height}, ratio={ratio:.2f})"

    return None


def detect_3d(file_path: str, probe: "ProbeResult") -> str | None:
    """
    Detect if a file is 3D using multiple methods.
    Returns the detection reason string, or None if not 3D.
    """
    # Check filename first (most reliable)
    result = detect_3d_from_filename(file_path)
    if result:
        return f"filename:{result}"

    # Check stream metadata
    result = detect_3d_from_metadata(probe)
    if result:
        return result

    # Check aspect ratio (least reliable, only for obvious cases)
    result = detect_3d_from_aspect_ratio(probe)
    if result:
        return result

    return None


def get_resolution_tier(width: int, height: int) -> str:
    """Determine resolution tier from dimensions."""
    pixels = width * height
    if pixels >= 3840 * 2160 * 0.8:  # 4K
        return "4k"
    elif pixels >= 1920 * 1080 * 0.8:  # 1080p
        return "1080p"
    elif pixels >= 1280 * 720 * 0.8:  # 720p
        return "720p"
    else:
        return "sd"


async def validate_file(file_path: str, config: ValidationConfig) -> ValidationResult:
    """
    Validate a media file.

    Performs multiple checks:
    1. ffprobe metadata extraction and sanity checks
    2. 3D detection (if replace_3d enabled)
    3. Duration sanity check
    4. Bitrate sanity check
    5. Deep scan (sample decode test) if enabled
    6. Full decode if enabled
    """
    result = ValidationResult(file_path=file_path, valid=True)
    log = logger.bind(file=file_path)

    # Step 1: Run ffprobe
    log.debug("Running ffprobe")
    probe = await run_ffprobe(file_path)

    if not probe.success:
        result.valid = False
        result.errors.append(f"ffprobe failed: {probe.error}")
        log.warning("ffprobe failed", error=probe.error)
        return result

    result.duration_seconds = probe.duration

    # Extract video info
    if probe.video_streams:
        video = probe.video_streams[0]
        result.width = video.get("width")
        result.height = video.get("height")
        result.codec = video.get("codec_name")

        if "bit_rate" in video:
            result.video_bitrate_kbps = int(video["bit_rate"]) // 1000
        elif "bit_rate" in probe.format_info:
            # Use overall bitrate as approximation
            result.video_bitrate_kbps = int(probe.format_info["bit_rate"]) // 1000

    # Step 2: 3D detection (if enabled)
    if config.replace_3d:
        detected_3d = detect_3d(file_path, probe)
        if detected_3d:
            result.valid = False
            result.errors.append(f"3D content detected: {detected_3d}")
            log.warning("3D content detected", detection=detected_3d)
            # Return early - no need to do further validation on 3D content
            return result

    # Step 3: Duration sanity check
    if config.check_duration_sanity and probe.duration:
        max_seconds = config.max_duration_hours * 3600
        if probe.duration > max_seconds:
            result.valid = False
            result.errors.append(
                f"Duration {probe.duration / 3600:.1f}h exceeds max {config.max_duration_hours}h"
            )
            log.warning(
                "Duration sanity check failed",
                duration_hours=probe.duration / 3600,
                max_hours=config.max_duration_hours,
            )

        # Also check for suspiciously short duration
        if probe.duration < 60:
            result.warnings.append(f"Duration is very short: {probe.duration:.1f}s")
            log.info("Suspiciously short duration", duration_seconds=probe.duration)

    # Step 4: Bitrate sanity check
    if config.check_bitrate and result.width and result.height and result.video_bitrate_kbps:
        tier = get_resolution_tier(result.width, result.height)
        min_bitrate = {
            "4k": config.min_bitrate_4k,
            "1080p": config.min_bitrate_1080p,
            "720p": config.min_bitrate_720p,
            "sd": 500,
        }.get(tier, 500)

        if result.video_bitrate_kbps < min_bitrate:
            result.warnings.append(
                f"Bitrate {result.video_bitrate_kbps}kbps is low for {tier} "
                f"(minimum: {min_bitrate}kbps)"
            )
            log.info(
                "Low bitrate detected",
                bitrate=result.video_bitrate_kbps,
                tier=tier,
                minimum=min_bitrate,
            )

    # Step 5: Deep scan (sample decode test)
    if config.deep_scan_enabled and probe.duration:
        deep_scan_mode = getattr(config, 'deep_scan_mode', 'partial')
        log.debug("Running deep scan", mode=deep_scan_mode)
        sample_duration = config.sample_duration_seconds
        decode_timeout = getattr(config, 'decode_timeout_seconds', 60)

        # Test beginning (always)
        success, errors, timed_out = await run_ffmpeg_decode_test(
            file_path, start_seconds=0, duration_seconds=sample_duration,
            timeout_seconds=decode_timeout
        )
        if not success:
            result.valid = False
            result.timed_out = result.timed_out or timed_out
            result.errors.extend([f"Decode error (start): {e}" for e in errors])
            log.warning("Decode test failed at start", errors=errors)

        # Test middle and end only in "full" mode (slower but more thorough)
        if deep_scan_mode == "full":
            # Test middle (only if start test passed - fail fast)
            if result.valid and probe.duration > sample_duration * 3:
                middle_start = (probe.duration / 2) - (sample_duration / 2)
                success, errors, timed_out = await run_ffmpeg_decode_test(
                    file_path, start_seconds=middle_start, duration_seconds=sample_duration,
                    timeout_seconds=decode_timeout
                )
                if not success:
                    result.valid = False
                    result.timed_out = result.timed_out or timed_out
                    result.errors.extend([f"Decode error (middle): {e}" for e in errors])
                    log.warning("Decode test failed at middle", errors=errors)

            # Test end (only if still valid - fail fast)
            if result.valid and probe.duration > sample_duration * 2:
                end_start = probe.duration - sample_duration
                success, errors, timed_out = await run_ffmpeg_decode_test(
                    file_path, start_seconds=end_start, duration_seconds=sample_duration,
                    timeout_seconds=decode_timeout
                )
                if not success:
                    result.valid = False
                    result.timed_out = result.timed_out or timed_out
                    result.errors.extend([f"Decode error (end): {e}" for e in errors])
                    log.warning("Decode test failed at end", errors=errors)

    # Step 6: Full decode (if enabled - very slow)
    if config.full_decode_enabled and result.valid:
        log.info("Running full decode test")
        success, errors, timed_out = await run_ffmpeg_decode_test(file_path, duration_seconds=0)
        if not success:
            result.valid = False
            result.timed_out = result.timed_out or timed_out
            result.errors.extend([f"Full decode error: {e}" for e in errors])
            log.warning("Full decode test failed", errors=errors)

    if result.valid:
        log.info("Validation passed")
    else:
        log.warning("Validation failed", errors=result.errors)

    return result
