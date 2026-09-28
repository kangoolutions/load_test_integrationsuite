"""FastAPI-App: REST-API, Live-Stream (SSE) und statische Oberfläche."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .auth import AuthError, TokenProvider
from .config import get_settings
from .files import DirCache, list_folder, preview
from .models import AuthTestRequest, ContinueRunRequest, LiveAdjust, PreviewRequest, StartRunRequest
from .runner import RunConflict, RunManager
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

settings = get_settings()
dircache = DirCache(settings.data_dir)


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = Store(settings.db_path)
    await store.open()
    app.state.store = store
    app.state.manager = RunManager(store, settings, cache=dircache)
    try:
        yield
    finally:
        await app.state.manager.shutdown()
        await store.close()


app = FastAPI(title="CPI Load Tester", version=__version__, lifespan=lifespan)


def _store(request: Request) -> Store:
    return request.app.state.store


def _manager(request: Request) -> RunManager:
    return request.app.state.manager


@app.exception_handler(ValueError)
async def _value_error(_: Request, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(RunConflict)
async def _conflict(_: Request, exc: RunConflict):
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.exception_handler(LookupError)
async def _not_found(_: Request, exc: LookupError):
    return JSONResponse({"detail": str(exc)}, status_code=404)


# -- System -------------------------------------------------------------------
@app.get("/api/health/live")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/api/defaults")
async def defaults() -> dict:
    return {
        "endpoint_url": settings.cpi_endpoint_url,
        "token_url": settings.cpi_token_url,
        "client_id": settings.cpi_client_id,
        "has_secret": bool(settings.cpi_client_secret),
        "version": __version__,
    }


@app.post("/api/auth/test")
async def auth_test(req: AuthTestRequest) -> dict:
    conn = req.connection.with_defaults(settings)
    async with httpx.AsyncClient(timeout=15) as client:
        provider = TokenProvider(client, conn.token_url, conn.client_id, conn.client_secret)
        try:
            await provider.get()
        except AuthError as exc:
            return {"ok": False, "error": str(exc)}
    return {"ok": True, "expires_in": provider.expires_in}


# -- Dateien ------------------------------------------------------------------
@app.get("/api/folders")
async def folders(path: str = "", refresh: bool = False) -> dict:
    return await asyncio.to_thread(list_folder, settings.data_dir, path, dircache, refresh)


@app.post("/api/folders/preview")
async def folder_preview(req: PreviewRequest) -> dict:
    return await asyncio.to_thread(
        preview, settings.data_dir, req.folder, req.pattern, req.recursive, req.max_files, dircache
    )


# -- Läufe --------------------------------------------------------------------
@app.get("/api/runs")
async def runs(request: Request, limit: int = Query(default=50, ge=1, le=500)) -> list[dict]:
    return await _store(request).list_runs(limit)


@app.post("/api/runs")
async def start_run(request: Request, req: StartRunRequest) -> dict:
    return {"id": await _manager(request).start(req)}


@app.get("/api/runs/{run_id}")
async def get_run(request: Request, run_id: int) -> dict:
    run = await _store(request).get_run(run_id)
    if run is None:
        raise LookupError(f"Lauf #{run_id} nicht gefunden.")
    return run


@app.delete("/api/runs/{run_id}")
async def delete_run(request: Request, run_id: int) -> dict:
    active = _manager(request).active
    if active is not None and active.run_id == run_id and not active.done:
        raise RunConflict("Ein aktiver Lauf kann nicht gelöscht werden.")
    await _store(request).delete_run(run_id)
    return {"deleted": run_id}


@app.post("/api/runs/{run_id}/pause")
async def pause_run(request: Request, run_id: int) -> dict:
    _manager(request).require_active(run_id).pause()
    return {"status": "paused"}


@app.post("/api/runs/{run_id}/unpause")
async def unpause_run(request: Request, run_id: int) -> dict:
    _manager(request).require_active(run_id).unpause()
    return {"status": "running"}


@app.post("/api/runs/{run_id}/stop")
async def stop_run(request: Request, run_id: int) -> dict:
    _manager(request).require_active(run_id).stop()
    return {"status": "stopping"}


@app.patch("/api/runs/{run_id}/live")
async def adjust_run(request: Request, run_id: int, change: LiveAdjust) -> dict:
    active = _manager(request).require_active(run_id)
    active.adjust(change)
    return {"concurrency": active.gate.limit, "rate_limit": active.pacer.rate}


@app.post("/api/runs/{run_id}/resume")
async def resume_run(request: Request, run_id: int, req: ContinueRunRequest) -> dict:
    return {"id": await _manager(request).resume(run_id, req.connection)}


@app.post("/api/runs/{run_id}/retry-failed")
async def retry_failed(request: Request, run_id: int, req: ContinueRunRequest) -> dict:
    return {"id": await _manager(request).retry_failed(run_id, req.connection)}


@app.get("/api/runs/{run_id}/items")
async def run_items(
    request: Request,
    run_id: int,
    status: str | None = Query(default=None, pattern="^(pending|ok|failed)$"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    return await _store(request).list_items(run_id, status, limit, offset)


@app.get("/api/runs/{run_id}/export.csv")
async def export_csv(
    request: Request, run_id: int, status: str | None = Query(default=None, pattern="^(pending|ok|failed)$")
):
    suffix = f"-{status}" if status else ""
    return StreamingResponse(
        _store(request).export_csv(run_id, status),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="lauf-{run_id}{suffix}.csv"'},
    )


@app.get("/api/events")
async def events(request: Request):
    """Server-Sent Events: jede Sekunde der Zustand des aktiven bzw. letzten Laufs und laufende Ordner-Scans."""

    async def stream():
        while not await request.is_disconnected():
            payload = json.dumps({"active": _manager(request).snapshot(), "scans": dircache.progress()})
            yield f"data: {payload}\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="static")
