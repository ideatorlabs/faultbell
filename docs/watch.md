# Server watch


Some failures never raise in your code: an endpoint that keeps answering 503, a process creeping toward its
memory limit, or a container killed for using too much memory. `faultbell.watch` turns those into normal
Faultbell cards.

```python
from fastapi import FastAPI, HTTPException

import faultbell
from faultbell.watch import Watch, WatchMiddleware

app = FastAPI(title="stock-api")
faultbell.install(app, service="stock-api")
watch = Watch(memory_limit_mb=512)                 # or FAULTBELL_MEMORY_LIMIT_MB, or the cgroup limit
app.add_middleware(WatchMiddleware, watch=watch)   # counts responses per route template


@app.get("/stock/{item_id}")
def stock(item_id: int) -> dict[str, int]:
    raise HTTPException(status_code=503)          # three of these in 5 minutes -> one card
```

Call `watch.start()` at startup to run memory checks and the heartbeat on a daemon thread. For crash
detection across restarts, give it a shared store: `Watch(store=SQLHeartbeatStore(engine))`
(`pip install "faultbell[sqlalchemy]"`). It creates one small table, `faultbell_heartbeats`.

| Check | Default | Change with |
|---|---|---|
| 5xx burst on one route | 3 in 5 minutes | `burst_5xx`, `window` |
| 4xx burst on one route | 30 in 5 minutes | `burst_4xx`, `window` |
| Memory | 80% of the limit | `memory_limit_mb`, `memory_alert_percent` |
| Crash | no heartbeat for 150 s and no clean stop | `beat_every`, `dead_after`, `store` |
| Repeat cards | one per check and route per 10 minutes | `quiet` |

Health-check paths (`/health`, `/ready`, …) are never counted (`ignore_paths`).

