"""AWS-API provisioners backed by floci (+ kubedock for container services)."""

import re
import secrets as _pysecrets
import time
from typing import Optional

from botocore.exceptions import ClientError

from ..emulators import AWS_ENDPOINT

DB_USER = "app"
DB_PASSWORD = "apppass"  # POC parity; per-env generated credentials are planned


def _direct_service(name: str, selector: dict[str, str], port: int) -> dict:
    """Service selecting the kubedock-spawned pod directly.

    Gives emulator-backed services the same ``servicename:standard-port``
    contract as the native backend, with data traffic going pod-to-pod
    instead of through the aws pod's reverse-proxy (and surviving emulator
    restarts). kubedock copies the container labels ministack sets (dbid /
    clusterid) onto the spawned pod, which is what makes this selectable.
    The aws:<published-port> endpoint remains valid for AWS-API fidelity.
    """
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name},
        "spec": {"selector": selector, "ports": [{"port": port, "targetPort": port}]},
    }


def _wait(label: str, timeout_s: int, poll, want, progress, interval: int = 2):
    """Poll `poll()` until it returns `want` or timeout. Returns last value."""
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            last = poll()
        except ClientError:
            last = None
        if last == want:
            return last
        time.sleep(interval)
    raise TimeoutError(f"timeout waiting for {label} (last status: {last})")


class S3Provisioner:
    def provision(self, buckets: list[str], ctx) -> dict:
        if not buckets:
            return {}
        s3 = ctx.session_factory().client("s3")
        for b in buckets:
            try:
                s3.head_bucket(Bucket=b)
            except ClientError:
                s3.create_bucket(Bucket=b)
            ctx.progress(f"s3: bucket {b}")
        return {
            "s3-buckets": {
                "BUCKETS": ",".join(buckets),
                "S3_ENDPOINT": AWS_ENDPOINT,
            }
        }


def _sm_k8s_name(secret_name: str) -> str:
    """app/api-key -> sm-app-api-key (a valid k8s Secret name)."""
    mangled = re.sub(r"[^a-z0-9-]+", "-", secret_name.lower()).strip("-")
    return f"sm-{mangled}"[:63].rstrip("-")


class SecretsManagerProvisioner:
    """Seed Secrets Manager with fixtures. In-process in the emulator.

    Literal ``value:`` entries converge to the spec (updated on re-up);
    ``generate: true`` entries get a random value on first creation and are
    left untouched afterwards, so re-ups never rotate them out from under a
    running app. Each secret is mirrored to a k8s Secret ``sm-<name>`` for
    apps that consume env vars instead of the SDK.
    """

    def provision(self, items, ctx) -> dict:
        secrets = {}
        sm = ctx.session_factory().client("secretsmanager")
        existing = {s["Name"] for s in sm.list_secrets().get("SecretList", [])}
        for item in items:
            if item.name in existing:
                if item.value is not None:
                    sm.update_secret(SecretId=item.name, SecretString=item.value)
                    ctx.progress(f"secretsmanager: {item.name} converged to spec value")
                else:
                    ctx.progress(f"secretsmanager: {item.name} exists (generated; kept)")
            else:
                value = item.value if item.value is not None else _pysecrets.token_urlsafe(24)
                sm.create_secret(Name=item.name, SecretString=value)
                ctx.progress(
                    f"secretsmanager: {item.name} created"
                    + (" (generated)" if item.generate else "")
                )
            got = sm.get_secret_value(SecretId=item.name)
            secrets[_sm_k8s_name(item.name)] = {
                "SECRET_NAME": item.name,
                "SECRET_ARN": got["ARN"],
                "SECRET_VALUE": got["SecretString"],
            }
        return secrets


class AlbProvisioner:
    """Emulated ALB with a working data plane (patched ministack image).

    Registers the target app's Service DNS name as an instance target; the
    patched emulator proxies data-plane requests to it. Reachable at
    http://aws:4566/_alb/<name>/ or via Host header <name>.alb.localhost.
    """

    def provision(self, items, ctx) -> dict:
        secrets = {}
        elbv2 = ctx.session_factory().client("elbv2")
        for alb in items:
            ctx.progress(f"alb: {alb.name} creating")
            lbs = elbv2.describe_load_balancers().get("LoadBalancers", [])
            lb = next((x for x in lbs if x["LoadBalancerName"] == alb.name), None)
            if lb is None:
                lb = elbv2.create_load_balancer(
                    Name=alb.name,
                    Subnets=["subnet-ephemeral-a", "subnet-ephemeral-b"],
                    Scheme="internet-facing",
                    Type="application",
                )["LoadBalancers"][0]
            lb_arn = lb["LoadBalancerArn"]

            tg_name = f"{alb.name}-tg"
            tgs = elbv2.describe_target_groups().get("TargetGroups", [])
            tg = next((t for t in tgs if t["TargetGroupName"] == tg_name), None)
            if tg is None:
                tg = elbv2.create_target_group(
                    Name=tg_name,
                    Protocol="HTTP",
                    Port=8080,
                    TargetType="instance",
                    VpcId="vpc-ephemeral",
                    HealthCheckPath="/healthz",
                )["TargetGroups"][0]
            tg_arn = tg["TargetGroupArn"]

            # target = the app's Service DNS name; kube-proxy balances pods
            elbv2.register_targets(
                TargetGroupArn=tg_arn,
                Targets=[{"Id": alb.target_app, "Port": 8080}],
            )

            listeners = elbv2.describe_listeners(LoadBalancerArn=lb_arn).get(
                "Listeners", []
            )
            if not listeners:
                elbv2.create_listener(
                    LoadBalancerArn=lb_arn,
                    Protocol="HTTP",
                    Port=80,
                    DefaultActions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
                )

            url = f"{AWS_ENDPOINT}/_alb/{alb.name}/"
            ctx.progress(
                f"alb: {alb.name} active -> {alb.target_app} (data plane {url})"
            )
            secrets[f"alb-{alb.name}"] = {
                "ALB_DNS_NAME": lb["DNSName"],
                "ALB_URL": url,
                "ALB_HOST": f"{alb.name}.alb.localhost",
                "ALB_TARGET_APP": alb.target_app,
            }
            public_url = self._expose(ctx, alb.name)
            if public_url:
                secrets[f"alb-{alb.name}"]["ALB_PUBLIC_URL"] = public_url
                ctx.progress(f"alb: {alb.name} exposed at {public_url}")
        return secrets

    @staticmethod
    def _expose(ctx, name: str):
        """Route <name>.<namespace>.localtest.me through the cluster ingress
        to the emulated ALB, rewriting Host to what its matcher expects.

        Traefik-specific (the Host rewrite needs a Middleware); silently
        skipped on clusters without the Traefik CRDs — there you'd use a
        real load balancer anyway.
        """
        middleware_body = {
            "kind": "Middleware",
            "metadata": {"name": f"alb-{name}-host"},
            "spec": {
                "headers": {"customRequestHeaders": {"Host": f"{name}.alb.localhost"}}
            },
        }
        group = None
        for api_version in ("traefik.io/v1alpha1", "traefik.containo.us/v1alpha1"):
            try:
                ctx.k8s.apply(
                    {"apiVersion": api_version, **middleware_body}, namespace=ctx.namespace
                )
                group = api_version
                break
            except Exception:
                continue
        if group is None:
            ctx.progress(f"alb: {name} not exposed (no Traefik middleware CRD)")
            return None

        host = f"{name}.{ctx.namespace}.localtest.me"
        ctx.k8s.apply(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "Ingress",
                "metadata": {
                    "name": f"alb-{name}",
                    "annotations": {
                        "traefik.ingress.kubernetes.io/router.middlewares": (
                            f"{ctx.namespace}-alb-{name}-host@kubernetescrd"
                        )
                    },
                },
                "spec": {
                    "rules": [
                        {
                            "host": host,
                            "http": {
                                "paths": [
                                    {
                                        "path": "/",
                                        "pathType": "Prefix",
                                        "backend": {
                                            "service": {
                                                "name": "aws",
                                                "port": {"number": 4566},
                                            }
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                },
            },
            namespace=ctx.namespace,
        )
        return f"http://{host}/"


class DynamoProvisioner:
    def provision(self, items, ctx) -> dict:
        secrets = {}
        ddb = ctx.session_factory().client("dynamodb")
        for table in items:
            ctx.progress(f"dynamodb: {table.name} creating")
            if table.name not in ddb.list_tables().get("TableNames", []):
                ddb.create_table(
                    TableName=table.name,
                    AttributeDefinitions=[
                        {"AttributeName": table.hash_key, "AttributeType": "S"}
                    ],
                    KeySchema=[{"AttributeName": table.hash_key, "KeyType": "HASH"}],
                    BillingMode="PAY_PER_REQUEST",
                )

            def status():
                return ddb.describe_table(TableName=table.name)["Table"]["TableStatus"]

            _wait(f"dynamodb/{table.name}", 60, status, "ACTIVE", ctx.progress)
            ctx.progress(f"dynamodb: {table.name} ACTIVE")
            secrets[f"dynamodb-{table.name}"] = {
                "TABLE_NAME": table.name,
                "HASH_KEY": table.hash_key,
                "DYNAMODB_ENDPOINT": AWS_ENDPOINT,
            }
        return secrets


class RdsProvisioner:
    def provision(self, items, ctx) -> dict:
        secrets = {}
        rds = ctx.session_factory().client("rds")
        for db in items:
            ctx.progress(f"rds: {db.name} ({db.engine}) creating")
            # floci returns 200 + empty list for unknown identifiers (real AWS
            # raises DBInstanceNotFound), so check emptiness, not exceptions.
            if not self._instances(rds, db.name):
                rds.create_db_instance(
                    DBInstanceIdentifier=db.name,
                    Engine=db.engine,
                    DBInstanceClass="db.t3.micro",
                    MasterUsername=DB_USER,
                    MasterUserPassword=DB_PASSWORD,
                    DBName=db.db_name,
                    AllocatedStorage=20,
                )

            def status():
                instances = self._instances(rds, db.name)
                return instances[0]["DBInstanceStatus"] if instances else None

            _wait(f"rds/{db.name}", 480, status, "available", ctx.progress)
            inst = self._instances(rds, db.name)[0]
            api_host = inst["Endpoint"]["Address"]
            api_port = inst["Endpoint"]["Port"]
            svc = f"rds-{db.name}"
            port = 5432 if db.engine == "postgres" else 3306
            ctx.k8s.apply(
                _direct_service(svc, {"ministack": "rds", "dbid": db.name}, port),
                namespace=ctx.namespace,
            )
            ctx.progress(
                f"rds: {db.name} available at {svc}:{port} (AWS API: {api_host}:{api_port})"
            )
            secrets[svc] = {
                "DATABASE_URL": f"{db.scheme}://{DB_USER}:{DB_PASSWORD}@{svc}:{port}/{db.db_name}",
                "DB_HOST": svc,
                "DB_PORT": str(port),
                "DB_USER": DB_USER,
                "DB_PASSWORD": DB_PASSWORD,
                "DB_NAME": db.db_name,
            }
        return secrets

    @staticmethod
    def _instances(rds, name: str) -> list:
        try:
            return rds.describe_db_instances(DBInstanceIdentifier=name)["DBInstances"]
        except ClientError:
            return []


class ElastiCacheProvisioner:
    def provision(self, items, ctx) -> dict:
        secrets = {}
        ec = ctx.session_factory().client("elasticache")
        for cache in items:
            ctx.progress(
                f"elasticache: {cache.name} creating "
                f"({cache.engine} {cache.resolved_version})"
            )
            if not self._clusters(ec, cache.name):
                ec.create_cache_cluster(
                    CacheClusterId=cache.name,
                    Engine=cache.engine,
                    EngineVersion=cache.resolved_version,
                    CacheNodeType="cache.t3.micro",
                    NumCacheNodes=1,
                )

            def status():
                clusters = self._clusters(ec, cache.name)
                return clusters[0]["CacheClusterStatus"] if clusters else None

            _wait(f"elasticache/{cache.name}", 240, status, "available", ctx.progress)
            r = ec.describe_cache_clusters(CacheClusterId=cache.name, ShowCacheNodeInfo=True)
            node = r["CacheClusters"][0]["CacheNodes"][0]["Endpoint"]
            api_host, api_port = node["Address"], node["Port"]
            svc = f"elasticache-{cache.name}"
            port = cache.port
            ctx.k8s.apply(
                _direct_service(svc, {"ministack": "elasticache", "clusterid": cache.name}, port),
                namespace=ctx.namespace,
            )
            ctx.progress(
                f"elasticache: {cache.name} available at {svc}:{port} "
                f"(AWS API: {api_host}:{api_port})"
            )
            if cache.engine == "memcached":
                secrets[svc] = {
                    "CACHE_ENGINE": "memcached",
                    "MEMCACHED_HOST": svc,
                    "MEMCACHED_PORT": str(port),
                }
            else:
                secrets[svc] = {
                    "CACHE_ENGINE": cache.engine,
                    "REDIS_URL": f"redis://{svc}:{port}",
                    "REDIS_HOST": svc,
                    "REDIS_PORT": str(port),
                }
        return secrets

    @staticmethod
    def _clusters(ec, name: str) -> list:
        try:
            return ec.describe_cache_clusters(CacheClusterId=name)["CacheClusters"]
        except ClientError:
            return []


class MskHybridProvisioner:
    """Native broker data-plane + emulator MSK control-plane registration.

    MiniStack emulates the MSK control plane but not the Kafka wire protocol;
    GetBootstrapBrokers honors MINISTACK_MSK_BOOTSTRAP (set by mayfly to the
    natively-deployed broker's address). So: deploy the real broker natively,
    then register the cluster through the AWS API so ListClusters /
    DescribeCluster / GetBootstrapBrokers all answer correctly for apps.
    """

    def provision(self, items, ctx) -> dict:
        from .native import MskNativeProvisioner

        secrets = MskNativeProvisioner().provision(items, ctx)
        kafka = ctx.session_factory().client("kafka")
        for cluster in items:
            ctx.progress(f"msk: {cluster.name} registering with emulator control plane")
            arn = MskProvisioner._find_arn(kafka, cluster.name)
            if not arn:
                arn = kafka.create_cluster(
                    ClusterName=cluster.name,
                    KafkaVersion="3.5.1",
                    NumberOfBrokerNodes=1,
                    BrokerNodeGroupInfo={
                        "InstanceType": "kafka.t3.small",
                        "ClientSubnets": ["subnet-ephemeral"],
                    },
                )["ClusterArn"]

            def state():
                return kafka.describe_cluster(ClusterArn=arn)["ClusterInfo"]["State"]

            _wait(f"msk/{cluster.name} control plane", 120, state, "ACTIVE", ctx.progress)
            brokers = kafka.get_bootstrap_brokers(ClusterArn=arn)["BootstrapBrokerString"]
            expected = secrets[f"msk-{cluster.name}"]["KAFKA_BROKERS"]
            if expected not in brokers:
                ctx.progress(
                    f"msk: WARNING GetBootstrapBrokers returned {brokers!r}, "
                    f"expected it to include {expected!r}"
                )
            ctx.progress(f"msk: {cluster.name} control plane ACTIVE, brokers {brokers}")
        return secrets


class MskProvisioner:
    def provision(self, items, ctx) -> dict:
        secrets = {}
        kafka = ctx.session_factory().client("kafka")
        for cluster in items:
            ctx.progress(f"msk: {cluster.name} creating")
            arn = self._find_arn(kafka, cluster.name)
            if not arn:
                arn = kafka.create_cluster(
                    ClusterName=cluster.name,
                    KafkaVersion="3.5.1",
                    NumberOfBrokerNodes=1,
                    BrokerNodeGroupInfo={
                        "InstanceType": "kafka.t3.small",
                        "ClientSubnets": ["subnet-ephemeral"],
                    },
                )["ClusterArn"]

            def state():
                return kafka.describe_cluster(ClusterArn=arn)["ClusterInfo"]["State"]

            _wait(f"msk/{cluster.name}", 480, state, "ACTIVE", ctx.progress)
            brokers = kafka.get_bootstrap_brokers(ClusterArn=arn)["BootstrapBrokerString"]
            ctx.progress(f"msk: {cluster.name} active, brokers {brokers}")
            secrets[f"msk-{cluster.name}"] = {"KAFKA_BROKERS": brokers}
        return secrets

    @staticmethod
    def _find_arn(kafka, name: str) -> Optional[str]:
        clusters = kafka.list_clusters(ClusterNameFilter=name).get("ClusterInfoList", [])
        for c in clusters:
            if c.get("ClusterName") == name:
                return c["ClusterArn"]
        return None
