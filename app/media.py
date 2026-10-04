"""Photographs: the gallery, and each edition's envelope.

Uploaded from the admin panel into Postgres, published from there into the
site's own repository (publish.py), and then emptied from the database — the
row stays, its bytes go. /api/media/{id} serves the bytes while they are here,
and afterwards sends the browser to the site's copy.

An upload is trusted for nothing. Its type is read from the file's own first
bytes — the browser's Content-Type is ignored — and only JPEG, PNG and WebP
get through. No SVG, which can carry script; nothing else at all.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from .db import fetch_all, fetch_one

# The panel shrinks photos before sending them, so a real upload is a few
# hundred kilobytes. This is the ceiling for one that somehow was not.
MAX_BYTES = 5 * 1024 * 1024

# Enough for years of a monthly club, and a floor under how much of the
# database a gallery can take.
MAX_GALLERY = 300

KINDS = ("gallery", "envelope", "site")

# The three places on the site whose picture is set from the panel.
SLOTS = ("hero", "meet", "subscribe")


def sniff(data: bytes) -> str | None:
    """The image type from the file's first bytes, or None."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def url(media_id) -> str | None:
    """Where the site loads a photo from. A path, not a full URL: the site puts
    the API's origin in front of it."""
    return f"/api/media/{media_id}" if media_id else None


def parse_id(value: str, missing: str = "No such photo.") -> uuid.UUID:
    """An id from a URL, as a UUID — anything else is simply not found, rather
    than reaching the database and coming back as a 500."""
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise HTTPException(status_code=404, detail=missing) from None


async def read_upload(request: Request) -> bytes:
    """The request body, refused as soon as it passes MAX_BYTES rather than
    after it has all been held in memory."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="That photo is over 5 MB.")

    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BYTES:
            raise HTTPException(status_code=413, detail="That photo is over 5 MB.")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise HTTPException(status_code=422, detail="No photo was sent.")
    return data


async def save(kind: str, data: bytes, caption: str | None) -> dict:
    content_type = sniff(data)
    if content_type is None:
        raise HTTPException(
            status_code=415, detail="Only JPEG, PNG and WebP photos can be uploaded."
        )
    if kind == "gallery":
        count = await fetch_one("select count(*)::int as n from media where kind = 'gallery'")
        if count["n"] >= MAX_GALLERY:
            raise HTTPException(
                status_code=409,
                detail=f"The gallery is full ({MAX_GALLERY} photos). Delete some first.",
            )
    return await fetch_one(
        """
        insert into media (kind, content_type, data, byte_size, caption)
        values (%s, %s, %s, %s, %s)
        returning id, kind, content_type, byte_size, caption, created_at
        """,
        (kind, content_type, data, len(data), caption),
    )


async def gallery() -> list[dict]:
    """The gallery, newest first. Never `select *` here — that would pull every
    photograph's bytes out of the database to list their captions."""
    rows = await fetch_all(
        "select id, caption, byte_size, created_at from media "
        "where kind = 'gallery' order by created_at desc"
    )
    return [
        {
            "id": str(r["id"]),
            "url": url(r["id"]),
            "caption": r["caption"] or "",
            "byte_size": r["byte_size"],
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]


async def site_images() -> dict[str, str | None]:
    """The panel-set picture for each slot, or None where the built-in one
    stands — plus `meetText`, the colour of the words over Meet Iris:
    "light" (white, the default) or "dark" (black)."""
    rows = await fetch_all(
        "select slot, media_id, text_tone, pos_x, pos_y, zoom from site_images"
    )
    chosen = {r["slot"]: url(r["media_id"]) for r in rows}
    tones = {r["slot"]: r["text_tone"] for r in rows}
    # How each picture sits in its frame — see site_images in schema.sql.
    frames = {
        r["slot"]: {"x": r["pos_x"], "y": r["pos_y"], "zoom": r["zoom"]} for r in rows
    }
    return {
        **{slot: chosen.get(slot) for slot in SLOTS},
        "meetText": tones.get("meet") or "light",
        "frames": {slot: frames.get(slot) or {"x": 50, "y": 50, "zoom": 100} for slot in SLOTS},
    }


router = APIRouter(prefix="/api", tags=["media"])


@router.get("/media/{media_id}", include_in_schema=False)
async def serve(media_id: str) -> Response:
    row = await fetch_one(
        "select data, content_type from media where id = %s", (parse_id(media_id),)
    )
    if not row:
        raise HTTPException(status_code=404, detail="No such photo.")
    if row["data"] is None:
        # Published and emptied from the database: the site has it now.
        from . import publish  # here, not at the top: publish imports this module
        return RedirectResponse(publish.site_url(media_id, row["content_type"]), status_code=302)
    return Response(
        content=bytes(row["data"]),
        media_type=row["content_type"],
        headers={
            # A photo never changes under its id — a new upload is a new id —
            # so browsers and Vercel's edge can keep it for good, and a
            # sleeping API is only woken for photos nobody has seen yet.
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": f'"{media_id}"',
        },
    )
