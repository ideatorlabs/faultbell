"""Shared fixtures: a real local HTTP server standing in for the webhook, and a clean Faultbell per test."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

import faultbell
from faultbell import _capture, _context


@dataclass
class Received:
    path: str
    headers: dict[str, str]
    body: bytes

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    def parts(self) -> dict[str, tuple[str | None, bytes]]:
        """Parse a multipart/form-data body into {name: (filename, data)}."""
        boundary = self.content_type.split("boundary=", 1)[1].encode()
        out: dict[str, tuple[str | None, bytes]] = {}
        for chunk in self.body.split(b"--" + boundary):
            chunk = chunk.strip(b"\r\n")
            if not chunk or chunk == b"--":
                continue
            head, _, data = chunk.partition(b"\r\n\r\n")
            disposition = next(
                line for line in head.decode().split("\r\n") if line.lower().startswith("content-disposition")
            )
            attrs = dict(part.strip().split("=", 1) for part in disposition.split(";")[1:] if "=" in part)
            name = attrs["name"].strip('"')
            filename = attrs.get("filename", "").strip('"') or None
            out[name] = (filename, data)
        return out

    def discord(self) -> tuple[dict[str, Any], str | None, str]:
        """(payload_json, attachment filename, attachment text) of a Discord multipart post."""
        parts = self.parts()
        payload = json.loads(parts["payload_json"][1])
        filename, data = parts["files[0]"]
        return payload, filename, data.decode()

    def embed(self) -> dict[str, Any]:
        return self.discord()[0]["embeds"][0]

    def fields(self) -> dict[str, str]:
        return {f["name"]: f["value"] for f in self.embed()["fields"]}

    def json(self) -> dict[str, Any]:
        return json.loads(self.body)


@dataclass
class FakeWebhook:
    url: str
    received: list[Received] = field(default_factory=list)
    #: Status codes to answer with, in order; the last one repeats.
    statuses: list[int] = field(default_factory=lambda: [204])
    delay: float = 0.0
    retry_after: float = 0.01
    #: Body of a 429 answer; default is Discord's shape ``{"retry_after": ...}``.
    body_429: dict[str, Any] | None = None
    _cond: threading.Condition = field(default_factory=threading.Condition)

    def record(self, item: Received) -> int:
        with self._cond:
            self.received.append(item)
            self._cond.notify_all()
            index = len(self.received) - 1
        return self.statuses[min(index, len(self.statuses) - 1)]

    def wait_for(self, n: int, timeout: float = 5.0) -> list[Received]:
        deadline = time.monotonic() + timeout
        with self._cond:
            while len(self.received) < n:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(left)
            return list(self.received)


def start_fake_webhook() -> tuple[FakeWebhook, ThreadingHTTPServer]:
    hook: FakeWebhook

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server API
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            headers = {k.lower(): v for k, v in self.headers.items()}
            status = hook.record(Received(self.path, headers, body))
            if hook.delay:
                time.sleep(hook.delay)
            reply = b""
            if status == 429:
                reply = json.dumps(hook.body_429 or {"retry_after": hook.retry_after}).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)
            except OSError:  # the client gave up (timeout test)
                pass

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    hook = FakeWebhook(url=f"http://127.0.0.1:{server.server_port}/api/hook")
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return hook, server


@pytest.fixture
def webhook() -> Iterator[FakeWebhook]:
    hook, server = start_fake_webhook()
    try:
        yield hook
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def clean_faultbell(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in list(os.environ):
        if name.startswith("FAULTBELL_") or name in ("GIT_SHA", "SOURCE_VERSION"):
            monkeypatch.delenv(name, raising=False)
    faultbell.uninstall()
    faultbell.reset()
    faultbell.configure(rate_limit=(1000, 1.0))
    ctx_token = _context.current.set(None)
    run_token = _capture.active_run.set(None)
    yield
    _capture.active_run.reset(run_token)
    _context.current.reset(ctx_token)
    faultbell.uninstall()
    faultbell.reset()


@pytest.fixture
def live(webhook: FakeWebhook, monkeypatch: pytest.MonkeyPatch) -> FakeWebhook:
    """Faultbell switched on (production) and pointed at the fake webhook through env vars."""
    monkeypatch.setenv("FAULTBELL_WEBHOOK_URL", webhook.url)
    monkeypatch.setenv("FAULTBELL_ENV", "production")
    return webhook


def raise_and_catch(exc: BaseException) -> BaseException:
    """Raise ``exc`` so it carries a real traceback, and return it."""
    try:
        raise exc
    except BaseException as caught:  # noqa: B036 - test helper
        return caught
