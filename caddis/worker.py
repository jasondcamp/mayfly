"""caddis worker — the Kafka consumer side of the pipeline.

Consumes file.uploaded events from the caddis topic (consumer group
"caddis-worker": add replicas and Kafka splits partitions across them),
checksums the object from S3, marks the row processed, and feeds the
activity stream in redis. Serves a heartbeat /healthz so dragonfly's APPS
card can see a wedged consumer.
"""

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3
import pg8000.native
import redis as redis_lib
from kafka import KafkaConsumer, KafkaProducer

TOPIC = os.environ.get("CADDIS_TOPIC", "caddis.files")
S3_BUCKET = os.environ.get("S3_BUCKET", "uploads")
STALE_AFTER = 60  # heartbeat turns unhealthy if the poll loop stalls this long

_last_poll = time.monotonic()


def _db():
    from urllib.parse import urlparse

    u = urlparse(os.environ["DATABASE_URL"])
    return pg8000.native.Connection(
        user=u.username, password=u.password, host=u.hostname,
        port=u.port or 5432, database=u.path.lstrip("/"), timeout=10,
    )


def _redis():
    return redis_lib.Redis(
        host=os.environ["REDIS_HOST"], port=int(os.environ.get("REDIS_PORT", "6379")),
        socket_timeout=5, decode_responses=True,
    )


def process(msg, producer):
    event = json.loads(msg.value)
    if event.get("event") != "file.uploaded":
        return
    file_id, s3_key = event["id"], event["s3_key"]

    picked = datetime.now(timezone.utc)
    lag_ms = None
    if event.get("published_at"):
        published = datetime.fromisoformat(event["published_at"])
        lag_ms = round((picked - published).total_seconds() * 1000, 1)

    t0 = time.monotonic()
    body = boto3.client("s3").get_object(Bucket=S3_BUCKET, Key=s3_key)["Body"].read()
    checksum = hashlib.sha256(body).hexdigest()
    checksum_ms = round((time.monotonic() - t0) * 1000, 1)

    con = _db()
    try:
        rows = con.run("SELECT steps FROM files WHERE id = :id", id=file_id)
        steps = json.loads(rows[0][0]) if rows and rows[0][0] else {}
        steps["worker_pickup_lag_ms"] = lag_ms
        steps["checksum_ms"] = checksum_ms
        con.run(
            "UPDATE files SET checksum = :c, status = 'processed', "
            "steps = :s, processed_at = now() WHERE id = :id",
            c=checksum, s=json.dumps(steps), id=file_id,
        )
    finally:
        con.close()

    r = _redis()
    r.incr("caddis:stats:processed")
    r.lpush("caddis:activity", json.dumps({
        "event": "file.processed", "id": file_id,
        "checksum": checksum[:12],
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }))
    r.ltrim("caddis:activity", 0, 19)

    producer.send(TOPIC, json.dumps(
        {"event": "file.processed", "id": file_id}).encode())
    print(f"processed {file_id} sha256={checksum[:12]}", flush=True)


class Heartbeat(BaseHTTPRequestHandler):
    def do_GET(self):
        fresh = (time.monotonic() - _last_poll) < STALE_AFTER
        code, body = (200, b"ok\n") if fresh else (503, b"consumer stalled\n")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def main():
    global _last_poll
    port = int(os.environ.get("PORT", "8080"))
    threading.Thread(
        target=lambda: ThreadingHTTPServer(("", port), Heartbeat).serve_forever(),
        daemon=True,
    ).start()

    brokers = os.environ["KAFKA_BROKERS"].split(",")
    consumer = KafkaConsumer(
        TOPIC,
        bootstrap_servers=brokers,
        group_id="caddis-worker",
        auto_offset_reset="earliest",
        enable_auto_commit=True,
    )
    producer = KafkaProducer(bootstrap_servers=brokers, request_timeout_ms=10000)
    print(f"caddis-worker consuming {TOPIC} from {brokers}", flush=True)

    while True:
        polled = consumer.poll(timeout_ms=5000)
        _last_poll = time.monotonic()
        for _tp, messages in polled.items():
            for msg in messages:
                try:
                    process(msg, producer)
                except Exception as e:  # keep consuming; the row stays 'uploaded'
                    print(f"process error: {type(e).__name__}: {e}", flush=True)


if __name__ == "__main__":
    main()
