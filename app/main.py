"""
CVG Neuron AI Orchestration System — FastAPI Application
Version: 2.0.0 | Clearview Geographic LLC

Entry point for the CVG Neuron REST API.
Runs on port 8808 — the central AI coordination hub for the CVG ecosystem.

Start:
    uvicorn app.main:app --host 0.0.0.0 --port 8808 --reload
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.logger import configure_logging, get_logger
from app.models.response import ErrorResponse
from app.orchestrator.neuron_orchestrator import NeuronOrchestrator
from app.routers import (
    dashboard, info, permissions, predict, process, settings as settings_router,
    status as status_router, train, users,
)

# ── Configure logging first ───────────────────────────────────────────────────
configure_logging(
    level=settings.log_level,
    fmt=settings.log_format,
    log_file=settings.log_file if not settings.is_development else None,
)
log = get_logger("app")


# ── Lifespan context manager ──────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Application startup and shutdown lifecycle.

    Startup:
      - Initialize NeuronOrchestrator (registers default agent pool)
      - Warm up CVG subsystem connections
      - Log startup banner

    Shutdown:
      - Graceful orchestrator shutdown (wait for active tasks)
      - Flush NeuroCache
    """
    # ── STARTUP ───────────────────────────────────────────────────────────────
    log.info(
        "═══════════════════════════════════════════",
    )
    log.info(
        "  CVG Neuron AI Orchestration System",
        version=settings.app_version,
        environment=settings.environment,
    )
    log.info(
        "  Neuron ID: %s | Queen: %s | Hive: %s",
        settings.neuron_id,
        settings.neuron_primary_queen,
        settings.neuron_primary_hive,
    )
    log.info(
        "═══════════════════════════════════════════",
    )

    orchestrator = NeuronOrchestrator()
    await orchestrator.startup()
    app.state.orchestrator = orchestrator
    app.state.started_at = time.time()

    log.info(
        "CVG Neuron ready",
        host=settings.host,
        port=settings.port,
        docs=f"http://{settings.host}:{settings.port}/docs",
    )

    yield  # ← Application runs here

    # ── SHUTDOWN ──────────────────────────────────────────────────────────────
    log.info("CVG Neuron shutting down...")
    await orchestrator.shutdown()
    log.info("CVG Neuron offline. Goodbye.")


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="CVG Neuron AI Orchestration System",
    description=(
        "Central AI orchestration engine for the CVG ecosystem. "
        "Self-optimizing, multi-agent, cognitive processing platform by "
        "Clearview Geographic LLC.\n\n"
        "**Neuron ID:** CVG-NEURON-001 | **Employee ID:** CVG-AI-001 | "
        "**Primary Queen:** CVG-QUEEN-13\n\n"
        "## Cognitive Processing Levels\n"
        "- **basic** — Single-agent direct response\n"
        "- **advanced** — Multi-agent parallel synthesis\n"
        "- **neural** — Deep processing with memory (NeuroCache + COMB)\n"
        "- **autonomous** — Self-directed execution with learning loop"
    ),
    version=settings.app_version,
    docs_url="/docs" if not settings.is_production else None,
    redoc_url="/redoc" if not settings.is_production else None,
    openapi_url="/openapi.json" if not settings.is_production else None,
    lifespan=lifespan,
    contact={
        "name": "Clearview Geographic LLC",
        "url": "https://cleargeo.tech",
        "email": "admin@cleargeo.tech",
    },
    license_info={
        "name": "Proprietary — Clearview Geographic LLC",
    },
)


# ── Middleware ────────────────────────────────────────────────────────────────

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Gzip compression for large responses
app.add_middleware(GZipMiddleware, minimum_size=1000)


# ── Request timing middleware ──────────────────────────────────────────────────

@app.middleware("http")
async def add_timing_header(request: Request, call_next):
    """Add X-Process-Time header to all responses."""
    t0 = time.monotonic()
    response = await call_next(request)
    duration_ms = int((time.monotonic() - t0) * 1000)
    response.headers["X-Process-Time-Ms"] = str(duration_ms)
    response.headers["X-Neuron-ID"] = settings.neuron_id
    return response


# ── Exception handlers ────────────────────────────────────────────────────────

@app.exception_handler(404)
async def not_found_handler(request: Request, exc: Any) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content=ErrorResponse(
            error="Not Found",
            detail=f"Path {request.url.path} not found",
        ).model_dump(mode="json"),
    )


@app.exception_handler(500)
async def internal_error_handler(request: Request, exc: Any) -> JSONResponse:
    log.error("Unhandled exception", path=request.url.path, error=str(exc))
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorResponse(
            error="Internal Server Error",
            detail="An unexpected error occurred. Check logs for details.",
        ).model_dump(mode="json"),
    )


# ── Root endpoint ─────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def root(request: Request):
    """Root redirect — returns quick identity info."""
    uptime = time.time() - getattr(request.app.state, "started_at", time.time())
    return {
        "system": "CVG Neuron AI Orchestration System",
        "neuron_id": settings.neuron_id,
        "version": settings.app_version,
        "status": "online",
        "uptime_seconds": round(uptime),
        "docs": f"http://{settings.host}:{settings.port}/docs",
        "health": f"http://{settings.host}:{settings.port}/api/status",
        "process": f"http://{settings.host}:{settings.port}/api/process",
    }


# ── Register routers ──────────────────────────────────────────────────────────

app.include_router(process.router)
app.include_router(status_router.router)
app.include_router(dashboard.router)
app.include_router(predict.router)
app.include_router(train.router)
app.include_router(settings_router.router)
app.include_router(users.router)
app.include_router(permissions.router)
app.include_router(info.router)


# ── Dev server entry point ────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.is_development,
        workers=1 if settings.is_development else settings.workers,
        log_level=settings.log_level.lower(),
        access_log=settings.is_development,
    )
