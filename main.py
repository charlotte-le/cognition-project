"""Main entry point for cognition-project.

Starts three things in one process on one asyncio loop: the reconciler loop, the
scheduled scan, and the web server. Ticks are serialized on that loop, so no
locking is needed anywhere; the two blocking jobs (Bandit, the GitHub API) are
pushed to threads so neither starves the other or the web server.

Triggers, in the order they matter:
  - the scheduled scan, which is what puts new findings into the ledger
  - a GitHub webhook delivery, which wakes the reconciler immediately
  - the TICK_SECONDS interval, which is the floor under both
"""

import asyncio
import logging
import sys

import uvicorn
from cognition.core import db, config, reconciler
from cognition.verification import scanner
from cognition.web import web

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def reconciler_task():
    """Run the reconciler loop, waking early when a webhook arrives."""
    logger.info("Starting reconciler loop (tick every %ss)", config.TICK_SECONDS)

    webhook = web.webhook_signal()

    while True:
        # Cleared before the tick, not after: a delivery that lands while the
        # tick is running leaves the Event set, so the wait below returns
        # immediately and the work it signalled is picked up on the next pass
        # instead of being swallowed.
        webhook.clear()

        try:
            await reconciler.tick()
        except Exception as e:
            logger.error(f"Error in reconciler tick: {e}")

        # Sleep until the interval expires or a webhook arrives, whichever comes
        # first. The interval is the floor: if the webhook never fires, or fires
        # and is lost, the loop still converges - it just converges slower.
        try:
            await asyncio.wait_for(webhook.wait(), timeout=config.TICK_SECONDS)
            logger.info("Webhook received - ticking immediately")
        except asyncio.TimeoutError:
            pass


async def scanner_task():
    """Re-scan the target repo on a schedule.

    This is the event that starts the pipeline. It runs once at startup and then
    every SCAN_INTERVAL_MINUTES, in a thread - a Bandit run over superset/ takes
    long enough that doing it on the event loop would stall every tick and every
    request for its duration.
    """
    if config.SCAN_INTERVAL_MINUTES <= 0:
        logger.info("Scheduled scan disabled (SCAN_INTERVAL_MINUTES=0); use POST /scan")
        return

    logger.info("Starting scheduled scan (every %sm)", config.SCAN_INTERVAL_MINUTES)

    while True:
        try:
            count = await asyncio.to_thread(scanner.run_scan)
            logger.info(f"Scheduled scan complete: {count} findings")
        except Exception as e:
            # Never fatal. A scan that fails is a stalled trigger, not a stopped
            # system: the reconciler still drives everything already in the
            # ledger, and run_scan has recorded the failure for the status page.
            logger.error(f"Scheduled scan failed: {e}")

        await asyncio.sleep(config.SCAN_INTERVAL_MINUTES * 60)


async def main():
    """Main entry point."""
    # Initialize database
    logger.info("Initializing database")
    db.init_db()
    
    # Start reconciler loop and web server concurrently
    logger.info("Starting services")
    
    # Start the reconciler loop and the scheduled scan
    background = [
        asyncio.create_task(reconciler_task()),
        asyncio.create_task(scanner_task()),
    ]

    # Configure uvicorn
    uvicorn_config = uvicorn.Config(
        app=web.app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )
    server = uvicorn.Server(uvicorn_config)

    # Run web server (this blocks)
    await server.serve()

    # Clean shutdown
    for task in background:
        task.cancel()
    await asyncio.gather(*background, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down")
        sys.exit(0)