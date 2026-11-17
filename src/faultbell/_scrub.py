"""Remove secrets and personal data from text before it leaves the process.

Applied to every title, message, field and traceback. The patterns are deliberately greedy: a card
that says ``[redacted]`` once too often is better than a leaked token.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import os
import re
from collections.abc import Callable, Iterable

from . import config

Replacement = str | Callable[[re.Match[str]], str]

_DATE_LIKE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ID_LIKE = re.compile(r"^[A-Za-z0-9_-]{6,}$")


def mask_phone(raw: str) -> str:
    """``"+1 555 0100 123"`` becomes ``"••0123"``: never more than the last four digits."""
    digits = re.sub(r"\D", "", raw or "")
    return f"••{digits[-4:]}" if digits else ""


def _phone_or_keep(match: re.Match[str]) -> str:
    text = match.group(0)
    digits = re.sub(r"\D", "", text)
    if not 10 <= len(digits) <= 13 or _DATE_LIKE.match(text):
        return text
    return mask_phone(text)


def customer_ref(raw: str) -> str:
    """A short, stable reference for a customer, never their name.

    Something id-like (letters/digits, at least one digit) becomes ``ref 1a2b3c4d``. Anything else (a
    name, an email) becomes a keyed 8-character hash, ``cust 5f0c9e21``, so repeated cards about the
    same person can be correlated without identifying them. Set ``FAULTBELL_REF_KEY`` to make the hash
    unguessable outside your deployment.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if _ID_LIKE.match(text) and any(ch.isdigit() for ch in text):
        return f"ref {text.replace('-', '')[:8]}"
    key = (os.environ.get("FAULTBELL_REF_KEY") or "faultbell").encode()
    return "cust " + hmac.new(key, text.lower().encode(), hashlib.sha256).hexdigest()[:8]


@functools.lru_cache(maxsize=8)
def _patterns(key_prefixes: tuple[str, ...]) -> tuple[tuple[re.Pattern[str], Replacement], ...]:
    rules: list[tuple[re.Pattern[str], Replacement]] = [
        # Webhook URLs (Discord, Slack and anything with a /webhooks/ or /hooks/ segment).
        (
            re.compile(r"https?://[^\s\"'<>]*?/(?:api/webhooks|webhooks?|hooks|services)/[^\s\"'<>]+", re.I),
            "[webhook]",
        ),
        # user:password@ in any URL.
        (re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@", re.I), r"\1[redacted]@"),
        (re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]+", re.I), "Bearer [redacted]"),
        (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), "[jwt]"),
    ]
    if key_prefixes:
        alternatives = "|".join(re.escape(p) for p in sorted(key_prefixes, key=len, reverse=True))
        rules.append(
            (
                re.compile(rf"(?<![A-Za-z0-9])({alternatives})(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{{8,}}"),
                r"\1[redacted]",
            )
        )
    rules += [
        # key=..., "token": "...", password: ... (one line at a time).
        (
            re.compile(
                r"(?i)\b([\w-]*?(?:key|token|passw(?:or)?d|pwd|secret|signature|authorization|cookie))\b"
                r"(['\"]?)([ \t]*[=:][ \t]*)(['\"]?)[^\s'\"&,;)}\]]+"
            ),
            r"\1\2\3\4[redacted]",
        ),
        (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[email]"),
        (re.compile(r"(?<![\w.])\+?\d[\d \-]{8,16}\d(?![\w.])"), _phone_or_keep),
        (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "[hex]"),
        # Long base64/url-safe runs that mix letters and digits.
        (
            re.compile(r"(?<![\w/])(?=[A-Za-z0-9+_=-]*\d)(?=[A-Za-z0-9+_=-]*[A-Za-z])[A-Za-z0-9+_=-]{40,}"),
            "[redacted]",
        ),
    ]
    return tuple(rules)


def scrub(text: str, *, key_prefixes: Iterable[str] | None = None) -> str:
    """Return ``text`` with secrets and personal data replaced.

    ``key_prefixes`` defaults to the configured list (``faultbell.config.DEFAULT_KEY_PREFIXES``
    unless you changed it).
    """
    if not text:
        return ""
    prefixes = tuple(key_prefixes) if key_prefixes is not None else config.settings().key_prefixes
    for pattern, replacement in _patterns(prefixes):
        text = pattern.sub(replacement, text)
    return text
