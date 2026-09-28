"""dragonfly's Azure check dispatch (module loaded straight from dragonfly/)."""

import importlib.util
import json
from pathlib import Path

import pytest

DRAGONFLY = Path(__file__).parent.parent / "dragonfly" / "app.py"


@pytest.fixture()
def dragonfly(monkeypatch):
    spec = importlib.util.spec_from_file_location("dragonfly_app", DRAGONFLY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # AWS discovery must not reach out anywhere during tests
    monkeypatch.setattr(mod, "_aws", lambda service: (_ for _ in ()).throw(RuntimeError("no aws")))
    return mod


def _ok(detail):
    return {"ok": True, "ms": 1, "detail": detail}


def test_azure_checks_dispatch(dragonfly, monkeypatch):
    calls = []

    def fake(kind):
        def checker(chk):
            calls.append((kind, chk["name"]))
            return _ok(f"{kind} fine")

        return checker

    monkeypatch.setattr(dragonfly, "check_keyvault", fake("keyvault"))
    monkeypatch.setattr(dragonfly, "check_servicebus", fake("servicebus"))
    monkeypatch.setattr(dragonfly, "check_pgflex", fake("pgflex"))
    monkeypatch.setattr(dragonfly, "check_azuresql", fake("azuresql"))
    checks = [
        {"kind": "keyvault", "name": "vault", "url": "http://azure:4577/vault-keyvault"},
        {"kind": "servicebus", "name": "main", "connection": "cs", "queues": ["q"]},
        {"kind": "pgflex", "name": "appdb", "host": "pgflex-appdb", "port": 5432},
        {"kind": "azuresql", "name": "coredb", "host": "azuresql-coredb", "port": 1433},
        {"kind": "cosmos", "name": "future-thing"},  # unknown kind: skipped
    ]
    monkeypatch.setenv("MAYFLY_AZURE_CHECKS", json.dumps(checks))
    report = dragonfly.run_all()
    azure_entries = {k: v for k, v in report.items() if v.get("kind") in
                     ("keyvault", "servicebus", "pgflex", "azuresql")}
    assert set(azure_entries) == {"vault", "main", "appdb", "coredb"}
    assert all(v["status"] == "available" for v in azure_entries.values())
    assert "future-thing" not in report
    assert len(calls) == 4


def test_azure_checks_absent_env(dragonfly, monkeypatch):
    monkeypatch.delenv("MAYFLY_AZURE_CHECKS", raising=False)
    report = dragonfly.run_all()
    assert not [v for v in report.values() if v.get("kind") == "keyvault"]


def test_pgflex_adapts_to_postgres_probe(dragonfly, monkeypatch):
    seen = {}

    def fake_check_postgres(inst):
        seen.update(inst)
        return _ok("pg fine")

    monkeypatch.setattr(dragonfly, "check_postgres", fake_check_postgres)
    r = dragonfly.check_pgflex(
        {"kind": "pgflex", "name": "appdb", "host": "pgflex-appdb",
         "port": 5432, "db": "orders", "user": "app"}
    )
    assert r["ok"]
    assert seen["Endpoint"] == {"Address": "pgflex-appdb", "Port": 5432}
    assert seen["DBName"] == "orders"
    assert seen["MasterUsername"] == "app"


def test_check_failures_are_reported_not_raised(dragonfly):
    # unreachable endpoints must degrade to ok:False entries, never crash
    r = dragonfly.check_keyvault(
        {"kind": "keyvault", "name": "v", "url": "http://127.0.0.1:1/v-keyvault",
         "secrets": ["k"]}
    )
    assert r["ok"] is False and "error" in r
    r = dragonfly.check_azuresql(
        {"kind": "azuresql", "name": "d", "host": "127.0.0.1", "port": 1,
         "db": "app", "user": "sa", "password": "x"}
    )
    assert r["ok"] is False and "error" in r


def test_azure_sdks_importable_for_dragonfly_image():
    # the Dockerfile installs these; the dev env mirrors it so the checks'
    # lazy imports are exercised
    pytest.importorskip("azure.keyvault.secrets")
    pytest.importorskip("azure.servicebus")
    pytest.importorskip("pytds")


# ------------------------------------------------------------- servicebus

class _FakeMsg:
    def __init__(self, body):
        self.body = body

    def __str__(self):
        return self.body


class _FakeSbClient:
    """Minimal stand-in for azure.servicebus.ServiceBusClient."""

    instances = 0

    def __init__(self):
        type(self).instances += 1
        self.queues: dict[str, list] = {}
        self.sent_topics: list = []
        self.closed = False

    @classmethod
    def from_connection_string(cls, conn):
        return cls()

    def close(self):
        self.closed = True

    def get_queue_sender(self, name):
        client = self

        class _S:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def send_messages(s, m):
                client.queues.setdefault(name, []).append(_FakeMsg(str(m.body_str)))

        return _S()

    def get_queue_receiver(self, name, max_wait_time=None):
        client = self

        class _R:
            def __enter__(r):
                return r

            def __exit__(r, *a):
                return False

            def __iter__(r):
                return iter(list(client.queues.get(name, [])))

            def complete_message(r, m):
                client.queues[name].remove(m)

            def abandon_message(r, m):
                pass

        return _R()

    def get_topic_sender(self, name):
        client = self

        class _S:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def send_messages(s, m):
                client.sent_topics.append(name)

        return _S()

    def get_subscription_receiver(self, *a, **k):
        raise AssertionError("topic check must not receive (floci-az 0.13.0 bug)")


class _FakeSbMessage:
    def __init__(self, body):
        self.body_str = body


@pytest.fixture()
def fake_sb(monkeypatch):
    pytest.importorskip("azure.servicebus")
    import azure.servicebus as sb

    _FakeSbClient.instances = 0
    monkeypatch.setattr(sb, "ServiceBusClient", _FakeSbClient)
    monkeypatch.setattr(sb, "ServiceBusMessage", _FakeSbMessage)
    return _FakeSbClient


def test_servicebus_queue_round_trip(dragonfly, fake_sb):
    r = dragonfly.check_servicebus(
        {"kind": "servicebus", "name": "main", "connection": "cs-q", "queues": ["jobs"]}
    )
    assert r["ok"], r
    assert "round-trip ok (queue jobs)" in r["detail"]


def test_servicebus_client_reused_across_sweeps(dragonfly, fake_sb):
    # kubedock's reverse proxy leaks a socket per connection: one long-lived
    # client per namespace, not one per sweep
    chk = {"kind": "servicebus", "name": "main", "connection": "cs-reuse", "queues": ["jobs"]}
    for _ in range(3):
        assert dragonfly.check_servicebus(chk)["ok"]
    assert fake_sb.instances == 1


def test_servicebus_topic_only_is_send_only(dragonfly, fake_sb):
    r = dragonfly.check_servicebus(
        {
            "kind": "servicebus", "name": "events-ns", "connection": "cs-t",
            "queues": [], "topics": [{"name": "events", "subscriptions": ["worker"]}],
        }
    )
    assert r["ok"], r
    assert r["detail"].startswith("send ok (topic events")


def test_servicebus_failure_drops_cached_client(dragonfly, fake_sb, monkeypatch):
    chk = {"kind": "servicebus", "name": "main", "connection": "cs-fail", "queues": ["jobs"]}
    assert dragonfly.check_servicebus(chk)["ok"]
    cached = dragonfly._clients[("servicebus", "cs-fail")]
    monkeypatch.setattr(cached, "get_queue_sender",
                        lambda name: (_ for _ in ()).throw(RuntimeError("link lost")))
    r = dragonfly.check_servicebus(chk)
    assert r["ok"] is False and "link lost" in r["error"]
    assert ("servicebus", "cs-fail") not in dragonfly._clients
    assert cached.closed


def test_sdk_pin_matches_dragonfly_image():
    # dragonfly's image and the dev env must run the same Service Bus SDK:
    # the unit tests are only meaningful against what ships
    import re
    from importlib.metadata import version

    dockerfile = (DRAGONFLY.parent / "Dockerfile").read_text()
    pinned = re.search(r"azure-servicebus==(\S+)", dockerfile).group(1)
    assert version("azure-servicebus") == pinned


# ------------------------------------------------------ dashboard subtitle

def _report(*kinds):
    return {f"svc-{i}": {"ok": True, "kind": k} for i, k in enumerate(kinds)}


@pytest.mark.parametrize(
    ("kinds", "expected"),
    [
        (("rds", "s3"), "AWS services discovered via the AWS control plane, verified live."),
        (("keyvault", "servicebus"), "Azure services from the environment spec, verified live."),
        (("dynamodb", "azuresql"),
         "AWS services discovered via the AWS control plane and "
         "Azure services from the environment spec, verified live."),
        ((), "Services verified live."),
        (("app",), "Services verified live."),  # app checks belong to no cloud
    ],
)
def test_describe_sources(dragonfly, kinds, expected):
    assert dragonfly.describe_sources(_report(*kinds)) == expected


def test_every_check_kind_belongs_to_a_cloud(dragonfly):
    # a new service kind must be added to CLOUDS, or the subtitle omits it
    import re

    source = DRAGONFLY.read_text()
    emitted = set(re.findall(r'r\["kind"\] = "([a-z]+)"', source)) - {"app"}
    azure = {"keyvault", "servicebus", "pgflex", "azuresql"}  # MAYFLY_AZURE_CHECKS kinds
    known = set().union(*(members for _, members, _ in dragonfly.CLOUDS))
    assert emitted | azure <= known, (emitted | azure) - known


def test_aws_discovery_skipped_without_aws_emulator(dragonfly, monkeypatch):
    # no AWS emulator declared -> mayfly injects no AWS_ENDPOINT_URL, and
    # dragonfly must not report a failed AWS discovery for a cloud that
    # isn't there
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("MAYFLY_AZURE_CHECKS", raising=False)
    report = dragonfly.run_all()
    assert not [k for k in report if k.endswith(" discovery")]


def test_aws_discovery_runs_with_aws_emulator(dragonfly, monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://aws:4566")
    report = dragonfly.run_all()
    # the fixture's _aws raises, so each AWS discovery reports its failure
    assert "rds discovery" in report and "s3 discovery" in report
