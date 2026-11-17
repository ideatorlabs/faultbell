"""Wiring Faultbell into a process: logging, sys/thread/asyncio hooks and an ASGI (FastAPI/Starlette)
middleware with a catch-all 500 handler."""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import threading
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from types import TracebackType
from typing import Any

from . import _context as context
from . import config, sender
from ._capture import capture_exception

_log = logging.getLogger("faultbell")

HOOK_MARK = "__faultbell_hook__"
REQUEST_ID_HEADER = "X-Request-Id"
SCOPE_KEY = "faultbell.request_id"
_QUIET_LOGGERS = ("faultbell", "urllib", "urllib3", "http.client", "httpx", "httpcore", "hpack")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


# --------------------------------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------------------------------
class AlertHandler(logging.Handler):
    """``logger.exception(...)`` or ``logger.error(..., exc_info=True)`` anywhere becomes a card."""

    _busy = threading.local()

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._busy, "on", False) or record.name.startswith(_QUIET_LOGGERS):
            return
        exc = record.exc_info[1] if record.exc_info else None
        if exc is None:
            return
        self._busy.on = True
        try:
            try:
                logged = f"{record.name}: {record.getMessage()}"
            except Exception:
                logged = record.name
            capture_exception(exc, handled=True, logged=logged)
        finally:
            self._busy.on = False


# --------------------------------------------------------------------------------------------------
# process hooks
# --------------------------------------------------------------------------------------------------
_installed = False
_handler: AlertHandler | None = None
_added_last_resort = False
_previous_sys_hook: Callable[..., Any] | None = None
_previous_thread_hook: Callable[..., Any] | None = None
_loops_seen: set[int] = set()


def _install_sys_hook() -> None:
    """Wrap ``sys.excepthook``; again if a library replaced it after us."""
    global _previous_sys_hook
    previous = sys.excepthook
    if getattr(previous, HOOK_MARK, False):
        return

    def hook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
        if not issubclass(exc_type, KeyboardInterrupt):
            capture_exception(exc, handled=False, job="main")
            sender.flush(2.0)
        previous(exc_type, exc, tb)

    setattr(hook, HOOK_MARK, True)
    _previous_sys_hook = _previous_sys_hook or previous
    sys.excepthook = hook


def _install_thread_hook() -> None:
    global _previous_thread_hook
    previous = threading.excepthook
    if getattr(previous, HOOK_MARK, False):
        return

    def hook(args: Any) -> None:
        if args.exc_value is not None and not isinstance(args.exc_value, SystemExit):
            name = getattr(args.thread, "name", None) or "thread"
            capture_exception(args.exc_value, handled=False, job=f"thread {name}")
        previous(args)

    setattr(hook, HOOK_MARK, True)
    _previous_thread_hook = previous
    threading.excepthook = hook


def install_loop_handler(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Report exceptions the event loop would only log (e.g. a task nobody awaited)."""
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
    previous = loop.get_exception_handler()
    if getattr(previous, HOOK_MARK, False):
        return

    def handler(lp: asyncio.AbstractEventLoop, ctx: dict[str, Any]) -> None:
        exc = ctx.get("exception")
        if exc is not None:
            capture_exception(exc, handled=False, job="asyncio")
        if previous is not None:
            previous(lp, ctx)
        else:
            lp.default_exception_handler(ctx)

    setattr(handler, HOOK_MARK, True)
    loop.set_exception_handler(handler)


def install(app: Any = None, *, service: str) -> None:
    """Wire Faultbell in. Safe to call more than once, per process and per app.

    Adds a logging handler (ERROR records with an exception), wraps ``sys.excepthook``,
    ``threading.excepthook`` and the running asyncio loop's exception handler, and for an ASGI app
    with ``add_middleware`` (FastAPI, Starlette) adds the request middleware and a 500 handler.
    Never raises.
    """
    global _installed, _handler, _added_last_resort
    try:
        config.set_installed_service(service)
        if not _installed:
            _installed = True
            root = logging.getLogger()
            if not root.handlers and logging.lastResort is not None:
                # Adding any root handler switches off logging.lastResort; keep stderr output as it was.
                root.addHandler(logging.lastResort)
                _added_last_resort = True
            _handler = AlertHandler()
            root.addHandler(_handler)
            _install_thread_hook()
        _install_sys_hook()
        install_loop_handler()
        if app is not None:
            install_asgi(app, service=service)
    except Exception:
        _log.warning("faultbell was not installed", exc_info=True)


def uninstall() -> None:
    """Undo ``install`` for the process (apps keep their middleware). Mostly for tests."""
    global _installed, _handler, _added_last_resort, _previous_sys_hook, _previous_thread_hook
    root = logging.getLogger()
    if _handler is not None:
        root.removeHandler(_handler)
        _handler = None
    if _added_last_resort and logging.lastResort is not None:
        root.removeHandler(logging.lastResort)
        _added_last_resort = False
    if _previous_sys_hook is not None and getattr(sys.excepthook, HOOK_MARK, False):
        sys.excepthook = _previous_sys_hook
    if _previous_thread_hook is not None and getattr(threading.excepthook, HOOK_MARK, False):
        threading.excepthook = _previous_thread_hook
    _previous_sys_hook = _previous_thread_hook = None
    _loops_seen.clear()
    _installed = False


# --------------------------------------------------------------------------------------------------
# ASGI
# --------------------------------------------------------------------------------------------------
def _incoming_request_id(scope: Scope) -> str | None:
    wanted = {h.lower().encode("latin-1") for h in config.settings().request_id_headers}
    for key, value in scope.get("headers") or []:
        if key.lower() in wanted:
            text = value.decode("latin-1").strip()
            if _REQUEST_ID_RE.match(text):
                return text
    return None


class RequestContextMiddleware:
    """Pure ASGI middleware: a fresh context and request id per request (echoed as ``X-Request-Id``),
    and any exception that escapes the app reported before it is re-raised (the 500 is unchanged)."""

    def __init__(self, app: ASGIApp, service: str = "") -> None:
        self.app = app
        self.service = service

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        _hook_running_loop()
        rid = _incoming_request_id(scope) or uuid.uuid4().hex[:16]
        scope[SCOPE_KEY] = rid
        initial = {"service": self.service} if self.service else {}
        token = context.current.set(context.Box(initial, rid, scope))

        async def send_with_id(message: Message) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                if not any(k.lower() == b"x-request-id" for k, _ in headers):
                    headers.append((b"x-request-id", rid.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception as exc:
            capture_exception(exc, handled=False, asgi_scope=scope)
            raise
        finally:
            context.current.reset(token)


def _hook_running_loop() -> None:
    """Hook each event loop once (the app's loop may not exist yet when install() runs)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - always inside a loop here
        return
    if id(loop) in _loops_seen:
        return
    _loops_seen.add(id(loop))
    install_loop_handler(loop)
    if _installed:
        _install_sys_hook()  # again, after every import has run


async def unhandled_500(request: Any, exc: Exception) -> Any:
    """Starlette's default 500 answer (plain text) plus the request id, so a user's report can be
    matched to the card."""
    from starlette.responses import PlainTextResponse

    capture_exception(exc, handled=False, asgi_scope=request.scope)  # exceptions outside the middleware
    rid = request.scope.get(SCOPE_KEY)
    return PlainTextResponse(
        "Internal Server Error", status_code=500, headers={REQUEST_ID_HEADER: rid} if rid else None
    )


def install_asgi(app: Any, *, service: str = "") -> None:
    """Add the middleware and the 500 handler to a FastAPI/Starlette app (once per app)."""
    state = getattr(app, "state", None)
    if state is not None and getattr(state, "faultbell_installed", False):
        return
    app.add_middleware(RequestContextMiddleware, service=service)
    if hasattr(app, "add_exception_handler"):
        app.add_exception_handler(Exception, unhandled_500)
    if state is not None:
        state.faultbell_installed = True
