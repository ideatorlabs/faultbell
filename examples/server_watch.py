"""Server watch on a FastAPI service: bursts of 5xx per route, memory, crash heartbeat.

python examples/local_webhook.py &
FAULTBELL_WEBHOOK_URL=http://127.0.0.1:8765/ FAULTBELL_SEND_LOCAL=1 uvicorn examples.server_watch:app
for i in 1 2 3; do curl -s -o /dev/null -w "%{http_code}\\n" http://127.0.0.1:8000/stock/7; done
# -> one card: "3 5xx responses in 5 minutes on GET /stock/{item_id}"
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

import faultbell
from faultbell.watch import Watch, WatchMiddleware

# 512 MB like a small container; pass store=SQLHeartbeatStore(engine) to detect crashes across restarts.
watch = Watch(memory_limit_mb=512)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    watch.start()
    yield
    watch.stop()


app = FastAPI(title="stock-api", lifespan=lifespan)
faultbell.install(app, service="stock-api")
app.add_middleware(WatchMiddleware, watch=watch)


@app.get("/stock/{item_id}")
def stock(item_id: int) -> dict[str, int]:
    if item_id == 7:  # the supplier feed for item 7 is down
        raise HTTPException(status_code=503, detail="supplier feed unavailable")
    return {"item_id": item_id, "on_hand": 12}
