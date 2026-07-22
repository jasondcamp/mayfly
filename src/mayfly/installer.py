"""Cluster-level mayfly components: the in-cluster reaper CronJob.

`mayfly install` applies these into the ``mayfly-system`` namespace so
expired environments are deleted on a schedule with nobody running the
CLI. There is deliberately no state here: the reaper runs ``mayfly reap``,
which reads the same labels/annotations the CLI does — the cluster's
labeled namespaces are the only store.
"""

from . import __version__

SYSTEM_NAMESPACE = "mayfly-system"
REAPER_NAME = "mayfly-reaper"
CLI_IMAGE = "ghcr.io/jasondcamp/mayfly-cli"
DEFAULT_SCHEDULE = "*/10 * * * *"
SYSTEM_LABELS = {"mayfly.dev/system": "true"}


def default_cli_image() -> str:
    return f"{CLI_IMAGE}:{__version__}"


def reaper_manifests(image: str, schedule: str) -> list[dict]:
    """Namespace + ServiceAccount + ClusterRole/Binding + CronJob.

    RBAC cannot scope namespace deletion by label, so the ClusterRole
    grants get/list/delete on namespaces and the label guard lives in
    ``mayfly reap`` itself (it only touches ``mayfly.dev/managed=true``,
    same as the CLI everywhere else).
    """
    namespace = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": SYSTEM_NAMESPACE, "labels": SYSTEM_LABELS},
    }
    service_account = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": REAPER_NAME, "labels": SYSTEM_LABELS},
    }
    cluster_role = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRole",
        "metadata": {"name": REAPER_NAME, "labels": SYSTEM_LABELS},
        "rules": [
            {
                "apiGroups": [""],
                "resources": ["namespaces"],
                "verbs": ["get", "list", "delete"],
            }
        ],
    }
    cluster_role_binding = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {"name": REAPER_NAME, "labels": SYSTEM_LABELS},
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": REAPER_NAME,
        },
        "subjects": [
            {
                "kind": "ServiceAccount",
                "name": REAPER_NAME,
                "namespace": SYSTEM_NAMESPACE,
            }
        ],
    }
    cron_job = {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {"name": REAPER_NAME, "labels": SYSTEM_LABELS},
        "spec": {
            "schedule": schedule,
            "concurrencyPolicy": "Forbid",
            "startingDeadlineSeconds": 300,
            "successfulJobsHistoryLimit": 3,
            "failedJobsHistoryLimit": 3,
            "jobTemplate": {
                "spec": {
                    "backoffLimit": 1,
                    "activeDeadlineSeconds": 600,
                    "template": {
                        "metadata": {"labels": {"app": REAPER_NAME}},
                        "spec": {
                            "serviceAccountName": REAPER_NAME,
                            "enableServiceLinks": False,
                            "restartPolicy": "Never",
                            "containers": [
                                {
                                    "name": "reaper",
                                    "image": image,
                                    "args": ["reap"],
                                    "resources": {
                                        "requests": {"cpu": "10m", "memory": "32Mi"},
                                        "limits": {"memory": "128Mi"},
                                    },
                                }
                            ],
                        },
                    },
                }
            },
        },
    }
    return [namespace, service_account, cluster_role, cluster_role_binding, cron_job]
