"""The Little Door Post API.

    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import get_settings
from .db import close_pool, fetch_one, open_pool
from .models import FIELD_SEPARATOR
from .payments import payments_available
from .routers import admin, subscriptions

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("littledoorpost")


# Plain ASCII on purpose — a Windows console in cp1252 turns box-drawing
# characters into mojibake, which is the opposite of helpful.
SETUP_HELP = """
==========================================================================
  The API cannot reach the database.

  1. Open backend/.env
  2. Paste your Neon connection string into DATABASE_URL
       Neon Console > your project > Connection string > Pooled
  3. Run:  python -m app.migrate
  4. Start again:  uvicorn app.main:app --reload --port 8000
==========================================================================
"""


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    try:
        await open_pool()
    except Exception as exc:
        # A stack trace here only buries the one line that helps.
        log.error("%s\n  (%s)\n", SETUP_HELP, exc)
        raise SystemExit(1) from None
    log.info("origins allowed: %s", ", ".join(settings.origins))
    if not settings.admin_token:
        log.warning("ADMIN_TOKEN is not set - /api/admin/* is disabled")
    if not payments_available():
        log.warning(
            "Razorpay keys are not set - sign-ups are saved but checkout stays closed"
        )
    yield
    await close_pool()


app = FastAPI(
    title="The Little Door Post",
    description="Sign-ups for a monthly envelope of letters, stickers, art prints and activity sheets.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Admin-Token"],
)


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """Reshape FastAPI's 422 into {error, fields} so the form can put each
    message under the input it belongs to.

    Whole-model checks (the India address rules in models.py) raise
    ``ValueError("field: message")``; that prefix is unpacked back into a field
    name here.
    """
    fields: dict[str, str] = {}
    for err in exc.errors():
        loc = [str(p) for p in err["loc"] if p not in ("body", "query", "path")]
        message = err["msg"].removeprefix("Value error, ")

        if loc:
            fields.setdefault(loc[-1], message)
            continue

        # A whole-model error: one or more "field: message" pairs joined by
        # FIELD_SEPARATOR. Anything else is shown against the form as a whole.
        for part in message.split(FIELD_SEPARATOR):
            name, sep, text = part.partition(":")
            if sep and text.strip():
                fields.setdefault(name.strip(), text.strip())
            else:
                fields.setdefault("form", part.strip())

    return JSONResponse(
        status_code=422,
        content={"error": "Some details need another look.", "fields": fields},
    )


@app.exception_handler(StarletteHTTPException)
async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """One error shape everywhere: {"error": "..."} — the site reads .error.

    Registered against *Starlette's* HTTPException, not FastAPI's. FastAPI's is
    a subclass, and some errors it raises internally — a body it cannot parse,
    for one — use the Starlette base directly. Handling only the subclass lets
    those slip through to the default handler and answer with {"detail": …},
    which the site does not read.
    """
    detail = exc.detail
    content = detail if isinstance(detail, dict) else {"error": str(detail)}
    return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)


@app.exception_handler(Exception)
async def unhandled_error(_request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"error": "Something went wrong on our side. Try again in a moment."},
    )


@app.get("/api/health")
async def health() -> JSONResponse:
    try:
        await fetch_one("select 1 as ok")
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"ok": False, "db": "down", "error": str(exc)})
    return JSONResponse({"ok": True, "db": "up", "payments": payments_available()})


app.include_router(subscriptions.router)
app.include_router(admin.router)
