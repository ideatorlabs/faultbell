"""The examples and every Python block in the README, run for real against a local webhook."""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

import faultbell
from conftest import FakeWebhook

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


def load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cron_job_example_end_to_end() -> None:
    """Run examples/cron_job.py as a real process against examples/local_webhook.py."""
    local_webhook = load(EXAMPLES / "local_webhook.py")
    server = local_webhook.serve(port=0)
    try:
        env = {
            k: v for k, v in os.environ.items() if not k.startswith("FAULTBELL_") and k not in ("GIT_SHA",)
        }
        env.update(
            FAULTBELL_WEBHOOK_URL=f"http://127.0.0.1:{server.server_port}/",
            FAULTBELL_SEND_LOCAL="1",
            FAULTBELL_APP_ROOT=str(ROOT),
        )
        result = subprocess.run(
            [sys.executable, str(EXAMPLES / "cron_job.py")],
            env=env,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    finally:
        server.shutdown()
        server.server_close()
    assert "'sent': 1" in result.stdout, result.stdout + result.stderr
    [card] = local_webhook.received
    embed = card["payload"]["embeds"][0]
    assert embed["title"] == "[LOCAL] Job nightly-sync: 2 of 5 tenants failed"
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["Tenants"] == "Sample Store, Green Grocer"
    assert fields["Demo items"].startswith("1 failure skipped")
    assert "owner@example.com" not in str(card)
    assert "Traceback" in card["attachment"]


def test_fastapi_example(live: FakeWebhook) -> None:
    module = load(EXAMPLES / "fastapi_app.py")
    client = TestClient(module.app, raise_server_exceptions=False)
    assert client.get("/orders/1").json() == {"average_item_price": 400.0}
    assert client.get("/orders/404").status_code == 404
    assert client.post("/orders/1/receipt").json() == {"status": "queued"}
    response = client.get("/orders/0", headers={"X-Tenant-Id": "t-2002"})
    assert response.status_code == 500
    faultbell.flush(5)
    receipt, crash = live.wait_for(2)
    assert receipt.embed()["title"] == "Handled ConnectionError in POST /orders/{order_id}/receipt"
    assert crash.embed()["title"] == "Unhandled ZeroDivisionError in GET /orders/{order_id}"
    assert crash.fields()["Tenant"] == "Demo Café (t-2002)"
    assert crash.fields()["Request id"] == response.headers["x-request-id"]


def readme_blocks() -> list[str]:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    return re.findall(r"```python\n(.*?)```\n", text, flags=re.S)


def block_with(text: str) -> str:
    [block] = [b for b in readme_blocks() if text in b]
    return block


def run(block: str) -> dict[str, object]:
    namespace: dict[str, object] = {"__name__": "readme"}
    exec(compile(block, "README.md", "exec"), namespace)
    return namespace


def test_every_readme_python_block_is_covered() -> None:
    markers = [
        "nightly-invoices",
        "FastAPI()",
        "TelegramTransport(os.environ",
        "env_detectors",
        "NtfyTransport",
        "WatchMiddleware",
    ]
    assert len(readme_blocks()) == len(markers)
    for marker in markers:
        block_with(marker)  # exactly one block per marker


def test_readme_quickstart_and_fastapi(live: FakeWebhook) -> None:
    run(block_with("nightly-invoices"))
    faultbell.flush(5)
    first, second = live.wait_for(2)
    assert first.embed()["title"] == "Handled ZeroDivisionError in billing-worker"
    assert second.embed()["title"] == "Job nightly-invoices: 1 of 3 invoices failed"

    app = run(block_with("FastAPI()"))["app"]
    client = TestClient(app, raise_server_exceptions=False)  # type: ignore[arg-type]
    assert client.get("/orders/0").status_code == 500
    faultbell.flush(5)
    crash = live.wait_for(3)[2]
    assert crash.embed()["title"] == "Unhandled ZeroDivisionError in GET /orders/{order_id}"
    assert crash.fields()["Tenant"] == "Demo Café (t-1001)"


def test_readme_channels_block(
    live: FakeWebhook, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_URL", live.url)
    run(block_with("TelegramTransport(os.environ"))
    # Telegram and PagerDuty have no settings here, so they are skipped; email is configured.
    assert capsys.readouterr().out.strip() == "['discord', 'slack', 'email', 'callback']"
    faultbell.configure(channels=[c for c in faultbell.settings().channels if c.name != "email"])  # no SMTP
    faultbell.capture(ValueError("from the README"))
    faultbell.flush(5)
    assert capsys.readouterr().out.strip() == "ALERT Handled ValueError in app"
    slack, discord = sorted(live.wait_for(2), key=lambda r: r.content_type)  # json < multipart
    assert discord.embed()["title"] == "Handled ValueError in app"  # the main webhook
    assert slack.json()["text"] == "Handled ValueError in app"


def test_readme_configure_block(live: FakeWebhook) -> None:
    run(block_with("env_detectors"))
    assert faultbell.settings().dedupe_window == 300


def test_readme_ntfy_block(live: FakeWebhook) -> None:
    ntfy = run(block_with("NtfyTransport"))["NtfyTransport"]
    faultbell.configure(channels=[ntfy(live.url, kinds=["unhandled"])])  # type: ignore[operator]
    faultbell.capture(ValueError("handled: not for ntfy"))
    faultbell.capture(ValueError("Café crashed"), handled=False)
    faultbell.flush(5)
    received = live.wait_for(3)
    pushes = [r for r in received if r.content_type.startswith("text/plain")]
    [push] = pushes
    assert push.body.decode() == "Café crashed"
    assert push.headers["title"] == "Unhandled ValueError in app"
    assert push.headers["priority"] == "high"


def test_readme_server_watch_block(live: FakeWebhook) -> None:
    app = run(block_with("WatchMiddleware"))["app"]
    client = TestClient(app, raise_server_exceptions=False)  # type: ignore[arg-type]
    for _ in range(3):
        assert client.get("/stock/7").status_code == 503
    faultbell.flush(5)
    [card] = live.wait_for(1)
    assert "ServerWatchAlert" in card.embed()["title"]
    assert "3 5xx responses in 5 minutes on GET /stock/{item_id}" in str(card.embed())


def test_server_watch_example(live: FakeWebhook) -> None:
    module = load(EXAMPLES / "server_watch.py")
    client = TestClient(module.app, raise_server_exceptions=False)
    assert client.get("/stock/1").json() == {"item_id": 1, "on_hand": 12}
    for _ in range(3):
        assert client.get("/stock/7").status_code == 503
    faultbell.flush(5)
    [card] = live.wait_for(1)
    assert "GET /stock/{item_id}" in str(card.embed())
    module.watch.stop()
