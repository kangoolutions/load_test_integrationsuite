import httpx
import pytest

from cpiload.auth import AuthError, TokenProvider

from .conftest import TOKEN_URL, FakeCPI


def _provider(fake: FakeCPI, client: httpx.AsyncClient) -> TokenProvider:
    return TokenProvider(client, TOKEN_URL, "sb-client", "secret")


async def test_token_is_cached(fake_cpi: FakeCPI):
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake_cpi)) as client:
        provider = _provider(fake_cpi, client)
        assert await provider.get() == "tok1"
        assert await provider.get() == "tok1"
    assert fake_cpi.token_calls == 1


async def test_invalidate_fetches_new_token_once(fake_cpi: FakeCPI):
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake_cpi)) as client:
        provider = _provider(fake_cpi, client)
        old = await provider.get()
        await provider.invalidate(old)
        await provider.invalidate(old)  # zweiter Worker mit demselben 401 – kein Doppel-Refresh
        assert await provider.get() == "tok2"
    assert fake_cpi.token_calls == 2


async def test_short_lived_token_is_refreshed(fake_cpi: FakeCPI):
    fake_cpi.token_ttl = 0
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake_cpi)) as client:
        provider = _provider(fake_cpi, client)
        await provider.get()
        await provider.get()
    assert fake_cpi.token_calls == 2


async def test_token_error_raises(fake_cpi: FakeCPI):
    fake_cpi.token_status = 401
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake_cpi)) as client:
        with pytest.raises(AuthError, match="HTTP 401"):
            await _provider(fake_cpi, client).get()


async def test_missing_credentials():
    async with httpx.AsyncClient() as client:
        with pytest.raises(AuthError, match="müssen gesetzt"):
            await TokenProvider(client, "", "", "").get()
