"""Apply schema.sql idempotently on lifespan boot under an advisory lock."""

import logging
from pathlib import Path

from api.shared.db import get_pool

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
_ADVISORY_LOCK_KEY = 0xE5_4A_15_00  # arbitrary 32-bit key — pagehub-evals namespace


async def apply_schema() -> None:
    if not _SCHEMA_PATH.exists():
        logger.warning("schema.sql not found at %s — skipping", _SCHEMA_PATH)
        return
    sql = _SCHEMA_PATH.read_text()
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _ADVISORY_LOCK_KEY)
            await conn.execute(sql)
    logger.info("schema applied")
