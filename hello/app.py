"""hello — a minimal app for testing load balancers / ingress in mayfly envs.

Every response identifies which pod served it (so a balancer's distribution
is visible) and echoes the request line plus any X-Forwarded-*/Via headers
the balancer injected.

  GET /         -> small HTML page: pod, request, forwarded headers
  GET /json     -> the same as JSON (curl-friendly)
  GET /healthz  -> "ok" (point ALB target-group / readiness checks here)

Zero dependencies; stdlib only.
"""

import json
import os
import socket
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("PORT", "8080"))
POD = socket.gethostname()
STARTED = datetime.now(timezone.utc).isoformat(timespec="seconds")

try:
    with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace") as f:
        NAMESPACE = f.read().strip()
except OSError:
    NAMESPACE = "unknown"

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>hello</title>
<style>
  * {{ box-sizing: border-box; margin: 0; }}
  body {{ background: #fcfcfb; color: #22221f; padding: 48px 24px;
         font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
         max-width: 720px; margin: 0 auto; }}
  h1 {{ font-size: 28px; margin-bottom: 4px; }}
  h1 em {{ color: #0ca30c; font-style: normal; }}
  .sub {{ color: #5f5f58; margin-bottom: 28px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
  td {{ padding: 6px 10px; border-top: 1px solid #e4e4e0; vertical-align: top; }}
  td:first-child {{ color: #5f5f58; white-space: nowrap; width: 160px; }}
  code {{ font: 13px ui-monospace, monospace; overflow-wrap: anywhere; }}
  .foot {{ margin-top: 24px; color: #8a8a82; font-size: 13px; }}
  .foot a {{ color: inherit; }}
</style></head>
<body>
<h1>hello from <em>{pod}</em></h1>
<div class="sub">namespace {namespace} &middot; refresh to see the balancer pick pods</div>
<table>{rows}</table>
<div class="foot"><a href="/json">JSON</a> &middot; served {now}</div>
</body></html>
"""


def info(handler):
    fwd = {
        k: v
        for k, v in handler.headers.items()
        if k.lower().startswith(("x-forwarded", "x-real-ip", "via", "forwarded"))
    }
    return {
        "pod": POD,
        "namespace": NAMESPACE,
        "started": STARTED,
        "request": f"{handler.command} {handler.path}",
        "client": handler.client_address[0],
        "host_header": handler.headers.get("Host", ""),
        "forwarded_headers": fwd,
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            body, code, ctype = b"ok\n", 200, "text/plain"
        elif self.path == "/json":
            body = json.dumps(info(self), indent=2).encode() + b"\n"
            code, ctype = 200, "application/json"
        else:
            data = info(self)
            rows = []
            for k, v in data.items():
                if k == "forwarded_headers":
                    v = "<br>".join(f"{h}: {val}" for h, val in v.items()) or "(none)"
                rows.append(f"<tr><td>{k}</td><td><code>{v}</code></td></tr>")
            body = PAGE.format(
                pod=POD,
                namespace=NAMESPACE,
                rows="".join(rows),
                now=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ).encode()
            code, ctype = 200, "text/html; charset=utf-8"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    print(f"hello ({POD}) starting on :{PORT}", flush=True)
    HTTPServer(("", PORT), Handler).serve_forever()
