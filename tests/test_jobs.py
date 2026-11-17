from __future__ import annotations

import logging

import pytest

import faultbell
from conftest import FakeWebhook, raise_and_catch
from faultbell import jobs

log = logging.getLogger("jobs.test")


def test_one_summary_card_per_run(live: FakeWebhook) -> None:
    faultbell.install(service="nightly-worker")
    tenants = [("t-1", "Demo Café"), ("t-2", "Sample Store"), ("t-3", "Corner Shop"), ("t-demo", "Demo Data")]
    with faultbell.job_run("nightly-sync", total=5, noun="tenants", demo_ids={"t-demo"}) as run:
        run.fail(
            raise_and_catch(TimeoutError("upstream timed out after 30s")), tenant="Demo Café", tenant_id="t-1"
        )
        run.fail(raise_and_catch(TimeoutError("upstream timed out after 31s")), tenant_id="t-2")
        with faultbell.context(tenant="Corner Shop", tenant_id="t-3"):
            try:
                raise ValueError("bad row 17")
            except ValueError:
                log.exception("row failed")  # folded into the run, not its own card
        run.fail(raise_and_catch(TimeoutError("demo tenant")), tenant_id="t-demo")
        run.fail(raise_and_catch(TimeoutError("demo flag")), tenant="Scratch", demo=True)
        faultbell.capture(raise_and_catch(KeyError("lookup")), job="enrich")
        assert len(tenants) == 4
    faultbell.flush(5)

    [request] = live.wait_for(1)
    payload, filename, attachment = request.discord()
    embed = payload["embeds"][0]
    assert embed["title"] == "Job nightly-sync: 4 of 5 tenants failed"
    assert embed["color"] == 0x7C66DC
    assert "Demo Café · TimeoutError: upstream timed out after 30s" in embed["description"]
    fields = request.fields()
    assert fields["Service"] == "nightly-worker"
    assert fields["Failed"] == "4 of 5"
    assert "TimeoutError: 2" in fields["Errors"]
    assert "ValueError: 1" in fields["Errors"]
    assert "KeyError (enrich): 1" in fields["Errors"]
    assert fields["Tenants"].startswith("Demo Café, t-2, Corner Shop")
    assert fields["Demo items"].startswith("2 failures skipped")
    assert filename is not None and filename.startswith("job-nightly-sync-")
    assert "Every failure in this run:" in attachment and "bad row 17" in attachment
    assert "demo tenant" not in attachment


def test_successful_run_sends_nothing(live: FakeWebhook) -> None:
    with faultbell.job_run("quiet", total=3):
        pass
    faultbell.flush(5)
    assert live.received == []


def test_nested_runs_fold_into_the_outer_one(live: FakeWebhook) -> None:
    with faultbell.job_run("outer", noun="items") as outer:
        with faultbell.job_run("inner", demo_ids=["d-1"]) as inner:
            assert inner.total is None
            inner.fail(raise_and_catch(RuntimeError("inner broke")), item="row-1")
            inner.fail(raise_and_catch(RuntimeError("demo row")), tenant_id="d-1")
        outer.fail(raise_and_catch(RuntimeError("outer broke")), item="row-2", step="outer")
    faultbell.flush(5)
    [request] = live.wait_for(1)
    assert request.embed()["title"] == "Job outer: 2 items failed"
    errors = request.fields()["Errors"]
    assert "RuntimeError (inner): 1" in errors and "RuntimeError: 1" in errors


def test_job_failed_inside_and_outside_a_run(live: FakeWebhook) -> None:
    with faultbell.job_run("import", total=1, noun="files"):
        try:
            raise OSError("disk full")
        except OSError:
            faultbell.job_failed(job="import", item="orders.csv")
    faultbell.flush(5)
    assert live.wait_for(1)[0].embed()["title"] == "Job import: 1 of 1 file failed"

    faultbell.job_failed(raise_and_catch(OSError("disk full again")), job="export", tenant="Sample Store")
    faultbell.job_failed(raise_and_catch(OSError("demo only")), job="export", demo=True)
    faultbell.job_failed(None, job="export")  # nothing being handled: ignored
    faultbell.flush(5)
    _, single = live.wait_for(2)
    assert single.embed()["title"] == "Handled OSError in job export"
    assert single.fields()["Job"] == "export"
    assert single.fields()["Tenant"] == "Sample Store"
    assert len(live.received) == 2


def test_log_then_fail_counts_once_and_takes_the_item_label(live: FakeWebhook) -> None:
    faultbell.install(service="billing")
    with faultbell.job_run("billing", total=2, noun="invoices") as run:
        try:
            raise RuntimeError("card declined")
        except RuntimeError as exc:
            log.exception("invoice failed")
            run.fail(exc, item="inv-2001")
        try:
            raise RuntimeError("demo invoice")
        except RuntimeError as exc:
            log.exception("invoice failed")
            run.fail(exc, demo=True)  # removes the folded record again
        run.fail(None)  # nothing being handled
        run.fail(raise_and_catch(type("Gone", (Exception,), {"status_code": 410})()))  # client error
    faultbell.flush(5)
    [request] = live.wait_for(1)
    assert request.embed()["title"] == "Job billing: 1 of 2 invoices failed"
    assert request.fields()["Invoices"] == "inv-2001"


def test_already_reported_exception_is_not_counted_again(live: FakeWebhook) -> None:
    exc = raise_and_catch(RuntimeError("seen before"))
    faultbell.capture(exc)
    with faultbell.job_run("again") as run:
        run.fail(exc, item="x")
    faultbell.flush(5)
    assert len(live.wait_for(1)) == 1
    assert live.received[0].embed()["title"] == "Handled RuntimeError in app"


def test_overflow_is_counted(live: FakeWebhook, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "FAILURES_KEPT", 2)
    monkeypatch.setattr(jobs, "TRACEBACKS_KEPT", 1)
    with faultbell.job_run("bulk", total=10) as run:
        for n in range(5):
            run.fail(raise_and_catch(RuntimeError(f"row {n}")), item=f"row-{n}")
        summary = run.summary()
    assert summary is not None and summary.failed == 5 and summary.first is not None
    faultbell.flush(5)
    assert live.wait_for(1)[0].embed()["title"] == "Job bulk: 5 of 10 items failed"


def test_unhandled_exception_inside_a_run_still_gets_its_own_card(live: FakeWebhook) -> None:
    with faultbell.job_run("cron"):
        faultbell.capture(raise_and_catch(MemoryError("big")), handled=False)
    faultbell.flush(5)
    assert live.wait_for(1)[0].embed()["title"] == "Unhandled MemoryError in app"


def test_singular_nouns() -> None:
    from faultbell.card import listed, singular

    assert [
        singular(n) for n in ("tenants", "addresses", "batches", "entries", "boxes", "class", "data")
    ] == [
        "tenant",
        "address",
        "batch",
        "entry",
        "box",
        "class",
        "data",
    ]
    assert listed(list("abcdefg")) == "a, b, c, d, e and 2 more"
