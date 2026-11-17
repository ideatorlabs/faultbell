# Channels

Set the variables for the channels you want; every alert goes to all of them.

| Channel | In code | Environment variables | What arrives |
| --- | --- | --- | --- |
| Discord | `DiscordTransport(url)` | `FAULTBELL_DISCORD_URL` (or the main `FAULTBELL_WEBHOOK_URL`) | An embed with the traceback attached as a file |
| Slack | `SlackTransport(url)` | `FAULTBELL_SLACK_URL` (an incoming webhook) | A Block Kit message with a colour bar and the end of the traceback |
| Microsoft Teams | `TeamsTransport(url)` | `FAULTBELL_TEAMS_URL` (a Workflows or incoming webhook) | One Adaptive Card |
| Google Chat | `GoogleChatTransport(url)` | `FAULTBELL_GOOGLE_CHAT_URL` (a space webhook) | A formatted text message |
| Telegram | `TelegramTransport(bot_token, chat_id)` | `FAULTBELL_TELEGRAM_BOT_TOKEN`, `FAULTBELL_TELEGRAM_CHAT_ID`, optional `FAULTBELL_TELEGRAM_THREAD_ID` | A bot message with the traceback as a file |
| PagerDuty | `PagerDutyTransport(routing_key)` | `FAULTBELL_PAGERDUTY_ROUTING_KEY` | An Events API v2 trigger; repeats share one incident. Crashes and job summaries only, by default |
| Email | `EmailTransport(to, host=..., port=..., username=..., password=...)` | `FAULTBELL_EMAIL_TO`, `FAULTBELL_SMTP_HOST`, `FAULTBELL_SMTP_PORT`, `FAULTBELL_SMTP_USER`, `FAULTBELL_SMTP_PASSWORD`, optional `FAULTBELL_EMAIL_FROM`, `FAULTBELL_SMTP_SECURITY`, `FAULTBELL_EMAIL_SUBJECT_PREFIX` | A text and HTML email with the traceback attached |
| Any JSON endpoint | `JsonWebhookTransport(url)` | `FAULTBELL_JSON_URL` | `Card.as_dict()` plus a `text` line (Zapier, Make, n8n, your own API) |
| Anything else | `CallbackTransport(fn)` | none | Your function gets the `Card`: SMS, a ticket, a queue message |

- **Which alerts a channel gets**: `kinds=` in code, or `FAULTBELL_<CHANNEL>_KINDS` (e.g.
  `FAULTBELL_EMAIL_KINDS=unhandled,job`). The kinds are `unhandled` (a crash nobody caught), `handled`
  (`capture()` or `logger.exception`) and `job` (a job-run summary).
- **Email security**: `FAULTBELL_SMTP_SECURITY` is `auto` by default: port 465 uses TLS, 587 uses STARTTLS,
  anything else plain SMTP (for a local relay). Set `starttls`, `ssl` or `none` to force one.
- **Chat webhooks can't upload files**, so Slack, Teams and Google Chat show the last 1,500 characters of the
  traceback. Discord, Telegram and email carry the full one.

The same in code (values passed here replace the channel variables; the main webhook is always kept):

```python
import os

import faultbell
from faultbell import CallbackTransport, EmailTransport, PagerDutyTransport, SlackTransport, TelegramTransport

faultbell.configure(
    channels=[
        SlackTransport(os.environ.get("SLACK_WEBHOOK_URL")),
        TelegramTransport(os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")),
        EmailTransport(
            "oncall@example.com",
            host="smtp.example.com",
            port=587,  # STARTTLS
            username=os.environ.get("SMTP_USER"),
            password=os.environ.get("SMTP_PASSWORD"),
            kinds=["unhandled", "job"],  # no email for handled exceptions
        ),
        PagerDutyTransport(os.environ.get("PAGERDUTY_ROUTING_KEY", "")),  # crashes and jobs only
        CallbackTransport(lambda card: print("ALERT", card.title), name="stdout"),
    ]
)
print([transport.name for transport, _ in faultbell.active_channels()])
```

A channel with missing settings (an empty URL or token) is skipped. Check what is configured, then send a
test alert to every channel:

```bash
python -m faultbell channels
python -m faultbell test
```

How delivery works: repeats are de-duplicated once, before the alert is sent anywhere. Channels are sent one
after another on Faultbell's own thread; one that fails or times out is counted in `stats()` and the rest
still get the alert. The `local` environment stays quiet on every channel unless `FAULTBELL_SEND_LOCAL=1`.
