"""End-to-end: capture() -> sender thread -> a real local HTTP server acting as the webhook."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

import faultbell
from conftest import FakeWebhook, raise_and_catch
from faultbell import Card, Delivery, Transport, sender


def fail_in_checkout(order_id: int) -> BaseException:
    return raise_and_catch(ValueError(f"total mismatch for order {order_id}"))


def test_card_with_embed_fields_and_attached_traceback(live: FakeWebhook) -> None:
    with faultbell.context(tenant="Demo Café", tenant_id="t-1001", customer="Asha", phone="+1 555 0100 123"):
        faultbell.set_context(feature="checkout", region="south")
        faultbell.capture(fail_in_checkout(42))
    assert faultbell.flush(5)

    [request] = live.wait_for(1)
    assert request.path == "/api/hook"
    assert request.content_type.startswith("multipart/form-data; boundary=")
    assert request.headers["user-agent"].startswith("faultbell/")
    payload, filename, attachment = request.discord()
    assert payload["allowed_mentions"] == {"parse": []}
    assert payload["attachments"] == [{"id": 0, "filename": filename}]
    embed = payload["embeds"][0]
    assert embed["title"] == "Handled ValueError in app"
    assert embed["color"] == 0xF5A524
    assert "total mismatch for order 42" in embed["description"]
    assert embed["author"]["name"] == "app · production"
    assert embed["footer"]["text"].startswith("Faultbell · production · alert ")
    fields = request.fields()
    assert fields["Environment"] == "production"
    assert fields["Tenant"] == "Demo Café (t-1001)"
    assert fields["Customer"].startswith("cust ") and fields["Customer"].endswith("••0123")
    assert fields["Feature"] == "checkout"
    assert fields["Region"] == "south"
    assert fields["Seen"] == "1x"
    raised_at = next(name for name in fields if name.startswith("Raised at "))
    assert raised_at.startswith("Raised at tests/conftest.py:") and raised_at.endswith(" in raise_and_catch")
    assert "> " in fields[raised_at]  # the source excerpt marks the failing line
    assert "conftest.py" in fields["Stack (your code first)"]
    assert filename is not None and filename.startswith("traceback-") and filename.endswith(".txt")
    assert attachment.startswith("Traceback (most recent call last)")
    assert "ValueError: total mismatch for order 42" in attachment
    assert faultbell.stats()["sent"] == 1


def test_planted_secrets_never_leave_the_process(live: FakeWebhook) -> None:
    secrets = {
        "email": "sam@example.com",
        "bearer": "Bearer abcDEF123.tokenvalue",
        "key": "sk_" + "live_51HxYzAbCdEf0123456789",
        "password": "password=correct-horse",
        "webhook": "https://discord.com/api/webhooks/1234567890/SeCrEtWeBhOoKtOkEn",
        "phone": "+1 555 0100 187",
        "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhc2hhIn0.sig_nature",
    }
    message = " | ".join(secrets.values())
    with faultbell.context(contact=secrets["email"], api_key="gh" + "p_0123456789abcdefABCDEF0123456789abcd"):
        faultbell.capture(raise_and_catch(RuntimeError(message)), job="sync sam@example.com")
    faultbell.flush(5)
    [request] = live.wait_for(1)
    body = request.body.decode()
    for planted in (
        "sam@example.com",
        "abcDEF123.tokenvalue",
        "51HxYzAbCdEf0123456789",
        "correct-horse",
        "SeCrEtWeBhOoKtOkEn",
        "555 0100",
        "eyJzdWIiOiJhc2hhIn0",
        "0123456789abcdefABCDEF",
    ):
        assert planted not in body, planted
    assert "••0187" in body
    assert "[email]" in request.embed()["title"]


def test_repeats_are_suppressed_within_the_window(live: FakeWebhook) -> None:
    now = [1000.0]
    faultbell.configure(clock=lambda: now[0])
    for order_id in (1, 2, 3):  # different numbers, same problem
        faultbell.capture(fail_in_checkout(order_id))
        faultbell.flush(5)
    assert len(live.received) == 1
    assert faultbell.stats()["suppressed"] == 2

    now[0] += 599
    faultbell.capture(fail_in_checkout(4))
    faultbell.flush(5)
    assert len(live.received) == 1

    now[0] += 2  # past the 10 minute window
    faultbell.capture(fail_in_checkout(5))
    faultbell.flush(5)
    first, second = live.wait_for(2)
    assert second.fields()["Seen"] == "4x in the last 10 min"
    assert first.embed()["footer"]["text"] == second.embed()["footer"]["text"]


def test_different_problems_are_not_merged(live: FakeWebhook) -> None:
    faultbell.capture(fail_in_checkout(1))
    faultbell.capture(raise_and_catch(KeyError("missing")))
    faultbell.flush(5)
    assert len(live.wait_for(2)) == 2


class NotFound(Exception):
    status_code = 404


class Conflict(Exception):
    status = 409


class RequestValidationError(Exception):
    pass


class Unavailable(Exception):
    status_code = 503


def test_client_errors_never_alert(live: FakeWebhook) -> None:
    for exc in (NotFound(), Conflict(), RequestValidationError()):
        faultbell.capture(raise_and_catch(exc))
    faultbell.capture(raise_and_catch(Unavailable("upstream down")))
    faultbell.flush(5)
    [request] = live.wait_for(1)
    assert request.embed()["title"] == "Handled Unavailable in app"
    assert faultbell.is_client_error(NotFound())
    assert not faultbell.is_client_error(Unavailable())


def test_same_exception_object_is_reported_once_and_interrupts_ignored(live: FakeWebhook) -> None:
    exc = fail_in_checkout(9)
    faultbell.capture(exc)
    faultbell.capture(exc)
    faultbell.capture(KeyboardInterrupt())
    faultbell.capture(SystemExit(1))
    faultbell.flush(5)
    assert faultbell.stats()["queued"] == 1


def test_no_webhook_means_no_op_and_no_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAULTBELL_ENV", "production")
    faultbell.capture(fail_in_checkout(1))
    with faultbell.job_run("nightly") as run:
        run.fail(fail_in_checkout(2), tenant="Demo Café")
    faultbell.job_failed(fail_in_checkout(3), job="nightly")
    assert faultbell.stats() == {"queued": 0, "sent": 0, "suppressed": 0, "dropped": 0, "failed": 0}
    assert not sender.is_running()
    assert not any(t.name == "faultbell-sender" for t in threading.enumerate())
    assert faultbell.flush(0.1)


def test_local_environment_is_silent_without_opt_in(
    webhook: FakeWebhook, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAULTBELL_WEBHOOK_URL", webhook.url)
    faultbell.capture(fail_in_checkout(1))
    assert not sender.is_running()
    monkeypatch.setenv("FAULTBELL_SEND_LOCAL", "1")
    faultbell.capture(fail_in_checkout(2))
    faultbell.flush(5)
    [request] = webhook.wait_for(1)
    assert request.embed()["title"].startswith("[LOCAL] Handled ValueError")


def test_webhook_500_is_dropped_quietly(live: FakeWebhook) -> None:
    live.statuses = [500]
    faultbell.capture(fail_in_checkout(1))
    assert faultbell.flush(5)
    assert len(live.received) == 1
    assert faultbell.stats()["failed"] == 1 and faultbell.stats()["sent"] == 0


def test_webhook_timeout_never_blocks_the_caller(live: FakeWebhook) -> None:
    live.delay = 1.0
    faultbell.configure(timeout=0.2)
    started = time.monotonic()
    faultbell.capture(fail_in_checkout(1))
    assert time.monotonic() - started < 0.1  # the caller only enqueues
    assert faultbell.flush(5)
    assert faultbell.stats()["failed"] == 1


def test_unreachable_webhook_counts_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAULTBELL_ENV", "production")
    monkeypatch.setenv("FAULTBELL_WEBHOOK_URL", "http://127.0.0.1:1/hook")
    faultbell.capture(fail_in_checkout(1))
    assert faultbell.flush(5)
    assert faultbell.stats()["failed"] == 1


def test_rate_limited_once_then_retried(live: FakeWebhook) -> None:
    live.statuses = [429, 204]
    faultbell.capture(fail_in_checkout(1))
    faultbell.flush(5)
    assert len(live.wait_for(2)) == 2
    assert faultbell.stats()["sent"] == 1


def test_sliding_window_rate_limiter() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    limiter = sender.RateLimiter(clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        limiter.wait(2, 1.0)
    assert slept == [1.0]


class BlockingTransport(Transport):
    def __init__(self) -> None:
        super().__init__("memory://blocking")
        self.entered = threading.Event()
        self.release = threading.Event()
        self.cards: list[Card] = []

    def send(self, card: Card, url: str, timeout: float) -> Delivery:
        self.entered.set()
        self.release.wait(5)
        self.cards.append(card)
        return Delivery(200)


def test_full_queue_drops_instead_of_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAULTBELL_ENV", "production")
    transport = BlockingTransport()
    faultbell.configure(transport=transport, queue_size=1)
    faultbell.capture(raise_and_catch(RuntimeError("first")))
    assert transport.entered.wait(5)  # the sender thread is now stuck in send()
    started = time.monotonic()
    for n in range(4):
        faultbell.capture(raise_and_catch(RuntimeError(f"burst {n}")))
    assert time.monotonic() - started < 0.5
    assert faultbell.stats()["dropped"] == 3
    transport.release.set()
    assert faultbell.flush(5)
    assert [c.message for c in transport.cards] == ["first", "burst 0"]


def test_json_transport(live: FakeWebhook, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAULTBELL_TRANSPORT", "json")
    faultbell.capture(fail_in_checkout(7), handled=False)
    faultbell.flush(5)
    [request] = live.wait_for(1)
    assert request.content_type == "application/json"
    data: dict[str, Any] = request.json()
    assert data["kind"] == "unhandled"
    assert data["title"] == "Unhandled ValueError in app"
    assert data["text"].startswith("Unhandled ValueError in app\n")
    assert "Traceback" in data["traceback"]
    assert {"name": "Environment", "value": "production"} in data["fields"]


def test_discord_embed_stays_within_limits(live: FakeWebhook) -> None:
    long_text = "word " * 3000
    with faultbell.context(**{f"extra_{n}": long_text for n in range(30)}):
        faultbell.capture(raise_and_catch(RuntimeError(long_text)))
    faultbell.flush(5)
    embed = live.wait_for(1)[0].embed()
    size = len(embed["title"]) + len(embed["description"]) + len(embed["footer"]["text"])
    size += len(embed["author"]["name"]) + sum(len(f["name"]) + len(f["value"]) for f in embed["fields"])
    assert size <= 6000
    assert len(embed["fields"]) <= 25
    assert all(len(f["value"]) <= 1024 for f in embed["fields"])


def test_exception_chain_is_shown(live: FakeWebhook) -> None:
    def load() -> None:
        try:
            {}["missing"]
        except KeyError as exc:
            raise RuntimeError("could not load settings") from exc

    faultbell.capture(raise_and_catch_call(load), handled=False)
    faultbell.flush(5)
    fields = live.wait_for(1)[0].fields()
    assert "KeyError" in fields["Caused by"]
    assert live.received[0].embed()["color"] == 0xE5484D


def raise_and_catch_call(fn: Any) -> BaseException:
    try:
        fn()
    except BaseException as exc:  # noqa: B036
        return exc
    raise AssertionError("did not raise")


def test_sender_survives_a_broken_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken(Transport):
        def encode(self, card: Card) -> tuple[bytes, str]:
            raise RuntimeError("encoder bug")

    monkeypatch.setenv("FAULTBELL_ENV", "production")
    faultbell.configure(transport=Broken("http://127.0.0.1:1/x"))
    faultbell.capture(fail_in_checkout(1))
    assert faultbell.flush(5)
    assert faultbell.stats()["failed"] == 1
    assert sender.is_running()
