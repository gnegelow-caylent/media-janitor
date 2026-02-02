"""Main entry point for Media Janitor."""

import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import structlog
import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .config import Config, load_config
from .janitor import Janitor
from .webhook import init_webhook_app


def setup_logging(config: Config):
    """Configure structured logging."""
    log_level = getattr(logging, config.logging.level.upper())

    # Ensure log directory exists
    log_file = Path(config.logging.file)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # Configure structlog
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Configure standard logging
    logging.basicConfig(
        level=log_level,
        format="%(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(config.logging.file),
        ],
    )


async def run_scheduler(janitor: Janitor, config: Config):
    """Run the background scheduler."""
    scheduler = AsyncIOScheduler()

    # Background scan - run every minute
    if config.scanner.enabled:
        if config.scanner.schedule:
            # User specified a cron schedule
            scheduler.add_job(
                janitor.run_background_scan,
                CronTrigger.from_crontab(config.scanner.schedule),
                id="background_scan",
            )
        else:
            # Run continuously (every minute)
            scheduler.add_job(
                janitor.run_background_scan,
                IntervalTrigger(minutes=1),
                id="background_scan",
            )

    # Daily summary email
    if config.email.enabled:
        hour, minute = map(int, config.email.daily_summary_time.split(":"))
        scheduler.add_job(
            janitor.send_daily_summary,
            CronTrigger(hour=hour, minute=minute),
            id="daily_summary",
        )

    scheduler.start()
    return scheduler


async def run_app(config: Config):
    """Run the main application."""
    logger = structlog.get_logger()

    # Log startup configuration summary
    logger.info("=" * 60)
    logger.info("MEDIA JANITOR STARTING")
    logger.info("=" * 60)
    logger.info(
        "Configuration loaded",
        radarr_instances=len(config.radarr),
        sonarr_instances=len(config.sonarr),
        scanner_enabled=config.scanner.enabled,
        scanner_mode=config.scanner.mode,
        files_per_hour=config.scanner.files_per_hour,
        auto_replace=config.actions.auto_replace,
        max_replacements_per_day=config.actions.max_replacements_per_day,
        email_enabled=config.email.enabled,
        webhook_enabled=config.webhook.enabled,
    )

    for r in config.radarr:
        logger.info(f"Radarr configured: {r.name}", url=r.url, path_mappings=len(r.path_mappings))
    for s in config.sonarr:
        logger.info(f"Sonarr configured: {s.name}", url=s.url, path_mappings=len(s.path_mappings))

    # Create janitor (don't initialize yet - do it in background)
    logger.info("Creating janitor...")
    janitor = Janitor(config)

    # Initialize webhook app first so server can start
    app = init_webhook_app(config, janitor)

    # Start scheduler
    scheduler = await run_scheduler(janitor, config)
    logger.info("Background scheduler started")

    # Initialize janitor in background (fetches library, can take a while)
    async def background_init():
        try:
            logger.info("Initializing janitor in background...")
            await janitor.initialize()
            status = janitor.get_status()
            logger.info(
                "Initial scan status",
                files_previously_scanned=status["scanner"]["scanned_count"],
                files_in_queue=status["scanner"]["queue_size"],
                initial_scan_done=status["scanner"]["initial_scan_done"],
            )
            logger.info("Background initialization complete")
        except Exception as e:
            logger.error("Background initialization failed", error=str(e))

    # Start background init
    asyncio.create_task(background_init())

    # Initialize and run webhook server
    if config.webhook.enabled:
        uvicorn_config = uvicorn.Config(
            app,
            host=config.webhook.host,
            port=config.webhook.port,
            log_level="info",
        )
        server = uvicorn.Server(uvicorn_config)

        logger.info(
            "Starting webhook server",
            host=config.webhook.host,
            port=config.webhook.port,
        )
        logger.info("=" * 60)
        logger.info("MEDIA JANITOR READY")
        logger.info("=" * 60)

        try:
            await server.serve()
        finally:
            scheduler.shutdown()
    else:
        # No webhook server, just run scheduler
        logger.info(
            "Starting Media Janitor (no webhook server)",
            scanner_enabled=config.scanner.enabled,
            email_enabled=config.email.enabled,
        )

        try:
            while True:
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            scheduler.shutdown()


def main():
    """Main entry point."""
    # Load config
    config_path = os.environ.get("MEDIA_JANITOR_CONFIG", "/data/config.yaml")

    try:
        config = load_config(config_path)
    except FileNotFoundError:
        print(f"Error: Configuration file not found: {config_path}")
        print("Please create a config.yaml file based on config.example.yaml")
        sys.exit(1)
    except Exception as e:
        print(f"Error loading configuration: {e}")
        sys.exit(1)

    # Setup logging
    setup_logging(config)

    # Run the app
    try:
        asyncio.run(run_app(config))
    except KeyboardInterrupt:
        print("\nShutting down...")


if __name__ == "__main__":
    main()
