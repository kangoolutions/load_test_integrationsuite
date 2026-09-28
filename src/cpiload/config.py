"""Laufzeit-Konfiguration aus Umgebungsvariablen bzw. `.env`.

App-Pfade tragen das Präfix `CPILOAD_`, damit sie nicht mit Compose-Variablen
(z. B. `HOST_DATA_DIR` für den Mount) kollidieren. Die `CPI_*`-Werte sind nur
Vorbelegungen für die Oberfläche – das Secret verlässt den Prozess nie.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    data_dir: Path = Field(default=_PROJECT_ROOT / "testdata", validation_alias="CPILOAD_DATA_DIR")
    db_path: Path = Field(default=_PROJECT_ROOT / "state" / "cpiload.db", validation_alias="CPILOAD_DB_PATH")
    static_dir: Path = Field(default=_PROJECT_ROOT / "static", validation_alias="CPILOAD_STATIC_DIR")

    cpi_endpoint_url: str = Field(default="", validation_alias="CPI_ENDPOINT_URL")
    cpi_token_url: str = Field(default="", validation_alias="CPI_TOKEN_URL")
    cpi_client_id: str = Field(default="", validation_alias="CPI_CLIENT_ID")
    cpi_client_secret: str = Field(default="", validation_alias="CPI_CLIENT_SECRET", repr=False)


@lru_cache
def get_settings() -> Settings:
    return Settings()
