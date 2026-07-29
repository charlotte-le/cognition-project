"""Main entry point for cognition-project.

Starts the reconciler loop and the web server in one process with asyncio.
One asyncio loop means ticks are serialized, so no locking is needed anywhere.
"""

import asyncio
import logging
import os
import sys

import uvicorn
import db
import config
import reconciler
import web

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def reconciler_task():
    """Run the reconciler loop."""
    logger.info("Starting reconciler loop")
    
    while True:
        try:
            # Check if webhook requested immediate tick
            if web.should_tick_immediately():
                logger.info("Webhook received - running immediate tick")
            
            await reconciler.tick()
        except Exception as e:
            logger.error(f"Error in reconciler tick: {e}")
        
        # Wait for next tick
        await asyncio.sleep(config.TICK_SECONDS)


async def main():
    """Main entry point."""
    # Initialize database
    logger.info("Initializing database")
    db.init_db()
    
    # Check if running in demo mode (no credentials or demo credentials)
    demo_mode = (
        not os.environ.get("DEVIN_API_KEY") or 
        not os.environ.get("DEVIN_ORG_ID") or
        os.environ.get("DEVIN_API_KEY") == "demo-key" or
        os.environ.get("DEVIN_ORG_ID") == "demo-org"
    )
    
    if demo_mode:
        logger.info("Running in DEMO mode - no live API calls")
        # Seed demo data if database is empty
        tasks = db.list_tasks()
        if not tasks:
            logger.info("Seeding demo data")
            import seed_demo
            seed_demo.seed_demo()
    else:
        logger.info("Running in LIVE mode")
        # In live mode, start with clean database - no demo data
        logger.info("Live mode - starting with clean database")
    
    # Start reconciler loop and web server concurrently
    logger.info("Starting services")
    
    # Only start reconciler in live mode (with real credentials)
    if not demo_mode:
        reconciler_loop = asyncio.create_task(reconciler_task())
    else:
        reconciler_loop = None
        logger.info("Demo mode - reconciler loop not started")
    
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
    if reconciler_loop:
        reconciler_loop.cancel()
        try:
            await reconciler_loop
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down")
        sys.exit(0)