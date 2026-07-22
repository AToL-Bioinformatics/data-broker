"""Broker configuration via environment variables or .env file.

Required variables:
  WEBIN_USER          ENA Webin account username (e.g. Webin-12345)
  WEBIN_PASS          ENA Webin account password
  CANOPY_BASE_URL     Base URL of the Canopy API
  CANOPY_USERNAME     Canopy login username
  CANOPY_PASSWORD     Canopy login password

Optional variables (defaults shown):
  ENA_DEV_BASE_URL    ENA dev drop-box submit endpoint
  ENA_PROD_BASE_URL   ENA prod drop-box submit endpoint
  BROKER_STATE_DIR    Where to store attempt state JSON files (~/.cache/broker/state)
  BROKER_RECEIPT_DIR  Where to store raw ENA receipts (~/.cache/broker/receipts)
  HTTP_TIMEOUT_SECONDS   Per-request timeout in seconds (30.0)
  HTTP_MAX_RETRIES       Max retry attempts for transient HTTP errors (3)
  HTTP_RETRY_MIN_WAIT    Minimum retry backoff in seconds (1.0)
  HTTP_RETRY_MAX_WAIT    Maximum retry backoff in seconds (30.0)

ToLID (optional — ToLID requests are skipped when TOLID_API_KEY is not set):
  TOLID_API_KEY       API key for the Sanger Tree of Life ID service
  TOLID_DEV_BASE_URL  ToLID dev/staging base URL
  TOLID_PROD_BASE_URL ToLID production base URL
"""

from __future__ import annotations
from functools import lru_cache
import os
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BrokerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ENA credentials
    webin_username: str = Field(alias="WEBIN_USER")
    webin_password: str = Field(alias="WEBIN_PASS")
    ena_dev_base_url: str = Field(
        default="https://wwwdev.ebi.ac.uk/ena/submit/drop-box/submit/",
        validation_alias=AliasChoices("ENA_DEV_BASE_URL", "ENA_BASE_URL"),
    )
    ena_prod_base_url: str = Field(
        default="https://www.ebi.ac.uk/ena/submit/drop-box/submit/",
        alias="ENA_PROD_BASE_URL",
    )

    # Canopy
    canopy_base_url: str = Field(alias="CANOPY_BASE_URL")
    canopy_username: str = Field(alias="CANOPY_USERNAME")
    canopy_password: str = Field(alias="CANOPY_PASSWORD")

    # Storage paths
    state_dir: str = Field(
        default=Path(
            os.getenv("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "broker/state"
        ).as_posix(),
        alias="BROKER_STATE_DIR",
    )
    receipt_dir: str = Field(
        Path(
            os.getenv("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
            "broker/receipts",
        ).as_posix(),
        alias="BROKER_RECEIPT_DIR",
    )

    # HTTP behaviour
    http_timeout_seconds: float = Field(default=30.0, alias="HTTP_TIMEOUT_SECONDS")
    http_max_retries: int = Field(default=3, alias="HTTP_MAX_RETRIES")
    http_retry_min_wait: float = Field(default=1.0, alias="HTTP_RETRY_MIN_WAIT")
    http_retry_max_wait: float = Field(default=30.0, alias="HTTP_RETRY_MAX_WAIT")

    # ToLID — optional; skip ToLID requests when api key is absent
    tolid_api_key: str | None = Field(default=None, alias="TOLID_API_KEY")
    tolid_dev_base_url: str = Field(
        default="https://id-staging.tol.sanger.ac.uk",
        validation_alias=AliasChoices("TOLID_DEV_BASE_URL", "TOLID_BASE_URL"),
    )
    tolid_prod_base_url: str = Field(
        default="https://id.tol.sanger.ac.uk",
        alias="TOLID_PROD_BASE_URL",
    )


@lru_cache(maxsize=1)
def get_settings() -> BrokerSettings:
    """Return the singleton settings instance.

    Cached after first call. In tests, call get_settings.cache_clear()
    before patching environment variables.
    """
    return BrokerSettings()
