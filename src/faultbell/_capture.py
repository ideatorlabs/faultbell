"""Capturing exceptions: decide whether to alert, snapshot, and enqueue. Cheap and never raises."""

from __future__ import annotations

import contextlib
import contextvars
import logging
import re
import traceback
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from . import _context as context
from . import config, sender
from .card import Event

if TYPE_CHECKING:
    from .jobs import JobRun

_log = logging.getLogger("faultbell")

REPORTED_MARK = "__faultbell_reported__"

#: The job run currently collecting failures (see ``faultbell.jobs``).
active_run: contextvars.ContextVar[JobRun | None] = contextvars.ContextVar("faultbell_run", default=None)

_CLIENT_ERROR_TYPES = ("RequestValidationError", "WebSocketRequestValidationError")


def is_client_error(exc: BaseException) -> bool:
    """True for anything carrying an int ``status_code`` or ``status`` below 500 (an HTTP 4xx, a
    framework's validation error...). Those are answers to bad input, not bugs, and never alert."""
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 500:
            return True
    return type(exc).__name__ in _CLIENT_ERROR_TYPES


def already_reported(exc: BaseException) -> bool:
    return bool(getattr(exc, REPORTED_MARK, False))


def mark_reported(exc: BaseException) -> None:
    # Some built-in exceptions refuse attributes; the worst case is a duplicate card.
    with contextlib.suppress(Exception):
        setattr(exc, REPORTED_MARK, True)


def snapshot_exception(exc: BaseException) -> traceback.TracebackException:
    """No frames and no local variables survive this: only file names, line numbers and messages."""
    return traceback.TracebackException(
        type(exc), exc, exc.__traceback__, lookup_lines=False, capture_locals=False
    )


def route_of(asgi_scope: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
    """``("GET", "/orders/{order_id}")``: the route template, never the raw path with ids in it."""
    if not asgi_scope:
        return None, None
    route = asgi_scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    if template:
        prefix = asgi_scope.get("root_path") or ""
        if prefix and not template.startswith(prefix):
            template = prefix.rstrip("/") + template
    else:  # no route matched: fold anything that looks like an id
        template = re.sub(r"/[0-9a-fA-F-]{8,}|/\d+", "/{id}", asgi_scope.get("path") or "")
    return asgi_scope.get("method"), template


def capture(exc: BaseException, *, handled: bool = True, job: str | None = None) -> None:
    """Report an exception. Returns at once; sending happens on Faultbell's own thread.

    Inside a ``job_run`` a handled exception is folded into the run's summary
    card instead of producing its own.
    """
    capture_exception(exc, handled=handled, job=job)


def capture_exception(
    exc: BaseException | None,
    *,
    handled: bool,
    job: str | None = None,
    logged: str | None = None,
    asgi_scope: Mapping[str, Any] | None = None,
) -> None:
    try:
        if not isinstance(exc, BaseException) or isinstance(exc, (KeyboardInterrupt, SystemExit)):
            return
        if is_client_error(exc) or already_reported(exc):
            return
        mark_reported(exc)
        if not config.enabled():
            return
        values, rid, scope = context.snapshot()
        run = active_run.get()
        if run is not None and handled:
            run.fold(exc, values, step=job)
            return
        method, route = route_of(asgi_scope or scope)
        event = Event(
            tbe=snapshot_exception(exc),
            exc_type=type(exc),
            handled=handled,
            job=job or values.pop("job", None),
            service=config.service_name(values.pop("service", None) or None),
            values=values,
            request_id=rid,
            method=method,
            route=route,
            logged=logged,
            at=datetime.now(timezone.utc),
        )
        sender.enqueue(event)
    except Exception:  # the alerter never breaks the app
        _log.debug("capture failed", exc_info=True)
