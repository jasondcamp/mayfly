from mayfly.installer import (
    DEFAULT_SCHEDULE,
    REAPER_NAME,
    SYSTEM_NAMESPACE,
    default_cli_image,
    reaper_manifests,
)


def test_reaper_manifests_shape():
    ms = reaper_manifests("ghcr.io/jasondcamp/mayfly-cli:1.2.3", DEFAULT_SCHEDULE)
    kinds = [m["kind"] for m in ms]
    assert kinds == [
        "Namespace",
        "ServiceAccount",
        "ClusterRole",
        "ClusterRoleBinding",
        "CronJob",
    ]
    ns = ms[0]
    assert ns["metadata"]["name"] == SYSTEM_NAMESPACE
    assert "mayfly.dev/managed" not in ns["metadata"]["labels"]  # never reapable


def test_reaper_cronjob_invariants():
    cron = reaper_manifests("img:v", "*/5 * * * *")[-1]
    assert cron["spec"]["schedule"] == "*/5 * * * *"
    assert cron["spec"]["concurrencyPolicy"] == "Forbid"
    pod = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["enableServiceLinks"] is False
    assert pod["serviceAccountName"] == REAPER_NAME
    c = pod["containers"][0]
    assert c["image"] == "img:v"
    assert c["args"] == ["reap"]


def test_reaper_rbac_scope():
    role = reaper_manifests("img:v", DEFAULT_SCHEDULE)[2]
    (rule,) = role["rules"]
    assert rule["resources"] == ["namespaces"]
    assert sorted(rule["verbs"]) == ["delete", "get", "list"]


def test_default_cli_image_tracks_version():
    from mayfly import __version__

    assert default_cli_image().endswith(f":{__version__}")
    assert "latest" not in default_cli_image()
