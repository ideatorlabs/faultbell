# Configuration

Environment variables (read on every alert, so runtime changes apply immediately):

| Variable | Default | Meaning |
| --- | --- | --- |
| `FAULTBELL_WEBHOOK_URL` | unset | The main webhook. With it and every channel variable unset, Faultbell is a total no-op and starts no thread. |
| `FAULTBELL_WEBHOOK_URL_<ENV>` | unset | Per-environment override, e.g. `FAULTBELL_WEBHOOK_URL_STAGING`. |
| `FAULTBELL_ENV` | detectors, else `local` | `production`, `staging`, `local` (aliases: `prod`, `live`, `stage`, `dev`, `development`). |
| `FAULTBELL_SEND_LOCAL` | `0` | `1` allows sending from the `local` environment. |
| `FAULTBELL_SERVICE` | `install()`'s name | Service name on every card (beats the name given in code). |
| `FAULTBELL_TRANSPORT` | `discord` | How the main webhook is posted: `discord`, `slack`, `teams`, `google_chat` or `json`. |
| `FAULTBELL_<CHANNEL>_*` | unset | One set per channel; see [Channels](https://github.com/ideatorlabs/faultbell/blob/main/channels.md#channels). |
| `FAULTBELL_RELEASE` | `GIT_SHA`, `SOURCE_VERSION` | Release / commit shown on the card. |
| `FAULTBELL_DEDUPE_WINDOW` | `600` | Seconds between two cards for the same problem. |
| `FAULTBELL_APP_ROOT` | working directory | Folder that holds *your* code. |
| `FAULTBELL_HOST` | hostname | Host shown on the card. |
| `FAULTBELL_REF_KEY` | built-in | Key for the short customer reference hash; set it to make references unguessable. |
| `FAULTBELL_ICON_URL` | unset | Optional icon for the Discord card's author and footer. |

In code, `faultbell.configure(...)` accepts the same and more; values passed there win over the environment:

```python
import os

import faultbell

faultbell.configure(
    env_detectors=[lambda: os.environ.get("MY_PLATFORM_STAGE")],  # used when FAULTBELL_ENV is unset
    webhook_urls={"staging": os.environ.get("STAGING_ALERTS_URL", "")},
    dedupe_window=300,
    key_prefixes=(*faultbell.DEFAULT_KEY_PREFIXES, "acme_live_"),
    queue_size=200,
    timeout=5.0,
    rate_limit=(5, 2.0),  # Discord allows about 5 posts per 2 seconds per webhook
)
```

Other options: `webhook_url`, `channels`, `env`, `service`, `send_local`, `transport`, `app_root`, `release`,
`request_id_headers`, `clock` (inject a fake clock in tests).

## Public API

| Call | What it does |
| --- | --- |
| `install(app=None, *, service)` | Logging handler, `sys` / thread / asyncio hooks; for an ASGI app the middleware and 500 handler. Idempotent. |
| `set_context(*, service, tenant, tenant_id, customer, phone, feature, **extra)` | Merge fields into the current request's or task's context. `None` keeps a value. |
| `context(**same)` | A block with its own context, restored on exit. |
| `request_id()` | The current request's id, or `None`. |
| `capture(exc, *, handled=True, job=None)` | Report an exception. Returns at once. |
| `job_run(job, *, total=None, noun="items", demo_ids=())` | One summary card for a whole run; yields an object with `fail(exc, *, tenant, tenant_id, item, demo, step)`. |
| `job_failed(exc=None, *, job, tenant, tenant_id, item, demo, **extra)` | An item failure from code without the run object. |
| `flush(timeout=2.0)` | Wait for queued alerts; `True` when drained. |
| `stats()` | Counters: `queued`, `sent`, `suppressed`, `dropped`, `failed` (sent and failed count each channel). |
| `active_channels()`, `enabled()` | The channels the next alert would go to, and whether there are any. |
| `scrub(text)`, `mask_phone(raw)`, `customer_ref(raw)` | The scrubbing helpers, usable on their own. |
| `configure(**settings)`, `reset()`, `uninstall()` | Settings; `reset` / `uninstall` are mostly for tests. |
