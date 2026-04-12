"""Broker configuration via environment variables or .env file.

Required variables:
  WEBIN_USERNAME      ENA Webin account username (e.g. Webin-12345)
  WEBIN_PASSWORD      ENA Webin account password
  CANOPY_BASE_URL     Base URL of the Canopy API
  CANOPY_USERNAME     Canopy login username
  CANOPY_PASSWORD     Canopy login password

Optional variables (defaults shown):
  ENA_BASE_URL        ENA drop-box submit endpoint
  BROKER_STATE_DIR    Where to store attempt state JSON files (~/.broker/state)
  BROKER_RECEIPT_DIR  Where to store raw ENA receipts (~/.broker/receipts)
  HTTP_TIMEOUT_SECONDS   Per-request timeout in seconds (30.0)
  HTTP_MAX_RETRIES       Max retry attempts for transient HTTP errors (3)
  HTTP_RETRY_MIN_WAIT    Minimum retry backoff in seconds (1.0)
  HTTP_RETRY_MAX_WAIT    Maximum retry backoff in seconds (30.0)
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BrokerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ENA credentials
    webin_username: str = Field(alias="WEBIN_USERNAME")
    webin_password: str = Field(alias="WEBIN_PASSWORD")
    # TODO handle prod and dev envs with separate vars or a single ENV var to switch URLs
    # ASSUMPTION: ENA drop-box submit URL. Override with ENA_BASE_URL env var.
    # The test server is https://wwwdev.ebi.ac.uk/ena/submit/drop-box/submit/
    ena_base_url: str = Field(
        default="https://wwwdev.ebi.ac.uk/ena/submit/drop-box/submit/",
        alias="ENA_BASE_URL",
    )

    # Canopy
    canopy_base_url: str = Field(alias="CANOPY_BASE_URL")
    canopy_username: str = Field(alias="CANOPY_USERNAME")
    canopy_password: str = Field(alias="CANOPY_PASSWORD")

    # Storage paths
    state_dir: str = Field(default="~/.broker/state", alias="BROKER_STATE_DIR")
    receipt_dir: str = Field(default="~/.broker/receipts", alias="BROKER_RECEIPT_DIR")

    # HTTP behaviour
    http_timeout_seconds: float = Field(default=30.0, alias="HTTP_TIMEOUT_SECONDS")
    http_max_retries: int = Field(default=3, alias="HTTP_MAX_RETRIES")
    http_retry_min_wait: float = Field(default=1.0, alias="HTTP_RETRY_MIN_WAIT")
    http_retry_max_wait: float = Field(default=30.0, alias="HTTP_RETRY_MAX_WAIT")


@lru_cache(maxsize=1)
def get_settings() -> BrokerSettings:
    """Return the singleton settings instance.

    Cached after first call. In tests, call get_settings.cache_clear()
    before patching environment variables.
    """
    return BrokerSettings()
