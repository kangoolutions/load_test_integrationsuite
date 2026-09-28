"""Request-Modelle der API und die daraus abgeleitete Lauf-Konfiguration."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .config import Settings

_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
# Diese Header setzt das Tool bzw. httpx selbst.
_RESERVED_HEADERS = {"authorization", "host", "content-length", "transfer-encoding", "connection"}


class Connection(BaseModel):
    endpoint_url: str = ""
    token_url: str = ""
    client_id: str = ""
    client_secret: str = Field(default="", repr=False)

    def with_defaults(self, settings: Settings) -> Connection:
        """Leere Felder aus der Umgebung (.env) auffüllen."""
        return Connection(
            endpoint_url=(self.endpoint_url or settings.cpi_endpoint_url).strip(),
            token_url=(self.token_url or settings.cpi_token_url).strip(),
            client_id=(self.client_id or settings.cpi_client_id).strip(),
            client_secret=self.client_secret or settings.cpi_client_secret,
        )

    def merged_over(self, stored: dict) -> Connection:
        """Für Fortsetzen/Wiederholen: gespeicherte Werte, durch nicht-leere Eingaben überschrieben."""
        return Connection(
            endpoint_url=self.endpoint_url or stored.get("endpoint_url", ""),
            token_url=self.token_url or stored.get("token_url", ""),
            client_id=self.client_id or stored.get("client_id", ""),
            client_secret=self.client_secret,
        )

    def validate_complete(self) -> None:
        missing = [
            label
            for label, value in (
                ("Endpoint-URL", self.endpoint_url),
                ("Token-URL", self.token_url),
                ("Client-ID", self.client_id),
                ("Client Secret", self.client_secret),
            )
            if not value
        ]
        if missing:
            raise ValueError("Fehlende Verbindungsdaten: " + ", ".join(missing))
        if not self.endpoint_url.startswith(("https://", "http://")):
            raise ValueError("Endpoint-URL muss mit https:// beginnen.")

    def public(self) -> dict:
        return {"endpoint_url": self.endpoint_url, "token_url": self.token_url, "client_id": self.client_id}


class HeaderEntry(BaseModel):
    name: str
    value: str = ""


class FileSelection(BaseModel):
    folder: str = ""
    pattern: str = "*"
    recursive: bool = False
    max_files: int = Field(default=0, ge=0)


class LoadSettings(BaseModel):
    concurrency: int = Field(default=10, ge=1, le=500)
    rate_limit: float = Field(default=0, ge=0, le=10_000)
    timeout_s: float = Field(default=60, ge=1, le=600)
    method: Literal["POST", "PUT"] = "POST"
    content_type: str = "auto"
    placeholders: bool = False
    headers: list[HeaderEntry] = Field(default_factory=list)
    max_consecutive_failures: int = Field(default=100, ge=0)

    @field_validator("headers")
    @classmethod
    def _check_headers(cls, headers: list[HeaderEntry]) -> list[HeaderEntry]:
        cleaned = []
        for h in headers:
            name = h.name.strip()
            if not name:
                continue
            if not _HEADER_NAME.match(name):
                raise ValueError(f"Ungültiger Header-Name: {name!r}")
            if name.lower() in _RESERVED_HEADERS:
                raise ValueError(f"Header {name!r} setzt das Tool selbst.")
            cleaned.append(HeaderEntry(name=name, value=h.value))
        return cleaned


class StartRunRequest(BaseModel):
    connection: Connection = Field(default_factory=Connection)
    files: FileSelection = Field(default_factory=FileSelection)
    load: LoadSettings = Field(default_factory=LoadSettings)


class ContinueRunRequest(BaseModel):
    """Fortsetzen oder Fehler erneut senden – Verbindung kann korrigiert werden."""

    connection: Connection = Field(default_factory=Connection)


class LiveAdjust(BaseModel):
    concurrency: int | None = Field(default=None, ge=1, le=500)
    rate_limit: float | None = Field(default=None, ge=0, le=10_000)


class PreviewRequest(BaseModel):
    folder: str = ""
    pattern: str = "*"
    recursive: bool = False
    max_files: int = Field(default=0, ge=0)


class AuthTestRequest(BaseModel):
    connection: Connection = Field(default_factory=Connection)
