from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from cpiload.config import Settings
from cpiload.runner import RunManager
from cpiload.store import Store

TOKEN_URL = "https://tenant.authentication.eu10.hana.ondemand.com/oauth/token"
ENDPOINT_URL = "https://tenant.it-cpi018-rt.cfapps.eu10-003.hana.ondemand.com/http/loadtest"


class FakeCPI:
    """In-Process-Ersatz für XSUAA + iFlow-Endpunkt (via httpx.MockTransport)."""

    def __init__(self) -> None:
        self.latency = 0.0
        self.token_calls = 0
        self.token_status = 200
        self.token_ttl = 3600
        self.fail = lambda request, n: False
        self.reject_next_401 = 0
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self.concurrent = 0
        self.max_concurrent = 0
        self._tokens_issued = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            self.token_calls += 1
            if self.token_status != 200:
                return httpx.Response(self.token_status, text="unauthorized client")
            self._tokens_issued += 1
            return httpx.Response(
                200, json={"access_token": f"tok{self._tokens_issued}", "expires_in": self.token_ttl}
            )

        assert request.headers["Authorization"].startswith("Bearer tok")
        if self.reject_next_401:
            self.reject_next_401 -= 1
            return httpx.Response(401)

        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if self.latency:
                await asyncio.sleep(self.latency)
        finally:
            self.concurrent -= 1

        self.requests.append(request)
        self.bodies.append(request.content)
        n = len(self.requests)
        if self.fail(request, n):
            return httpx.Response(500, text="<html>Internal Server Error</html>")
        return httpx.Response(200, headers={"SAP_MessageProcessingLogID": f"MPL{n:05d}"})


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    base = tmp_path / "data"
    (base / "batch").mkdir(parents=True)
    for i in range(1, 31):
        (base / "batch" / f"msg_{i}.xml").write_text(
            f"<Msg><Id>{{{{uuid}}}}</Id><Seq>{{{{seq}}}}</Seq><Nr>{i}</Nr></Msg>", encoding="utf-8"
        )
    return base


@pytest.fixture
def settings(tmp_path: Path, data_dir: Path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=data_dir,
        db_path=tmp_path / "state" / "test.db",
        cpi_endpoint_url=ENDPOINT_URL,
        cpi_token_url=TOKEN_URL,
        cpi_client_id="sb-client",
        cpi_client_secret="secret",
    )


@pytest.fixture
def fake_cpi() -> FakeCPI:
    return FakeCPI()


@pytest.fixture
async def store(settings: Settings):
    s = Store(settings.db_path)
    await s.open()
    yield s
    await s.close()


@pytest.fixture
def manager(store: Store, settings: Settings, fake_cpi: FakeCPI) -> RunManager:
    return RunManager(store, settings, transport=httpx.MockTransport(fake_cpi))
