import asyncio
import re
import time
from collections import Counter

import pytest

from cpiload.models import Connection, FileSelection, HeaderEntry, LoadSettings, StartRunRequest
from cpiload.runner import Pacer, RunConflict, RunManager, content_type_for
from cpiload.store import Store

from .conftest import FakeCPI


def _request(**load) -> StartRunRequest:
    return StartRunRequest(
        connection=Connection(),  # Defaults kommen aus den Settings
        files=FileSelection(folder="batch", pattern="*.xml"),
        load=LoadSettings(**load),
    )


async def _run_to_end(manager: RunManager, req: StartRunRequest) -> int:
    run_id = await manager.start(req)
    await manager.active.task
    return run_id


def _nr(body: bytes) -> int:
    return int(re.search(rb"<Nr>(\d+)</Nr>", body).group(1))


async def test_full_run_sends_each_file_once(manager: RunManager, store: Store, fake_cpi: FakeCPI):
    fake_cpi.latency = 0.01
    run_id = await _run_to_end(manager, _request(concurrency=5))

    run = await store.get_run(run_id)
    assert run["status"] == "completed"
    assert (run["total"], run["ok"], run["failed"]) == (30, 30, 0)
    assert sorted(_nr(b) for b in fake_cpi.bodies) == list(range(1, 31))
    assert fake_cpi.max_concurrent <= 5
    assert fake_cpi.token_calls == 1
    assert run["summary"]["status_codes"] == {"200": 30}
    assert run["summary"]["latency"]["p95"] is not None

    items = await store.list_items(run_id, "ok", 5, 0)
    assert items[0]["path"] == "batch/msg_1.xml"
    assert items[0]["mpl_id"].startswith("MPL")


async def test_max_files_limits_smoke_test(manager: RunManager, store: Store, fake_cpi: FakeCPI):
    req = _request()
    req.files.max_files = 3
    run_id = await _run_to_end(manager, req)
    assert (await store.get_run(run_id))["total"] == 3
    assert sorted(_nr(b) for b in fake_cpi.bodies) == [1, 2, 3]


async def test_placeholders_and_custom_headers(manager: RunManager, fake_cpi: FakeCPI):
    req = _request(
        placeholders=True,
        headers=[HeaderEntry(name="X-Filename", value="{{filename}}"), HeaderEntry(name="SAP_ApplicationID", value="{{uuid}}")],
    )
    req.files.max_files = 2
    await _run_to_end(manager, req)

    for request in fake_cpi.requests:
        body = request.content.decode()
        assert "{{" not in body
        uuid = re.search(r"<Id>([^<]+)</Id>", body).group(1)
        assert request.headers["SAP_ApplicationID"] == uuid
        assert request.headers["X-Filename"].startswith("msg_")
        assert request.headers["Content-Type"] == "application/xml"


async def test_placeholders_disabled_sends_raw_file(manager: RunManager, fake_cpi: FakeCPI):
    req = _request()
    req.files.max_files = 1
    await _run_to_end(manager, req)
    assert b"{{uuid}}" in fake_cpi.bodies[0]


async def test_errors_recorded_and_retry_failed(manager: RunManager, store: Store, fake_cpi: FakeCPI):
    fake_cpi.fail = lambda request, n: _nr(request.content) % 10 == 0
    run_id = await _run_to_end(manager, _request(concurrency=3))

    run = await store.get_run(run_id)
    assert (run["ok"], run["failed"]) == (27, 3)
    assert run["summary"]["status_codes"] == {"200": 27, "500": 3}
    failed = await store.list_items(run_id, "failed", 10, 0)
    assert [f["path"] for f in failed] == ["batch/msg_10.xml", "batch/msg_20.xml", "batch/msg_30.xml"]
    assert "Internal Server Error" in failed[0]["error"]
    assert manager.snapshot()["recent_errors"][0]["code"] == "500"

    fake_cpi.fail = lambda request, n: False
    retry_id = await manager.retry_failed(run_id, Connection())
    await manager.active.task
    retry = await store.get_run(retry_id)
    assert retry["parent_run_id"] == run_id
    assert (retry["total"], retry["ok"], retry["failed"]) == (3, 3, 0)


async def test_401_refreshes_token_and_retries(manager: RunManager, store: Store, fake_cpi: FakeCPI):
    fake_cpi.reject_next_401 = 1
    req = _request(concurrency=1)
    req.files.max_files = 2
    run_id = await _run_to_end(manager, req)
    run = await store.get_run(run_id)
    assert (run["ok"], run["failed"]) == (2, 0)
    assert fake_cpi.token_calls == 2


async def test_rate_limit_paces_requests(manager: RunManager, fake_cpi: FakeCPI):
    req = _request(concurrency=10, rate_limit=40)
    req.files.max_files = 20
    started = time.monotonic()
    await _run_to_end(manager, req)
    # 20 Requests bei 40/s → erster sofort, letzter nach ~19/40 s
    assert time.monotonic() - started >= 0.45


async def test_stop_and_resume_without_duplicates(manager: RunManager, store: Store, fake_cpi: FakeCPI):
    fake_cpi.latency = 0.05
    run_id = await manager.start(_request(concurrency=2))
    await asyncio.sleep(0.3)
    manager.active.stop()
    await manager.active.task

    stopped = await store.get_run(run_id)
    assert stopped["status"] == "stopped"
    assert 0 < stopped["sent"] < 30
    assert stopped["resumable"]

    fixed = "https://tenant.it-cpi018-rt.cfapps.eu10-003.hana.ondemand.com/http/loadtest/v2"
    await manager.resume(run_id, Connection(endpoint_url=fixed))
    await manager.active.task
    done = await store.get_run(run_id)
    assert done["endpoint_url"] == fixed
    assert done["config"]["connection"]["endpoint_url"] == fixed
    assert str(fake_cpi.requests[-1].url) == fixed
    assert done["status"] == "completed"
    assert done["sent"] == 30
    counts = Counter(_nr(b) for b in fake_cpi.bodies)
    assert sorted(counts) == list(range(1, 31))
    assert max(counts.values()) == 1


async def test_pause_holds_dispatch(manager: RunManager, fake_cpi: FakeCPI):
    fake_cpi.latency = 0.02
    await manager.start(_request(concurrency=2))
    await asyncio.sleep(0.1)
    manager.active.pause()
    await asyncio.sleep(0.1)  # laufende Requests abschließen lassen
    sent = len(fake_cpi.requests)
    await asyncio.sleep(0.2)
    assert len(fake_cpi.requests) == sent
    assert manager.snapshot()["status"] == "paused"
    manager.active.unpause()
    await manager.active.task
    assert len(fake_cpi.requests) == 30


async def test_auto_pause_after_consecutive_failures(manager: RunManager, fake_cpi: FakeCPI):
    fake_cpi.fail = lambda request, n: True
    await manager.start(_request(concurrency=1, max_consecutive_failures=5))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if manager.snapshot()["status"] == "paused":
            break
    snap = manager.snapshot()
    assert snap["status"] == "paused"
    assert "5 Fehler in Folge" in snap["message"]
    assert snap["failed"] == 5
    manager.active.stop()
    await manager.active.task


async def test_live_adjust_rate_and_concurrency(manager: RunManager, fake_cpi: FakeCPI):
    fake_cpi.latency = 0.01
    await manager.start(_request(concurrency=1, rate_limit=5))
    await asyncio.sleep(0.1)
    from cpiload.models import LiveAdjust

    manager.active.adjust(LiveAdjust(concurrency=8, rate_limit=0))
    started = time.monotonic()
    await manager.active.task
    assert time.monotonic() - started < 2  # bei 5 msg/s wären es ~6 s
    assert manager.snapshot()["concurrency"] == 8


async def test_auth_failure_marks_run_failed_and_resumable(manager: RunManager, store: Store, fake_cpi: FakeCPI):
    fake_cpi.token_status = 401
    run_id = await _run_to_end(manager, _request())
    run = await store.get_run(run_id)
    assert run["status"] == "failed"
    assert "HTTP 401" in run["message"]
    assert run["sent"] == 0 and run["resumable"]
    assert fake_cpi.requests == []


async def test_only_one_active_run(manager: RunManager, fake_cpi: FakeCPI):
    fake_cpi.latency = 0.05
    await manager.start(_request(concurrency=1))
    with pytest.raises(RunConflict):
        await manager.start(_request())
    manager.active.stop()
    await manager.active.task


async def test_missing_folder_rejected(manager: RunManager):
    req = _request()
    req.files.pattern = "*.json"
    with pytest.raises(ValueError, match="Keine passenden Dateien"):
        await manager.start(req)


async def test_csv_export(manager: RunManager, store: Store):
    req = _request()
    req.files.max_files = 3
    run_id = await _run_to_end(manager, req)
    csv = "".join([chunk async for chunk in store.export_csv(run_id, None)])
    lines = csv.lstrip("﻿").strip().splitlines()
    assert lines[0].startswith("seq;datei;status;code")
    assert len(lines) == 4
    assert lines[1].startswith("1;batch/msg_1.xml;ok;200;")


def test_content_type_mapping():
    assert content_type_for("a.XML", "auto") == "application/xml"
    assert content_type_for("a.json", "auto") == "application/json"
    assert content_type_for("UTILMD_1.edi", "auto") == "text/plain"
    assert content_type_for("blob.bin", "auto") == "application/octet-stream"
    assert content_type_for("a.xml", "text/xml; charset=utf-8") == "text/xml; charset=utf-8"


async def test_pacer_holds_target_rate():
    pacer = Pacer(200)
    started = time.monotonic()
    for _ in range(201):
        await pacer.wait()
    # 200 Intervalle à 5 ms → 1 s; ohne Nachholen der Aufwach-Verspätung driftet es deutlich darüber.
    assert 0.95 <= time.monotonic() - started <= 1.05
