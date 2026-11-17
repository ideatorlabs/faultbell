# Limits & roadmap

## Limitations and non-goals

- Not a replacement for a full error tracker: no UI, no search, no issue history, no performance tracing.
- De-duplication state lives in each process; several workers can each send the first card of a problem.
- Delivery is best effort by design. A dropped alert is counted in `stats()`, never retried later.
- Chat webhooks (Slack, Teams, Google Chat) can't take file uploads, so they show only the end of the
  traceback. Each channel's message stays inside that service's documented size limits.
- Channels are sent one after another on one thread, so a slow channel (an SMTP server near its timeout)
  delays the ones after it by up to `timeout` seconds.
- Scrubbing is pattern based. It catches common shapes of secrets and personal data, not every possible one.
  Keep secrets out of exception messages where you can.

## Roadmap

- A response watcher (bursts of 5xx/4xx per route) and a memory watermark card.
- More channels on request (Opsgenie, SMS through a provider).
- Optional shared de-duplication through Redis.
