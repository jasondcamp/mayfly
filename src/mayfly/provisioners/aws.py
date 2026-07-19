"""AWS-API provisioners backed by floci (+ kubedock for container services)."""

import time

from botocore.exceptions import ClientError

from ..emulators import AWS_ENDPOINT

DB_USER = "app"
DB_PASSWORD = "apppass"  # POC parity; per-env generated credentials are planned


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
            host = inst["Endpoint"]["Address"]
            port = str(inst["Endpoint"]["Port"])
            ctx.progress(f"rds: {db.name} available at {host}:{port}")
            secrets[f"rds-{db.name}"] = {
                "DATABASE_URL": f"{db.scheme}://{DB_USER}:{DB_PASSWORD}@{host}:{port}/{db.db_name}",
                "DB_HOST": host,
                "DB_PORT": port,
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
            ctx.progress(f"elasticache: {cache.name} creating")
            if not self._clusters(ec, cache.name):
                ec.create_cache_cluster(
                    CacheClusterId=cache.name,
                    Engine="redis",
                    CacheNodeType="cache.t3.micro",
                    NumCacheNodes=1,
                )

            def status():
                clusters = self._clusters(ec, cache.name)
                return clusters[0]["CacheClusterStatus"] if clusters else None

            _wait(f"elasticache/{cache.name}", 240, status, "available", ctx.progress)
            r = ec.describe_cache_clusters(CacheClusterId=cache.name, ShowCacheNodeInfo=True)
            node = r["CacheClusters"][0]["CacheNodes"][0]["Endpoint"]
            host, port = node["Address"], str(node["Port"])
            ctx.progress(f"elasticache: {cache.name} available at {host}:{port}")
            secrets[f"elasticache-{cache.name}"] = {
                "REDIS_URL": f"redis://{host}:{port}",
                "REDIS_HOST": host,
                "REDIS_PORT": port,
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
    def _find_arn(kafka, name: str) -> str | None:
        clusters = kafka.list_clusters(ClusterNameFilter=name).get("ClusterInfoList", [])
        for c in clusters:
            if c.get("ClusterName") == name:
                return c["ClusterArn"]
        return None
