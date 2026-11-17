from __future__ import annotations

import contextvars
import threading

import faultbell
from faultbell import _context


def test_set_context_merges_and_none_keeps_values() -> None:
    faultbell.set_context(tenant="Demo Café", feature="checkout")
    faultbell.set_context(tenant=None, feature="refunds", attempt=2)
    values, rid, scope = _context.snapshot()
    assert values == {"tenant": "Demo Café", "feature": "refunds", "attempt": "2"}
    assert rid is None and scope is None
    assert faultbell.request_id() is None


def test_phone_and_customer_never_stored_in_full() -> None:
    faultbell.set_context(phone="+1 555 0100 123", customer="Asha Rao", note="mail sam@example.com")
    values, _, _ = _context.snapshot()
    assert values["phone"] == "••0123"
    assert values["customer"].startswith("cust ") and "Asha" not in values["customer"]
    assert values["note"] == "mail [email]"


def test_context_block_restores_the_outer_context() -> None:
    faultbell.set_context(tenant="Outer")
    with faultbell.context(tenant="Inner", item="row-1"):
        assert _context.snapshot()[0] == {"tenant": "Inner", "item": "row-1"}
        faultbell.set_context(step="two")
        assert _context.snapshot()[0]["step"] == "two"
    assert _context.snapshot()[0] == {"tenant": "Outer"}


def test_threadpool_copies_share_the_box() -> None:
    """A sync dependency runs on a *copy* of the request context; its writes must still be seen."""
    token = _context.current.set(_context.Box({}, "req-12345678"))
    try:
        copy = contextvars.copy_context()
        worker = threading.Thread(
            target=copy.run, args=(faultbell.set_context,), kwargs={"tenant": "Sample Store"}
        )
        worker.start()
        worker.join()
        assert _context.snapshot()[0] == {"tenant": "Sample Store"}
        assert faultbell.request_id() == "req-12345678"
    finally:
        _context.current.reset(token)


def test_bad_values_never_raise() -> None:
    class Unprintable:
        def __str__(self) -> str:
            raise RuntimeError("no")

    faultbell.set_context(thing=Unprintable())
    with faultbell.context(thing=Unprintable()):
        pass
