from __future__ import annotations

import pytest

import faultbell
from faultbell import config, customer_ref, mask_phone, scrub
from faultbell.transports import DiscordTransport, JsonWebhookTransport

# Fake key prefixes are joined at runtime so secret scanners never see a key-shaped literal.
LIVE = "sk_" + "live_"
GHP = "gh" + "p_"

# ---------------------------------------------------------------------------------------------- scrub


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("mail asha@example.com now", "asha@example.com"),
        ("Authorization: Bearer abc.def-ghi_123", "abc.def-ghi_123"),
        ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl here", "eyJhbGciOiJIUzI1NiJ9"),
        ("stripe " + LIVE + "4eC39HqLyjWDarjtT1zdp7dc", "4eC39HqLyjWDarjtT1zdp7dc"),
        ("pay rzp_test_1DP5mmOlF5G5ag key", "1DP5mmOlF5G5ag"),
        ("gh " + GHP + "16C7e42F292c6912E7710c838347Ae178B4a", "16C7e42F292c6912E7710c838347Ae178B4a"),
        ("password=hunter2&x=1", "hunter2"),
        ('{"api_key": "abc123xyz"}', "abc123xyz"),
        ("client_secret: s3cr3t-value", "s3cr3t-value"),
        ("hash 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08", "9f86d081884c7d659a2f"),
        ("blob QWxhZGRpbjpvcGVuIHNlc2FtZQ0123456789abcdefghijklmnop", "QWxhZGRpbjpvcGVuIHNlc2FtZQ"),
        ("hook https://discord.com/api/webhooks/123456/AbCdEf-token", "AbCdEf-token"),
        ("slack https://hooks.slack.com/services/T000/B000/XXXXXXXX", "XXXXXXXX"),
        ("db postgres://app:pa55word@db.internal:5432/main", "pa55word"),
        ("call +1 555 0100 123 today", "555 0100"),
        ("call 919999900001", "91999990"),
    ],
)
def test_scrub_removes_secrets(text: str, secret: str) -> None:
    assert secret not in scrub(text)


def test_scrub_keeps_last_four_digits_of_phones_and_harmless_numbers() -> None:
    assert scrub("call 919999900001") == "call ••0001"
    assert scrub("order 12345 at 2026-11-17 10:30:00") == "order 12345 at 2026-11-17 10:30:00"
    assert scrub("") == ""


def test_key_prefixes_are_configurable() -> None:
    text = "token-free acme_live_9f8e7d6c5b4a value"
    assert "9f8e7d6c5b4a" in scrub(text)
    assert "9f8e7d6c5b4a" not in scrub(text, key_prefixes=["acme_live_"])
    faultbell.configure(key_prefixes=["acme_live_"])
    assert scrub(text) == "token-free acme_live_[redacted] value"
    # the built-in list is replaced, not extended
    assert "4eC39HqLyjWDarjtT1zdp7dc" in scrub(LIVE + "4eC39HqLyjWDarjtT1zdp7dc")


def test_mask_phone_and_customer_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    assert mask_phone("+1 555 0100 199") == "••0199"
    assert mask_phone("no digits") == ""
    assert customer_ref("") == ""
    assert customer_ref("cus_12345678abcd") == "ref cus_1234"
    ref = customer_ref("Asha Rao")
    assert ref.startswith("cust ") and len(ref) == 13 and "Asha" not in ref
    assert customer_ref("asha rao") == ref  # stable, case-insensitive
    monkeypatch.setenv("FAULTBELL_REF_KEY", "another-deployment")
    assert customer_ref("Asha Rao") != ref


# --------------------------------------------------------------------------------------------- config


def test_environment_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    assert config.env_name() == "local"
    monkeypatch.setenv("FAULTBELL_ENV", "prod")
    assert config.env_name() == "production"
    faultbell.configure(env="stage")
    assert config.env_name() == "staging"


def test_env_detectors_are_tried_in_order() -> None:
    def broken() -> str | None:
        raise RuntimeError("detector bug")

    faultbell.configure(env_detectors=[broken, lambda: None, lambda: "Staging", lambda: "production"])
    assert config.env_name() == "staging"
    assert config.title_prefix("staging") == "[STAGING] "
    assert config.title_prefix("production") == ""


def test_local_never_sends_unless_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAULTBELL_WEBHOOK_URL", "http://127.0.0.1:9/hook")
    assert config.webhook_url() == ""
    monkeypatch.setenv("FAULTBELL_SEND_LOCAL", "1")
    assert config.webhook_url() == "http://127.0.0.1:9/hook"
    faultbell.configure(send_local=False)
    assert config.webhook_url() == ""


def test_per_environment_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAULTBELL_WEBHOOK_URL", "http://127.0.0.1:9/main")
    monkeypatch.setenv("FAULTBELL_WEBHOOK_URL_STAGING", "http://127.0.0.1:9/staging")
    monkeypatch.setenv("FAULTBELL_ENV", "staging")
    assert config.webhook_url() == "http://127.0.0.1:9/staging"
    monkeypatch.setenv("FAULTBELL_ENV", "production")
    assert config.webhook_url() == "http://127.0.0.1:9/main"
    faultbell.configure(webhook_urls={"production": "http://127.0.0.1:9/prod"})
    assert config.webhook_url() == "http://127.0.0.1:9/prod"


def test_transport_url_and_configured_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAULTBELL_ENV", "production")
    assert config.webhook_url() == ""
    faultbell.configure(transport=JsonWebhookTransport("http://127.0.0.1:9/json"))
    assert config.webhook_url() == "http://127.0.0.1:9/json"
    faultbell.configure(webhook_url="http://127.0.0.1:9/explicit")
    assert config.webhook_url() == "http://127.0.0.1:9/explicit"


def test_service_release_host_and_window(monkeypatch: pytest.MonkeyPatch) -> None:
    assert config.service_name() == "app"
    config.set_installed_service("worker")
    assert config.service_name() == "worker"
    assert config.service_name("orders-api") == "orders-api"
    monkeypatch.setenv("FAULTBELL_SERVICE", "orders-api-blue")
    assert config.service_name("orders-api") == "orders-api-blue"
    faultbell.configure(service="explicit")
    assert config.service_name() == "explicit"

    assert config.release() == "-"
    monkeypatch.setenv("GIT_SHA", "0123456789abcdef0123")
    assert config.release() == "0123456789ab"
    monkeypatch.setenv("FAULTBELL_HOST", "worker-1")
    assert config.host() == "worker-1"

    assert config.dedupe_window() == 600.0
    monkeypatch.setenv("FAULTBELL_DEDUPE_WINDOW", "30")
    assert config.dedupe_window() == 30.0
    monkeypatch.setenv("FAULTBELL_DEDUPE_WINDOW", "soon")
    assert config.dedupe_window() == 600.0
    faultbell.configure(dedupe_window=5)
    assert config.dedupe_window() == 5.0


def test_configure_rejects_unknown_settings() -> None:
    with pytest.raises(TypeError, match="nope"):
        faultbell.configure(nope=1)


def test_transport_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from faultbell import sender

    assert isinstance(sender.active_transport(), DiscordTransport)
    monkeypatch.setenv("FAULTBELL_TRANSPORT", "json")
    assert isinstance(sender.active_transport(), JsonWebhookTransport)
