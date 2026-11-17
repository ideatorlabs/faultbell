# Development

## Running the examples

```bash
uv venv .venv && uv pip install -p .venv -e ".[dev]"
.venv/bin/python examples/local_webhook.py &              # prints every card it receives
export FAULTBELL_WEBHOOK_URL=http://127.0.0.1:8765/ FAULTBELL_SEND_LOCAL=1
python -m faultbell test                                  # one test alert to every channel
.venv/bin/python examples/cron_job.py                     # one summary card for 5 tenants
uv pip install -p .venv uvicorn
.venv/bin/uvicorn examples.fastapi_app:app                # then: curl -i http://127.0.0.1:8000/orders/0
```

## Testing

```bash
uv venv .venv && uv pip install -p .venv -e ".[dev]"
.venv/bin/pytest --cov
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy
```

The suite never touches a real webhook: a `http.server` running in the test process plays the webhook, and
the README's Python blocks and both examples are executed against it.
