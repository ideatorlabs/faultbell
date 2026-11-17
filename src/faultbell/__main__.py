"""``python -m faultbell channels`` lists where alerts would go; ``python -m faultbell test`` sends one test
alert to every channel and reports what happened. Both read the same ``FAULTBELL_*`` variables as your app."""

from __future__ import annotations

import argparse
import sys
from urllib.parse import urlsplit

from . import config, sender
from ._capture import capture
from ._version import __version__


class FaultbellTestAlert(Exception):
    """Raised on purpose by ``python -m faultbell test``."""


def _where(destination: str) -> str:
    """Scheme and host only: webhook paths and bot URLs carry secrets."""
    parts = urlsplit(destination)
    if parts.scheme in ("http", "https"):
        return f"{parts.scheme}://{parts.hostname or ''}/…"
    return destination


def _list() -> int:
    found = config.channels()
    env = config.env_name()
    if not found:
        reason = (
            "this is the local environment (set FAULTBELL_SEND_LOCAL=1 or FAULTBELL_ENV)"
            if env == "local"
            else "no channel is configured"
        )
        print(f"No channels: {reason}.")
        return 1
    print(f"Environment: {env}")
    for transport, destination in found:
        kinds = ", ".join(sorted(transport.kinds)) if transport.kinds else "all"
        print(f"  {transport.name:<12} {_where(destination):<45} kinds: {kinds}")
    return 0


def _test(timeout: float) -> int:
    if _list():
        return 1
    try:
        raise FaultbellTestAlert("This is a test alert from `python -m faultbell test`. All good.")
    except FaultbellTestAlert as exc:
        capture(exc, handled=False, job=None)
    drained = sender.flush(timeout)
    stats = sender.stats()
    print(f"Sent: {stats['sent']}  Failed: {stats['failed']}  Dropped: {stats['dropped']}")
    if not drained:
        print("Timed out waiting for the channels.", file=sys.stderr)
    return 0 if drained and stats["failed"] == 0 and stats["sent"] > 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m faultbell", description=__doc__)
    parser.add_argument("--version", action="version", version=f"faultbell {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("channels", help="list the channels alerts would go to")
    test = sub.add_parser("test", help="send one test alert to every channel")
    test.add_argument("--timeout", type=float, default=15.0, help="seconds to wait for delivery")
    args = parser.parse_args(argv)
    if args.command == "channels":
        return _list()
    config.configure(dedupe_window=0)
    return _test(args.timeout)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
