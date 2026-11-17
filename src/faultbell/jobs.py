"""Job runs: however many items fail in one run of a cron job, the channel gets ONE summary card."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from typing import Any

from . import _context as context
from . import config, sender
from ._capture import (
    active_run,
    already_reported,
    capture,
    is_client_error,
    mark_reported,
    snapshot_exception,
)
from ._scrub import scrub
from .card import Event, Failure, RunSummary, type_name

_log = logging.getLogger("faultbell")

FAILURES_KEPT = 2000  # per run; beyond this failures are only counted
TRACEBACKS_KEPT = 5  # the first few keep a traceback (the first is attached)
_FAILURE_MARK = "__faultbell_run_failure__"


def _label(tenant: str | None, tenant_id: str | None, item: str | None) -> str:
    if tenant:
        return scrub(str(tenant))[:80]
    if item:
        return scrub(str(item))[:80]
    return scrub(str(tenant_id))[:8] if tenant_id else ""


class JobRun:
    """One run of a job. Collects failures; the outermost run sends one card when it ends.

    Create it with :func:`job_run`. Its methods never raise into the job.
    """

    def __init__(self, job: str, total: int | None, noun: str, demo_ids: Iterable[Any] = ()) -> None:
        self.job = job
        self.total = total
        self.noun = noun
        self.demo_ids: set[str] = {str(i) for i in demo_ids or ()}
        self.failures: list[Failure] = []
        self.overflow = 0
        self.demo_skipped = 0
        self.values, self.request_id, _ = context.snapshot()

    def _is_demo(self, tenant_id: Any) -> bool:
        return tenant_id is not None and str(tenant_id) in self.demo_ids

    def fail(
        self,
        exc: BaseException | None = None,
        *,
        tenant: str | None = None,
        tenant_id: str | None = None,
        item: str | None = None,
        demo: bool = False,
        step: str | None = None,
    ) -> None:
        """Record one item's failure (default: the exception currently being handled).

        ``demo=True``, or a ``tenant_id`` listed in ``demo_ids``, marks demo/test data: counted on the
        card but never alerted on its own.
        """
        try:
            exc = exc if exc is not None else sys.exc_info()[1]
            if exc is None or is_client_error(exc):
                return
            # A logger.exception() inside the item may already have folded this exception into the run.
            recorded = getattr(exc, _FAILURE_MARK, None)
            if recorded is not None and not any(f is recorded for f in self.failures):
                recorded = None
            reported_elsewhere = already_reported(exc) and recorded is None
            mark_reported(exc)
            if demo or self._is_demo(tenant_id):
                self.demo_skipped += 1
                if recorded is not None:
                    self.failures.remove(recorded)
                return
            if recorded is not None:  # attribute the earlier record to this item
                recorded.key = item or tenant_id or tenant or recorded.key
                recorded.label = _label(tenant, tenant_id, item) or recorded.label
                return
            if reported_elsewhere:
                return
            self._remember(
                exc, self._add(exc, _label(tenant, tenant_id, item), item or tenant_id or tenant, step)
            )
        except Exception:
            _log.debug("JobRun.fail failed", exc_info=True)

    def fold(self, exc: BaseException, values: dict[str, str], step: str | None = None) -> None:
        """A ``capture()`` or ``logger.exception()`` inside the run; the item comes from the context."""
        tenant, tenant_id, item = values.get("tenant"), values.get("tenant_id"), values.get("item")
        if self._is_demo(tenant_id):
            self.demo_skipped += 1
            return
        self._remember(
            exc,
            self._add(
                exc, _label(tenant, tenant_id, item), item or tenant_id or tenant, step or values.get("job")
            ),
        )

    @staticmethod
    def _remember(exc: BaseException, failure: Failure | None) -> None:
        if failure is not None:
            with suppress(Exception):
                setattr(exc, _FAILURE_MARK, failure)

    def _add(self, exc: BaseException, label: str, key: str | None, step: str | None) -> Failure | None:
        if not config.enabled():  # alerting is off: nothing to collect
            return None
        if len(self.failures) >= FAILURES_KEPT:
            self.overflow += 1
            return None
        if step == self.job:
            step = None
        with_tb = sum(1 for f in self.failures if f.tbe is not None) < TRACEBACKS_KEPT
        tbe = snapshot_exception(exc) if with_tb else None
        name = type_name(tbe) if tbe is not None else type(exc).__name__
        failure = Failure(
            key=key or f"#{len(self.failures)}",
            label=label or "unknown",
            error=f"{name} ({step})" if step else name,
            message=scrub(str(exc))[:300],
            tbe=tbe,
        )
        self.failures.append(failure)
        return failure

    def summary(self) -> RunSummary | None:
        """What the card will show, or ``None`` when nothing failed."""
        if not self.failures and not self.overflow:
            return None
        errors: dict[str, int] = {}
        labels: dict[str, str] = {}
        for f in self.failures:
            errors[f.error] = errors.get(f.error, 0) + 1
            labels.setdefault(f.key, f.label)
        return RunSummary(
            job=self.job,
            total=self.total,
            noun=self.noun,
            failed=len(labels) + self.overflow,
            errors=errors,
            items=list(labels.values()),
            demo_skipped=self.demo_skipped,
            first=next((f for f in self.failures if f.tbe is not None), None),
            lines=[f"{f.label} · {f.error}: {f.message}" for f in self.failures],
        )

    def send(self) -> None:
        summary = self.summary()
        if summary is None or not config.enabled():
            return
        values = dict(self.values)
        sender.enqueue(
            Event(
                tbe=summary.first.tbe if summary.first else None,
                exc_type=None,
                handled=True,
                job=self.job,
                service=config.service_name(values.pop("service", None) or None),
                values=values,
                request_id=self.request_id,
                at=datetime.now(timezone.utc),
                run=summary,
            )
        )


class NestedRun:
    """A :func:`job_run` inside another one: failures fold into the outer run, tagged with this job."""

    def __init__(self, outer: JobRun, job: str) -> None:
        self.outer = outer
        self.job = job
        self.total: int | None = None  # the outer run's total is the one shown

    def fail(self, exc: BaseException | None = None, *, step: str | None = None, **kwargs: Any) -> None:
        self.outer.fail(exc, step=step or self.job, **kwargs)


@contextmanager
def job_run(
    job: str, *, total: int | None = None, noun: str = "items", demo_ids: Iterable[Any] = ()
) -> Iterator[JobRun | NestedRun]:
    """Collect every failure in one run of ``job`` and send a single summary card when it ends.

    Example::

        with job_run("nightly-sync", total=len(tenants), noun="tenants") as run:
            for t in tenants:
                try:
                    sync(t)
                except Exception as exc:
                    run.fail(exc, tenant=t.name, tenant_id=t.id)

    ``capture()``, ``job_failed()`` and ``logger.exception()`` inside the block are folded in too. The
    card says e.g. "Job nightly-sync: 3 of 41 tenants failed" and is de-duplicated like any other.
    Failures for ids in ``demo_ids`` (or ``fail(..., demo=True)``) are never alerted. A run nested
    inside another folds into the outer one.
    """
    outer = active_run.get()
    if outer is not None:
        outer.demo_ids.update(str(i) for i in demo_ids or ())
        yield NestedRun(outer, job)
        return
    run = JobRun(job, total, noun, demo_ids)
    token = active_run.set(run)
    try:
        yield run
    finally:
        active_run.reset(token)
        try:
            run.send()
        except Exception:
            _log.debug("job run summary failed", exc_info=True)


def job_failed(
    exc: BaseException | None = None,
    *,
    job: str,
    tenant: str | None = None,
    tenant_id: str | None = None,
    item: str | None = None,
    demo: bool = False,
    **extra: Any,
) -> None:
    """An item in ``job`` failed, reported from code that has no run object.

    Inside a :func:`job_run` it folds into the summary; outside one it sends a normal card.
    """
    try:
        exc = exc if exc is not None else sys.exc_info()[1]
        if exc is None:
            return
        run = active_run.get()
        if run is not None:
            run.fail(exc, tenant=tenant, tenant_id=tenant_id, item=item, demo=demo, step=job)
            return
        if demo:
            mark_reported(exc)
            return
        with context.context(tenant=tenant, tenant_id=tenant_id, item=item, **extra):
            capture(exc, handled=True, job=job)
    except Exception:
        _log.debug("job_failed failed", exc_info=True)
