"""A tiny local webhook that prints every card it receives. Try Faultbell without a Discord server.

python examples/local_webhook.py            # listens on http://127.0.0.1:8765/
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

received: list[dict[str, Any]] = []


def _parse(content_type: str, body: bytes) -> dict[str, Any]:
    """Return {"payload": ..., "attachment": ...} for Discord multipart posts, or the JSON body."""
    if not content_type.startswith("multipart/form-data"):
        return {"payload": json.loads(body or b"{}"), "attachment": None}
    boundary = content_type.split("boundary=", 1)[1].encode()
    out: dict[str, Any] = {"payload": None, "attachment": None}
    for chunk in body.split(b"--" + boundary):
        head, _, data = chunk.strip(b"\r\n").partition(b"\r\n\r\n")
        if b'name="payload_json"' in head:
            out["payload"] = json.loads(data)
        elif b'name="files[0]"' in head:
            out["attachment"] = data.decode("utf-8", "replace")
    return out


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server API
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        card = _parse(self.headers.get("Content-Type", ""), body)
        received.append(card)
        payload = card["payload"] or {}
        embed = (payload.get("embeds") or [payload])[0]
        print(f"--- card: {embed.get('title')}", flush=True)
        for item in embed.get("fields", []):
            print(f"    {item['name']}: {item['value'][:120]!r}", flush=True)
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


def serve(port: int = 8765) -> ThreadingHTTPServer:
    """Start the server on a background thread and return it (``port=0`` picks a free port)."""
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    srv = serve(port)
    print(f"listening on http://127.0.0.1:{srv.server_port}/  (Ctrl+C to stop)", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()
