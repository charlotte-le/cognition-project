"""Main entry point for cognition-project.

Starts the reconciler loop and the web server in one process with asyncio.
One asyncio loop means ticks are serialized, so no locking is needed anywhere.
"""

import asyncio
import logging
import sys

import uvicorn
from cognition.core import db, config, reconciler
from cognition.web import web

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
    
    # Start reconciler loop and web server concurrently
    logger.info("Starting services")
    
    # Start reconciler loop
    reconciler_loop = asyncio.create_task(reconciler_task())
    
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