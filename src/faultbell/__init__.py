"""Faultbell: drop-in exception alerting for Python services and cron jobs.

One readable, scrubbed alert per problem, sent from a background thread to Discord, Slack, Microsoft
Teams, Google Chat, Telegram, PagerDuty, email, any JSON webhook or your own function.

    import faultbell
    faultbell.install(app, service="orders-api")   # app is optional

Configure at least one channel (e.g. ``FAULTBELL_SLACK_URL``); with none, every call is a no-op.
"""

from __future__ import annotations

from . import config as _config
from . import sender as _sender
from ._capture import capture, is_client_error
from ._context import context, request_id, set_context
from ._scrub import customer_ref, mask_phone, scrub
from ._version import __version__
from .card import Card, Field
from .channels import (
    CallbackTransport,
    EmailTransport,
    GoogleChatTransport,
    PagerDutyTransport,
    SlackTransport,
    TeamsTransport,
    TelegramTransport,
    channels_from_env,
)
from .config import DEFAULT_KEY_PREFIXES, Settings, configure, enabled, settings
from .config import channels as active_channels
from .integrations import RequestContextMiddleware, install, install_asgi, uninstall
from .jobs import JobRun, job_failed, job_run
from .sender import flush, stats
from .transports import Delivery, DiscordTransport, JsonWebhookTransport, Transport


def reset() -> None:
    """Forget settings, de-duplication state and counters, and stop the sender thread.

    Meant for tests; ``install()`` hooks stay in place (use :func:`uninstall` for those).
    """
    _sender.reset_state()
    _config.reset_settings()


__all__ = [
    "enabled",
    "channels_from_env",
    "active_channels",
    "TelegramTransport",
    "TeamsTransport",
    "SlackTransport",
    "PagerDutyTransport",
    "GoogleChatTransport",
    "EmailTransport",
    "CallbackTransport",
    "DEFAULT_KEY_PREFIXES",
    "Card",
    "Delivery",
    "DiscordTransport",
    "Field",
    "JobRun",
    "JsonWebhookTransport",
    "RequestContextMiddleware",
    "Settings",
    "Transport",
    "__version__",
    "capture",
    "configure",
    "context",
    "customer_ref",
    "flush",
    "install",
    "install_asgi",
    "is_client_error",
    "job_failed",
    "job_run",
    "mask_phone",
    "request_id",
    "reset",
    "scrub",
    "set_context",
    "settings",
    "stats",
    "uninstall",
]
