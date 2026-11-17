# Concepts

```
 your code                       Faultbell                                   webhook
 ─────────                       ────────                                   ───────
 install() hooks ─┐
 logger.exception ├─> capture ─> client error? already seen? ──> snapshot ─> bounded queue
 capture(exc)     │              no webhook? -> stop               (no frames, no locals)   │
 job_run().fail ──┘              inside job_run? -> fold into run                           v
                                                                         sender thread (daemon)
                                                                fingerprint -> de-dupe window
                                                                build Card -> scrub -> rate limit
                                                                each channel ──> Discord / Slack / Teams /
                                                                                 Chat / Telegram / PagerDuty /
                                                                                 email / webhook / your fn
```

- **Card**: a transport-neutral description of one alert (title, message, fields, traceback attachment).
- **Fingerprint**: exception type + deepest frame in *your* code + message with numbers and ids removed.
  Same fingerprint inside the window: suppressed and counted.
- **Your code vs library code**: frames under the app root (default: the working directory) that are not in
  `site-packages`, the standard library or Faultbell itself. The card shows the source around the deepest one.
- **Environment**: `production`, `staging`, `local` (or any name). Non-production titles are prefixed, e.g.
  `[STAGING]`. `local` never sends unless `FAULTBELL_SEND_LOCAL=1`, because a developer's `.env` often holds
  the production webhook.
