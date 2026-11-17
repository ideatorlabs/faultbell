"""Turning a captured exception (or a finished job run) into a transport-neutral :class:`Card`."""

from __future__ import annotations

import hashlib
import linecache
import os
import re
import sys
import sysconfig
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import config
from ._scrub import scrub

SHOWN = 5  # error types and items listed on a job card
TRACEBACK_MAX = 200_000
MESSAGE_MAX = 300

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
_LIBRARY_DIRS = tuple(
    {
        os.path.abspath(p) + os.sep
        for p in (sysconfig.get_paths().get("stdlib"), sysconfig.get_paths().get("platstdlib"))
        if p
    }
)


# --------------------------------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------------------------------
@dataclass
class Failure:
    """One failed item inside a job run."""

    key: str
    label: str
    error: str
    message: str
    tbe: traceback.TracebackException | None


@dataclass
class RunSummary:
    job: str
    total: int | None
    noun: str
    failed: int
    errors: dict[str, int]
    items: list[str]
    demo_skipped: int
    first: Failure | None
    lines: list[str]


@dataclass
class Event:
    """A snapshot taken at capture time: no frames, no locals, safe to hand to another thread."""

    tbe: traceback.TracebackException | None
    exc_type: type[BaseException] | None
    handled: bool
    service: str
    at: datetime
    job: str | None = None
    values: dict[str, str] = field(default_factory=dict)
    request_id: str | None = None
    method: str | None = None
    route: str | None = None
    logged: str | None = None
    run: RunSummary | None = None


@dataclass
class Field:
    name: str
    value: str
    inline: bool = True
    #: Preformatted text (source, stack, error list); transports may render it as a code block.
    code: bool = False


@dataclass
class Card:
    """What every transport renders. All text is already scrubbed."""

    kind: str  # "unhandled" | "handled" | "job"
    title: str
    message: str
    fields: list[Field]
    environment: str
    service: str
    fingerprint: str
    timestamp: datetime
    seen: int
    attachment_name: str
    attachment: bytes

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "message": self.message,
            "environment": self.environment,
            "service": self.service,
            "fingerprint": self.fingerprint,
            "timestamp": self.timestamp.isoformat(),
            "seen": self.seen,
            "fields": [{"name": f.name, "value": f.value} for f in self.fields],
            "attachment_name": self.attachment_name,
            "traceback": self.attachment.decode("utf-8", "replace"),
        }


# --------------------------------------------------------------------------------------------------
# frames
# --------------------------------------------------------------------------------------------------
def is_app_frame(filename: str) -> bool:
    """Your code: under the app root, not a dependency, not the standard library, not Faultbell."""
    if not filename or filename.startswith("<"):
        return False
    path = os.path.abspath(filename)
    root = config.app_root()
    if not path.startswith(root.rstrip(os.sep) + os.sep):
        return False
    if any(part in path for part in ("site-packages", "dist-packages", os.sep + "node_modules" + os.sep)):
        return False
    if path.startswith(_PACKAGE_DIR + os.sep):
        return False
    return not path.startswith(_LIBRARY_DIRS)


def rel(filename: str) -> str:
    path = os.path.abspath(filename or "")
    root = config.app_root().rstrip(os.sep)
    return os.path.relpath(path, root) if path.startswith(root + os.sep) else os.path.basename(path)


def deepest_app_frame(tbe: traceback.TracebackException) -> traceback.FrameSummary | None:
    frames = [f for f in tbe.stack if is_app_frame(f.filename)]
    if frames:
        return frames[-1]
    return tbe.stack[-1] if len(tbe.stack) else None


def type_name(tbe: traceback.TracebackException, *, qualified: bool = False) -> str:
    """Exception type name without touching ``exc_type`` (deprecated on 3.13+)."""
    if sys.version_info >= (3, 13):
        full = tbe.exc_type_str
        return full if qualified else full.rsplit(".", 1)[-1]
    exc_type = tbe.exc_type
    if exc_type is None:  # pragma: no cover - only for hand-built objects
        return "Exception"
    return f"{exc_type.__module__}.{exc_type.__qualname__}" if qualified else exc_type.__name__


def message_of(tbe: traceback.TracebackException) -> str:
    return scrub(str(tbe) or "")


# --------------------------------------------------------------------------------------------------
# fingerprint
# --------------------------------------------------------------------------------------------------
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}")
_VOLATILE = re.compile(r"\[(?:hex|redacted|email|jwt|webhook)\]|••\d{4}|\d+")


def fingerprint(event: Event) -> str:
    """Same problem, same fingerprint: the type, the deepest frame in your code and the message with
    numbers and ids removed. A job run's fingerprint is its job plus the kinds of error it hit."""
    if event.run is not None:
        raw = f"run|{event.job}|" + "|".join(sorted(event.run.errors))
    elif event.tbe is not None:
        tbe = event.tbe
        frame = deepest_app_frame(tbe)
        where = f"{rel(frame.filename)}:{frame.name}" if frame else ""
        message = _VOLATILE.sub("#", _UUID.sub("#", message_of(tbe)))
        raw = f"{type_name(tbe, qualified=True)}|{where}|{message[:MESSAGE_MAX]}"
    else:
        raw = f"empty|{event.job}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:10]


# --------------------------------------------------------------------------------------------------
# text pieces
# --------------------------------------------------------------------------------------------------
def source_excerpt(frame: traceback.FrameSummary) -> str:
    lineno = frame.lineno or 0
    rows = []
    for n in range(max(1, lineno - 2), lineno + 3):
        line = linecache.getline(frame.filename, n).rstrip()
        if not line and n > lineno:
            break
        marker = ">" if n == lineno else " "
        rows.append(f"{marker} {n:>4} {line}")
    return "\n".join(rows)


def stack_summary(tbe: traceback.TracebackException) -> str:
    app = [f for f in tbe.stack if is_app_frame(f.filename)]
    hidden = len(tbe.stack) - len(app)
    rows = [f"{rel(f.filename)}:{f.lineno} in {f.name}" for f in reversed(app)]
    if hidden:
        rows.append(f"+ {hidden} library frame{'s' if hidden != 1 else ''}")
    return "\n".join(rows) or "-"


def cause_chain(tbe: traceback.TracebackException, depth: int = 4) -> str:
    rows: list[str] = []
    cur = tbe
    while len(rows) < depth:
        nxt = cur.__cause__ or (None if cur.__suppress_context__ else cur.__context__)
        if nxt is None:
            break
        frame = deepest_app_frame(nxt)
        where = f" ({rel(frame.filename)}:{frame.lineno})" if frame else ""
        rows.append(f"{type_name(nxt)}: {message_of(nxt)[:MESSAGE_MAX]}{where}")
        cur = nxt
    return "\n".join(rows)


def _window_text(seen: int) -> str:
    if seen <= 1:
        return "1x"
    minutes = config.dedupe_window() / 60
    span = f"{minutes:g} min" if minutes >= 1 else f"{config.dedupe_window():g} s"
    return f"{seen}x in the last {span}"


def singular(noun: str) -> str:
    for plural, single in (("sses", "ss"), ("shes", "sh"), ("ches", "ch"), ("xes", "x"), ("ies", "y")):
        if noun.endswith(plural):
            return noun[: -len(plural)] + single
    return noun[:-1] if noun.endswith("s") and not noun.endswith("ss") else noun


def listed(names: list[str], shown: int = SHOWN) -> str:
    """``"a, b, c, d, e and 7 more"``."""
    head = ", ".join(names[:shown])
    return f"{head} and {len(names) - shown} more" if len(names) > shown else head


def summary_title(run: RunSummary) -> str:
    if run.total:
        noun = singular(run.noun) if run.total == 1 else run.noun
        return f"Job {run.job}: {run.failed} of {run.total} {noun} failed"
    noun = singular(run.noun) if run.failed == 1 else run.noun
    return f"Job {run.job}: {run.failed} {noun} failed"


def _f(name: str, value: str | None, *, inline: bool = True, code: bool = False) -> Field:
    text = scrub((value or "").strip()) or "-"
    return Field(name=scrub(name), value=text, inline=inline, code=code)


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text)[:40].strip("-") or "alert"


# --------------------------------------------------------------------------------------------------
# cards
# --------------------------------------------------------------------------------------------------
def build_card(event: Event, fp: str, seen: int) -> Card:
    if event.run is not None:
        return _job_card(event, event.run, fp, seen)
    if event.tbe is None:  # pragma: no cover - events are always built with one
        raise ValueError("event has no exception")
    tbe = event.tbe
    env = config.env_name()
    kind_name = type_name(tbe)
    if event.job:
        where = f"job {event.job}"
    elif event.route:
        where = f"{event.method or ''} {event.route}".strip()
    else:
        where = event.service
    status = "Handled" if event.handled else "Unhandled"
    title = scrub(f"{config.title_prefix(env)}{status} {kind_name} in {where}")
    values = dict(event.values)
    customer = " ".join(
        v for v in (values.pop("customer", ""), values.pop("customer_id", ""), values.pop("phone", "")) if v
    )
    tenant, tenant_id = values.pop("tenant", ""), values.pop("tenant_id", "")
    if tenant and tenant_id:
        tenant = f"{tenant} ({tenant_id})"
    fields = [
        _f("Service", event.service),
        _f("Environment", env),
        _f("Type", kind_name),
        _f("Job" if event.job else "Route", event.job or where),
        _f("Request id", event.request_id),
        _f("Tenant", tenant or tenant_id),
        _f("Customer", customer),
        _f("Feature", values.pop("feature", "")),
        _f("Release", config.release()),
        _f("Seen", _window_text(seen)),
        _f("Host", config.host()),
    ]
    fields += [_f(k.replace("_", " ").capitalize(), v) for k, v in sorted(values.items())]
    if event.logged:
        fields.append(_f("Logged", event.logged, inline=False))
    frame = deepest_app_frame(tbe)
    if frame is not None:
        fields.append(
            _f(
                f"Raised at {rel(frame.filename)}:{frame.lineno} in {frame.name}",
                source_excerpt(frame),
                inline=False,
                code=True,
            )
        )
    fields.append(_f("Stack (your code first)", stack_summary(tbe), inline=False, code=True))
    causes = cause_chain(tbe)
    if causes:
        fields.append(_f("Caused by", causes, inline=False, code=True))
    text = scrub("".join(tbe.format()))[-TRACEBACK_MAX:]
    return Card(
        kind="handled" if event.handled else "unhandled",
        title=title,
        message=message_of(tbe) or kind_name,
        fields=fields,
        environment=env,
        service=scrub(event.service),
        fingerprint=fp,
        timestamp=event.at,
        seen=seen,
        attachment_name=f"traceback-{_safe_name(event.request_id or fp)}.txt",
        attachment=text.encode("utf-8", "replace"),
    )


def _job_card(event: Event, run: RunSummary, fp: str, seen: int) -> Card:
    env = config.env_name()
    first = run.first
    errors = sorted(run.errors.items(), key=lambda kv: (-kv[1], kv[0]))
    error_rows = [f"{name}: {count}" for name, count in errors[:SHOWN]]
    if len(errors) > SHOWN:
        error_rows.append(f"and {len(errors) - SHOWN} more types")
    failed = f"{run.failed} of {run.total}" if run.total else str(run.failed)
    fields = [
        _f("Service", event.service),
        _f("Environment", env),
        _f("Job", run.job),
        _f("Failed", failed),
        _f("Release", config.release()),
        _f("Seen", _window_text(seen)),
        _f("Errors", "\n".join(error_rows), inline=False, code=True),
        _f(run.noun.capitalize(), listed(run.items), inline=False),
    ]
    if run.demo_skipped:
        n = run.demo_skipped
        fields.append(
            _f("Demo items", f"{n} failure{'s' if n != 1 else ''} skipped (demo data, not alerted)")
        )
    fields += [_f("Request id", event.request_id), _f("Host", config.host())]
    tbe = first.tbe if first else None
    if tbe is not None:
        frame = deepest_app_frame(tbe)
        if frame is not None:
            fields.append(
                _f(
                    f"First failure at {rel(frame.filename)}:{frame.lineno} in {frame.name}",
                    source_excerpt(frame),
                    inline=False,
                    code=True,
                )
            )
        fields.append(_f("Stack (your code first)", stack_summary(tbe), inline=False, code=True))
    if first is not None:
        message = f"{first.label} · {first.error}: {first.message}"
    else:
        message = run.lines[0] if run.lines else f"{run.failed} failed"
    body = (
        f"{summary_title(run)}\n\nEvery failure in this run:\n"
        + "\n".join(run.lines)
        + "\n\nFirst traceback:\n"
        + ("".join(tbe.format()) if tbe is not None else "-\n")
    )
    return Card(
        kind="job",
        title=scrub(config.title_prefix(env) + summary_title(run)),
        message=scrub(message),
        fields=fields,
        environment=env,
        service=scrub(event.service),
        fingerprint=fp,
        timestamp=event.at or datetime.now(timezone.utc),
        seen=seen,
        attachment_name=f"job-{_safe_name(run.job)}-{fp}.txt",
        attachment=scrub(body)[-TRACEBACK_MAX:].encode("utf-8", "replace"),
    )
