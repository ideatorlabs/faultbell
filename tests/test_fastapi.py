from __future__ import annotations

import logging

import pytest
from fastapi import Depends, FastAPI, HTTPException, WebSocket
from fastapi.testclient import TestClient

import faultbell
from conftest import FakeWebhook
from faultbell.integrations import RequestContextMiddleware


def make_app() -> FastAPI:
    app = FastAPI()
    faultbell.install(app, service="orders-api")

    def current_tenant() -> str:  # a *sync* dependency: runs in the threadpool
        faultbell.set_context(tenant="Demo Café", tenant_id="t-1001", customer="cus_0001xyz")
        return "t-1001"

    @app.get("/orders/{order_id}")
    def get_order(order_id: int, tenant: str = Depends(current_tenant)) -> dict[str, int]:
        if order_id == 404:
            raise HTTPException(status_code=404, detail="no such order")
        return {"total": 100 // (order_id - order_id)}

    @app.get("/refunds/{refund_id}")
    async def refund(refund_id: int) -> dict[str, str]:
        try:
            raise ConnectionError(f"payment gateway refused refund {refund_id}")
        except ConnectionError:
            logging.getLogger("orders.refunds").exception("refund %s failed", refund_id)
        return {"status": "queued"}

    class Conflict(Exception):
        status_code = 409

    @app.get("/conflict")
    def conflict() -> None:
        raise Conflict("already paid")

    @app.get("/me")
    def me() -> dict[str, str | None]:
        return {"request_id": faultbell.request_id()}

    return app


@pytest.fixture
def client(live: FakeWebhook) -> TestClient:
    return TestClient(make_app(), raise_server_exceptions=False)


def test_unhandled_route_error_sends_one_card(client: TestClient, live: FakeWebhook) -> None:
    response = client.get("/orders/7")
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    rid = response.headers["x-request-id"]
    faultbell.flush(5)

    [request] = live.wait_for(1)
    payload, filename, attachment = request.discord()
    embed = payload["embeds"][0]
    assert embed["title"] == "Unhandled ZeroDivisionError in GET /orders/{order_id}"
    assert embed["color"] == 0xE5484D
    fields = request.fields()
    assert fields["Service"] == "orders-api"
    assert fields["Route"] == "GET /orders/{order_id}"
    assert fields["Request id"] == rid
    assert fields["Tenant"] == "Demo Café (t-1001)"  # set in the sync dependency
    assert fields["Customer"] == "ref cus_0001"
    assert filename == f"traceback-{rid}.txt"
    assert "ZeroDivisionError" in attachment and "get_order" in attachment
    assert any(name.startswith("Raised at tests/test_fastapi.py:") for name in fields)


def test_incoming_request_id_is_reused(client: TestClient, live: FakeWebhook) -> None:
    response = client.get("/me", headers={"X-Request-Id": "edge-0001-abcd"})
    assert response.json() == {"request_id": "edge-0001-abcd"}
    assert response.headers["x-request-id"] == "edge-0001-abcd"
    bad = client.get("/me", headers={"X-Request-Id": "no"})  # too short: replaced
    assert bad.headers["x-request-id"] != "no"


def test_client_errors_do_not_alert(client: TestClient, live: FakeWebhook) -> None:
    assert client.get("/orders/404").status_code == 404
    assert client.get("/orders/not-a-number").status_code == 422
    assert client.get("/conflict").status_code == 500  # a 4xx-shaped exception the app forgot to map
    faultbell.flush(5)
    assert live.received == []


def test_logged_exception_in_a_route_is_a_handled_card(client: TestClient, live: FakeWebhook) -> None:
    assert client.get("/refunds/3").json() == {"status": "queued"}
    faultbell.flush(5)
    [request] = live.wait_for(1)
    assert request.embed()["title"] == "Handled ConnectionError in GET /refunds/{refund_id}"
    assert request.fields()["Logged"] == "orders.refunds: refund 3 failed"


def test_install_is_idempotent_per_app(live: FakeWebhook) -> None:
    app = FastAPI()
    faultbell.install(app, service="a")
    faultbell.install(app, service="a")
    assert sum(1 for m in app.user_middleware if m.cls is RequestContextMiddleware) == 1
    root = logging.getLogger()
    assert sum(1 for h in root.handlers if type(h).__name__ == "AlertHandler") == 1


def test_unmatched_route_template_folds_ids() -> None:
    from faultbell._capture import route_of

    assert route_of({"method": "GET", "path": "/files/12345/9f8e7d6c-aaaa"}) == ("GET", "/files/{id}/{id}")
    assert route_of(None) == (None, None)

    class Route:
        path_format = "/items/{item_id}"

    assert route_of({"method": "POST", "route": Route(), "root_path": "/v2"}) == (
        "POST",
        "/v2/items/{item_id}",
    )


def test_websocket_and_lifespan_pass_through(live: FakeWebhook) -> None:
    app = make_app()

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("hi")
        await websocket.close()

    with TestClient(app) as client, client.websocket_connect("/ws") as conn:
        assert conn.receive_text() == "hi"
