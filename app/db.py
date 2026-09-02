"""Neon connection pool.

psycopg 3 talks plain Postgres to Neon, so the pooled connection string from the
Neon console works as-is — ``?sslmode=require`` and all.

**Why the sync pool behind async functions.** psycopg's *async* mode cannot run
on Windows' default ProactorEventLoop, and which loop you get depends on how
uvicorn was started: ``--reload`` yields a selector loop and works, plain
``uvicorn app.main:app`` yields a proactor loop and dies with "Psycopg cannot
use the 'ProactorEventLoop'". Rather than depend on that, the pool here is the
synchronous one and every call is handed to a worker thread. It behaves
identically on Windows and Linux, under --reload or not, and the callers still
just ``await fetch_one(...)``.

At this scale the thread hop costs nothing — the queries are short and the pool
is capped well below Starlette's threadpool.
"""

from __future__ import annotations

import logging
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from starlette.concurrency import run_in_threadpool

from .config import get_settings

log = logging.getLogger("littledoorpost.db")

_pool: ConnectionPool | None = None


def _open_pool() -> None:
    global _pool
    settings = get_settings()
    if not settings.dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy backend/.env.example to backend/.env "
            "and paste your Neon connection string."
        )
    _pool = ConnectionPool(
        settings.dsn,
        min_size=1,
        max_size=10,
        open=False,
        kwargs={"row_factory": dict_row},
    )
    _pool.open(wait=True, timeout=15)
    log.info("connected to Neon")


async def open_pool() -> None:
    await run_in_threadpool(_open_pool)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        pool_to_close, _pool = _pool, None
        await run_in_threadpool(pool_to_close.close)


def pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("the database pool is not open")
    return _pool


def _run(sql: str, params: Any, mode: str):
    """One statement in its own transaction — psycopg commits when the
    ``connection()`` block exits cleanly, and rolls back if it raises."""
    with pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if mode == "all":
                return cur.fetchall()
            if mode == "one":
                return cur.fetchone() if cur.description else None
            return None


async def fetch_all(sql: str, params: Any = None) -> list[dict]:
    return await run_in_threadpool(_run, sql, params, "all")


async def fetch_one(sql: str, params: Any = None) -> dict | None:
    return await run_in_threadpool(_run, sql, params, "one")


async def execute(sql: str, params: Any = None) -> None:
    await run_in_threadpool(_run, sql, params, "none")
