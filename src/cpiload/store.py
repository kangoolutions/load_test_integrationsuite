"""SQLite-Persistenz für Läufe und Ergebnisse pro Datei.

Pro Lauf eine Zeile in `runs`, pro Datei eine in `run_items`. Ergebnisse
werden gepuffert und gebündelt geschrieben, damit 100+ msg/s nicht an
einzelnen Commits hängen.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from .stats import utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    status        TEXT NOT NULL,
    message       TEXT,
    folder        TEXT NOT NULL,
    endpoint_url  TEXT NOT NULL,
    config_json   TEXT NOT NULL,
    parent_run_id INTEGER,
    total         INTEGER NOT NULL DEFAULT 0,
    ok            INTEGER NOT NULL DEFAULT 0,
    failed        INTEGER NOT NULL DEFAULT 0,
    active_s      REAL NOT NULL DEFAULT 0,
    summary_json  TEXT
);
CREATE TABLE IF NOT EXISTS run_items (
    run_id      INTEGER NOT NULL,
    seq         INTEGER NOT NULL,
    path        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    code        TEXT,
    latency_ms  REAL,
    mpl_id      TEXT,
    error       TEXT,
    sent_at     TEXT,
    PRIMARY KEY (run_id, seq)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_run_items_status ON run_items (run_id, status, seq);
"""

# Läufe, die beim Start der App noch als aktiv markiert sind, wurden hart beendet.
ACTIVE_STATES = ("created", "running", "paused", "stopping")
RESUMABLE_STATES = ("stopped", "interrupted", "failed")


@dataclass(slots=True)
class ItemResult:
    run_id: int
    seq: int
    ok: bool
    code: str | None
    latency_ms: float | None
    mpl_id: str | None
    error: str | None
    sent_at: str


class Store:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._db: aiosqlite.Connection | None = None
        self._pending: list[ItemResult] = []
        self._lock = asyncio.Lock()

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Store nicht geöffnet"
        return self._db

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.executescript(SCHEMA)
        placeholders = ",".join("?" * len(ACTIVE_STATES))
        await self._db.execute(
            f"UPDATE runs SET status='interrupted', message='App wurde während des Laufs beendet.' "
            f"WHERE status IN ({placeholders})",
            ACTIVE_STATES,
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self.flush()
            await self._db.close()
            self._db = None

    # -- Läufe ----------------------------------------------------------------
    async def create_run(
        self,
        *,
        folder: str,
        endpoint_url: str,
        config: dict,
        paths: Iterable[str],
        parent_run_id: int | None = None,
    ) -> int:
        async with self._lock:
            cur = await self.db.execute(
                "INSERT INTO runs (created_at, status, folder, endpoint_url, config_json, parent_run_id) "
                "VALUES (?, 'created', ?, ?, ?, ?)",
                (utc_now(), folder, endpoint_url, json.dumps(config), parent_run_id),
            )
            run_id = cur.lastrowid
            total = 0
            batch: list[tuple[int, int, str]] = []
            for seq, path in enumerate(paths, start=1):
                batch.append((run_id, seq, path))
                total = seq
                if len(batch) >= 5000:
                    await self.db.executemany("INSERT INTO run_items (run_id, seq, path) VALUES (?, ?, ?)", batch)
                    batch.clear()
            if batch:
                await self.db.executemany("INSERT INTO run_items (run_id, seq, path) VALUES (?, ?, ?)", batch)
            await self.db.execute("UPDATE runs SET total=? WHERE id=?", (total, run_id))
            await self.db.commit()
            return run_id

    async def update_run(self, run_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        async with self._lock:
            await self.db.execute(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))
            await self.db.commit()

    async def get_run(self, run_id: int) -> dict | None:
        async with self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)) as cur:
            row = await cur.fetchone()
        return _run_dict(row) if row else None

    async def list_runs(self, limit: int = 50) -> list[dict]:
        async with self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)) as cur:
            return [_run_dict(r) for r in await cur.fetchall()]

    async def delete_run(self, run_id: int) -> None:
        async with self._lock:
            await self.db.execute("DELETE FROM run_items WHERE run_id=?", (run_id,))
            await self.db.execute("DELETE FROM runs WHERE id=?", (run_id,))
            await self.db.commit()

    # -- Items ----------------------------------------------------------------
    async def iter_pending(self, run_id: int, batch_size: int = 1000) -> AsyncIterator[list[tuple[int, str]]]:
        """Keyset-Pagination über offene Items – stabil, während parallel Ergebnisse geschrieben werden."""
        last = 0
        while True:
            async with self.db.execute(
                "SELECT seq, path FROM run_items WHERE run_id=? AND status='pending' AND seq>? ORDER BY seq LIMIT ?",
                (run_id, last, batch_size),
            ) as cur:
                rows = [(r["seq"], r["path"]) for r in await cur.fetchall()]
            if not rows:
                return
            last = rows[-1][0]
            yield rows

    async def item_paths(self, run_id: int, status: str) -> list[str]:
        async with self.db.execute(
            "SELECT path FROM run_items WHERE run_id=? AND status=? ORDER BY seq", (run_id, status)
        ) as cur:
            return [r["path"] for r in await cur.fetchall()]

    async def list_items(self, run_id: int, status: str | None, limit: int, offset: int) -> list[dict]:
        sql = "SELECT seq, path, status, code, latency_ms, mpl_id, error, sent_at FROM run_items WHERE run_id=?"
        args: list = [run_id]
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY seq LIMIT ? OFFSET ?"
        args += [limit, offset]
        async with self.db.execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]

    def add_result(self, result: ItemResult) -> None:
        self._pending.append(result)

    @property
    def pending_results(self) -> int:
        return len(self._pending)

    async def flush(self) -> None:
        if not self._pending:
            return
        batch, self._pending = self._pending, []
        async with self._lock:
            await self.db.executemany(
                "UPDATE run_items SET status=?, code=?, latency_ms=?, mpl_id=?, error=?, sent_at=? "
                "WHERE run_id=? AND seq=?",
                [
                    ("ok" if r.ok else "failed", r.code, r.latency_ms, r.mpl_id, r.error, r.sent_at, r.run_id, r.seq)
                    for r in batch
                ],
            )
            counts: dict[int, list[int]] = {}
            for r in batch:
                c = counts.setdefault(r.run_id, [0, 0])
                c[0 if r.ok else 1] += 1
            for run_id, (ok, failed) in counts.items():
                await self.db.execute("UPDATE runs SET ok=ok+?, failed=failed+? WHERE id=?", (ok, failed, run_id))
            await self.db.commit()

    async def summarize(self, run_id: int) -> dict:
        """Kennzahlen über alle Sitzungen eines Laufs aus den gespeicherten Einzelergebnissen."""
        async with self.db.execute(
            "SELECT COALESCE(code, 'ERR') AS code, COUNT(*) AS n FROM run_items "
            "WHERE run_id=? AND status!='pending' GROUP BY code ORDER BY n DESC",
            (run_id,),
        ) as cur:
            codes = {r["code"]: r["n"] for r in await cur.fetchall()}
        async with self.db.execute(
            "SELECT COUNT(latency_ms) AS n, AVG(latency_ms) AS avg, MIN(latency_ms) AS min, MAX(latency_ms) AS max "
            "FROM run_items WHERE run_id=? AND latency_ms IS NOT NULL",
            (run_id,),
        ) as cur:
            agg = dict(await cur.fetchone())
        latency = {"avg": agg["avg"], "min": agg["min"], "max": agg["max"], "p50": None, "p95": None, "p99": None}
        n = agg["n"] or 0
        for p in (50, 95, 99):
            if n:
                offset = min(n - 1, max(0, int(round(p / 100 * n)) - 1))
                async with self.db.execute(
                    "SELECT latency_ms FROM run_items WHERE run_id=? AND latency_ms IS NOT NULL "
                    "ORDER BY latency_ms LIMIT 1 OFFSET ?",
                    (run_id, offset),
                ) as cur:
                    latency[f"p{p}"] = (await cur.fetchone())["latency_ms"]
        return {"status_codes": codes, "latency": latency}

    async def export_csv(self, run_id: int, status: str | None) -> AsyncIterator[str]:
        """Streamt Ergebnisse als CSV (Semikolon, UTF-8 mit BOM – öffnet sauber in Excel)."""
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=";")
        yield "﻿"
        writer.writerow(["seq", "datei", "status", "code", "latenz_ms", "mpl_id", "gesendet_um", "fehler"])
        sql = "SELECT seq, path, status, code, latency_ms, mpl_id, sent_at, error FROM run_items WHERE run_id=?"
        args: list = [run_id]
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY seq"
        async with self.db.execute(sql, args) as cur:
            while rows := await cur.fetchmany(5000):
                for r in rows:
                    latency = f"{r['latency_ms']:.1f}".replace(".", ",") if r["latency_ms"] is not None else ""
                    writer.writerow(
                        [r["seq"], r["path"], r["status"], r["code"] or "", latency,
                         r["mpl_id"] or "", r["sent_at"] or "", r["error"] or ""]
                    )
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()


def _run_dict(row: aiosqlite.Row) -> dict:
    data = dict(row)
    data["config"] = json.loads(data.pop("config_json") or "{}")
    summary = data.pop("summary_json")
    data["summary"] = json.loads(summary) if summary else None
    data["sent"] = data["ok"] + data["failed"]
    data["resumable"] = data["status"] in RESUMABLE_STATES and data["sent"] < data["total"]
    return data
