"""FastAPI application entry point for pagehub-evals."""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.config import get_settings
from api.shared.db import close_pool, init_pool
from api.shared.schema import apply_schema
from api.shared.twin_middleware import TwinMiddleware

from api.collections.routes import router as collections_router
from api.environments.routes import router as environments_router
from api.evaluations.routes import router as evaluations_router
from api.events.routes import router as events_router
from api.requests.routes import router as requests_router
from api.runs.routes import router as runs_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Sentry init at module load — bypasses settings so it captures
# errors that happen during settings loading. Requires SENTRY_DSN +
# ENVIRONMENT + GIT_SHA + SENTRY_TRACES_SAMPLE_RATE; if any is missing,
# Sentry stays off (no defaults — see api/config.py policy).
_sentry_dsn = os.getenv("SENTRY_DSN", "").strip() or None
if _sentry_dsn:
    _env = os.getenv("ENVIRONMENT", "").strip() or None
    _git_sha = os.getenv("GIT_SHA", "").strip() or None
    _rate = os.getenv("SENTRY_TRACES_SAMPLE_RATE", "").strip() or None
    if not (_env and _git_sha and _rate):
        logger.warning(
            "SENTRY_DSN set but ENVIRONMENT/GIT_SHA/SENTRY_TRACES_SAMPLE_RATE missing — Sentry disabled"
        )
    else:
        try:
            import sentry_sdk
            from sentry_sdk.integrations.fastapi import FastApiIntegration

            sentry_sdk.init(
                dsn=_sentry_dsn,
                environment=_env,
                release=_git_sha,
                traces_sample_rate=float(_rate),
                integrations=[FastApiIntegration()],
            )
            logger.info("Sentry initialized (env=%s)", _env)
        except ImportError:
            logger.warning("sentry-sdk not installed, skipping Sentry init")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("Starting pagehub-evals API (env=%s)", settings.env)
    # In env=test we skip pool init so unit tests don't need a live DB.
    # Integration tests override the lifespan (or set env != "test").
    skip_db = settings.env == "test"
    if not skip_db:
        await init_pool(settings.database_url)
        await apply_schema()
    try:
        yield
    finally:
        if not skip_db:
            await close_pool()
        logger.info("Shutting down pagehub-evals API")


app = FastAPI(
    title="pagehub-evals",
    description="Ground-truth gate for LLM coding harnesses, with inspiration from Postman.",
    version="0.1.0",
    lifespan=lifespan,
)

_settings = get_settings()

# CORS — APP_URL is the canonical origin (Cloudflare Pages site).
_allowed_origins = [_settings.app_url] if _settings.app_url else []
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Twin override (X-Twin-* dev-only).
app.add_middleware(TwinMiddleware)

app.include_router(environments_router, tags=["Environments"])
app.include_router(requests_router, tags=["Requests"])
app.include_router(evaluations_router, tags=["Evaluations"])
app.include_router(collections_router, tags=["Collections"])
app.include_router(runs_router, tags=["Runs"])
app.include_router(events_router, tags=["Events"])


@app.get("/")
async def root():
    return {"name": "pagehub-evals", "version": "0.1.0", "docs": "/docs"}


@app.get("/health")
async def health():
    settings = get_settings()
    return {
        "status": "ok",
        "version": "0.1.0",
        "env": settings.env,
        "git_sha": settings.git_sha,
    }


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint scraped by platform/eyes."""
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
    from fastapi import Response

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
