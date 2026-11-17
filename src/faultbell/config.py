"""Settings and environment resolution.

Everything has a sensible default and can be set two ways: environment variables (handy at deploy time)
or ``configure`` (handy in code and tests). Values passed to ``configure`` win over environment
variables. Settings are read when an alert is captured or sent, so changing the environment at runtime
takes effect immediately.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .transports import Transport

ENV_PREFIX = "FAULTBELL_"

#: Common API key prefixes. Anything that starts with one of these and looks like a key is redacted.
DEFAULT_KEY_PREFIXES: tuple[str, ...] = (
    "sk_",
    "sk-",
    "pk_live_",
    "rk_",
    "rzp_",
    "ghp_",
    "gho_",
    "ghu_",
    "ghs_",
    "github_pat_",
    "glpat-",
    "xoxb-",
    "xoxp-",
    "AKIA",
    "AIza",
)

_ENV_ALIASES = {
    "prod": "production",
    "live": "production",
    "stage": "staging",
    "dev": "local",
    "development": "local",
}

EnvDetector = Callable[[], "str | None"]


@dataclass
class Settings:
    """Runtime settings. Create or change them with ``configure``."""

    #: Webhook used in every environment (beats ``FAULTBELL_WEBHOOK_URL``).
    webhook_url: str | None = None
    #: Per-environment webhooks, e.g. ``{"staging": "https://..."}`` (beats ``FAULTBELL_WEBHOOK_URL_<ENV>``).
    webhook_urls: Mapping[str, str] = field(default_factory=dict)
    #: The environment name (beats ``FAULTBELL_ENV`` and the detectors).
    env: str | None = None
    #: Callables tried in order when no environment is set; the first non-empty answer wins.
    env_detectors: tuple[EnvDetector, ...] = ()
    #: Service name shown on every card (beats ``FAULTBELL_SERVICE``).
    service: str | None = None
    #: Allow sending from the ``local`` environment (beats ``FAULTBELL_SEND_LOCAL``).
    send_local: bool | None = None
    #: How the main webhook (``webhook_url`` / ``FAULTBELL_WEBHOOK_URL``) is posted. Defaults to
    #: ``FAULTBELL_TRANSPORT`` (``discord``, ``slack``, ``teams``, ``google_chat`` or ``json``), else Discord.
    transport: Transport | None = None
    #: Extra channels; every card goes to each of them (plus the main webhook, if set). When empty, the
    #: channels described by ``FAULTBELL_*`` variables are used (see ``faultbell.channels``).
    channels: tuple[Transport, ...] = ()
    #: Repeats of one problem are sent at most once per this many seconds.
    dedupe_window: float | None = None
    #: Alerts waiting to be sent; more than this are dropped.
    queue_size: int = 200
    #: HTTP timeout for one webhook call, in seconds.
    timeout: float = 5.0
    #: At most ``rate_limit[0]`` posts per ``rate_limit[1]`` seconds (Discord allows about 5 per 2 s).
    rate_limit: tuple[int, float] = (5, 2.0)
    #: Key prefixes that are redacted wherever they appear.
    key_prefixes: tuple[str, ...] = DEFAULT_KEY_PREFIXES
    #: Your code lives under this folder; frames elsewhere are "framework" frames. Default: the cwd.
    app_root: str | None = None
    #: Release / commit shown on the card (beats ``FAULTBELL_RELEASE``, ``GIT_SHA``, ``SOURCE_VERSION``).
    release: str | None = None
    #: Incoming request headers that may carry a request id.
    request_id_headers: tuple[str, ...] = ("x-request-id",)
    #: Clock used for the de-duplication window (inject a fake one in tests).
    clock: Callable[[], float] = time.monotonic


_lock = threading.Lock()
_settings = Settings()


def settings() -> Settings:
    """The active settings."""
    return _settings


def configure(**changes: Any) -> Settings:
    """Change settings, e.g. ``configure(webhook_url=..., dedupe_window=60)``. Unknown names raise
    ``TypeError``. Returns the new settings."""
    global _settings
    known = {f.name for f in fields(Settings)}
    unknown = set(changes) - known
    if unknown:
        raise TypeError(f"unknown setting(s): {', '.join(sorted(unknown))}")
    if "env_detectors" in changes:
        changes["env_detectors"] = tuple(changes["env_detectors"] or ())
    if "key_prefixes" in changes:
        changes["key_prefixes"] = tuple(changes["key_prefixes"] or ())
    if "channels" in changes:
        changes["channels"] = tuple(changes["channels"] or ())
    with _lock:
        _settings = replace(_settings, **changes)
    return _settings


def reset_settings() -> None:
    """Back to defaults (used by ``faultbell.reset``)."""
    global _settings, _installed_service
    with _lock:
        _settings = Settings()
        _installed_service = ""


def _env(name: str) -> str:
    return (os.environ.get(ENV_PREFIX + name) or "").strip()


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def normalize_env(name: str) -> str:
    name = name.strip().lower()
    return _ENV_ALIASES.get(name, name)


def env_name() -> str:
    """``production``, ``staging``, ``local`` or any other name you use.

    Order: ``configure(env=...)``, ``FAULTBELL_ENV``, your detectors, else ``local``.
    """
    s = _settings
    if s.env:
        return normalize_env(s.env)
    explicit = _env("ENV")
    if explicit:
        return normalize_env(explicit)
    for detect in s.env_detectors:
        try:
            found = detect()
        except Exception:  # a broken detector must not break alerting
            found = None
        if found:
            return normalize_env(str(found))
    return "local"


def send_local() -> bool:
    if _settings.send_local is not None:
        return bool(_settings.send_local)
    return _truthy(_env("SEND_LOCAL"))


def _muted() -> bool:
    """A ``local`` environment never sends unless ``FAULTBELL_SEND_LOCAL=1``: a developer's ``.env``
    often carries the production settings, and their mistakes are not production incidents."""
    return env_name() == "local" and not send_local()


def webhook_url() -> str:
    """The main webhook for this environment, or ``""`` when it is unset or this environment is muted."""
    if _muted():
        return ""
    return _main_url()


def _main_url() -> str:
    env = env_name()
    s = _settings
    per_env = (s.webhook_urls or {}).get(env) or _env("WEBHOOK_URL_" + env.upper().replace("-", "_"))
    if per_env:
        return per_env.strip()
    transport_url = getattr(s.transport, "url", None)
    return (s.webhook_url or transport_url or _env("WEBHOOK_URL") or "").strip()


def channels() -> list[tuple[Transport, str]]:
    """Every channel that will get the next card, as ``(transport, destination)`` pairs.

    The main webhook comes first, then ``configure(channels=...)`` or, when that is empty, the channels
    in the environment. Empty when nothing is configured or this environment is muted.
    """
    if _muted():
        return []
    from .channels import channels_from_env
    from .transports import transport_named

    found: list[tuple[Transport, str]] = []
    url = _main_url()
    if url:
        main = _settings.transport or transport_named(_env("TRANSPORT") or "discord")
        found.append((main, url))
    for transport in _settings.channels or tuple(channels_from_env()):
        if transport.ready():
            found.append((transport, transport.destination()))
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[Transport, str]] = []
    for transport, dest in found:
        key = (type(transport).__name__, dest)
        if key not in seen:
            seen.add(key)
            unique.append((transport, dest))
    return unique


def enabled() -> bool:
    """True when at least one channel would get a card here; otherwise every call is a no-op."""
    try:
        return bool(channels())
    except Exception:
        return False


_installed_service = ""  # the name given to install(); used outside requests


def set_installed_service(name: str) -> None:
    global _installed_service
    _installed_service = name or _installed_service


def service_name(fallback: str | None = None) -> str:
    """``configure(service=)``, ``FAULTBELL_SERVICE``, the request's service, ``install()``'s, else "app"."""
    return _settings.service or _env("SERVICE") or fallback or _installed_service or "app"


def title_prefix(env: str) -> str:
    """Production titles are bare; anything else says where it came from (``[STAGING] ``)."""
    return "" if env == "production" else f"[{env.upper()}] "


def release() -> str:
    value = (
        _settings.release
        or _env("RELEASE")
        or os.environ.get("GIT_SHA")
        or os.environ.get("SOURCE_VERSION")
        or ""
    ).strip()
    return value[:12] or "-"


def host() -> str:
    try:
        return (_env("HOST") or socket.gethostname() or "-")[:80]
    except Exception:
        return "-"


def dedupe_window() -> float:
    if _settings.dedupe_window is not None:
        return max(0.0, float(_settings.dedupe_window))
    raw = _env("DEDUPE_WINDOW")
    try:
        return max(0.0, float(raw)) if raw else 600.0
    except ValueError:
        return 600.0


def app_root() -> str:
    return os.path.abspath(_settings.app_root or _env("APP_ROOT") or os.getcwd())
