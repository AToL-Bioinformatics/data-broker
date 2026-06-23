from __future__ import annotations

from broker.config import BrokerSettings


def test_dev_and_prod_urls_are_loaded_from_settings():
    settings = BrokerSettings(
        WEBIN_USERNAME="Webin-test",
        WEBIN_PASSWORD="secret",
        CANOPY_BASE_URL="http://canopy.test",
        CANOPY_USERNAME="user@example.com",
        CANOPY_PASSWORD="hunter2",
        ENA_DEV_BASE_URL="http://ena.dev/submit",
        ENA_PROD_BASE_URL="http://ena.prod/submit",
        TOLID_DEV_BASE_URL="http://tolid.dev",
        TOLID_PROD_BASE_URL="http://tolid.prod",
    )

    assert settings.ena_dev_base_url == "http://ena.dev/submit"
    assert settings.ena_prod_base_url == "http://ena.prod/submit"
    assert settings.tolid_dev_base_url == "http://tolid.dev"
    assert settings.tolid_prod_base_url == "http://tolid.prod"


def test_legacy_single_url_env_vars_still_map_to_dev_urls():
    settings = BrokerSettings(
        WEBIN_USERNAME="Webin-test",
        WEBIN_PASSWORD="secret",
        CANOPY_BASE_URL="http://canopy.test",
        CANOPY_USERNAME="user@example.com",
        CANOPY_PASSWORD="hunter2",
        ENA_BASE_URL="http://ena.legacy/submit",
        TOLID_BASE_URL="http://tolid.legacy",
    )

    assert settings.ena_dev_base_url == "http://ena.legacy/submit"
    assert settings.tolid_dev_base_url == "http://tolid.legacy"
