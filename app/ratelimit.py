"""A small in-memory rate limiter.

Enough to stop one bored person tapping a button fifty times. It is deliberately
not a distributed one: this runs as a single process, and reaching for Redis to
guard a mail club's reminder form would be more machinery than the problem.

Two consequences worth knowing. The counters live in memory, so a restart or a
free-tier sleep forgets them — which is fine, because the thing being protected
is an inbox, not a bank. And if the API is ever scaled to more than one
instance, each keeps its own count and the effective limit multiplies; swap this
for a shared store at that point.
"""

from __future__ import annotations

import logging
import time
from collections import deque

from fastapi import HTTPException, Request

log = logging.getLogger("littledoorpost.ratelimit")

# key -> timestamps of recent hits, oldest first
_hits: dict[str, deque[float]] = {}
_last_swept = 0.0


def client_ip(request: Request) -> str:
    """The caller's address, as seen from behind Render's proxy.

    `request.client.host` is the proxy itself there, so everyone would share one
    bucket and the first visitor of the hour would lock out the rest.
    X-Forwarded-For is a chain; the original client is the first entry.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _sweep(now: float) -> None:
    """Drop buckets nobody has touched lately, so memory cannot creep."""
    global _last_swept
    if now - _last_swept < 300:
        return
    _last_swept = now
    for key in [k for k, hits in _hits.items() if not hits or now - hits[-1] > 3600]:
        _hits.pop(key, None)


def limit(name: str, *, times: int, seconds: int, message: str):
    """A dependency that allows `times` requests per `seconds`, per caller.

    A sliding window rather than a fixed one: a fixed window lets somebody send
    the whole allowance at 10:59 and the whole allowance again at 11:00.
    """

    async def dependency(request: Request) -> None:
        now = time.monotonic()
        _sweep(now)

        key = f"{name}:{client_ip(request)}"
        hits = _hits.setdefault(key, deque())

        cutoff = now - seconds
        while hits and hits[0] < cutoff:
            hits.popleft()

        if len(hits) >= times:
            retry_after = max(1, int(hits[0] + seconds - now))
            log.info("rate limited %s (%d in %ds)", key, len(hits), seconds)
            raise HTTPException(
                status_code=429,
                detail=message,
                headers={"Retry-After": str(retry_after)},
            )

        hits.append(now)

    return dependency
