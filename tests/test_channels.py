"""Every channel end to end: a card goes from capture() through the sender thread to a real local server
(HTTP for the chat/paging channels, a small SMTP server for email)."""

from __future__ import annotations

import base64
import email
import email.policy
import os
import socketserver
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

import faultbell
from conftest import FakeWebhook, raise_and_catch, start_fake_webhook
from faultbell import (
    CallbackTransport,
    Card,
    Delivery,
    DiscordTransport,
    EmailTransport,
    GoogleChatTransport,
    JsonWebhookTransport,
    PagerDutyTransport,
    SlackTransport,
    TeamsTransport,
    TelegramTransport,
    channels_from_env,
    config,
)
from faultbell.transports import parse_kinds, transport_named


# --------------------------------------------------------------------------------------------------
# a tiny SMTP server
# --------------------------------------------------------------------------------------------------
@dataclass
class Mail:
    sender: str
    recipients: list[str]
    data: bytes

    def parsed(self) -> email.message.EmailMessage:
        msg = email.message_from_bytes(self.data, policy=email.policy.default)
        assert isinstance(msg, email.message.EmailMessage)
        return msg


@dataclass
class FakeSmtp:
    port: int = 0
    mails: list[Mail] = field(default_factory=list)
    logins: list[tuple[str, str]] = field(default_factory=list)
    reject: set[str] = field(default_factory=set)
    done: threading.Event = field(default_factory=threading.Event)


def _smtp_handler(box: FakeSmtp) -> type[socketserver.StreamRequestHandler]:
    class Handler(socketserver.StreamRequestHandler):
        def say(self, line: str) -> None:
            self.wfile.write(line.encode() + b"\r\n")

        def handle(self) -> None:
            self.say("220 fake.example.com ESMTP")
            sender, rcpts = "", []
            while True:
                raw = self.rfile.readline()
                if not raw:
                    return
                line = raw.decode().rstrip("\r\n")
                verb = line.split(" ", 1)[0].upper()
                if verb == "EHLO":
                    self.wfile.write(b"250-fake.example.com\r\n250-AUTH PLAIN\r\n250 SIZE 10000000\r\n")
                elif verb == "HELO":
                    self.say("250 fake.example.com")
                elif verb == "AUTH":
                    blob = line.split(" ", 2)[2]
                    _, user, password = base64.b64decode(blob).decode().split("\0")
                    box.logins.append((user, password))
                    self.say("235 ok")
                elif verb == "MAIL":
                    sender, rcpts = line.split(":", 1)[1].split()[0].strip("<>"), []
                    self.say("250 ok")
                elif verb == "RCPT":
                    rcpt = line.split(":", 1)[1].strip().strip("<>")
                    if rcpt in box.reject:
                        self.say("550 no such user")
                    else:
                        rcpts.append(rcpt)
                        self.say("250 ok")
                elif verb == "DATA":
                    self.say("354 go ahead")
                    body = []
                    while (chunk := self.rfile.readline()) not in (b".\r\n", b""):
                        body.append(chunk[1:] if chunk.startswith(b"..") else chunk)
                    box.mails.append(Mail(sender, rcpts, b"".join(body)))
                    box.done.set()
                    self.say("250 queued")
                elif verb in ("RSET", "NOOP"):
                    self.say("250 ok")
                elif verb == "QUIT":
                    self.say("221 bye")
                    return
                else:
                    self.say("502 not implemented")

    return Handler


@pytest.fixture
def smtp() -> Iterator[FakeSmtp]:
    box = FakeSmtp()
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _smtp_handler(box))
    server.daemon_threads = True
    box.port = server.server_address[1]
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        yield box
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAULTBELL_ENV", "production")


@pytest.fixture
def second_webhook() -> Iterator[FakeWebhook]:
    hook, server = start_fake_webhook()
    try:
        yield hook
    finally:
        server.shutdown()
        server.server_close()


def boom(message: str = "payment declined for order 42") -> BaseException:
    return raise_and_catch(ValueError(message))


def base_url(hook: FakeWebhook) -> str:
    return hook.url.rsplit("/api/hook", 1)[0]


# --------------------------------------------------------------------------------------------------
# email
# --------------------------------------------------------------------------------------------------
def test_email_end_to_end(prod: None, smtp: FakeSmtp) -> None:
    channel = EmailTransport(
        "ops@example.com, dev@example.com",
        host="127.0.0.1",
        port=smtp.port,
        sender="alerts@example.com",
        username="alerts",
        password="app-password",
        security="none",
        subject_prefix="[shop] ",
    )
    faultbell.configure(channels=[channel])
    faultbell.capture(boom("charge failed for asha@example.com"))
    assert faultbell.flush(5)

    [mail] = smtp.mails
    assert smtp.logins == [("alerts", "app-password")]
    assert mail.sender == "alerts@example.com"
    assert mail.recipients == ["ops@example.com", "dev@example.com"]
    msg = mail.parsed()
    assert msg["Subject"] == "[shop] Handled ValueError in app"
    assert msg["To"] == "ops@example.com, dev@example.com"
    assert msg["X-Faultbell-Fingerprint"]
    text = msg.get_body(preferencelist=("plain",))
    html_part = msg.get_body(preferencelist=("html",))
    assert text is not None and html_part is not None
    body = text.get_content()
    assert "charge failed for" in body and "asha@example.com" not in body  # scrubbed
    assert "Environment: production" in body
    assert "<h3" in html_part.get_content()
    [attachment] = list(msg.iter_attachments())
    assert attachment.get_filename().startswith("traceback-")
    assert "Traceback (most recent call last)" in attachment.get_content()
    assert faultbell.stats()["sent"] == 1


def test_email_rejected_recipient_counts_as_failed(prod: None, smtp: FakeSmtp) -> None:
    smtp.reject.add("nobody@example.com")
    faultbell.configure(
        channels=[EmailTransport("nobody@example.com", host="127.0.0.1", port=smtp.port, security="none")]
    )
    faultbell.capture(boom())
    assert faultbell.flush(5)
    assert smtp.mails == []
    assert faultbell.stats()["failed"] == 1


def test_email_unreachable_server_never_raises(prod: None) -> None:
    faultbell.configure(
        channels=[EmailTransport("ops@example.com", host="127.0.0.1", port=1, security="none")], timeout=0.5
    )
    faultbell.capture(boom())
    assert faultbell.flush(5)
    assert faultbell.stats()["failed"] == 1


def test_email_settings() -> None:
    assert EmailTransport("a@example.com", port=465).security == "ssl"
    assert EmailTransport("a@example.com", port=587).security == "starttls"
    assert EmailTransport("a@example.com", port=25).security == "none"
    assert EmailTransport("a@example.com", username="u").port == 587
    assert EmailTransport(["a@example.com", " "], host="smtp.example.com").to == ["a@example.com"]
    assert EmailTransport("a@example.com", host="smtp.example.com", port=2525).destination() == (
        "smtp://smtp.example.com:2525"
    )
    assert not EmailTransport("", host="smtp.example.com").ready()
    assert EmailTransport("", host="smtp.example.com").destination() == ""
    assert EmailTransport("a@example.com", username="me@example.com").sender == "me@example.com"
    with pytest.raises(ValueError):
        EmailTransport("a@example.com", security="tls1")


# --------------------------------------------------------------------------------------------------
# chat and paging channels
# --------------------------------------------------------------------------------------------------
def test_slack_block_kit_message(prod: None, webhook: FakeWebhook) -> None:
    faultbell.configure(channels=[SlackTransport(webhook.url)])
    with faultbell.context(tenant="Demo Café & Co", feature="checkout"):
        faultbell.capture(boom("bad <b>markup</b> & more"))
    assert faultbell.flush(5)

    [request] = webhook.wait_for(1)
    assert request.content_type == "application/json"
    payload = request.json()
    assert payload["text"] == "Handled ValueError in app"
    [attachment] = payload["attachments"]
    assert attachment["color"] == "#F5A524"
    blocks = attachment["blocks"]
    assert blocks[0] == {
        "type": "header",
        "text": {"type": "plain_text", "text": "Handled ValueError in app"},
    }
    assert "bad &lt;b&gt;markup&lt;/b&gt; &amp; more" in blocks[1]["text"]["text"]
    field_texts = [f["text"] for b in blocks if "fields" in b for f in b["fields"]]
    assert "*Tenant*\nDemo Café &amp; Co" in field_texts
    assert all(len(b.get("fields", [])) <= 10 for b in blocks)
    assert any("*Traceback*" in b.get("text", {}).get("text", "") for b in blocks)
    assert blocks[-1]["type"] == "context"
    assert blocks[-1]["elements"][0]["text"].startswith("Faultbell · app · production · alert ")


def test_slack_respects_size_limits() -> None:
    transport = SlackTransport("http://127.0.0.1:9/x")
    card = _card(message="x" * 10_000, traceback="y" * 50_000, extra_fields=40)
    payload = transport.payload(card)
    blocks = payload["attachments"][0]["blocks"]
    assert len(blocks) <= SlackTransport.BLOCKS_MAX
    for block in blocks:
        if "text" in block and block["type"] == "section":
            assert len(block["text"]["text"]) <= SlackTransport.TEXT_MAX
        for f in block.get("fields", []):
            assert len(f["text"]) <= SlackTransport.FIELD_MAX


def test_teams_adaptive_card(prod: None, webhook: FakeWebhook) -> None:
    faultbell.configure(channels=[TeamsTransport(webhook.url)])
    faultbell.capture(boom(), handled=False)
    assert faultbell.flush(5)

    payload = webhook.wait_for(1)[0].json()
    assert payload["type"] == "message"
    [attachment] = payload["attachments"]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = attachment["content"]
    assert card["type"] == "AdaptiveCard" and card["version"] == "1.4"
    title = card["body"][0]
    assert title["text"] == "Unhandled ValueError in app" and title["color"] == "Attention"
    facts = next(b["facts"] for b in card["body"] if b["type"] == "FactSet")
    assert {"title": "Environment", "value": "production"} in facts
    assert any(b.get("text") == "Traceback" for b in card["body"])


def test_google_chat_text(prod: None, webhook: FakeWebhook) -> None:
    faultbell.configure(channels=[GoogleChatTransport(webhook.url)])
    faultbell.capture(boom())
    assert faultbell.flush(5)
    text = webhook.wait_for(1)[0].json()["text"]
    assert text.startswith("*Handled ValueError in app*\n```payment declined for order 42```")
    assert "*Environment:* production" in text
    assert len(text) <= GoogleChatTransport.TEXT_MAX


def test_telegram_document_with_html_caption(prod: None, webhook: FakeWebhook) -> None:
    channel = TelegramTransport(
        "123456:test-token", "-1001234", thread_id=7, silent=True, api_base=base_url(webhook)
    )
    faultbell.configure(channels=[channel])
    faultbell.capture(boom("<script> & friends"))
    assert faultbell.flush(5)

    [request] = webhook.wait_for(1)
    assert request.path == "/bot123456:test-token/sendDocument"
    parts = request.parts()
    assert parts["chat_id"][1] == b"-1001234"
    assert parts["parse_mode"][1] == b"HTML"
    assert parts["message_thread_id"][1] == b"7"
    assert parts["disable_notification"][1] == b"true"
    caption = parts["caption"][1].decode()
    assert caption.startswith("<b>Handled ValueError in app</b>")
    assert "&lt;script&gt; &amp; friends" in caption
    assert len(caption) <= TelegramTransport.CAPTION_MAX
    filename, document = parts["document"]
    assert filename is not None and filename.endswith(".txt")
    assert b"Traceback (most recent call last)" in document


def test_telegram_caption_never_cuts_a_tag() -> None:
    transport = TelegramTransport("1:t", "2")
    caption = transport.caption(_card(message="m" * 280, extra_fields=40))
    assert len(caption) <= TelegramTransport.CAPTION_MAX
    assert caption.count("<b>") == caption.count("</b>")
    assert not TelegramTransport("", "2").ready() and TelegramTransport("", "2").destination() == ""


def test_telegram_429_retry_after_in_parameters(prod: None, webhook: FakeWebhook) -> None:
    webhook.statuses = [429, 200]
    webhook.body_429 = {"ok": False, "error_code": 429, "parameters": {"retry_after": 0.01}}
    faultbell.configure(channels=[TelegramTransport("1:t", "2", api_base=base_url(webhook))])
    faultbell.capture(boom())
    assert faultbell.flush(5)
    assert len(webhook.wait_for(2)) == 2
    assert faultbell.stats()["sent"] == 1


def test_pagerduty_pages_for_crashes_and_jobs_only(prod: None, webhook: FakeWebhook) -> None:
    faultbell.configure(channels=[PagerDutyTransport("routing-key-1", url=webhook.url)])
    faultbell.capture(boom("handled, no page"))
    faultbell.capture(boom("crash"), handled=False)
    with faultbell.job_run("nightly", total=2, noun="tenants") as run:
        run.fail(raise_and_catch(TimeoutError("slow")), tenant="Demo Café", tenant_id="t-1")
    assert faultbell.flush(5)

    requests = webhook.wait_for(2)
    assert len(requests) == 2
    crash, job = (r.json() for r in requests)
    assert crash["routing_key"] == "routing-key-1"
    assert crash["event_action"] == "trigger"
    assert crash["dedup_key"].startswith("faultbell-")
    assert crash["payload"]["severity"] == "critical"
    assert crash["payload"]["summary"] == "Unhandled ValueError in app: crash"
    assert crash["payload"]["group"] == "production"
    assert "Traceback" in crash["payload"]["custom_details"]["traceback"]
    assert job["payload"]["severity"] == "error" and job["payload"]["class"] == "job"


def test_pagerduty_custom_kinds_and_severity() -> None:
    transport = PagerDutyTransport("k", kinds=None, severity={"handled": "info"})
    assert transport.wants(_card(kind="handled"))
    assert transport.payload(_card(kind="handled"))["payload"]["severity"] == "info"
    assert not PagerDutyTransport("").ready()


def test_json_webhook_as_extra_channel(prod: None, webhook: FakeWebhook) -> None:
    faultbell.configure(channels=[JsonWebhookTransport(webhook.url, headers={"X-Token": "abc"})])
    faultbell.capture(boom())
    assert faultbell.flush(5)
    [request] = webhook.wait_for(1)
    assert request.headers["x-token"] == "abc"
    assert request.json()["kind"] == "handled"


# --------------------------------------------------------------------------------------------------
# your own function
# --------------------------------------------------------------------------------------------------
def test_callback_channel(prod: None) -> None:
    got: list[Card] = []
    faultbell.configure(channels=[CallbackTransport(got.append, name="sms")])
    faultbell.capture(boom())
    assert faultbell.flush(5)
    [card] = got
    assert card.title == "Handled ValueError in app"
    assert card.as_dict()["environment"] == "production"
    assert faultbell.stats()["sent"] == 1
    assert CallbackTransport(got.append, name="sms").destination() == "callback:sms"


def test_callback_failures_are_counted_not_raised(prod: None) -> None:
    def explode(card: Card) -> None:
        raise RuntimeError("queue down")

    faultbell.configure(
        channels=[
            CallbackTransport(explode, name="a"),
            CallbackTransport(lambda card: Delivery(503), name="b"),
        ]
    )
    faultbell.capture(boom())
    assert faultbell.flush(5)
    assert faultbell.stats()["failed"] == 2


# --------------------------------------------------------------------------------------------------
# several channels at once
# --------------------------------------------------------------------------------------------------
def test_one_card_fans_out_to_every_channel(
    prod: None,
    webhook: FakeWebhook,
    second_webhook: FakeWebhook,
    smtp: FakeSmtp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAULTBELL_WEBHOOK_URL", webhook.url)  # the main webhook: Discord
    got: list[Card] = []
    faultbell.configure(
        channels=[
            SlackTransport(second_webhook.url),
            EmailTransport("ops@example.com", host="127.0.0.1", port=smtp.port, security="none"),
            CallbackTransport(got.append),
        ]
    )
    assert [t.name for t, _ in faultbell.active_channels()] == ["discord", "slack", "email", "callback"]
    faultbell.capture(boom())
    faultbell.capture(boom())  # the same problem again: de-duplicated before fan-out
    assert faultbell.flush(5)

    assert len(webhook.wait_for(1)) == 1 and "embeds" in webhook.received[0].discord()[0]
    assert len(second_webhook.wait_for(1)) == 1 and "attachments" in second_webhook.received[0].json()
    assert len(smtp.mails) == 1
    assert len(got) == 1
    stats = faultbell.stats()
    assert stats["sent"] == 4 and stats["suppressed"] == 1


def test_a_failing_channel_does_not_stop_the_others(prod: None, webhook: FakeWebhook) -> None:
    webhook.statuses = [500]
    got: list[Card] = []
    faultbell.configure(channels=[SlackTransport(webhook.url), CallbackTransport(got.append)])
    faultbell.capture(boom())
    assert faultbell.flush(5)
    assert len(got) == 1
    assert faultbell.stats()["failed"] == 1 and faultbell.stats()["sent"] == 1


def test_kinds_filter_per_channel(prod: None, webhook: FakeWebhook) -> None:
    jobs_only: list[Card] = []
    everything: list[Card] = []
    faultbell.configure(
        channels=[
            CallbackTransport(jobs_only.append, kinds="job"),
            CallbackTransport(everything.append, name="all"),
        ]
    )
    faultbell.capture(boom())
    with faultbell.job_run("sync", total=1) as run:
        run.fail(raise_and_catch(TimeoutError("slow")), tenant_id="t-1")
    assert faultbell.flush(5)
    assert [c.kind for c in jobs_only] == ["job"]
    assert sorted(c.kind for c in everything) == ["handled", "job"]


def test_parse_kinds() -> None:
    assert parse_kinds(None) is None
    assert parse_kinds("") is None
    assert parse_kinds("Unhandled, job") == frozenset({"unhandled", "job"})
    assert parse_kinds(["handled"]) == frozenset({"handled"})
    with pytest.raises(ValueError, match="unknown card kind"):
        parse_kinds("errors")


def test_same_destination_is_sent_once(prod: None, webhook: FakeWebhook) -> None:
    faultbell.configure(webhook_url=webhook.url, channels=[DiscordTransport(webhook.url)])
    assert len(faultbell.active_channels()) == 1
    faultbell.capture(boom())
    assert faultbell.flush(5)
    assert len(webhook.wait_for(1)) == 1


# --------------------------------------------------------------------------------------------------
# configuration from the environment
# --------------------------------------------------------------------------------------------------
def test_channels_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {
        "FAULTBELL_DISCORD_URL": "https://discord.example.com/hook",
        "FAULTBELL_SLACK_URL": "https://slack.example.com/hook",
        "FAULTBELL_SLACK_KINDS": "unhandled,job",
        "FAULTBELL_TEAMS_URL": "https://teams.example.com/hook",
        "FAULTBELL_GOOGLE_CHAT_URL": "https://chat.example.com/hook",
        "FAULTBELL_JSON_URL": "https://ops.example.com/alerts",
        "FAULTBELL_TELEGRAM_BOT_TOKEN": "1:t",
        "FAULTBELL_TELEGRAM_CHAT_ID": "-100",
        "FAULTBELL_TELEGRAM_THREAD_ID": "9",
        "FAULTBELL_PAGERDUTY_ROUTING_KEY": "rk",
        "FAULTBELL_EMAIL_TO": "ops@example.com,dev@example.com",
        "FAULTBELL_EMAIL_FROM": "alerts@example.com",
        "FAULTBELL_SMTP_HOST": "smtp.example.com",
        "FAULTBELL_SMTP_PORT": "465",
        "FAULTBELL_SMTP_USER": "alerts",
        "FAULTBELL_SMTP_PASSWORD": "pw",
        "FAULTBELL_EMAIL_SUBJECT_PREFIX": "[api] ",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    found = {t.name: t for t in channels_from_env()}
    assert list(found) == [
        "discord",
        "slack",
        "teams",
        "google_chat",
        "json",
        "telegram",
        "pagerduty",
        "email",
    ]
    assert found["slack"].kinds == frozenset({"unhandled", "job"})
    assert found["discord"].kinds is None
    telegram = found["telegram"]
    assert isinstance(telegram, TelegramTransport) and telegram.thread_id == 9
    assert found["pagerduty"].kinds == frozenset({"unhandled", "job"})
    mail = found["email"]
    assert isinstance(mail, EmailTransport)
    assert (mail.host, mail.port, mail.security, mail.sender) == (
        "smtp.example.com",
        465,
        "ssl",
        "alerts@example.com",
    )
    assert mail.to == ["ops@example.com", "dev@example.com"] and mail.subject_prefix == "[api]"

    # local machines stay quiet unless asked
    assert faultbell.active_channels() == [] and not faultbell.enabled()
    monkeypatch.setenv("FAULTBELL_SEND_LOCAL", "1")
    assert len(faultbell.active_channels()) == 8 and faultbell.enabled()
    # explicit channels replace the environment's
    faultbell.configure(channels=[CallbackTransport(lambda card: None)])
    assert [t.name for t, _ in faultbell.active_channels()] == ["callback"]


def test_env_only_slack_end_to_end(webhook: FakeWebhook, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAULTBELL_ENV", "production")
    monkeypatch.setenv("FAULTBELL_SLACK_URL", webhook.url)
    assert config.webhook_url() == ""  # no main webhook, yet alerting is on
    faultbell.capture(boom())
    assert faultbell.flush(5)
    assert webhook.wait_for(1)[0].json()["text"] == "Handled ValueError in app"


def test_main_webhook_transport_by_name() -> None:
    assert isinstance(transport_named("slack"), SlackTransport)
    assert isinstance(transport_named("Teams"), TeamsTransport)
    assert isinstance(transport_named("google-chat"), GoogleChatTransport)
    assert isinstance(transport_named("nope"), DiscordTransport)
    assert repr(SlackTransport()) == "<SlackTransport>"


def test_enabled_is_false_when_channels_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> list[Any]:
        raise RuntimeError("bad config")

    monkeypatch.setattr(config, "channels", broken)
    assert faultbell.config.enabled() is False


# --------------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------------
def _card(
    *, kind: str = "handled", message: str = "boom", traceback: str = "Traceback ...", extra_fields: int = 0
) -> Card:
    from datetime import datetime, timezone

    from faultbell import Field

    fields = [Field("Environment", "production"), Field("Stack", "a.py:1 in f", inline=False, code=True)]
    fields += [Field(f"Extra {i}", "v" * 300) for i in range(extra_fields)]
    return Card(
        kind=kind,
        title="Handled ValueError in app",
        message=message,
        fields=fields,
        environment="production",
        service="app",
        fingerprint="abc123",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        seen=1,
        attachment_name="traceback-abc123.txt",
        attachment=traceback.encode(),
    )


class RecordingSmtp:
    """Stands in for smtplib.SMTP / SMTP_SSL to check the TLS and error paths without a TLS server."""

    calls: list[str] = []
    fail_with: BaseException | None = None

    def __init__(self, host: str, port: int, timeout: float = 0, context: Any = None) -> None:
        RecordingSmtp.calls.append(f"connect {type(self).__name__} {host}:{port} ssl={context is not None}")

    def starttls(self, context: Any = None) -> None:
        RecordingSmtp.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        RecordingSmtp.calls.append(f"login {user}")

    def send_message(self, msg: Any, from_addr: str, to_addrs: list[str]) -> None:
        if RecordingSmtp.fail_with is not None:
            raise RecordingSmtp.fail_with
        RecordingSmtp.calls.append(f"send {from_addr} -> {','.join(to_addrs)}")

    def quit(self) -> None:
        if RecordingSmtp.fail_with is not None:
            raise OSError("already closed")
        RecordingSmtp.calls.append("quit")

    def close(self) -> None:
        RecordingSmtp.calls.append("close")


class RecordingSmtpSsl(RecordingSmtp):
    pass


@pytest.mark.parametrize(
    ("port", "opening"),
    [
        (587, ["connect RecordingSmtp smtp.example.com:587 ssl=False", "starttls", "login u"]),
        (465, ["connect RecordingSmtpSsl smtp.example.com:465 ssl=True", "login u"]),
    ],
)
def test_email_tls_modes(monkeypatch: pytest.MonkeyPatch, port: int, opening: list[str]) -> None:
    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", RecordingSmtp)
    monkeypatch.setattr(smtplib, "SMTP_SSL", RecordingSmtpSsl)
    RecordingSmtp.calls, RecordingSmtp.fail_with = [], None
    transport = EmailTransport(
        "ops@example.com", host="smtp.example.com", port=port, username="u", password="p"
    )
    assert transport.send(_card(), transport.destination(), 1.0).ok
    assert RecordingSmtp.calls == [*opening, "send u -> ops@example.com", "quit"]


def test_email_server_errors_become_failed_deliveries(monkeypatch: pytest.MonkeyPatch) -> None:
    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", RecordingSmtp)
    transport = EmailTransport("ops@example.com", host="smtp.example.com", port=25)
    RecordingSmtp.calls, RecordingSmtp.fail_with = [], smtplib.SMTPDataError(452, b"mailbox full")
    assert transport.send(_card(), "", 1.0).status == 452
    assert RecordingSmtp.calls[-1] == "close"  # quit failed, the connection is closed instead
    RecordingSmtp.fail_with = smtplib.SMTPRecipientsRefused({"ops@example.com": (550, b"no")})
    assert transport.send(_card(), "", 1.0).status == 550
    RecordingSmtp.fail_with = None


# --------------------------------------------------------------------------------------------------
# python -m faultbell
# --------------------------------------------------------------------------------------------------
def _cli(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("FAULTBELL_")}
    return subprocess.run(
        [sys.executable, "-m", "faultbell", *args],
        env={**clean, **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cli_lists_channels_without_secrets(webhook: FakeWebhook) -> None:
    result = _cli(
        "channels",
        FAULTBELL_ENV="production",
        FAULTBELL_SLACK_URL=webhook.url + "/T000/B000/secret-part",
        FAULTBELL_EMAIL_TO="ops@example.com",
        FAULTBELL_SMTP_HOST="smtp.example.com",
        FAULTBELL_EMAIL_KINDS="unhandled",
    )
    assert result.returncode == 0, result.stderr
    assert "Environment: production" in result.stdout
    assert "slack" in result.stdout and "http://127.0.0.1/…" in result.stdout
    assert "secret-part" not in result.stdout
    assert "smtp://smtp.example.com:25" in result.stdout and "kinds: unhandled" in result.stdout


def test_cli_test_sends_one_alert_everywhere(webhook: FakeWebhook, second_webhook: FakeWebhook) -> None:
    result = _cli(
        "test",
        "--timeout",
        "10",
        FAULTBELL_ENV="production",
        FAULTBELL_WEBHOOK_URL=webhook.url,
        FAULTBELL_SLACK_URL=second_webhook.url,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Sent: 2  Failed: 0" in result.stdout
    assert webhook.wait_for(1)[0].embed()["title"] == "Unhandled FaultbellTestAlert in app"
    assert second_webhook.wait_for(1)[0].json()["text"] == "Unhandled FaultbellTestAlert in app"


def test_cli_reports_failures_and_quiet_environments(webhook: FakeWebhook) -> None:
    webhook.statuses = [500]
    failed = _cli("test", FAULTBELL_ENV="production", FAULTBELL_SLACK_URL=webhook.url)
    assert failed.returncode == 1 and "Failed: 1" in failed.stdout
    local = _cli("channels", FAULTBELL_SLACK_URL=webhook.url)
    assert local.returncode == 1 and "local environment" in local.stdout
    nothing = _cli("test", FAULTBELL_ENV="production")
    assert nothing.returncode == 1 and "no channel is configured" in nothing.stdout


def test_cli_in_process(
    webhook: FakeWebhook, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from faultbell.__main__ import main

    monkeypatch.setenv("FAULTBELL_ENV", "production")
    assert main(["test"]) == 1  # nothing configured
    monkeypatch.setenv("FAULTBELL_GOOGLE_CHAT_URL", webhook.url)
    assert main(["channels"]) == 0
    assert main(["test", "--timeout", "5"]) == 0
    out = capsys.readouterr().out
    assert "google_chat" in out and "Sent: 1  Failed: 0" in out
    assert webhook.wait_for(1)[0].json()["text"].startswith("*Unhandled FaultbellTestAlert in app*")
    monkeypatch.setattr(faultbell.sender, "flush", lambda timeout: False)
    assert main(["test", "--timeout", "0"]) == 1
    assert "Timed out" in capsys.readouterr().err
