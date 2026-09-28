"""Lauf-Engine: liest Dateien, sendet sie kontrolliert parallel an den iFlow.

Ein Dispatcher zieht offene Items seitenweise aus SQLite und startet pro Datei
einen Task – begrenzt durch ein Gate (Parallelität) und einen Pacer
(msg/s). Beides lässt sich während des Laufs ändern. Stop und Pause warten
auf laufende Requests, damit beim Fortsetzen nichts doppelt gesendet wird.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import time
from pathlib import PurePosixPath

import httpx

from .auth import AuthError, TokenProvider
from .config import Settings
from .files import DirCache, scan_files
from .models import Connection, LiveAdjust, LoadSettings, StartRunRequest
from .stats import LiveStats, utc_now
from .store import RESUMABLE_STATES, ItemResult, Store
from .templating import TemplateContext, render_bytes, render_text

log = logging.getLogger(__name__)

MPL_HEADER = "SAP_MessageProcessingLogID"
ERROR_BODY_LIMIT = 1000

_CONTENT_TYPES = {
    ".xml": "application/xml",
    ".json": "application/json",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".edi": "text/plain",
    ".edifact": "text/plain",
}


class RunConflict(Exception):
    """Es läuft bereits ein Lauf bzw. der Lauf ist in einem unpassenden Zustand."""


def content_type_for(filename: str, configured: str) -> str:
    if configured and configured != "auto":
        return configured
    suffix = PurePosixPath(filename).suffix.lower()
    return _CONTENT_TYPES.get(suffix) or mimetypes.guess_type(filename)[0] or "application/octet-stream"


class Gate:
    """Semaphore mit zur Laufzeit änderbarem Limit (ein einzelner Acquirer: der Dispatcher)."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.active = 0
        self._changed = asyncio.Event()

    async def acquire(self) -> None:
        while self.active >= self.limit:
            self._changed.clear()
            await self._changed.wait()
        self.active += 1

    def release(self) -> None:
        self.active -= 1
        self._changed.set()

    def set_limit(self, limit: int) -> None:
        self.limit = limit
        self._changed.set()


class Pacer:
    """Gleichmäßiger Takt statt Token-Bucket.

    Verspätetes Aufwachen (Timer-Granularität, Dispatcher-Overhead) wird bis zu
    einem Intervall nachgeholt, damit die Zielrate im Mittel exakt gehalten wird.
    Nach längeren Wartephasen (Gate voll, Pause) gibt es dagegen keinen Burst.
    """

    def __init__(self, rate: float) -> None:
        self.rate = rate
        self._next = time.monotonic()
        self._wake = asyncio.Event()

    async def wait(self) -> None:
        while self.rate > 0:
            interval = 1 / self.rate
            now = time.monotonic()
            self._next = max(self._next, now - interval)
            delay = self._next - now
            if delay <= 0:
                self._next += interval
                return
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except TimeoutError:
                pass

    def set_rate(self, rate: float) -> None:
        self.rate = rate
        self._next = time.monotonic()
        self._wake.set()

    def interrupt(self) -> None:
        self._wake.set()


class ActiveRun:
    def __init__(
        self,
        *,
        run_id: int,
        run: dict,
        load: LoadSettings,
        connection: Connection,
        store: Store,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None,
    ) -> None:
        self.run_id = run_id
        self.folder = run["folder"]
        self.load = load
        self.connection = connection
        self._store = store
        self._base = settings.data_dir
        self._transport = transport
        self._prior_active_s = run.get("active_s") or 0.0

        self.status = "running"
        self.message: str | None = None
        self.stats = LiveStats(run["total"], already_ok=run["ok"], already_failed=run["failed"])
        self.gate = Gate(load.concurrency)
        self.pacer = Pacer(load.rate_limit)
        self._resume_event = asyncio.Event()
        self._resume_event.set()
        self._stopping = False
        self._consecutive_failures = 0
        self._tasks: set[asyncio.Task] = set()
        self.task: asyncio.Task | None = None

    @property
    def done(self) -> bool:
        return self.task is not None and self.task.done()

    # -- Steuerung ------------------------------------------------------------
    def pause(self, message: str | None = None) -> None:
        if self.status != "running":
            return
        self.status = "paused"
        self.message = message
        self._resume_event.clear()
        self.pacer.interrupt()
        self.stats.pause_clock()

    def unpause(self) -> None:
        if self.status != "paused":
            return
        self.status = "running"
        self.message = None
        self._consecutive_failures = 0
        self.stats.resume_clock()
        self._resume_event.set()

    def stop(self) -> None:
        if self.status not in ("running", "paused"):
            return
        self.status = "stopping"
        self._stopping = True
        self._resume_event.set()
        self.pacer.interrupt()

    def adjust(self, change: LiveAdjust) -> None:
        if change.concurrency is not None:
            self.load.concurrency = change.concurrency
            self.gate.set_limit(change.concurrency)
        if change.rate_limit is not None:
            self.load.rate_limit = change.rate_limit
            self.pacer.set_rate(change.rate_limit)

    # -- Ablauf ---------------------------------------------------------------
    async def execute(self) -> None:
        await self._store.update_run(self.run_id, status="running", started_at=utc_now(), message=None)
        timeout = httpx.Timeout(self.load.timeout_s, connect=min(15.0, self.load.timeout_s))
        limits = httpx.Limits(max_connections=None, max_keepalive_connections=200)
        flusher: asyncio.Task | None = None
        try:
            async with httpx.AsyncClient(timeout=timeout, limits=limits, transport=self._transport) as client:
                try:
                    tokens = TokenProvider(
                        client, self.connection.token_url, self.connection.client_id, self.connection.client_secret
                    )
                    await tokens.get()  # fail fast, bevor die erste Datei angefasst wird
                    self.stats.resume_clock()
                    flusher = asyncio.create_task(self._flush_loop())
                    await self._dispatch(client, tokens)
                finally:
                    # Laufende Requests auch im Fehlerfall abschließen, solange der Client noch offen ist.
                    if self._tasks:
                        await asyncio.gather(*self._tasks, return_exceptions=True)
            self.status = "stopped" if self._stopping else "completed"
        except AuthError as exc:
            self.status, self.message = "failed", str(exc)
        except Exception as exc:  # noqa: BLE001 – Lauf sauber beenden, Fehler im UI zeigen
            log.exception("Lauf %s abgebrochen", self.run_id)
            self.status, self.message = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            if flusher:
                flusher.cancel()
            self.stats.pause_clock()
            await self._finalize()

    async def _dispatch(self, client: httpx.AsyncClient, tokens: TokenProvider) -> None:
        async for batch in self._store.iter_pending(self.run_id):
            for seq, path in batch:
                if not await self._next_slot():
                    return
                task = asyncio.create_task(self._send(client, tokens, seq, path))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def _next_slot(self) -> bool:
        while True:
            await self._resume_event.wait()
            if self._stopping:
                return False
            await self.gate.acquire()
            await self.pacer.wait()
            if self._stopping:
                self.gate.release()
                return False
            if self._resume_event.is_set():
                return True
            self.gate.release()  # während des Wartens pausiert

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            self.stats.tick()
            try:
                await self._store.flush()
            except Exception:  # noqa: BLE001
                log.exception("Flush fehlgeschlagen")

    async def _send(self, client: httpx.AsyncClient, tokens: TokenProvider, seq: int, path: str) -> None:
        self.stats.in_flight += 1
        try:
            ok, code, latency, mpl_id, error = await self._request(client, tokens, seq, path)
        finally:
            self.stats.in_flight -= 1
            self.gate.release()

        sent_at = utc_now()
        self._store.add_result(
            ItemResult(self.run_id, seq, ok, code, latency, mpl_id, error, sent_at)
        )
        self.stats.record(
            ok=ok,
            status_key=code,
            latency_ms=latency,
            error=None if ok else {
                "seq": seq, "path": path, "code": code, "mpl_id": mpl_id,
                "error": (error or "")[:300], "at": sent_at,
            },
        )
        if ok:
            self._consecutive_failures = 0
            return
        self._consecutive_failures += 1
        limit = self.load.max_consecutive_failures
        if limit and self._consecutive_failures >= limit and self.status == "running":
            self.pause(f"Automatisch pausiert: {self._consecutive_failures} Fehler in Folge (zuletzt {code}).")

    async def _request(
        self, client: httpx.AsyncClient, tokens: TokenProvider, seq: int, path: str
    ) -> tuple[bool, str, float | None, str | None, str | None]:
        filename = PurePosixPath(path).name
        try:
            body = await asyncio.to_thread((self._base / path).read_bytes)
        except OSError as exc:
            return False, "FILE", None, None, f"Datei nicht lesbar: {exc}"

        ctx = TemplateContext(seq=seq, filename=filename)
        if self.load.placeholders:
            body = render_bytes(body, ctx)
        headers = {"Content-Type": content_type_for(filename, self.load.content_type)}
        for h in self.load.headers:
            headers[h.name] = render_text(h.value, ctx)

        try:
            token = await tokens.get()
            resp, latency = await self._http(client, headers, token, body)
            if resp.status_code == 401:
                await tokens.invalidate(token)
                token = await tokens.get()
                resp, latency = await self._http(client, headers, token, body)
        except AuthError as exc:
            return False, "AUTH", None, None, str(exc)
        except httpx.TimeoutException as exc:
            return False, "TIMEOUT", None, None, f"Timeout nach {self.load.timeout_s:g} s ({type(exc).__name__})"
        except httpx.HTTPError as exc:
            return False, "CONN", None, None, f"{type(exc).__name__}: {exc}"

        mpl_id = resp.headers.get(MPL_HEADER)
        ok = 200 <= resp.status_code < 300
        error = None if ok else (resp.text or resp.reason_phrase)[:ERROR_BODY_LIMIT]
        return ok, str(resp.status_code), latency, mpl_id, error

    async def _http(
        self, client: httpx.AsyncClient, headers: dict, token: str, body: bytes
    ) -> tuple[httpx.Response, float]:
        started = time.perf_counter()
        resp = await client.request(
            self.load.method,
            self.connection.endpoint_url,
            content=body,
            headers={**headers, "Authorization": f"Bearer {token}"},
        )
        return resp, (time.perf_counter() - started) * 1000

    async def _finalize(self) -> None:
        await self._store.flush()
        summary = await self._store.summarize(self.run_id)
        active_s = self._prior_active_s + self.stats.elapsed()
        summary["active_s"] = active_s
        summary["rate_avg"] = self.stats.sent / active_s if active_s > 0 else None
        await self._store.update_run(
            self.run_id,
            status=self.status,
            message=self.message,
            finished_at=utc_now(),
            active_s=active_s,
            summary_json=json.dumps(summary),
        )

    def snapshot(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "message": self.message,
            "folder": self.folder,
            "endpoint_url": self.connection.endpoint_url,
            "concurrency": self.gate.limit,
            "rate_limit": self.pacer.rate,
            **self.stats.snapshot(),
        }


class RunManager:
    """Hält höchstens einen aktiven Lauf und startet/steuert ihn."""

    def __init__(
        self,
        store: Store,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        cache: DirCache | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._transport = transport
        self._cache = cache
        self.active: ActiveRun | None = None

    def _ensure_idle(self) -> None:
        if self.active is not None and not self.active.done:
            raise RunConflict(f"Lauf #{self.active.run_id} ist noch aktiv.")

    def require_active(self, run_id: int | None = None) -> ActiveRun:
        if self.active is None or self.active.done or (run_id is not None and self.active.run_id != run_id):
            raise RunConflict("Kein aktiver Lauf.")
        return self.active

    async def start(self, req: StartRunRequest) -> int:
        self._ensure_idle()
        conn = req.connection.with_defaults(self._settings)
        conn.validate_complete()
        paths = await asyncio.to_thread(
            scan_files,
            self._settings.data_dir,
            req.files.folder,
            req.files.pattern,
            req.files.recursive,
            req.files.max_files,
            self._cache,
        )
        if not paths:
            raise ValueError("Keine passenden Dateien im gewählten Ordner.")
        config = {"files": req.files.model_dump(), "load": req.load.model_dump(), "connection": conn.public()}
        self._ensure_idle()
        run_id = await self._store.create_run(
            folder=req.files.folder, endpoint_url=conn.endpoint_url, config=config, paths=paths
        )
        await self._launch(run_id, req.load, conn)
        return run_id

    async def resume(self, run_id: int, override: Connection) -> int:
        self._ensure_idle()
        run = await self._get(run_id)
        if run["status"] not in RESUMABLE_STATES or run["sent"] >= run["total"]:
            raise RunConflict("Dieser Lauf kann nicht fortgesetzt werden.")
        conn = override.merged_over(run["config"].get("connection", {})).with_defaults(self._settings)
        conn.validate_complete()
        # Korrigierte Verbindung (z. B. iFlow-Pfad) in der Historie nachziehen.
        config = {**run["config"], "connection": conn.public()}
        await self._store.update_run(run_id, endpoint_url=conn.endpoint_url, config_json=json.dumps(config))
        await self._launch(run_id, LoadSettings(**run["config"]["load"]), conn)
        return run_id

    async def retry_failed(self, run_id: int, override: Connection) -> int:
        self._ensure_idle()
        run = await self._get(run_id)
        paths = await self._store.item_paths(run_id, "failed")
        if not paths:
            raise ValueError("Dieser Lauf hat keine fehlgeschlagenen Dateien.")
        conn = override.merged_over(run["config"].get("connection", {})).with_defaults(self._settings)
        conn.validate_complete()
        config = {**run["config"], "connection": conn.public()}
        new_id = await self._store.create_run(
            folder=run["folder"], endpoint_url=conn.endpoint_url, config=config, paths=paths, parent_run_id=run_id
        )
        await self._launch(new_id, LoadSettings(**run["config"]["load"]), conn)
        return new_id

    async def _get(self, run_id: int) -> dict:
        run = await self._store.get_run(run_id)
        if run is None:
            raise LookupError(f"Lauf #{run_id} nicht gefunden.")
        return run

    async def _launch(self, run_id: int, load: LoadSettings, conn: Connection) -> None:
        run = await self._get(run_id)
        active = ActiveRun(
            run_id=run_id,
            run=run,
            load=load,
            connection=conn,
            store=self._store,
            settings=self._settings,
            transport=self._transport,
        )
        active.task = asyncio.create_task(active.execute(), name=f"run-{run_id}")
        self.active = active

    def snapshot(self) -> dict | None:
        return self.active.snapshot() if self.active else None

    async def shutdown(self, timeout: float = 15.0) -> None:
        if self.active is None or self.active.done:
            return
        self.active.stop()
        try:
            await asyncio.wait_for(asyncio.shield(self.active.task), timeout=timeout)
        except TimeoutError:
            log.warning("Lauf %s nicht rechtzeitig beendet", self.active.run_id)
