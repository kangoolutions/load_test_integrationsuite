"""OAuth2 Client Credentials gegen XSUAA (Service Key der Process Integration Runtime)."""

from __future__ import annotations

import asyncio
import time

import httpx


class AuthError(Exception):
    """Token konnte nicht beschafft werden."""


class TokenProvider:
    """Holt, cached und erneuert ein Access Token.

    Parallele Worker teilen sich ein Token; bei Ablauf holt genau ein Worker
    ein neues (Lock mit Double-Check), die anderen warten darauf.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        token_url: str,
        client_id: str,
        client_secret: str,
        refresh_skew_s: float = 60.0,
    ) -> None:
        self._client = client
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._skew = refresh_skew_s
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at = 0.0
        self.expires_in: int | None = None

    def _valid(self) -> bool:
        return self._token is not None and time.monotonic() < self._expires_at

    async def get(self) -> str:
        if self._valid():
            return self._token  # type: ignore[return-value]
        async with self._lock:
            if not self._valid():
                await self._fetch()
            return self._token  # type: ignore[return-value]

    async def invalidate(self, token: str) -> None:
        """Verwirft das Token nach einem 401 – nur wenn es noch das aktuelle ist."""
        async with self._lock:
            if self._token == token:
                self._token = None
                self._expires_at = 0.0

    async def _fetch(self) -> None:
        if not (self._token_url and self._client_id and self._client_secret):
            raise AuthError("Token-URL, Client-ID und Client Secret müssen gesetzt sein.")
        try:
            resp = await self._client.post(
                self._token_url,
                data={"grant_type": "client_credentials"},
                auth=(self._client_id, self._client_secret),
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise AuthError(f"Token-Endpunkt nicht erreichbar: {type(exc).__name__}: {exc}") from exc
        if resp.status_code != 200:
            hint = ""
            if resp.status_code == 401:
                hint = (" – Client-ID/Secret prüfen. Kommt das Secret aus der .env: in einfache Anführungszeichen"
                        " setzen, ein '$' wird sonst von Docker Compose als Variable aufgelöst.")
            raise AuthError(f"Token-Abruf fehlgeschlagen: HTTP {resp.status_code} {resp.text[:300]}{hint}")
        try:
            data = resp.json()
            token = data["access_token"]
        except (ValueError, KeyError) as exc:
            raise AuthError("Token-Antwort enthält kein access_token.") from exc

        raw_expiry = data.get("expires_in")
        expires_in = int(raw_expiry) if raw_expiry is not None else 3600
        # Bei sehr kurzen Laufzeiten nicht mehr als die Hälfte als Puffer abziehen.
        skew = min(self._skew, expires_in / 2)
        self._token = token
        self.expires_in = expires_in
        self._expires_at = time.monotonic() + expires_in - skew
