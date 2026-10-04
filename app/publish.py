"""Publishing the site's content to the frontend repository on GitHub.

Why this exists. The landing page used to ask this API for its photographs, its
prices and what is in the envelope every time it loaded. On Render's and Neon's
free tiers that means waking a sleeping server and a sleeping database before a
single picture can appear. Now the page carries all of it itself: the admin
panel writes it into the frontend's own files (react-app/src/content/*.js and
react-app/public/media/*) through the GitHub API, and the host rebuilds the site
from that commit. Nothing on the page waits for this server any more — only the
sign-up form does, and it is warmed up while the reader browses.

The database stays the place the panel edits and the place the charge is worked
out from. This module copies what the site shows out of it:

    src/content/prices.js    the rate card
    src/content/envelope.js  the edition on sale, and what is in its envelope
    src/content/images.js    hero / Meet Iris / sign-up pictures, and the gallery
    src/content/design.js    the colour choices
    public/media/<id>.<ext>  every photograph those files point at

One publish is ONE commit (the Git Data API: blobs, a tree, a commit, a ref
update), however many files changed — otherwise ten gallery uploads would start
ten site builds. Photographs are named by their id and never change, so one
already in the repository is left alone; one nothing points at any more is
deleted. Only files named like that are ever deleted, so anything put in
public/media by hand is safe.

Saves in the panel call schedule(), which waits a few seconds for more changes
and then publishes once. publish() does it right now. The outcome is kept in
`status` for the panel to show.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

from starlette.concurrency import run_in_threadpool

from . import cycles, design, media, plans
from .config import get_settings
from .db import fetch_all

log = logging.getLogger("littledoorpost.publish")

API = "https://api.github.com"
CONTENT_DIR = "src/content"
MEDIA_DIR = "public/media"
# Only files we named ourselves are ever deleted: <uuid>.<ext>.
OWN_MEDIA = re.compile(rf"^{re.escape(MEDIA_DIR)}/[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-"
                       r"[0-9a-f]{4}-[0-9a-f]{12}\.(webp|jpg|png)$")
EXT = {"image/webp": "webp", "image/jpeg": "jpg", "image/png": "png"}

# How long to wait for further changes before publishing. Ten gallery photos
# uploaded one after another are one publish, not ten.
DEBOUNCE_SECONDS = 10

HEADER = (
    "/* Written by the admin panel's publish step - do not edit by hand; the next\n"
    " * publish would overwrite it. Change this in the panel instead. */\n"
)


class PublishError(Exception):
    """Something the panel can show as a sentence."""


# The latest outcome, for the panel. In memory: it answers "is the site up to
# date right now", and a restart simply resets it to idle.
status: dict = {
    "state": "idle",      # idle | publishing | ok | error
    "pending": False,     # a change is waiting for its publish
    "at": None,
    "message": None,
    "error": None,
    "commit": None,
    "url": None,
    "files": 0,
}

_lock = asyncio.Lock()
_timer: asyncio.Task | None = None


def configured() -> bool:
    s = get_settings()
    return bool(s.github_token and s.github_repo)


def describe() -> dict:
    s = get_settings()
    return {
        "configured": configured(),
        "repo": s.github_repo if configured() else None,
        "branch": s.github_branch,
        **status,
    }


# ── GitHub, over HTTPS (the standard library, like mail.py) ─────────────────

def _gh(method: str, path: str, body: dict | None = None) -> dict:
    s = get_settings()
    request = urllib.request.Request(
        f"{API}/repos/{s.github_repo}{path}",
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {s.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "little-door-post-admin",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("message", detail)
        except ValueError:
            pass
        hint = ""
        if exc.code == 401:
            hint = " - GITHUB_TOKEN is wrong or has expired"
        elif exc.code in (403, 404):
            hint = (f" - the token needs Contents: read and write on {s.github_repo}, "
                    f"and the branch '{s.github_branch}' has to exist")
        raise PublishError(f"GitHub answered {exc.code}: {detail[:200]}{hint}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PublishError(f"GitHub could not be reached: {exc}") from None


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _inspect() -> dict:
    """Where the branch is now, and every file in it (path -> blob sha)."""
    s = get_settings()
    ref = _gh("GET", f"/git/ref/heads/{s.github_branch}")
    commit_sha = ref["object"]["sha"]
    tree_sha = _gh("GET", f"/git/commits/{commit_sha}")["tree"]["sha"]
    tree = _gh("GET", f"/git/trees/{tree_sha}?recursive=1")
    if tree.get("truncated"):
        raise PublishError("The repository is too large to list through the GitHub API.")
    files = {e["path"]: e["sha"] for e in tree["tree"] if e["type"] == "blob"}
    return {"commit": commit_sha, "tree": tree_sha, "files": files}


def _commit(repo: dict, texts: dict[str, str], photos: dict[str, bytes], keep: set[str]) -> dict | None:
    """One commit with every change, or None if nothing differs."""
    s = get_settings()
    existing = repo["files"]
    entries: list[dict] = []
    changed: list[str] = []

    for path, text in texts.items():
        if existing.get(path) != _blob_sha(text.encode("utf-8")):
            entries.append({"path": path, "mode": "100644", "type": "blob", "content": text})
            changed.append(path)

    for path, data in photos.items():
        blob = _gh("POST", "/git/blobs", {"content": base64.b64encode(data).decode("ascii"),
                                          "encoding": "base64"})
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        changed.append(path)

    for path in existing:
        if OWN_MEDIA.match(path) and path not in keep:
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            changed.append(path)

    if not entries:
        return None

    tree = _gh("POST", "/git/trees", {"base_tree": repo["tree"], "tree": entries})
    added = sum(1 for p in changed if p.startswith(MEDIA_DIR) and p in photos)
    message = "Site content: published from the admin panel"
    if added:
        message += f" ({added} new photo{'s' if added != 1 else ''})"
    commit = _gh("POST", "/git/commits", {
        "message": message, "tree": tree["sha"], "parents": [repo["commit"]],
    })
    _gh("PATCH", f"/git/refs/heads/{s.github_branch}", {"sha": commit["sha"]})
    return {"sha": commit["sha"], "files": len(changed)}


# ── what the site shows, out of the database ────────────────────────────────

def _id_of(url: str | None) -> str | None:
    """'/api/media/<id>' -> '<id>'."""
    return url.rsplit("/", 1)[-1] if url else None


async def _snapshot() -> tuple[dict[str, str], dict[str, str]]:
    """The four content files as text, and each photograph's id -> repo path."""
    edition = await cycles.current()
    items = await cycles.contents_for(edition)
    catalogue = await plans.catalogue()
    gallery = await media.gallery()
    site = await media.site_images()
    theme = await design.theme()

    wanted = {g["id"] for g in gallery}
    wanted |= {i for i in (_id_of(site[s]) for s in media.SLOTS) if i}
    envelope_id = str(edition["envelope_media_id"]) if edition.get("envelope_media_id") else None
    if envelope_id:
        wanted.add(envelope_id)

    rows = await fetch_all(
        "select id, content_type from media where id = any(%s)",
        ([uuid.UUID(i) for i in wanted],),
    ) if wanted else []
    paths = {str(r["id"]): f"{MEDIA_DIR}/{r['id']}.{EXT[r['content_type']]}" for r in rows}

    def src(media_id: str | None) -> str | None:
        # Public path, as the browser asks for it: public/ is the site's root.
        return "/" + paths[media_id].removeprefix("public/") if media_id in paths else None

    def js(data: dict) -> str:
        return HEADER + "export default " + json.dumps(data, indent=2, ensure_ascii=False) + ";\n"

    texts = {
        f"{CONTENT_DIR}/prices.js": js({"published": True, "plans": catalogue}),
        f"{CONTENT_DIR}/envelope.js": js({
            "published": True,
            "edition": cycles.describe(edition),
            "items": [{"title": i["title"], "detail": i.get("detail", "")} for i in items],
            "image": src(envelope_id),
        }),
        f"{CONTENT_DIR}/images.js": js({
            "published": True,
            **{slot: src(_id_of(site[slot])) for slot in media.SLOTS},
            "meetText": site["meetText"],
            "frames": site["frames"],
            "gallery": [{"src": src(g["id"]), "caption": g["caption"]}
                        for g in gallery if src(g["id"])],
        }),
        f"{CONTENT_DIR}/design.js": js({"published": True, "theme": theme}),
    }
    return texts, paths


async def _publish() -> dict:
    texts, paths = await _snapshot()
    repo = await run_in_threadpool(_inspect)

    # Only photographs the repository does not hold yet are read out of the
    # database — their bytes are the heavy part.
    missing = {i: p for i, p in paths.items() if p not in repo["files"]}
    photos: dict[str, bytes] = {}
    if missing:
        rows = await fetch_all(
            "select id, data from media where id = any(%s)",
            ([uuid.UUID(i) for i in missing],),
        )
        photos = {missing[str(r["id"])]: bytes(r["data"]) for r in rows}

    return await run_in_threadpool(_commit, repo, texts, photos, set(paths.values())) or {}


# ── when ────────────────────────────────────────────────────────────────────

def _record(**fields) -> None:
    status.update(at=datetime.now(timezone.utc).isoformat(timespec="seconds"), **fields)


async def publish() -> dict:
    """Publish now. Returns the new status; raises PublishError on failure
    (which is also recorded, so the panel shows it on its next look)."""
    global _timer
    if not configured():
        raise PublishError(
            "GitHub is not set up: add GITHUB_TOKEN to the backend's environment "
            "and the site will update from the panel."
        )
    if _timer is not None:  # publishing now supersedes waiting
        _timer.cancel()
        _timer = None

    async with _lock:
        status.update(state="publishing", pending=False, error=None)
        try:
            result = await _publish()
        except PublishError as exc:
            log.error("publish failed: %s", exc)
            _record(state="error", error=str(exc), message=None)
            raise
        except Exception as exc:  # noqa: BLE001 - whatever it was, the panel should say so
            log.exception("publish failed")
            _record(state="error", error=f"{type(exc).__name__}: {exc}", message=None)
            raise PublishError(f"{type(exc).__name__}: {exc}") from None

    if result:
        s = get_settings()
        log.info("published %d files to %s", result["files"], s.github_repo)
        _record(state="ok", commit=result["sha"][:7], files=result["files"],
                url=f"https://github.com/{s.github_repo}/commit/{result['sha']}",
                message="Sent to GitHub. The site rebuilds from it - live in about a minute.")
    else:
        _record(state="ok", message="The site already has all of this - nothing to send.", files=0)
    return describe()


async def _after_delay() -> None:
    global _timer
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return
    _timer = None
    try:
        await publish()
    except PublishError:
        pass  # recorded in `status` for the panel


def schedule() -> None:
    """A change was saved: publish once things go quiet. Quietly does nothing
    when GitHub is not set up — saving in the panel must never fail for that."""
    global _timer
    if not configured():
        return
    status["pending"] = True
    if _timer is not None:
        _timer.cancel()
    _timer = asyncio.get_running_loop().create_task(_after_delay())
