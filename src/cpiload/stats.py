"""Live-Kennzahlen eines Laufs: Zähler, Latenz-Perzentile, Durchsatz, Zeitreihe."""

from __future__ import annotations

import math
import time
from collections import Counter, deque
from datetime import datetime, timezone

# Log-Buckets mit ~2 % Auflösung: exakt genug für p95/p99, konstanter Speicher.
_BUCKET_BASE = math.log(1.02)
TIMELINE_INTERVAL_S = 2.0
TIMELINE_MAX_POINTS = 120


class LatencyHistogram:
    def __init__(self) -> None:
        self._buckets: Counter[int] = Counter()
        self.count = 0
        self.total = 0.0
        self.min = math.inf
        self.max = 0.0

    def add(self, ms: float) -> None:
        ms = max(ms, 0.01)
        self._buckets[int(math.log(ms) / _BUCKET_BASE)] += 1
        self.count += 1
        self.total += ms
        self.min = min(self.min, ms)
        self.max = max(self.max, ms)

    def percentile(self, p: float) -> float | None:
        if not self.count:
            return None
        rank = p / 100 * self.count
        seen = 0
        for idx in sorted(self._buckets):
            seen += self._buckets[idx]
            if seen >= rank:
                # Bucket-Mitte, begrenzt auf die real gemessenen Extremwerte.
                return min(max(math.exp((idx + 0.5) * _BUCKET_BASE), self.min), self.max)
        return self.max

    def summary(self) -> dict:
        if not self.count:
            return {"avg": None, "min": None, "max": None, "p50": None, "p95": None, "p99": None}
        return {
            "avg": self.total / self.count,
            "min": self.min,
            "max": self.max,
            "p50": self.percentile(50),
            "p95": self.percentile(95),
            "p99": self.percentile(99),
        }


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    values.sort()
    return values[min(len(values) - 1, int(math.ceil(0.95 * len(values))) - 1)]


class LiveStats:
    """Wird vom Runner bei jedem Ergebnis aktualisiert; `snapshot()` liefert das UI-Modell.

    Die Laufzeit zählt nur aktive Phasen – Pausen verfälschen weder Ø msg/s noch ETA.
    """

    def __init__(self, total: int, already_ok: int = 0, already_failed: int = 0) -> None:
        self.total = total
        self.ok = already_ok
        self.failed = already_failed
        self.sent_this_session = 0
        self.in_flight = 0
        self.status_codes: Counter[str] = Counter()
        self.latency = LatencyHistogram()
        self.recent_errors: deque[dict] = deque(maxlen=50)
        self.timeline: list[tuple[float, float, float | None, int]] = []

        self._active_s = 0.0
        self._resumed_at: float | None = None
        self._completions: deque[float] = deque()
        self._interval_start = time.monotonic()
        self._interval_count = 0
        self._interval_errors = 0
        self._interval_latencies: list[float] = []

    @property
    def sent(self) -> int:
        return self.ok + self.failed

    # -- Phasen ---------------------------------------------------------------
    def resume_clock(self) -> None:
        if self._resumed_at is None:
            self._resumed_at = time.monotonic()
            self._interval_start = self._resumed_at

    def pause_clock(self) -> None:
        if self._resumed_at is not None:
            self._active_s += time.monotonic() - self._resumed_at
            self._resumed_at = None

    def elapsed(self) -> float:
        running = time.monotonic() - self._resumed_at if self._resumed_at is not None else 0.0
        return self._active_s + running

    # -- Ergebnisse -----------------------------------------------------------
    def record(self, *, ok: bool, status_key: str, latency_ms: float | None, error: dict | None) -> None:
        now = time.monotonic()
        self.sent_this_session += 1
        if ok:
            self.ok += 1
        else:
            self.failed += 1
            self._interval_errors += 1
            if error:
                self.recent_errors.appendleft(error)
        self.status_codes[status_key] += 1
        if latency_ms is not None:
            self.latency.add(latency_ms)
            self._interval_latencies.append(latency_ms)
        self._completions.append(now)
        self._interval_count += 1

    def _rate_now(self, window_s: float = 5.0) -> float:
        now = time.monotonic()
        while self._completions and self._completions[0] < now - window_s:
            self._completions.popleft()
        if self._resumed_at is None:
            return 0.0
        span = min(window_s, max(now - self._resumed_at, 1e-6))
        return len(self._completions) / span

    def tick(self) -> None:
        """Schreibt einen Zeitreihenpunkt, sobald ein Intervall voll ist."""
        now = time.monotonic()
        span = now - self._interval_start
        if span < TIMELINE_INTERVAL_S or self._resumed_at is None:
            return
        rate = self._interval_count / span
        self.timeline.append((round(self.elapsed(), 1), rate, _p95(self._interval_latencies), self._interval_errors))
        self._interval_start = now
        self._interval_count = 0
        self._interval_errors = 0
        self._interval_latencies = []

    def _downsampled_timeline(self) -> list[dict]:
        points = self.timeline
        if len(points) > TIMELINE_MAX_POINTS:
            step = len(points) / TIMELINE_MAX_POINTS
            points = [points[int(i * step)] for i in range(TIMELINE_MAX_POINTS - 1)] + [points[-1]]
        return [{"t": t, "rate": r, "p95": p, "errors": e} for t, r, p, e in points]

    def snapshot(self) -> dict:
        elapsed = self.elapsed()
        rate_avg = self.sent_this_session / elapsed if elapsed > 0 else 0.0
        remaining = max(self.total - self.sent, 0)
        rate_now = self._rate_now()
        basis = rate_now or rate_avg
        return {
            "total": self.total,
            "sent": self.sent,
            "ok": self.ok,
            "failed": self.failed,
            "in_flight": self.in_flight,
            "elapsed_s": elapsed,
            "rate_now": rate_now,
            "rate_avg": rate_avg,
            "eta_s": remaining / basis if basis > 0 and remaining else None,
            "latency": self.latency.summary(),
            "status_codes": dict(self.status_codes.most_common()),
            "recent_errors": list(self.recent_errors),
            "timeline": self._downsampled_timeline(),
        }

    def final_summary(self) -> dict:
        snap = self.snapshot()
        return {
            "elapsed_s": snap["elapsed_s"],
            "rate_avg": snap["rate_avg"],
            "latency": snap["latency"],
            "status_codes": snap["status_codes"],
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
