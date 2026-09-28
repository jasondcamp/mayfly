"""Spec-model tests for the Azure surface (emulators.azure + service classes)."""

import pytest
from pydantic import ValidationError

from mayfly.spec import EnvSpec, KeyVaultSecretSpec

# every service class needs its cloud emulator declared
EMULATORS = {"azure": {"kind": "floci-az"}}

AZURE_FULL = {
    "seed": "az-test",
    "emulators": {"azure": {"kind": "floci-az", "expose": True}},
    "services": {
        "keyvault": [
            {
                "name": "app-secrets",
                "secrets": [
                    {"name": "api-key", "value": "test-key-123"},
                    {"name": "signing-key", "generate": True},
                ],
            }
        ],
        "servicebus": [
            {
                "name": "main",
                "queues": ["jobs", "emails"],
                "topics": [{"name": "events", "subscriptions": ["worker", "audit"]}],
            }
        ],
        "postgresflexible": [{"name": "appdb", "dbName": "app", "version": "16"}],
        "azuresql": [{"name": "coredb", "dbName": "app"}],
    },
}


def test_azure_full_spec():
    spec = EnvSpec.model_validate(AZURE_FULL)
    assert spec.emulators.azure.kind == "floci-az"
    assert spec.emulators.azure.expose is True
    assert spec.emulators.aws is None
    kv = spec.services.keyvault[0]
    assert kv.name == "app-secrets"
    assert kv.secrets[0].value == "test-key-123"
    assert kv.secrets[1].generate is True
    sb = spec.services.servicebus[0]
    assert sb.queues == ["jobs", "emails"]
    assert sb.topics[0].subscriptions == ["worker", "audit"]
    assert spec.services.postgresflexible[0].db_name == "app"
    assert spec.services.azuresql[0].db_name == "app"


def test_azure_defaults():
    spec = EnvSpec.model_validate({"seed": "x", "emulators": EMULATORS})
    assert spec.emulators.azure.kind == "floci-az"
    assert spec.emulators.azure.image is None
    assert spec.emulators.azure.expose is False
    assert spec.services.keyvault == []
    assert spec.services.servicebus == []
    assert spec.services.postgresflexible == []
    assert spec.services.azuresql == []


def test_azure_latest_version_rejected():
    with pytest.raises(ValidationError, match="pinned tag"):
        EnvSpec.model_validate(
            {"seed": "x", "emulators": {"azure": {"kind": "floci-az", "version": "latest"}}}
        )


def test_azure_unknown_kind_rejected():
    with pytest.raises(ValidationError):
        EnvSpec.model_validate({"seed": "x", "emulators": {"azure": {"kind": "azurite"}}})


def test_azure_unknown_keys_rejected():
    with pytest.raises(ValidationError):
        EnvSpec.model_validate(
            {"seed": "x", "emulators": {"azure": {"kind": "floci-az", "exposed": True}}}
        )


# ---------------------------------------------------------------- keyvault
def test_keyvault_secret_value_xor_generate():
    for bad in (
        {"name": "x"},                              # neither
        {"name": "x", "value": "v", "generate": True},  # both
    ):
        with pytest.raises(ValidationError, match="exactly one"):
            EnvSpec.model_validate(
                {"seed": "x", "emulators": EMULATORS, "services": {"keyvault": [{"name": "vault", "secrets": [bad]}]}}
            )


def test_keyvault_secret_name_rules():
    ok = KeyVaultSecretSpec.model_validate({"name": "API-key-2", "value": "v"})
    assert ok.env_key == "SECRET_API_KEY_2"
    for bad in ("has space", "under_score", "slash/y", "", "a" * 128):
        with pytest.raises(ValidationError):
            KeyVaultSecretSpec.model_validate({"name": bad, "value": "v"})


def test_keyvault_vault_name_rules():
    def make(name):
        return EnvSpec.model_validate(
            {"seed": "x", "emulators": EMULATORS, "services": {"keyvault": [{"name": name}]}}
        )

    assert make("app-secrets").services.keyvault[0].name == "app-secrets"
    assert make("abc").services.keyvault[0].name == "abc"
    for bad in (
        "ab",                    # too short
        "a" * 25,                # too long
        "9vault",                # must start with a letter
        "Bad-Case",              # not a DNS label
        "has_underscore",
    ):
        with pytest.raises(ValidationError):
            make(bad)


def test_keyvault_empty_secrets_allowed():
    spec = EnvSpec.model_validate(
        {"seed": "x", "emulators": EMULATORS, "services": {"keyvault": [{"name": "vault"}]}}
    )
    assert spec.services.keyvault[0].secrets == []


# --------------------------------------------------------------- servicebus
def test_servicebus_requires_queue_or_topic():
    with pytest.raises(ValidationError, match="at least one queue or topic"):
        EnvSpec.model_validate(
            {"seed": "x", "emulators": EMULATORS, "services": {"servicebus": [{"name": "main"}]}}
        )


def test_servicebus_queue_only_and_topic_only():
    q = EnvSpec.model_validate(
        {"seed": "x", "emulators": EMULATORS, "services": {"servicebus": [{"name": "a", "queues": ["jobs"]}]}}
    )
    assert q.services.servicebus[0].topics == []
    t = EnvSpec.model_validate(
        {"seed": "x", "emulators": EMULATORS, "services": {"servicebus": [{"name": "b", "topics": [{"name": "ev"}]}]}}
    )
    assert t.services.servicebus[0].queues == []
    assert t.services.servicebus[0].topics[0].subscriptions == []


def test_servicebus_entity_name_rules():
    ok = EnvSpec.model_validate(
        {
            "seed": "x",
            "emulators": EMULATORS, "services": {
                "servicebus": [
                    {"name": "main", "queues": ["orders.created", "a-b_c", "Q1"]}
                ]
            },
        }
    )
    assert len(ok.services.servicebus[0].queues) == 3
    for bad in ("", ".dot-lead", "trail.", "has space", "slash/y"):
        with pytest.raises(ValidationError):
            EnvSpec.model_validate(
                {"seed": "x", "emulators": EMULATORS, "services": {"servicebus": [{"name": "main", "queues": [bad]}]}}
            )
    with pytest.raises(ValidationError):  # subscription names validated too
        EnvSpec.model_validate(
            {
                "seed": "x",
                "emulators": EMULATORS, "services": {
                    "servicebus": [
                        {"name": "main", "topics": [{"name": "ev", "subscriptions": ["no way"]}]}
                    ]
                },
            }
        )


def test_servicebus_namespace_is_dns_label():
    with pytest.raises(ValidationError):
        EnvSpec.model_validate(
            {"seed": "x", "emulators": EMULATORS, "services": {"servicebus": [{"name": "Bad_NS", "queues": ["q"]}]}}
        )


# ---------------------------------------------------------------- databases
def test_postgresflexible_defaults_and_version():
    spec = EnvSpec.model_validate(
        {
            "seed": "x",
            "emulators": EMULATORS, "services": {
                "postgresflexible": [{"name": "a"}, {"name": "b", "version": "15"}]
            },
        }
    )
    a, b = spec.services.postgresflexible
    assert (a.db_name, a.resolved_version, a.backend) == ("app", "16", "auto")
    assert b.resolved_version == "15"


def test_azuresql_defaults_and_alias():
    spec = EnvSpec.model_validate(
        {"seed": "x", "emulators": EMULATORS, "services": {"azuresql": [{"name": "core", "dbName": "orders"}]}}
    )
    assert spec.services.azuresql[0].db_name == "orders"


def test_azure_db_names_are_dns_labels():
    for cls in ("postgresflexible", "azuresql"):
        with pytest.raises(ValidationError):
            EnvSpec.model_validate(
                {"seed": "x", "emulators": EMULATORS, "services": {cls: [{"name": "Bad_Name"}]}}
            )


def test_azure_db_unknown_fields_rejected():
    with pytest.raises(ValidationError):  # engine belongs to rds, not pgflex
        EnvSpec.model_validate(
            {"seed": "x", "emulators": EMULATORS, "services": {"postgresflexible": [{"name": "a", "engine": "postgres"}]}}
        )
    with pytest.raises(ValidationError):  # no version knob on azuresql yet
        EnvSpec.model_validate(
            {"seed": "x", "emulators": EMULATORS, "services": {"azuresql": [{"name": "a", "version": "2022"}]}}
        )


# -------------------------------------------------------------------- misc
def test_azure_spec_hash_sensitive():
    a = EnvSpec.model_validate(AZURE_FULL)
    raw = {**AZURE_FULL, "emulators": {"azure": {"kind": "floci-az", "expose": False}}}
    b = EnvSpec.model_validate(raw)
    assert a.spec_hash() != b.spec_hash()


def test_azure_set_override_named_list_entry():
    from mayfly.spec import apply_overrides

    raw = {
        "seed": "x",
        "emulators": EMULATORS, "services": {"postgresflexible": [{"name": "appdb", "dbName": "app"}]},
    }
    apply_overrides(raw, ["services.postgresflexible.appdb.dbName=other"])
    assert EnvSpec.model_validate(raw).services.postgresflexible[0].db_name == "other"


def test_backend_literal_enforced():
    with pytest.raises(ValidationError):
        EnvSpec.model_validate(
            {"seed": "x", "emulators": EMULATORS, "services": {"keyvault": [{"name": "vault", "backend": "cloud"}]}}
        )
