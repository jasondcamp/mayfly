"""Native provisioners: container services as plain Deployments + Services.

kubedock 0.18 doesn't implement the Docker volumes API (501), which floci's
container-backed services require — so RDS/ElastiCache/MSK are provisioned
directly by mayfly instead of through the floci->kubedock Docker path. The
Secret contract is identical, so apps can't tell the difference.
"""

POSTGRES_IMAGE = "postgres:16-alpine"
MYSQL_IMAGE = "mysql:8.4"
MARIADB_IMAGE = "mariadb:11"
VALKEY_IMAGE = "valkey/valkey:8-alpine"
REDPANDA_IMAGE = "redpandadata/redpanda:v24.2.18"

DB_USER = "app"
DB_PASSWORD = "apppass"  # POC parity; per-env generated credentials are planned


def _deployment(name: str, container: dict) -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "labels": {"app": name, "mayfly.dev/service": "true"}},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {"enableServiceLinks": False, "containers": [container]},
            },
        },
    }


def _service(name: str, port: int) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name},
        "spec": {"selector": {"app": name}, "ports": [{"port": port, "targetPort": port}]},
    }


def _env(env: dict[str, str]) -> list[dict]:
    return [{"name": k, "value": v} for k, v in env.items()]


class RdsNativeProvisioner:
    def provision(self, items, ctx) -> dict:
        secrets = {}
        for db in items:
            svc = f"rds-{db.name}"
            if db.engine == "postgres":
                port = 5432
                container = {
                    "name": "postgres",
                    "image": POSTGRES_IMAGE,
                    "ports": [{"containerPort": port}],
                    "env": _env(
                        {
                            "POSTGRES_USER": DB_USER,
                            "POSTGRES_PASSWORD": DB_PASSWORD,
                            "POSTGRES_DB": db.db_name,
                        }
                    ),
                    "readinessProbe": {
                        "exec": {"command": ["pg_isready", "-U", DB_USER, "-d", db.db_name]},
                        "initialDelaySeconds": 2,
                        "periodSeconds": 2,
                    },
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "128Mi"},
                        "limits": {"memory": "512Mi"},
                    },
                }
            else:
                port = 3306
                image = MYSQL_IMAGE if db.engine == "mysql" else MARIADB_IMAGE
                prefix = "MYSQL" if db.engine == "mysql" else "MARIADB"
                container = {
                    "name": db.engine,
                    "image": image,
                    "ports": [{"containerPort": port}],
                    "env": _env(
                        {
                            f"{prefix}_USER": DB_USER,
                            f"{prefix}_PASSWORD": DB_PASSWORD,
                            f"{prefix}_DATABASE": db.db_name,
                            f"{prefix}_ROOT_PASSWORD": DB_PASSWORD,
                        }
                    ),
                    "readinessProbe": {
                        "tcpSocket": {"port": port},
                        "initialDelaySeconds": 5,
                        "periodSeconds": 3,
                    },
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "256Mi"},
                        "limits": {"memory": "768Mi"},
                    },
                }
            ctx.progress(f"rds: {db.name} ({db.engine}) deploying")
            ctx.k8s.apply_all([_deployment(svc, container), _service(svc, port)], ctx.namespace)
            ctx.k8s.wait_deployment(ctx.namespace, svc, timeout=300)
            ctx.progress(f"rds: {db.name} available at {svc}:{port}")
            secrets[svc] = {
                "DATABASE_URL": f"{db.scheme}://{DB_USER}:{DB_PASSWORD}@{svc}:{port}/{db.db_name}",
                "DB_HOST": svc,
                "DB_PORT": str(port),
                "DB_USER": DB_USER,
                "DB_PASSWORD": DB_PASSWORD,
                "DB_NAME": db.db_name,
            }
        return secrets


class ElastiCacheNativeProvisioner:
    def provision(self, items, ctx) -> dict:
        secrets = {}
        for cache in items:
            svc = f"elasticache-{cache.name}"
            port = 6379
            container = {
                "name": "valkey",
                "image": VALKEY_IMAGE,
                "ports": [{"containerPort": port}],
                "readinessProbe": {
                    "exec": {"command": ["valkey-cli", "ping"]},
                    "initialDelaySeconds": 1,
                    "periodSeconds": 2,
                },
                "resources": {
                    "requests": {"cpu": "25m", "memory": "32Mi"},
                    "limits": {"memory": "256Mi"},
                },
            }
            ctx.progress(f"elasticache: {cache.name} deploying")
            ctx.k8s.apply_all([_deployment(svc, container), _service(svc, port)], ctx.namespace)
            ctx.k8s.wait_deployment(ctx.namespace, svc, timeout=180)
            ctx.progress(f"elasticache: {cache.name} available at {svc}:{port}")
            secrets[svc] = {
                "REDIS_URL": f"redis://{svc}:{port}",
                "REDIS_HOST": svc,
                "REDIS_PORT": str(port),
            }
        return secrets


class MskNativeProvisioner:
    def provision(self, items, ctx) -> dict:
        from ..emulators import KAFKA_PORT

        secrets = {}
        for cluster in items:
            svc = f"msk-{cluster.name}"
            port = KAFKA_PORT
            container = {
                "name": "redpanda",
                "image": REDPANDA_IMAGE,
                "args": [
                    "redpanda", "start",
                    "--mode", "dev-container",
                    "--smp", "1",
                    "--memory", "512M",
                    "--kafka-addr", f"PLAINTEXT://0.0.0.0:{port}",
                    "--advertise-kafka-addr", f"PLAINTEXT://{svc}:{port}",
                ],
                "ports": [{"containerPort": port}],
                "readinessProbe": {
                    "exec": {"command": ["rpk", "cluster", "health", "--exit-when-healthy"]},
                    "initialDelaySeconds": 5,
                    "periodSeconds": 3,
                    "timeoutSeconds": 10,
                },
                "resources": {
                    "requests": {"cpu": "100m", "memory": "512Mi"},
                    "limits": {"memory": "1Gi"},
                },
            }
            ctx.progress(f"msk: {cluster.name} deploying (redpanda)")
            ctx.k8s.apply_all([_deployment(svc, container), _service(svc, port)], ctx.namespace)
            ctx.k8s.wait_deployment(ctx.namespace, svc, timeout=300)
            brokers = f"{svc}:{port}"
            for topic in cluster.topics:
                ctx.progress(f"msk: {cluster.name} creating topic {topic}")
                ctx.k8s.exec_in_deployment(
                    ctx.namespace, svc,
                    ["rpk", "topic", "create", topic, "--brokers", f"localhost:{port}"],
                )
            ctx.progress(f"msk: {cluster.name} active, brokers {brokers}")
            secrets[svc] = {"KAFKA_BROKERS": brokers}
        return secrets
