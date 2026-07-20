"""dragonfly — mayfly's in-cluster connectivity verifier.

Discovers services the way a real AWS application would: by asking the AWS
control plane. Using the AWS_ENDPOINT_URL/credentials mayfly injects into
every app pod, it calls

  rds:         describe-db-instances
  elasticache: describe-cache-clusters
  kafka (msk): list-clusters + get-bootstrap-brokers

then round-trips real data through every instance found. No configuration,
no mounted secrets, no Kubernetes API access — declare a service in the
mayfly spec and a tile appears.

Serves:
  GET /         -> web interface: live per-instance tiles, refreshed every 5s
  GET /api      -> JSON report of every check
  GET /healthz  -> 200 when every discovered instance verifies, else 503
                   (use as the readiness probe: the pod only goes Ready
                   once connectivity is proven)
"""

import json
import os
import time
import uuid
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))
# mayfly's provisioner defaults; AWS APIs never return passwords
DB_PASSWORD = os.environ.get("DB_PASSWORD", "apppass")


def _aws(service):
    import boto3

    return boto3.client(service)  # endpoint/creds/region from mayfly's env


def _check(fn):
    start = time.monotonic()
    try:
        detail = fn()
        return {"ok": True, "ms": round((time.monotonic() - start) * 1000), "detail": detail}
    except Exception as e:  # report, never crash
        return {
            "ok": False,
            "ms": round((time.monotonic() - start) * 1000),
            "error": f"{type(e).__name__}: {e}",
        }


def check_postgres(inst):
    def run():
        import pg8000.native

        host = inst["Endpoint"]["Address"]
        port = inst["Endpoint"]["Port"]
        con = pg8000.native.Connection(
            user=inst.get("MasterUsername", "app"),
            password=DB_PASSWORD,
            host=host,
            port=port,
            database=inst.get("DBName", "app"),
            timeout=5,
        )
        try:
            token = str(uuid.uuid4())
            con.run("CREATE TABLE IF NOT EXISTS dragonfly (token text, at timestamptz DEFAULT now())")
            con.run("INSERT INTO dragonfly (token) VALUES (:t)", t=token)
            rows = con.run("SELECT count(*) FROM dragonfly WHERE token = :t", t=token)
            assert rows[0][0] == 1, f"inserted row not found (token {token})"
            return f"insert/select round-trip ok via {host}:{port}"
        finally:
            con.close()

    return _check(run)


def check_memcached(node):
    def run():
        from pymemcache.client.base import Client

        host, port = node["Address"], node["Port"]
        c = Client((host, int(port)), connect_timeout=5, timeout=5)
        token = str(uuid.uuid4())
        key = f"dragonfly-{token[:8]}"
        c.set(key, token, expire=60)
        got = c.get(key)
        assert got is not None and got.decode() == token, f"set/get mismatch: {got!r}"
        c.delete(key)
        c.close()
        return f"memcached set/get round-trip ok via {host}:{port}"

    return _check(run)


def check_redis(node):
    def run():
        import redis

        host, port = node["Address"], node["Port"]
        r = redis.Redis(host=host, port=port, socket_timeout=5, socket_connect_timeout=5)
        token = str(uuid.uuid4())
        key = f"dragonfly:{token}"
        r.set(key, token, ex=60)
        got = r.get(key)
        assert got is not None and got.decode() == token, f"SET/GET mismatch: {got!r}"
        r.delete(key)
        return f"SET/GET round-trip ok via {host}:{port}"

    return _check(run)


def check_kafka(brokers):
    def run():
        from kafka import KafkaConsumer, KafkaProducer, TopicPartition

        topic = os.environ.get("DRAGONFLY_TOPIC", "dragonfly")
        servers = brokers.split(",")
        token = str(uuid.uuid4()).encode()

        producer = KafkaProducer(bootstrap_servers=servers, request_timeout_ms=5000)
        try:
            meta = producer.send(topic, token).get(timeout=10)
        finally:
            producer.close()

        consumer = KafkaConsumer(
            bootstrap_servers=servers,
            enable_auto_commit=False,
            consumer_timeout_ms=5000,
            request_timeout_ms=6000,
        )
        try:
            tp = TopicPartition(meta.topic, meta.partition)
            consumer.assign([tp])
            consumer.seek(tp, meta.offset)
            for msg in consumer:
                assert msg.value == token, f"consumed {msg.value!r}, produced {token!r}"
                return (
                    f"produce/consume round-trip ok via {brokers} "
                    f"(topic {topic}, offset {meta.offset})"
                )
            raise AssertionError(f"produced to offset {meta.offset} but consumed nothing back")
        finally:
            consumer.close()

    return _check(run)


def check_s3(s3, bucket):
    def run():
        token = str(uuid.uuid4())
        key = f"dragonfly/{token}"
        s3.put_object(Bucket=bucket, Key=key, Body=token.encode())
        got = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        assert got.decode() == token, f"put/get mismatch: {got!r}"
        s3.delete_object(Bucket=bucket, Key=key)
        return f"put/get/delete round-trip ok (s3://{bucket})"

    return _check(run)


def check_dynamo(ddb, table):
    def run():
        # discover the hash key from the table itself — still zero config
        schema = ddb.describe_table(TableName=table)["Table"]["KeySchema"]
        hash_key = next(k["AttributeName"] for k in schema if k["KeyType"] == "HASH")
        token = str(uuid.uuid4())
        item_key = {hash_key: {"S": token}}
        ddb.put_item(TableName=table, Item={**item_key, "probe": {"S": "dragonfly"}})
        got = ddb.get_item(TableName=table, Key=item_key).get("Item")
        assert got and got["probe"]["S"] == "dragonfly", f"put/get mismatch: {got!r}"
        ddb.delete_item(TableName=table, Key=item_key)
        return f"put/get/delete round-trip ok (table {table}, hash key {hash_key})"

    return _check(run)


def check_alb(name):
    def run():
        import urllib.request

        url = f"{os.environ['AWS_ENDPOINT_URL']}/_alb/{name}/"
        with urllib.request.urlopen(url, timeout=10) as resp:
            code = resp.status
        assert code < 500, f"data plane returned HTTP {code}"
        return f"HTTP {code} through the ALB data plane (/_alb/{name}/)"

    return _check(run)


def run_all():
    """Discover via the AWS control plane, then verify each instance."""
    report = {}

    def _try(kind, fn):
        try:
            fn()
        except Exception as e:
            report[f"{kind} discovery"] = {
                "ok": False,
                "kind": kind,
                "error": f"{type(e).__name__}: {e}",
            }

    def rds():
        for inst in _aws("rds").describe_db_instances()["DBInstances"]:
            r = check_postgres(inst)
            r["kind"] = "rds"
            r["status"] = inst.get("DBInstanceStatus", "unknown").lower()
            report[inst["DBInstanceIdentifier"]] = r

    def elasticache():
        clusters = _aws("elasticache").describe_cache_clusters(ShowCacheNodeInfo=True)
        for c in clusters["CacheClusters"]:
            nodes = c.get("CacheNodes") or []
            if not nodes:
                report[c["CacheClusterId"]] = {
                    "ok": False, "kind": "elasticache",
                    "error": "no cache nodes reported",
                }
                continue
            engine = c.get("Engine", "redis")
            if engine == "memcached":
                r = check_memcached(nodes[0]["Endpoint"])
            else:
                r = check_redis(nodes[0]["Endpoint"])
            r["kind"] = "elasticache"
            r["status"] = c.get("CacheClusterStatus", "unknown").lower()
            r["detail"] = r.get("detail", "") and (
                f"{engine} {c.get('EngineVersion', '')}: " + r["detail"]
            )
            report[c["CacheClusterId"]] = r

    def msk():
        kafka = _aws("kafka")
        for c in kafka.list_clusters().get("ClusterInfoList", []):
            brokers = kafka.get_bootstrap_brokers(ClusterArn=c["ClusterArn"])[
                "BootstrapBrokerString"
            ]
            r = check_kafka(brokers)
            r["kind"] = "msk"
            r["status"] = c.get("State", "unknown").lower()
            report[c["ClusterName"]] = r

    def dynamodb():
        ddb = _aws("dynamodb")
        for table in ddb.list_tables().get("TableNames", []):
            r = check_dynamo(ddb, table)
            r["kind"] = "dynamodb"
            try:
                r["status"] = ddb.describe_table(TableName=table)["Table"][
                    "TableStatus"
                ].lower()
            except Exception:
                r["status"] = "unknown"
            report[table] = r

    def s3():
        client = _aws("s3")
        for b in client.list_buckets().get("Buckets", []):
            r = check_s3(client, b["Name"])
            r["kind"] = "s3"
            r["status"] = "available"  # s3 has no lifecycle status
            report[b["Name"]] = r

    def alb():
        for lb in _aws("elbv2").describe_load_balancers().get("LoadBalancers", []):
            r = check_alb(lb["LoadBalancerName"])
            r["kind"] = "alb"
            r["status"] = lb.get("State", {}).get("Code", "unknown").lower()
            report[lb["LoadBalancerName"]] = r

    _try("rds", rds)
    _try("elasticache", elasticache)
    _try("msk", msk)
    _try("dynamodb", dynamodb)
    _try("s3", s3)
    _try("alb", alb)
    return report


_cache_lock = threading.Lock()
_cache = {"at": 0.0, "report": None}
CACHE_TTL = 5.0


def cached_report():
    """run_all(), memoized briefly so kubelet probes and page refreshes
    don't each trigger a full round-trip sweep."""
    with _cache_lock:
        if _cache["report"] is not None and time.monotonic() - _cache["at"] < CACHE_TTL:
            return _cache["report"]
    report = run_all()
    with _cache_lock:
        _cache.update(at=time.monotonic(), report=report)
    return report


def healthy(report):
    return bool(report) and all(c.get("ok") for c in report.values())


# Status colors pair with icon + word — state is never color alone.
PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>dragonfly</title>
<style>
  :root {
    --surface: #fcfcfb; --tile: #ffffff; --border: #e4e4e0;
    --ink: #22221f; --ink-2: #5f5f58; --ink-3: #8a8a82;
    --good: #0ca30c; --warn: #fab219; --critical: #d03b3b;
  }
  * { box-sizing: border-box; margin: 0; }
  body { background: var(--surface); color: var(--ink);
         font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
         padding: 32px 24px; max-width: 880px; margin: 0 auto; }
  header { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap;
           margin-bottom: 6px; }
  h1 { font-size: 20px; font-weight: 650; letter-spacing: .01em; }
  h1 .fly { font-weight: 400; color: var(--ink-3); }
  #overall { font-size: 14px; font-weight: 600; }
  .sub { color: var(--ink-2); font-size: 13px; margin-bottom: 24px; }
  .tiles { display: grid; gap: 10px; grid-template-columns: 1fr; }
  .card { background: var(--tile); border: 1px solid var(--border);
          border-radius: 8px; padding: 10px 14px 6px; }
  .name { font-size: 13px; font-weight: 650; text-transform: uppercase;
          letter-spacing: .05em; margin-bottom: 6px; display: flex;
          align-items: baseline; }
  .name .kind { font-weight: 400; color: var(--ink-3); text-transform: none;
                letter-spacing: 0; margin-left: 8px; }
  .name .count { font-weight: 400; font-size: 12px; color: var(--ink-3);
                 margin-left: auto; }
  .name .count.bad { color: var(--critical); font-weight: 600; }
  .row { display: flex; align-items: center; gap: 8px; font-size: 13px;
         white-space: nowrap; padding: 3px 0; }
  .row + .row { border-top: 1px solid var(--border); }
  .dot { width: 9px; height: 9px; border-radius: 50%; flex: none; }
  .ok    .dot { background: var(--good); }
  .slow  .dot { background: var(--warn); }
  .fail  .dot { background: var(--critical); }
  .app { min-width: 110px; font-weight: 550; overflow: hidden;
         text-overflow: ellipsis; }
  .detail { color: var(--ink-2); flex: 1; overflow: hidden;
            text-overflow: ellipsis; }
  .fail .detail { color: var(--critical); }
  .ms { color: var(--ink-2); }
  .fail-detail { color: var(--critical); font-size: 12.5px; white-space: normal;
                 overflow-wrap: anywhere; padding: 0 0 4px 17px; }
  .empty { color: var(--ink-3); font-size: 14px; }
  footer { margin-top: 24px; color: var(--ink-3); font-size: 12.5px; }
  footer a { color: inherit; }
</style></head>
<body>
<header><h1>dragon<span class="fly">fly</span></h1><span id="overall"></span></header>
<div class="sub">Services discovered via the AWS control plane, verified live.</div>
<div class="tiles" id="tiles"></div>
<footer>Auto-refreshes every 5s · <a href="/api">JSON</a> · <span id="stamp"></span></footer>
<script>
const ORDER = ["alb", "rds", "elasticache", "msk", "dynamodb", "s3"];
const TITLES = { rds: ["RDS", "postgres"], elasticache: ["ELASTICACHE", "redis"],
                 msk: ["MSK", "kafka"], dynamodb: ["DYNAMODB", "dynamo"],
                 s3: ["S3", "buckets"], alb: ["ALB", "elbv2"] };
const SLOW_MS = 1000;
function row(label, c) {
  const slow = c.ok && c.ms >= SLOW_MS;
  const cls = c.ok ? (slow ? "slow" : "ok") : "fail";
  const body = c.ok ? c.detail : c.error;
  const ms = c.ms != null ? c.ms + " ms" : "";
  let html = `<div class="row ${cls}" title="${body}"><span class="dot"></span>
    <span class="app">${label}</span><span class="detail">${body}</span>
    <span class="ms">${ms}</span></div>`;
  if (!c.ok) html += `<div class="fail-detail">${body}</div>`;
  return html;
}
function card(kind, entries) {
  const [title, sub] = TITLES[kind] || [kind.toUpperCase(), ""];
  const ok = entries.filter(([, c]) => c.ok).length;
  const countCls = ok === entries.length ? "count" : "count bad";
  return `<div class="card">
    <div class="name">${title}<span class="kind">\\u2014 ${sub}</span>
    <span class="${countCls}">${ok}/${entries.length} ok</span></div>
    ${entries.map(([l, c]) => row(l, c)).join("")}
  </div>`;
}
async function refresh() {
  try {
    const r = await fetch("/api", { cache: "no-store" });
    const data = await r.json();
    const entries = Object.entries(data.checks);
    const groups = {};
    for (const [k, c] of entries) (groups[c.kind] ||= []).push([k, c]);
    const kinds = [...ORDER.filter(k => groups[k]),
                   ...Object.keys(groups).filter(k => !ORDER.includes(k))];
    document.getElementById("tiles").innerHTML = entries.length
      ? kinds.map(k => card(k, groups[k])).join("")
      : '<div class="empty">No services discovered via the AWS control plane.</div>';
    const el = document.getElementById("overall");
    el.textContent = data.healthy ? "\\u2713 all connected" : "\\u2717 attention needed";
    el.style.color = data.healthy ? "var(--good)" : "var(--critical)";
    document.getElementById("stamp").textContent =
      "last check " + new Date().toLocaleTimeString();
  } catch (e) {
    document.getElementById("overall").textContent = "\\u2717 dragonfly unreachable";
  }
}
refresh(); setInterval(refresh, 5000);
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            body, code, ctype = PAGE.encode(), 200, "text/html; charset=utf-8"
        else:
            report = cached_report()
            if self.path == "/healthz":
                code = 200 if healthy(report) else 503
                body = b"ok\n" if code == 200 else b"unhealthy\n"
                ctype = "text/plain"
            else:  # /api (and anything else): JSON report
                code = 200
                body = (
                    json.dumps({"healthy": healthy(report), "checks": report}, indent=2).encode()
                    + b"\n"
                )
                ctype = "application/json"
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # probe/client gave up before the response was ready

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    print(f"dragonfly starting on :{PORT}", flush=True)
    print(json.dumps({"startup_checks": run_all()}, indent=2), flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
