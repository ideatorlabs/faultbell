"""Channels beyond Discord: Slack, Microsoft Teams, Google Chat, Telegram, PagerDuty, email and your own
function. Each one is a ``Transport``; list several and every card goes to all
of them (see ``faultbell.configure`` ``channels=`` or the ``FAULTBELL_*`` variables read by
``channels_from_env``).

Everything here uses the standard library only. Card text arrives already scrubbed.
"""

from __future__ import annotations

import html
import json
import os
import smtplib
import socket
import ssl
import uuid
from collections.abc import Callable, Iterable, Mapping
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any

from .card import Card
from .transports import Delivery, DiscordTransport, JsonWebhookTransport, Transport

__all__ = [
    "CallbackTransport",
    "EmailTransport",
    "GoogleChatTransport",
    "PagerDutyTransport",
    "SlackTransport",
    "TeamsTransport",
    "TelegramTransport",
    "channels_from_env",
]

#: How much of the traceback chat channels show inline (they can't attach a file through a webhook).
TRACEBACK_TAIL = 1500
COLORS = {"unhandled": "#E5484D", "handled": "#F5A524", "job": "#7C66DC"}


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _tail(card: Card, limit: int = TRACEBACK_TAIL) -> str:
    text = card.attachment.decode("utf-8", "replace").strip()
    return text if len(text) <= limit else "…" + text[-(limit - 1) :]


def _footer(card: Card) -> str:
    return f"Faultbell · {card.service} · {card.environment} · alert {card.fingerprint}"


def _json(payload: Mapping[str, Any]) -> tuple[bytes, str]:
    return json.dumps(payload, ensure_ascii=False).encode(), "application/json"


# --------------------------------------------------------------------------------------------------
# Slack
# --------------------------------------------------------------------------------------------------
class SlackTransport(Transport):
    """A Slack incoming webhook: Block Kit message with a colour bar, fields and the traceback tail."""

    name = "slack"
    HEADER_MAX, TEXT_MAX, FIELD_MAX, FIELDS_MAX, BLOCKS_MAX = 150, 3000, 2000, 10, 50

    @staticmethod
    def escape(text: str) -> str:
        """Slack mrkdwn needs ``&``, ``<`` and ``>`` escaped (and nothing else)."""
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _code(self, text: str, limit: int) -> str:
        body = self.escape(text.replace("```", "'''"))
        return "```" + _cut(body, limit - 6) + "```"

    def payload(self, card: Card) -> dict[str, Any]:
        blocks: list[dict[str, Any]] = [
            {"type": "header", "text": {"type": "plain_text", "text": _cut(card.title, self.HEADER_MAX)}},
            {"type": "section", "text": {"type": "mrkdwn", "text": self._code(card.message or "-", 1000)}},
        ]
        short = [f for f in card.fields if f.inline and not f.code]
        long = [f for f in card.fields if not (f.inline and not f.code)]
        for start in range(0, len(short), self.FIELDS_MAX):
            chunk = short[start : start + self.FIELDS_MAX]
            blocks.append(
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": _cut(f"*{self.escape(f.name)}*\n{self.escape(f.value)}", self.FIELD_MAX),
                        }
                        for f in chunk
                    ],
                }
            )
        for f in long:
            value = self._code(f.value, self.TEXT_MAX - 200) if f.code else self.escape(f.value)
            text = _cut(f"*{self.escape(f.name)}*\n{value}", self.TEXT_MAX)
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*Traceback*\n" + self._code(_tail(card), self.TEXT_MAX)},
            }
        )
        blocks = blocks[: self.BLOCKS_MAX - 1]
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": self.escape(_footer(card))}]}
        )
        return {
            "text": _cut(card.title, 300),  # notification / fallback text
            "attachments": [{"color": COLORS.get(card.kind, COLORS["handled"]), "blocks": blocks}],
        }

    def encode(self, card: Card) -> tuple[bytes, str]:
        return _json(self.payload(card))


# --------------------------------------------------------------------------------------------------
# Microsoft Teams
# --------------------------------------------------------------------------------------------------
class TeamsTransport(Transport):
    """A Microsoft Teams webhook (a Workflows "post to a channel when a webhook request is received"
    URL, or a classic incoming webhook): one Adaptive Card."""

    name = "teams"
    TEXT_MAX = 4000

    def card(self, card: Card) -> dict[str, Any]:
        facts = [
            {"title": f.name, "value": _cut(f.value, 500)} for f in card.fields if f.inline and not f.code
        ]
        body: list[dict[str, Any]] = [
            {
                "type": "TextBlock",
                "text": _cut(card.title, 300),
                "weight": "Bolder",
                "size": "Medium",
                "color": "Attention" if card.kind == "unhandled" else "Warning",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": _cut(card.message or "-", 1000),
                "fontType": "Monospace",
                "wrap": True,
            },
        ]
        if facts:
            body.append({"type": "FactSet", "facts": facts})
        for f in card.fields:
            if f.inline and not f.code:
                continue
            body.append(
                {"type": "TextBlock", "text": f.name, "weight": "Bolder", "wrap": True, "spacing": "Medium"}
            )
            body.append(
                {
                    "type": "TextBlock",
                    "text": _cut(f.value, self.TEXT_MAX),
                    "fontType": "Monospace" if f.code else "Default",
                    "wrap": True,
                }
            )
        body.append({"type": "TextBlock", "text": "Traceback", "weight": "Bolder", "spacing": "Medium"})
        body.append({"type": "TextBlock", "text": _tail(card), "fontType": "Monospace", "wrap": True})
        body.append(
            {"type": "TextBlock", "text": _footer(card), "isSubtle": True, "size": "Small", "wrap": True}
        )
        return {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.4",
            "msteams": {"width": "Full"},
            "body": body,
        }

    def payload(self, card: Card) -> dict[str, Any]:
        return {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "contentUrl": None,
                    "content": self.card(card),
                }
            ],
        }

    def encode(self, card: Card) -> tuple[bytes, str]:
        return _json(self.payload(card))


# --------------------------------------------------------------------------------------------------
# Google Chat
# --------------------------------------------------------------------------------------------------
class GoogleChatTransport(Transport):
    """A Google Chat space webhook: one formatted text message."""

    name = "google_chat"
    TEXT_MAX = 4000

    def text(self, card: Card) -> str:
        rows = [f"*{card.title}*", "```" + _cut((card.message or "-").replace("```", "'''"), 800) + "```"]
        rows += [f"*{f.name}:* {f.value}" for f in card.fields if f.inline and not f.code]
        tail = _tail(card, 1200).replace("```", "'''")
        rows += ["*Traceback*", f"```{tail}```", f"_{_footer(card)}_"]
        return _cut("\n".join(rows), self.TEXT_MAX)

    def encode(self, card: Card) -> tuple[bytes, str]:
        return _json({"text": self.text(card)})


# --------------------------------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------------------------------
class TelegramTransport(Transport):
    """A Telegram bot message to a chat (user, group or channel): an HTML caption with the traceback
    attached as a file (``sendDocument``)."""

    name = "telegram"
    CAPTION_MAX = 1024

    def __init__(
        self,
        bot_token: str,
        chat_id: str | int,
        *,
        thread_id: int | None = None,
        silent: bool = False,
        api_base: str = "https://api.telegram.org",
        kinds: Iterable[str] | str | None = None,
    ) -> None:
        super().__init__(None, kinds=kinds)
        self.bot_token = (bot_token or "").strip()
        self.chat_id = str(chat_id).strip()
        self.thread_id = thread_id
        self.silent = silent
        self.api_base = api_base.rstrip("/")

    def ready(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def destination(self) -> str:
        if not self.ready():
            return ""
        return f"{self.api_base}/bot{self.bot_token}/sendDocument"

    def caption(self, card: Card) -> str:
        esc = html.escape
        lines = [f"<b>{esc(card.title)}</b>", f"<code>{esc(_cut(card.message or '-', 300))}</code>"]
        for f in card.fields:
            if f.inline and not f.code and f.value != "-":
                lines.append(f"<b>{esc(f.name)}:</b> {esc(_cut(f.value, 120))}")
        lines.append(f"<i>{esc(_footer(card))}</i>")
        out: list[str] = []
        for line in lines:  # whole lines only, so no HTML tag is ever cut in half
            if len("\n".join([*out, line])) > self.CAPTION_MAX:
                break
            out.append(line)
        return "\n".join(out)

    def encode(self, card: Card) -> tuple[bytes, str]:
        boundary = "faultbell" + uuid.uuid4().hex
        fields: dict[str, str] = {
            "chat_id": self.chat_id,
            "caption": self.caption(card),
            "parse_mode": "HTML",
        }
        if self.thread_id is not None:
            fields["message_thread_id"] = str(self.thread_id)
        if self.silent:
            fields["disable_notification"] = "true"
        parts = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
            + value.encode()
            + b"\r\n"
            for key, value in fields.items()
        ]
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
            f'filename="{card.attachment_name}"\r\n'
            "Content-Type: text/plain; charset=utf-8\r\n\r\n".encode()
            + card.attachment
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        return b"".join(parts), f"multipart/form-data; boundary={boundary}"


# --------------------------------------------------------------------------------------------------
# PagerDuty
# --------------------------------------------------------------------------------------------------
class PagerDutyTransport(Transport):
    """PagerDuty Events API v2. Repeats of one problem share a ``dedup_key`` (the card's fingerprint), so
    PagerDuty groups them into one incident. By default only crashes and job summaries page someone."""

    name = "pagerduty"
    SEVERITY = {"unhandled": "critical", "job": "error", "handled": "warning"}

    def __init__(
        self,
        routing_key: str,
        *,
        url: str = "https://events.pagerduty.com/v2/enqueue",
        kinds: Iterable[str] | str | None = ("unhandled", "job"),
        severity: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(url, kinds=kinds)
        self.routing_key = (routing_key or "").strip()
        self.severity = {**self.SEVERITY, **(severity or {})}

    def ready(self) -> bool:
        return bool(self.routing_key and self.url)

    def payload(self, card: Card) -> dict[str, Any]:
        return {
            "routing_key": self.routing_key,
            "event_action": "trigger",
            "dedup_key": f"faultbell-{card.fingerprint}",
            "client": "Faultbell",
            "payload": {
                "summary": _cut(f"{card.title}: {card.message}", 1024),
                "source": card.service,
                "severity": self.severity.get(card.kind, "error"),
                "component": card.service,
                "group": card.environment,
                "class": card.kind,
                "timestamp": card.timestamp.isoformat(),
                "custom_details": {
                    **{f.name: _cut(f.value, 2000) for f in card.fields},
                    "traceback": _tail(card, 4000),
                },
            },
        }

    def encode(self, card: Card) -> tuple[bytes, str]:
        return _json(self.payload(card))


# --------------------------------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------------------------------
class EmailTransport(Transport):
    """Email through any SMTP server: a plain-text body (plus a simple HTML version) with the traceback
    attached. ``security`` is ``"starttls"``, ``"ssl"``, ``"none"`` or ``"auto"`` (465 -> ssl,
    587 -> starttls, anything else -> none)."""

    name = "email"

    def __init__(
        self,
        to: str | Iterable[str],
        *,
        host: str = "localhost",
        port: int | None = None,
        sender: str | None = None,
        username: str | None = None,
        password: str | None = None,
        security: str = "auto",
        subject_prefix: str = "",
        kinds: Iterable[str] | str | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        super().__init__(None, kinds=kinds)
        items = to.split(",") if isinstance(to, str) else list(to)
        self.to = [t.strip() for t in items if t and t.strip()]
        self.host = (host or "").strip()
        self.port = int(port) if port else (587 if username else 25)
        self.username = username or None
        self.password = password or None
        self.sender = sender or self.username or f"faultbell@{socket.gethostname() or 'localhost'}"
        self.security = self._security(security, self.port)
        self.subject_prefix = (subject_prefix or "").strip()
        self.ssl_context = ssl_context

    @staticmethod
    def _security(mode: str, port: int) -> str:
        mode = (mode or "auto").strip().lower()
        if mode not in ("auto", "starttls", "ssl", "none"):
            raise ValueError("security must be auto, starttls, ssl or none")
        if mode != "auto":
            return mode
        return {465: "ssl", 587: "starttls"}.get(port, "none")

    def ready(self) -> bool:
        return bool(self.to and self.host)

    def destination(self) -> str:
        return f"smtp://{self.host}:{self.port}" if self.ready() else ""

    def message(self, card: Card) -> EmailMessage:
        subject = " ".join(f"{self.subject_prefix} {card.title}".split())
        msg = EmailMessage()
        msg["Subject"] = _cut(subject, 200)
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.to)
        msg["Date"] = formatdate(localtime=False)
        msg["Message-ID"] = make_msgid(domain=(self.sender.rpartition("@")[2] or None))
        msg["X-Faultbell-Fingerprint"] = card.fingerprint

        lines = [card.title, "", card.message or "-", ""]
        for f in card.fields:
            if f.inline and not f.code:
                lines.append(f"{f.name}: {f.value}")
        for f in card.fields:
            if not (f.inline and not f.code):
                lines += ["", f"{f.name}:", *("    " + row for row in f.value.splitlines())]
        lines += ["", "The full traceback is attached.", "", "-- ", _footer(card)]
        msg.set_content("\n".join(lines))

        esc = html.escape
        rows = "".join(
            f"<tr><td style='padding:2px 12px 2px 0;color:#666'>{esc(f.name)}</td>"
            f"<td>{esc(f.value)}</td></tr>"
            for f in card.fields
            if f.inline and not f.code
        )
        blocks = "".join(
            f"<h4 style='margin:16px 0 4px'>{esc(f.name)}</h4><pre style='margin:0'>{esc(f.value)}</pre>"
            for f in card.fields
            if not (f.inline and not f.code)
        )
        color = COLORS.get(card.kind, COLORS["handled"])
        msg.add_alternative(
            f"<div style='font-family:system-ui,sans-serif;font-size:14px'>"
            f"<h3 style='margin:0 0 8px;border-left:4px solid {color};padding-left:8px'>"
            f"{esc(card.title)}</h3>"
            f"<pre style='background:#f6f6f6;padding:8px'>{esc(card.message or '-')}</pre>"
            f"<table>{rows}</table>{blocks}"
            f"<p style='color:#888;font-size:12px'>{esc(_footer(card))}</p></div>",
            subtype="html",
        )
        msg.add_attachment(card.attachment, maintype="text", subtype="plain", filename=card.attachment_name)
        return msg

    def send(self, card: Card, url: str, timeout: float) -> Delivery:
        msg = self.message(card)
        context = self.ssl_context or ssl.create_default_context()
        smtp: smtplib.SMTP
        if self.security == "ssl":
            smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=timeout, context=context)
        else:
            smtp = smtplib.SMTP(self.host, self.port, timeout=timeout)
        try:
            if self.security == "starttls":
                smtp.starttls(context=context)
            if self.username:
                smtp.login(self.username, self.password or "")
            smtp.send_message(msg, from_addr=self.sender, to_addrs=self.to)
        except smtplib.SMTPRecipientsRefused:
            return Delivery(550)
        except smtplib.SMTPResponseException as err:
            return Delivery(err.smtp_code if err.smtp_code >= 300 else 554)
        finally:
            try:
                smtp.quit()
            except Exception:
                smtp.close()
        return Delivery(200)  # accepted; reported as a 2xx like the HTTP channels


# --------------------------------------------------------------------------------------------------
# Your own function
# --------------------------------------------------------------------------------------------------
class CallbackTransport(Transport):
    """Hand each card to your function: SMS, a ticket system, a message queue, a log pipeline.

    The function gets the ``Card`` (``card.as_dict()`` is JSON-ready). Returning a
    ``Delivery`` reports its status; returning anything else counts as sent; raising
    counts as failed. It runs on Faultbell's sender thread, never in your request.
    """

    name = "callback"

    def __init__(
        self,
        fn: Callable[[Card], Any],
        *,
        name: str = "callback",
        kinds: Iterable[str] | str | None = None,
    ) -> None:
        super().__init__(None, kinds=kinds)
        self.fn = fn
        self.label = name

    def destination(self) -> str:
        return f"callback:{self.label}"

    def send(self, card: Card, url: str, timeout: float) -> Delivery:
        result = self.fn(card)
        return result if isinstance(result, Delivery) else Delivery(200)


# --------------------------------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------------------------------
def _env(name: str) -> str:
    return (os.environ.get("FAULTBELL_" + name) or "").strip()


def _kinds(prefix: str, default: Iterable[str] | None = None) -> Iterable[str] | str | None:
    raw = _env(prefix + "_KINDS")
    return raw or default


def channels_from_env() -> list[Transport]:
    """Every channel configured through environment variables (see the README's channel table)."""
    found: list[Transport] = []
    if url := _env("DISCORD_URL"):
        found.append(DiscordTransport(url, kinds=_kinds("DISCORD")))
    if url := _env("SLACK_URL"):
        found.append(SlackTransport(url, kinds=_kinds("SLACK")))
    if url := _env("TEAMS_URL"):
        found.append(TeamsTransport(url, kinds=_kinds("TEAMS")))
    if url := _env("GOOGLE_CHAT_URL"):
        found.append(GoogleChatTransport(url, kinds=_kinds("GOOGLE_CHAT")))
    if url := _env("JSON_URL"):
        found.append(JsonWebhookTransport(url, kinds=_kinds("JSON")))
    token, chat = _env("TELEGRAM_BOT_TOKEN"), _env("TELEGRAM_CHAT_ID")
    if token and chat:
        thread = _env("TELEGRAM_THREAD_ID")
        found.append(
            TelegramTransport(
                token,
                chat,
                thread_id=int(thread) if thread.isdigit() else None,
                api_base=_env("TELEGRAM_API_BASE") or "https://api.telegram.org",
                kinds=_kinds("TELEGRAM"),
            )
        )
    if key := _env("PAGERDUTY_ROUTING_KEY"):
        found.append(
            PagerDutyTransport(
                key,
                url=_env("PAGERDUTY_URL") or "https://events.pagerduty.com/v2/enqueue",
                kinds=_kinds("PAGERDUTY", ("unhandled", "job")),
            )
        )
    to = _env("EMAIL_TO")
    if to:
        port = _env("SMTP_PORT")
        found.append(
            EmailTransport(
                to,
                host=_env("SMTP_HOST") or "localhost",
                port=int(port) if port.isdigit() else None,
                sender=_env("EMAIL_FROM") or None,
                username=_env("SMTP_USER") or None,
                password=_env("SMTP_PASSWORD") or None,
                security=_env("SMTP_SECURITY") or "auto",
                subject_prefix=_env("EMAIL_SUBJECT_PREFIX"),
                kinds=_kinds("EMAIL"),
            )
        )
    return found
