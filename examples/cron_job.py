"""A cron-style job: however many tenants fail, the channel gets ONE summary card.

python examples/local_webhook.py &                      # or use a real Discord webhook
FAULTBELL_WEBHOOK_URL=http://127.0.0.1:8765/ FAULTBELL_SEND_LOCAL=1 python examples/cron_job.py
"""

from __future__ import annotations

import logging

import faultbell

log = logging.getLogger("nightly_sync")

TENANTS = [
    {"id": "t-1001", "name": "Demo Café"},
    {"id": "t-1002", "name": "Sample Store"},
    {"id": "t-1003", "name": "Corner Bakery"},
    {"id": "t-1004", "name": "Green Grocer"},
    {"id": "t-demo", "name": "Demo Tenant"},  # demo data: failures here are counted, never alerted
]


def sync_tenant(tenant: dict[str, str]) -> None:
    if tenant["id"] in ("t-1002", "t-demo"):
        raise TimeoutError(f"inventory API timed out for {tenant['id']} after 30s")
    if tenant["id"] == "t-1004":
        raise ValueError("price list has 3 rows without a SKU; contact owner@example.com")


def main() -> dict[str, int]:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    faultbell.install(service="nightly-sync")
    with faultbell.job_run("nightly-sync", total=len(TENANTS), noun="tenants", demo_ids={"t-demo"}) as run:
        for tenant in TENANTS:
            with faultbell.context(tenant=tenant["name"], tenant_id=tenant["id"]):
                try:
                    sync_tenant(tenant)
                    log.info("synced %s", tenant["id"])
                except Exception as exc:
                    log.warning("sync failed for %s: %s", tenant["id"], exc)
                    run.fail(exc, tenant=tenant["name"], tenant_id=tenant["id"])
    faultbell.flush(5)
    stats = faultbell.stats()
    print(f"faultbell: {stats}")
    return stats


if __name__ == "__main__":
    main()
