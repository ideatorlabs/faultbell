---
hide:
  - navigation
---

<div class="il-hero" markdown>
<img src="assets/brand/icon-512.png" alt="" width="112" height="112">
<div markdown>
# Faultbell
<p class="il-tagline">Exception alerts, wherever your team already looks</p>
<p class="il-by">An open-source package by <a href="https://ideatorlabs.github.io/">Ideator Labs</a></p>
[Get started](#install){ .md-button .md-button--primary }
[GitHub](https://github.com/ideatorlabs/faultbell){ .md-button }
</div>
</div>

## Why it exists

Small teams rarely run a full error-tracking stack, so production exceptions end up in logs nobody checks.
Faultbell sends each problem to the channels you already watch (Discord, Slack, Microsoft Teams, Google
Chat, Telegram, PagerDuty, email, any JSON webhook or your own function) as one tidy alert with the
traceback, without leaking secrets, flooding the channel or slowing your requests down.

## Features

- **One call to wire in**: `install(app, service=...)` adds a logging handler, `sys` / `threading` / `asyncio`
  hooks and, for FastAPI/Starlette, a request-id middleware plus a catch-all 500 handler. Idempotent.
- **Never in the request path**: capture only snapshots the exception (no frames, no locals) onto a bounded
  queue; one daemon thread sends. Full queue, webhook error or timeout: the alert is dropped and counted.
  Faultbell never raises into your code.
- **No noise**: client errors (anything with an int `status_code` / `status` below 500) never alert. Repeats
  of the same problem are sent at most once per window (default 10 minutes); the next card says how many
  were seen.
- **Job runs**: `job_run()` folds every item failure in a cron run into **one** summary card
  ("Job nightly-sync: 3 of 41 tenants failed"). Nested runs fold into the outer one; demo/test tenants never
  alert.
- **Scrubbed everywhere** (title, message, fields, traceback): emails, Bearer tokens, JWTs, API keys by prefix
  (configurable), `key=` / `token=` / `password=` values, long hex/base64 strings, webhook URLs, URL
  credentials, phone numbers (last four digits kept). Customers appear only as short references.
- **Context that follows the request**: `set_context()` / `context()` backed by a `ContextVar`, shared
  correctly with sync dependencies that run in a thread pool.
- **Every channel you use**: Discord, Slack, Microsoft Teams, Google Chat, Telegram, PagerDuty, email (any
  SMTP server), any JSON webhook, or a function of your own for SMS, tickets or a queue. Set one environment
  variable per channel, or list them in code.
- **Several channels at once**: one alert goes to all of them; a failing channel never blocks the others.
  Each channel can take only some alerts (e.g. PagerDuty and email for crashes, Slack for everything).
- **Check your setup in one command**: `python -m faultbell test` sends a test alert to every channel.
- **Zero dependencies** in the core (standard library only). FastAPI support is an optional extra.

## Install

```bash
pip install faultbell            # core, standard library only
pip install "faultbell[fastapi]" # with FastAPI
```

> Until the first PyPI release, install straight from GitHub:
> `pip install "faultbell @ git+https://github.com/ideatorlabs/faultbell"`

## 60-second quickstart

Create a webhook in Discord (channel settings, Integrations, Webhooks) and export it:

```bash
export FAULTBELL_WEBHOOK_URL="<your Discord webhook URL>"
export FAULTBELL_ENV=production          # or FAULTBELL_SEND_LOCAL=1 to try it from your laptop
```

```python
import faultbell

faultbell.install(service="billing-worker")  # no webhook configured = everything is a no-op

try:
    1 / 0
except ZeroDivisionError as exc:
    faultbell.capture(exc)  # logger.exception(...) works too once install() has run

with faultbell.job_run("nightly-invoices", total=3, noun="invoices") as run:
    for invoice_id in ("inv-1", "inv-2", "inv-3"):
        try:
            if invoice_id == "inv-2":
                raise TimeoutError("payment gateway timed out")
        except Exception as exc:
            run.fail(exc, item=invoice_id)

faultbell.flush()  # short scripts: wait for the cards to go out before exiting
```

That sends two cards: "Handled ZeroDivisionError in billing-worker" and
"Job nightly-invoices: 1 of 3 invoices failed".

With FastAPI:

```python
from fastapi import FastAPI

import faultbell

app = FastAPI()
faultbell.install(app, service="orders-api")


@app.get("/orders/{order_id}")
def get_order(order_id: int) -> dict[str, float]:
    faultbell.set_context(tenant="Demo Café", tenant_id="t-1001", customer="cus_0001")
    return {"total": 100 / order_id}  # /orders/0 -> "Unhandled ZeroDivisionError in GET /orders/{order_id}"
```

Every response carries an `X-Request-Id` header; the same id is on the card, so a user's report can be
matched to the alert.
