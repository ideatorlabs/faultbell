# Extending

For something not covered, `CallbackTransport(fn)` is often enough. For a new HTTP service, subclass
`Transport` and implement `encode()`, or override `send()` for full control. A push-notification channel for
[ntfy](https://ntfy.sh):

```python
import faultbell
from faultbell import Card, Delivery, Transport
from faultbell.transports import http_post


class NtfyTransport(Transport):
    name = "ntfy"

    def send(self, card: Card, url: str, timeout: float) -> Delivery:
        headers = {
            **self.headers,
            "Title": card.title.encode("ascii", "replace").decode(),  # HTTP headers are ASCII
            "Priority": "high" if card.kind == "unhandled" else "default",
        }
        return http_post(
            url, card.message.encode(), "text/plain; charset=utf-8", timeout=timeout, headers=headers
        )


faultbell.configure(channels=[NtfyTransport("https://ntfy.sh/your-alerts-topic", kinds=["unhandled"])])
```

A channel decides `ready()` (configured enough to send) and `destination()` (passed to `send()` as `url`);
the defaults use the URL given to the constructor.
