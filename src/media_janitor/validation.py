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
) -> tuple[bool, list[str]]:
    """
    Run ffmpeg decode test on a portion of the file.
    Returns (success, list of errors).
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
        # Timeout based on duration - should complete faster than real-time
        timeout = max(120, duration_seconds * 2)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)

        errors = []
        if stderr:
            # Parse ffmpeg error output
            error_text = stderr.decode()
            # Filter out common non-critical warnings
            for line in error_text.strip().split("\n"):
                if line and not _is_ignorable_ffmpeg_warning(line):
                    errors.append(line)

        return process.returncode == 0 and len(errors) == 0, errors

    except asyncio.TimeoutError:
        return False, ["ffmpeg decode test timed out"]
    except Exception as e:
        return False, [str(e)]


def _is_ignorable_ffmpeg_warning(line: str) -> bool:
    """Check if an ffmpeg warning line can be safely ignored."""
    ignorable_patterns = [
        r"Last message repeated",
        r"Discarding ID3 tags",
        r"deprecated pixel format",
        r"Consider increasing the -probesize",
    ]
    return any(re.search(pattern, line, re.IGNORECASE) for pattern in ignorable_patterns)


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
    2. Duration sanity check
    3. Bitrate sanity check
    4. Deep scan (sample decode test) if enabled
    5. Full decode if enabled
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

    # Step 2: Duration sanity check
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

    # Step 3: Bitrate sanity check
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

    # Step 4: Deep scan (sample decode test)
    if config.deep_scan_enabled and probe.duration:
        log.debug("Running deep scan")
        sample_duration = config.sample_duration_seconds

        # Test beginning
        success, errors = await run_ffmpeg_decode_test(
            file_path, start_seconds=0, duration_seconds=sample_duration
        )
        if not success:
            result.valid = False
            result.errors.extend([f"Decode error (start): {e}" for e in errors])
            log.warning("Decode test failed at start", errors=errors)

        # Test middle
        if probe.duration > sample_duration * 3:
            middle_start = (probe.duration / 2) - (sample_duration / 2)
            success, errors = await run_ffmpeg_decode_test(
                file_path, start_seconds=middle_start, duration_seconds=sample_duration
            )
            if not success:
                result.valid = False
                result.errors.extend([f"Decode error (middle): {e}" for e in errors])
                log.warning("Decode test failed at middle", errors=errors)

        # Test end
        if probe.duration > sample_duration * 2:
            end_start = probe.duration - sample_duration
            success, errors = await run_ffmpeg_decode_test(
                file_path, start_seconds=end_start, duration_seconds=sample_duration
            )
            if not success:
                result.valid = False
                result.errors.extend([f"Decode error (end): {e}" for e in errors])
                log.warning("Decode test failed at end", errors=errors)

    # Step 5: Full decode (if enabled - very slow)
    if config.full_decode_enabled and result.valid:
        log.info("Running full decode test")
        success, errors = await run_ffmpeg_decode_test(file_path, duration_seconds=0)
        if not success:
            result.valid = False
            result.errors.extend([f"Full decode error: {e}" for e in errors])
            log.warning("Full decode test failed", errors=errors)

    if result.valid:
        log.info("Validation passed")
    else:
        log.warning("Validation failed", errors=result.errors)

    return result
