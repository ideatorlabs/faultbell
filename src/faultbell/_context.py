"""Per-request and per-task context attached to every card.

The context lives in a ``ContextVar`` that holds a small mutable *box* around an immutable mapping.
Writes replace the mapping (copy-on-write) but keep the box. The box matters for web frameworks that
run sync dependencies in a thread pool: each worker runs on a *copy* of the request's context, but every
copy points at the same box, so a ``set_context()`` made in a dependency is visible to the rest of the
request. The request middleware and ``context`` give each request/block a fresh box and restore the
previous one on exit.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from types import MappingProxyType
from typing import Any

from ._scrub import customer_ref, mask_phone, scrub

_log = logging.getLogger("faultbell")

VALUE_MAX = 200
KEY_MAX = 60


class Box:
    """Context for one request or block. ``values`` is replaced, never mutated."""

    __slots__ = ("values", "request_id", "asgi_scope")

    def __init__(
        self,
        values: Mapping[str, str] | None = None,
        request_id: str | None = None,
        asgi_scope: Mapping[str, Any] | None = None,
    ) -> None:
        self.values: Mapping[str, str] = MappingProxyType(dict(values or {}))
        self.request_id = request_id
        self.asgi_scope = asgi_scope


current: contextvars.ContextVar[Box | None] = contextvars.ContextVar("faultbell_context", default=None)


def clean_value(key: str, value: Any) -> str:
    """Phones keep their last four digits, customers become short references, the rest is scrubbed."""
    text = str(value).strip()
    if key == "phone":
        return mask_phone(text)
    if key in ("customer", "customer_id"):
        return customer_ref(text)
    return scrub(text)[:VALUE_MAX]


def merged(base: Mapping[str, str], updates: Mapping[str, Any]) -> dict[str, str]:
    out = dict(base)
    for key, value in updates.items():
        if value is None:  # None means "leave whatever was set before"
            continue
        out[str(key)[:KEY_MAX]] = clean_value(key, value)
    return out


def _updates(
    service: str | None,
    tenant: str | None,
    tenant_id: str | None,
    customer: str | None,
    phone: str | None,
    feature: str | None,
    extra: Mapping[str, Any],
) -> dict[str, Any]:
    return dict(
        service=service,
        tenant=tenant,
        tenant_id=tenant_id,
        customer=customer,
        phone=phone,
        feature=feature,
        **extra,
    )


def set_context(
    *,
    service: str | None = None,
    tenant: str | None = None,
    tenant_id: str | None = None,
    customer: str | None = None,
    phone: str | None = None,
    feature: str | None = None,
    **extra: Any,
) -> None:
    """Add fields to the current request's or task's context. ``None`` keeps an earlier value.

    ``phone`` is masked to its last four digits and ``customer`` becomes a short reference before it is
    stored, so neither ever leaves the process in full. Never raises.
    """
    try:
        updates = _updates(service, tenant, tenant_id, customer, phone, feature, extra)
        box = current.get()
        if box is None:  # a bare script or thread: give it its own box
            current.set(Box(merged({}, updates)))
            return
        box.values = MappingProxyType(merged(box.values, updates))
    except Exception:
        _log.debug("set_context failed", exc_info=True)


@contextmanager
def context(
    *,
    service: str | None = None,
    tenant: str | None = None,
    tenant_id: str | None = None,
    customer: str | None = None,
    phone: str | None = None,
    feature: str | None = None,
    **extra: Any,
) -> Iterator[None]:
    """A block (one job, one loop iteration) with its own context, seeded from the surrounding one.

    The surrounding context is not changed and is back in effect when the block exits.
    """
    token = None
    try:
        outer = current.get()
        updates = _updates(service, tenant, tenant_id, customer, phone, feature, extra)
        token = current.set(
            Box(
                merged(outer.values if outer else {}, updates),
                outer.request_id if outer else None,
                outer.asgi_scope if outer else None,
            )
        )
    except Exception:
        _log.debug("context() setup failed", exc_info=True)
    try:
        yield
    finally:
        if token is not None:
            current.reset(token)


def request_id() -> str | None:
    """The current request's id (set by the ASGI middleware), or ``None``."""
    box = current.get()
    return box.request_id if box else None


def snapshot() -> tuple[dict[str, str], str | None, Mapping[str, Any] | None]:
    box = current.get()
    if box is None:
        return {}, None, None
    return dict(box.values), box.request_id, box.asgi_scope
