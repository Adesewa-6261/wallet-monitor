"""
lib/db.py

Postgres access.

Serverless functions are short-lived and can run many at once, so a pool per
invocation would exhaust the database's connection limit quickly. asyncpg's pool
is created once per warm instance and reused, with a low ceiling to stay within
Neon's free tier.
"""

from typing import Any, Optional

import asyncpg

from .core.config import config

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            config.database_url,
            min_size=1,
            max_size=5,
            # Neon closes idle connections; recycling avoids using a dead one.
            max_inactive_connection_lifetime=60,
            statement_cache_size=0,  # required when going through a pooler
        )
    return _pool


async def fetch(query: str, *args: Any) -> list[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(query, *args)


async def fetchrow(query: str, *args: Any) -> Optional[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(query, *args)


async def execute(query: str, *args: Any) -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.execute(query, *args)


async def executemany(query: str, args: list[tuple]) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(query, args)
