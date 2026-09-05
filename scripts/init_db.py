"""Database initialization and user seeding CLI script."""

import asyncio
import logging
import sys
import os

# Add workspace directory to python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.database import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("init_db")


async def main():
    logger.info("Initializing database schema and seeding RBAC users...")
    await init_db()
    logger.info("Database initialization completed successfully.")


if __name__ == "__main__":
    asyncio.run(main())
