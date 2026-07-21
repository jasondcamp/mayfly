"""caddis — mayfly's sample full-stack app (Flask API).

A little file collector (caddisfly larvae build cases from what they
gather). Demonstrates every mayfly service doing its real job:

  upload -> S3 object + postgres row (via pgbouncer) + kafka event
  worker -> consumes the event, checksums the file, marks it processed
  redis  -> live stats counters + the pipeline activity feed
  secrets manager -> HMAC signing key for download links, fetched at boot

Config (all provided by the mayfly spec):
  DATABASE_URL          postgres DSN (pointed at pgbouncer)
  REDIS_HOST/REDIS_PORT from the elasticache secret
  KAFKA_BROKERS         from the msk secret
  S3_BUCKET             bucket for uploads (default "uploads")
  SIGNING_SECRET_NAME   Secrets Manager secret holding the HMAC key
  AWS_ENDPOINT_URL      injected by mayfly (the in-namespace emulator)
"""

import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime, timezone

import boto3
import pg8000.native
import redis as redis_lib
from flask import Flask, jsonify, request, Response
from kafka import KafkaProducer

app = Flask(__name__)

S3_BUCKET = os.environ.get("S3_BUCKET", "uploads")
TOPIC = os.environ.get("CADDIS_TOPIC", "caddis.files")
MAX_BYTES = int(os.environ.get("CADDIS_MAX_BYTES", str(25 * 1024 * 1024)))

_db_url = os.environ["DATABASE_URL"]


def db():
    # a connection per request is fine — pgbouncer is doing the pooling,
    # which is exactly the point of having it in the environment
    from urllib.parse import urlparse

    u = urlparse(_db_url)
    return pg8000.native.Connection(
        user=u.username, password=u.password, host=u.hostname,
        port=u.port or 5432, database=u.path.lstrip("/"), timeout=10,
    )


def rds_init():
    con = db()
    try:
        con.run(
            """CREATE TABLE IF NOT EXISTS files (
                id text PRIMARY KEY,
                name text NOT NULL,
                size bigint NOT NULL,
                content_type text,
                s3_key text NOT NULL,
                checksum text,
                status text NOT NULL DEFAULT 'uploaded',
                created_at timestamptz NOT NULL DEFAULT now()
            )"""
        )
        con.run("ALTER TABLE files ADD COLUMN IF NOT EXISTS steps text")
        con.run("ALTER TABLE files ADD COLUMN IF NOT EXISTS processed_at timestamptz")
    finally:
        con.close()


def redis_client():
    return redis_lib.Redis(
        host=os.environ["REDIS_HOST"], port=int(os.environ.get("REDIS_PORT", "6379")),
        socket_timeout=5, decode_responses=True,
    )


_producer = None


def producer():
    global _producer
    if _producer is None:
        _producer = KafkaProducer(
            bootstrap_servers=os.environ["KAFKA_BROKERS"].split(","),
            value_serializer=lambda v: json.dumps(v).encode(),
            request_timeout_ms=10000,
        )
    return _producer


_signing_key = None


def signing_key() -> bytes:
    global _signing_key
    if _signing_key is None:
        name = os.environ.get("SIGNING_SECRET_NAME", "app/signing-key")
        sm = boto3.client("secretsmanager")
        _signing_key = sm.get_secret_value(SecretId=name)["SecretString"].encode()
    return _signing_key


def sign(file_id: str) -> str:
    return hmac.new(signing_key(), file_id.encode(), hashlib.sha256).hexdigest()[:32]


def push_activity(r, event: dict):
    event["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    r.lpush("caddis:activity", json.dumps(event))
    r.ltrim("caddis:activity", 0, 19)


@app.get("/healthz")
def healthz():
    return "ok\n"


@app.post("/api/files")
def upload():
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify(error="multipart field 'file' required"), 400
    data = f.read()
    if len(data) > MAX_BYTES:
        return jsonify(error=f"file exceeds {MAX_BYTES} bytes"), 413

    file_id = uuid.uuid4().hex[:12]
    s3_key = f"caddis/{file_id}/{f.filename}"
    steps = {"received": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}

    t0 = time.monotonic()
    boto3.client("s3").put_object(
        Bucket=S3_BUCKET, Key=s3_key, Body=data,
        ContentType=f.content_type or "application/octet-stream",
    )
    steps["s3_put_ms"] = round((time.monotonic() - t0) * 1000, 1)

    t0 = time.monotonic()
    con = db()
    try:
        con.run(
            "INSERT INTO files (id, name, size, content_type, s3_key, steps) "
            "VALUES (:id, :name, :size, :ct, :key, :steps)",
            id=file_id, name=f.filename, size=len(data),
            ct=f.content_type, key=s3_key, steps=json.dumps(steps),
        )
    finally:
        con.close()
    steps["db_insert_ms"] = round((time.monotonic() - t0) * 1000, 1)

    r = redis_client()
    r.incr("caddis:stats:files")
    r.incrby("caddis:stats:bytes", len(data))
    push_activity(r, {"event": "file.uploaded", "id": file_id, "name": f.filename})

    t0 = time.monotonic()
    producer().send(TOPIC, {
        "event": "file.uploaded", "id": file_id, "s3_key": s3_key,
        "published_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    })
    producer().flush(timeout=10)
    steps["kafka_publish_ms"] = round((time.monotonic() - t0) * 1000, 1)

    con = db()
    try:
        con.run("UPDATE files SET steps = :s WHERE id = :id",
                s=json.dumps(steps), id=file_id)
    finally:
        con.close()

    return jsonify(id=file_id, name=f.filename, size=len(data), status="uploaded"), 201


@app.get("/api/files")
def list_files():
    con = db()
    try:
        rows = con.run(
            "SELECT id, name, size, content_type, checksum, status, created_at, "
            "steps, processed_at FROM files ORDER BY created_at DESC LIMIT 50"
        )
    finally:
        con.close()
    out = []
    for r0, r1, r2, r3, r4, r5, r6, r7, r8 in rows:
        rec = {
            "id": r0, "name": r1, "size": r2, "content_type": r3,
            "checksum": r4, "status": r5, "created_at": r6.isoformat(),
            "steps": json.loads(r7) if r7 else {},
            "processed_at": r8.isoformat() if r8 else None,
            "token": sign(r0),
        }
        if r8:
            rec["pipeline_ms"] = round((r8 - r6).total_seconds() * 1000, 1)
        out.append(rec)
    return jsonify(files=out)


@app.get("/api/files/<file_id>/download")
def download(file_id):
    token = request.args.get("token", "")
    if not hmac.compare_digest(token, sign(file_id)):
        return jsonify(error="invalid or missing token"), 403
    con = db()
    try:
        rows = con.run(
            "SELECT name, s3_key, content_type FROM files WHERE id = :id", id=file_id
        )
    finally:
        con.close()
    if not rows:
        return jsonify(error="not found"), 404
    name, s3_key, ctype = rows[0]
    obj = boto3.client("s3").get_object(Bucket=S3_BUCKET, Key=s3_key)
    return Response(
        obj["Body"].read(),
        mimetype=ctype or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.get("/api/stats")
def stats():
    r = redis_client()
    activity = [json.loads(x) for x in r.lrange("caddis:activity", 0, 9)]
    return jsonify(
        files=int(r.get("caddis:stats:files") or 0),
        bytes=int(r.get("caddis:stats:bytes") or 0),
        processed=int(r.get("caddis:stats:processed") or 0),
        activity=activity,
    )


def _wait_and_init():
    for attempt in range(30):
        try:
            rds_init()
            return
        except Exception:
            time.sleep(2)
    rds_init()  # final attempt, let it raise


_wait_and_init()
