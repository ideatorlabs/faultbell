from __future__ import annotations

import asyncio
import logging
import sys
import threading

import pytest

import faultbell
from conftest import FakeWebhook, raise_and_catch
from faultbell import integrations


def test_logging_handler_reports_logged_exceptions(live: FakeWebhook) -> None:
    faultbell.install(service="worker")
    logger = logging.getLogger("worker.payments")
    try:
        raise RuntimeError("settlement file missing")
    except RuntimeError:
        logger.exception("settlement %s failed", "2026-11-17")
        logging.getLogger("faultbell.internal").exception("never alerts")
    logger.error("no exception attached: no card")
    faultbell.flush(5)
    [request] = live.wait_for(1)
    assert request.embed()["title"] == "Handled RuntimeError in worker"
    assert request.fields()["Logged"] == "worker.payments: settlement 2026-11-17 failed"


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_thread_hook(live: FakeWebhook) -> None:
    faultbell.install(service="worker")

    def boom() -> None:
        raise LookupError("thread blew up")

    worker = threading.Thread(target=boom, name="poller")
    worker.start()
    worker.join()
    faultbell.flush(5)
    [request] = live.wait_for(1)
    assert request.embed()["title"] == "Unhandled LookupError in job thread poller"


def test_sys_excepthook(live: FakeWebhook) -> None:
    calls: list[str] = []
    original = sys.excepthook
    sys.excepthook = lambda *args: calls.append("previous")
    try:
        faultbell.install(service="cli")
        faultbell.install(service="cli")  # wrapping twice must not double report
        exc = raise_and_catch(SystemError("fatal"))
        sys.excepthook(type(exc), exc, exc.__traceback__)
        sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
        assert calls == ["previous", "previous"]
        faultbell.uninstall()
        assert sys.excepthook is not original  # restored to the lambda we installed
    finally:
        sys.excepthook = original
    [request] = live.wait_for(1)
    assert request.embed()["title"] == "Unhandled SystemError in job main"
    assert len(live.received) == 1


def test_asyncio_loop_handler(live: FakeWebhook) -> None:
    seen: list[str] = []

    async def main() -> None:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda lp, ctx: seen.append(ctx["message"]))
        faultbell.install(service="loop")
        integrations.install_loop_handler()  # already hooked: no double wrap
        loop.call_exception_handler(
            {"message": "task failed", "exception": raise_and_catch(OSError("socket"))}
        )
        loop.call_exception_handler({"message": "no exception here"})

    asyncio.run(main())
    faultbell.flush(5)
    [request] = live.wait_for(1)
    assert request.embed()["title"] == "Unhandled OSError in job asyncio"
    assert seen == ["task failed", "no exception here"]


def test_asyncio_default_handler_is_kept(live: FakeWebhook) -> None:
    async def main() -> None:
        faultbell.install(service="loop")
        logging.getLogger("asyncio").disabled = True
        try:
            asyncio.get_running_loop().call_exception_handler(
                {"message": "x", "exception": raise_and_catch(OSError("default handler"))}
            )
        finally:
            logging.getLogger("asyncio").disabled = False

    asyncio.run(main())
    faultbell.flush(5)
    assert live.wait_for(1)[0].embed()["title"] == "Unhandled OSError in job asyncio"


def test_install_never_raises_and_uninstall_restores(monkeypatch: object) -> None:
    class Weird:
        state = None

        def add_middleware(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("app refuses middleware")

    faultbell.install(Weird(), service="x")  # logged, not raised
    assert getattr(sys.excepthook, integrations.HOOK_MARK, False)
    assert getattr(threading.excepthook, integrations.HOOK_MARK, False)
    faultbell.uninstall()
    assert not getattr(sys.excepthook, integrations.HOOK_MARK, False)
    assert not getattr(threading.excepthook, integrations.HOOK_MARK, False)
    assert not any(isinstance(h, integrations.AlertHandler) for h in logging.getLogger().handlers)
