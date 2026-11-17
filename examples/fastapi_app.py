"""A FastAPI service with Faultbell installed.

python examples/local_webhook.py &
FAULTBELL_WEBHOOK_URL=http://127.0.0.1:8765/ FAULTBELL_SEND_LOCAL=1 uvicorn examples.fastapi_app:app
curl -i http://127.0.0.1:8000/orders/0        # 500 -> one "Unhandled ZeroDivisionError" card
curl -i http://127.0.0.1:8000/orders/404      # 404 -> client error, no card
"""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, Header, HTTPException

import faultbell

app = FastAPI(title="orders-api")
faultbell.install(app, service="orders-api")
log = logging.getLogger("orders")

ORDERS = {1: {"items": 3, "amount": 1200}, 0: {"items": 0, "amount": 0}}


def current_tenant(x_tenant_id: str = Header(default="t-1001")) -> str:
    # A sync dependency runs in a thread pool; its context still reaches the rest of the request.
    faultbell.set_context(tenant="Demo Café", tenant_id=x_tenant_id, feature="orders")
    return x_tenant_id


@app.get("/orders/{order_id}")
def get_order(order_id: int, tenant_id: str = Depends(current_tenant)) -> dict[str, float]:
    order = ORDERS.get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return {"average_item_price": order["amount"] / order["items"]}  # order 0 divides by zero


@app.post("/orders/{order_id}/receipt")
def send_receipt(order_id: int, tenant_id: str = Depends(current_tenant)) -> dict[str, str]:
    try:
        raise ConnectionError(f"mail relay refused receipt for order {order_id}")
    except ConnectionError:
        log.exception("receipt not sent")  # handled: the user still gets an answer, you get a card
    return {"status": "queued"}


if __name__ == "__main__":
    import uvicorn  # uv pip install uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
