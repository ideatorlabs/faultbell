"""Where cards go: the :class:`Transport` base class, Discord (the default) and a generic JSON webhook.

More channels (Slack, Microsoft Teams, Google Chat, Telegram, PagerDuty, email, your own function) live in
:mod:`faultbell.channels`. Adding another means subclassing :class:`Transport` and implementing
:meth:`Transport.encode` (HTTP) or overriding :meth:`Transport.send` (anything else).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ._version import __version__
from .card import Card

USER_AGENT = f"faultbell/{__version__}"


@dataclass(frozen=True)
class Delivery:
    """The webhook's answer. ``retry_after`` (seconds) is set for a 429."""

    status: int
    retry_after: float | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def http_post(
    url: str,
    body: bytes,
    content_type: str,
    *,
    timeout: float,
    headers: Mapping[str, str] | None = None,
) -> Delivery:
    """POST with the standard library. HTTP errors become a :class:`Delivery`; network errors raise."""
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": content_type, "User-Agent": USER_AGENT, **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured URL
            return Delivery(response.status)
    except urllib.error.HTTPError as err:
        retry_after = None
        if err.code == 429:
            try:
                answer = json.loads(err.read() or b"{}")
                # Discord: {"retry_after": s}; Telegram: {"parameters": {"retry_after": s}}
                hint = answer.get("retry_after") or (answer.get("parameters") or {}).get("retry_after")
                retry_after = float(hint)  # type: ignore[arg-type]  # None -> TypeError -> header
            except Exception:
                try:
                    retry_after = float(err.headers.get("Retry-After") or 1)
                except (TypeError, ValueError):
                    retry_after = 1.0
        return Delivery(err.code, retry_after)


KINDS = ("unhandled", "handled", "job")


class Transport:
    """One channel. ``url`` is where it posts; ``kinds`` limits which cards it gets.

    ``kinds`` is any of ``"unhandled"`` (a crash nobody caught), ``"handled"`` (``capture()`` or a logged
    exception) and ``"job"`` (a job-run summary). ``None`` means all of them.
    """

    name = "base"

    def __init__(
        self,
        url: str | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        kinds: Iterable[str] | str | None = None,
    ) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.kinds = parse_kinds(kinds)

    def wants(self, card: Card) -> bool:
        """Whether this channel should get ``card`` (its ``kinds`` filter)."""
        return self.kinds is None or card.kind in self.kinds

    def ready(self) -> bool:
        """Configured well enough to send. HTTP channels need a URL."""
        return bool(self.destination())

    def destination(self) -> str:
        """Where this channel delivers; passed to :meth:`send` as ``url``."""
        return (self.url or "").strip()

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"

    def encode(self, card: Card) -> tuple[bytes, str]:
        """Return ``(body, content_type)`` for one card."""
        raise NotImplementedError

    def send(self, card: Card, url: str, timeout: float) -> Delivery:
        """Deliver one card. Override for non-HTTP transports. May raise on network errors."""
        body, content_type = self.encode(card)
        return http_post(url, body, content_type, timeout=timeout, headers=self.headers)


class JsonWebhookTransport(Transport):
    """POSTs the card as plain JSON (``Card.as_dict()`` plus a ``text`` summary line)."""

    name = "json"

    def encode(self, card: Card) -> tuple[bytes, str]:
        payload: dict[str, Any] = {"text": f"{card.title}\n{card.message}", **card.as_dict()}
        return json.dumps(payload).encode(), "application/json"


class DiscordTransport(Transport):
    """One embed per card with the traceback attached as a text file (multipart upload)."""

    name = "discord"

    COLORS = {"unhandled": 0xE5484D, "handled": 0xF5A524, "job": 0x7C66DC}
    TITLE_MAX, DESC_MAX, FIELD_NAME_MAX, FIELD_VALUE_MAX = 256, 4096, 256, 1024
    TOTAL_MAX, FOOTER_MAX, AUTHOR_MAX, FIELDS_MAX = 6000, 2048, 256, 25

    def __init__(
        self,
        url: str | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        kinds: Iterable[str] | str | None = None,
        icon_url: str | None = None,
        footer: str = "Faultbell",
    ) -> None:
        super().__init__(url, headers=headers, kinds=kinds)
        self.icon_url = icon_url if icon_url is not None else (os.environ.get("FAULTBELL_ICON_URL") or None)
        self.footer = footer

    # -- rendering -------------------------------------------------------------------------------
    @staticmethod
    def code_block(text: str, limit: int) -> str:
        text = text.replace("```", "'''")
        inner = limit - 8
        if len(text) > inner:
            text = text[: inner - 1] + "…"
        return f"```\n{text}\n```"

    def _field(self, name: str, value: str, inline: bool, code: bool) -> dict[str, Any]:
        if code:
            value = self.code_block(value, self.FIELD_VALUE_MAX)
        value = value.strip() or "-"
        if len(value) > self.FIELD_VALUE_MAX:
            value = value[: self.FIELD_VALUE_MAX - 1] + "…"
        return {"name": name[: self.FIELD_NAME_MAX] or "-", "value": value, "inline": inline}

    @staticmethod
    def _size(embed: dict[str, Any]) -> int:
        return (
            len(embed.get("title", ""))
            + len(embed.get("description", ""))
            + len(embed.get("footer", {}).get("text", ""))
            + len(embed.get("author", {}).get("name", ""))
            + sum(len(f["name"]) + len(f["value"]) for f in embed.get("fields", []))
        )

    def _fit(self, embed: dict[str, Any]) -> dict[str, Any]:
        """Stay inside Discord's limits: shorten the longest field, then drop trailing fields."""
        embed["fields"] = embed["fields"][: self.FIELDS_MAX]
        while self._size(embed) > self.TOTAL_MAX and embed["fields"]:
            longest = max(embed["fields"], key=lambda f: len(f["value"]))
            if len(longest["value"]) > 200:
                over = self._size(embed) - self.TOTAL_MAX
                keep = max(100, len(longest["value"]) - over - 10)
                longest["value"] = longest["value"][:keep] + "…"
                if longest["value"].count("```") % 2:
                    longest["value"] += "```"
            else:
                embed["fields"].pop()
        over = self._size(embed) - self.TOTAL_MAX
        if over > 0:
            embed["description"] = embed["description"][: max(0, len(embed["description"]) - over)]
        return embed

    def embed(self, card: Card) -> dict[str, Any]:
        author: dict[str, Any] = {"name": f"{card.service} · {card.environment}"[: self.AUTHOR_MAX]}
        footer: dict[str, Any] = {
            "text": f"{self.footer} · {card.environment} · alert {card.fingerprint}"[: self.FOOTER_MAX]
        }
        if self.icon_url:
            author["icon_url"] = self.icon_url
            footer["icon_url"] = self.icon_url
        embed = {
            "author": author,
            "title": card.title[: self.TITLE_MAX],
            "color": self.COLORS.get(card.kind, self.COLORS["handled"]),
            "description": self.code_block(card.message or "-", self.DESC_MAX),
            "fields": [self._field(f.name, f.value, f.inline, f.code) for f in card.fields],
            "footer": footer,
            "timestamp": card.timestamp.isoformat(),
        }
        return self._fit(embed)

    def payload(self, card: Card) -> dict[str, Any]:
        return {
            "embeds": [self.embed(card)],
            "allowed_mentions": {"parse": []},  # never ping anyone
            "attachments": [{"id": 0, "filename": card.attachment_name}],
        }

    def encode(self, card: Card) -> tuple[bytes, str]:
        boundary = "faultbell" + uuid.uuid4().hex
        payload = json.dumps(self.payload(card)).encode()
        parts = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\n'
            "Content-Type: application/json\r\n\r\n".encode()
            + payload
            + b"\r\n",
            f'--{boundary}\r\nContent-Disposition: form-data; name="files[0]"; '
            f'filename="{card.attachment_name}"\r\n'
            "Content-Type: text/plain; charset=utf-8\r\n\r\n".encode()
            + card.attachment
            + b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
        return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def parse_kinds(kinds: Iterable[str] | str | None) -> frozenset[str] | None:
    """``"unhandled,job"`` or an iterable -> a validated set; empty or ``None`` -> ``None`` (everything)."""
    if kinds is None:
        return None
    items = kinds.split(",") if isinstance(kinds, str) else list(kinds)
    chosen = frozenset(k.strip().lower() for k in items if k and k.strip())
    unknown = chosen - set(KINDS)
    if unknown:
        raise ValueError(f"unknown card kind(s) {sorted(unknown)}; use {', '.join(KINDS)}")
    return chosen or None


def transport_named(name: str) -> Transport:
    """A URL-based transport by name: ``discord`` (default), ``json``/``webhook``, ``slack``, ``teams``,
    ``google_chat``. Unknown names fall back to Discord."""
    from .channels import GoogleChatTransport, SlackTransport, TeamsTransport

    by_name: dict[str, type[Transport]] = {
        "discord": DiscordTransport,
        "json": JsonWebhookTransport,
        "webhook": JsonWebhookTransport,
        "slack": SlackTransport,
        "teams": TeamsTransport,
        "google_chat": GoogleChatTransport,
        "googlechat": GoogleChatTransport,
    }
    return by_name.get(name.strip().lower().replace("-", "_"), DiscordTransport)()
