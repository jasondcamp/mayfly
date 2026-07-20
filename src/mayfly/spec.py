"""Environment spec: versioned, validated schema for env.yaml files."""

import hashlib
import re
from datetime import timedelta
from pathlib import Path
from typing import Literal, Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

API_VERSION = "mayfly/v1alpha1"
DEFAULT_TTL = "8h"

_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_TTL_RE = re.compile(r"^(\d+)([mhd])$")
_TTL_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_ttl(value: str) -> timedelta:
    m = _TTL_RE.match(value)
    if not m:
        raise ValueError(f"invalid ttl {value!r}: expected e.g. 30m, 8h, 2d")
    return timedelta(**{_TTL_UNITS[m.group(2)]: int(m.group(1))})


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _validate_dns_name(v: str) -> str:
    if not _NAME_RE.match(v):
        raise ValueError(f"{v!r} must be a lowercase DNS-1123 label")
    return v


class EmulatorSpec(_StrictModel):
    kind: Literal["ministack", "floci"] = "ministack"
    image: Optional[str] = None  # default: pinned per kind (see emulators.EMULATORS)
    version: Optional[str] = None  # image tag; default: pinned per kind

    @field_validator("version")
    @classmethod
    def _no_latest(cls, v: Optional[str]) -> Optional[str]:
        if v == "latest":
            raise ValueError("emulator.version must be a pinned tag, not 'latest'")
        return v


Backend = Literal["auto", "emulator", "native"]


class S3Spec(_StrictModel):
    buckets: list[str] = Field(default_factory=list)

    _names = field_validator("buckets")(lambda cls, v: [_validate_dns_name(b) for b in v])


class RdsSpec(_StrictModel):
    name: str
    engine: Literal["postgres", "mysql", "mariadb"] = "postgres"
    db_name: str = Field(default="app", alias="dbName")
    backend: Backend = "auto"

    _name = field_validator("name")(lambda cls, v: _validate_dns_name(v))

    @property
    def scheme(self) -> str:
        return "postgresql" if self.engine == "postgres" else "mysql"


class ElastiCacheSpec(_StrictModel):
    name: str
    engine: Literal["redis", "valkey", "memcached"] = "redis"
    version: Optional[str] = None  # engine version -> image tag; default per engine
    backend: Backend = "auto"

    _name = field_validator("name")(lambda cls, v: _validate_dns_name(v))

    @property
    def resolved_version(self) -> str:
        if self.version:
            return self.version
        return {"redis": "7.2", "valkey": "8", "memcached": "1.6"}[self.engine]

    @property
    def port(self) -> int:
        return 11211 if self.engine == "memcached" else 6379


class MskSpec(_StrictModel):
    name: str
    topics: list[str] = Field(default_factory=list)
    backend: Backend = "auto"

    _name = field_validator("name")(lambda cls, v: _validate_dns_name(v))


class DynamoSpec(_StrictModel):
    name: str
    hash_key: str = Field(default="id", alias="hashKey")
    backend: Backend = "auto"  # emulator-only; native would error

    _name = field_validator("name")(lambda cls, v: _validate_dns_name(v))


class SecretsManagerSpec(_StrictModel):
    name: str  # SM naming: letters/digits and /_+=.@- (slashes are idiomatic)
    value: Optional[str] = None  # literal fixture value (test data, not prod creds)
    generate: bool = False  # random value per environment (kept across re-ups)
    backend: Backend = "auto"  # emulator-only (in-process)

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not re.match(r"^[A-Za-z0-9/_+=.@-]{1,256}$", v):
            raise ValueError(f"{v!r} is not a valid Secrets Manager secret name")
        return v

    @model_validator(mode="after")
    def _value_xor_generate(self) -> "SecretsManagerSpec":
        if bool(self.value) == self.generate:
            raise ValueError(
                f"secret {self.name!r}: set exactly one of value: or generate: true"
            )
        return self


class AlbSpec(_StrictModel):
    name: str
    target_app: str = Field(alias="targetApp")  # app (from apps:) to route to
    backend: Backend = "auto"  # emulator-only (patched ministack data plane)

    _name = field_validator("name")(lambda cls, v: _validate_dns_name(v))


class ServicesSpec(_StrictModel):
    s3: S3Spec = Field(default_factory=S3Spec)
    rds: list[RdsSpec] = Field(default_factory=list)
    elasticache: list[ElastiCacheSpec] = Field(default_factory=list)
    msk: list[MskSpec] = Field(default_factory=list)
    dynamodb: list[DynamoSpec] = Field(default_factory=list)
    alb: list[AlbSpec] = Field(default_factory=list)
    secretsmanager: list[SecretsManagerSpec] = Field(default_factory=list)


class ResourcesSpec(_StrictModel):
    cpu: str = "10m"  # request
    memory: str = "32Mi"  # request
    cpu_limit: Optional[str] = Field(default=None, alias="cpuLimit")
    memory_limit: str = Field(default="256Mi", alias="memoryLimit")


class ReadinessSpec(_StrictModel):
    tcp: bool = False  # tcpSocket probe instead of httpGet (non-HTTP apps)
    path: str = "/"
    port: Optional[int] = None  # default: the app's port
    initial_delay_seconds: int = Field(default=2, alias="initialDelaySeconds", ge=0)
    period_seconds: int = Field(default=5, alias="periodSeconds", ge=1)
    timeout_seconds: Optional[int] = Field(default=None, alias="timeoutSeconds", ge=1)


class SecretRefSpec(_StrictModel):
    name: str
    prefix: Optional[str] = None  # env-var prefix, e.g. CACHE_A_ -> CACHE_A_REDIS_HOST

    @field_validator("prefix")
    @classmethod
    def _check_prefix(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not re.match(r"^[A-Z][A-Z0-9_]*_$", v):
            raise ValueError(
                f"prefix {v!r} must be UPPER_SNAKE ending with '_' (e.g. CACHE_A_)"
            )
        return v


SecretRef = Union[str, SecretRefSpec]


class AppIngressSpec(_StrictModel):
    # default: <app>.<namespace>.localtest.me; "*" matches any host (useful
    # for hitting an ALB's raw DNS name before real DNS exists)
    host: Optional[str] = None
    class_name: Optional[str] = Field(default=None, alias="className")
    annotations: dict[str, str] = Field(default_factory=dict)  # e.g. alb.ingress.kubernetes.io/*
    # escape hatch, same merge semantics as the app-level patch: deep-merged
    # onto the generated Ingress (mayfly re-asserts the resource name)
    patch: dict = Field(default_factory=dict)


class AppSpec(_StrictModel):
    enabled: bool = True
    image: str
    port: int = 80
    # in-namespace Service port (the "<name>:8080" convention); override for
    # protocol-native ports, e.g. 5432 for a pgbouncer/postgres-shaped app
    service_port: int = Field(default=8080, alias="servicePort")
    command: list[str] = Field(default_factory=list)  # override image entrypoint
    args: list[str] = Field(default_factory=list)
    replicas: int = Field(default=1, ge=1)
    env: dict[str, str] = Field(default_factory=dict)
    # env-from these mayfly secrets; entries are a name or {name, prefix} —
    # a prefix namespaces colliding keys (e.g. several elasticache secrets)
    secrets: list[SecretRef] = Field(default_factory=list)
    resources: ResourcesSpec = Field(default_factory=ResourcesSpec)
    readiness: Optional[ReadinessSpec] = None  # httpGet probe; omit for none
    image_pull_secret: Optional[str] = Field(default=None, alias="imagePullSecret")
    ingress: Optional[AppIngressSpec] = None  # opt-in; omit for cluster-internal only
    # escape hatch for anything without a dedicated field: arbitrary YAML
    # deep-merged onto the generated Deployment (maps merge; lists of named
    # objects merge by name; other lists replace). mayfly re-asserts its
    # invariants (selector, app label, enableServiceLinks) after the merge.
    patch: dict = Field(default_factory=dict)


class EnvSpec(_StrictModel):
    api_version: str = Field(default=API_VERSION, alias="apiVersion")
    seed: str
    namespace_prefix: Optional[str] = Field(default=None, alias="namespacePrefix")
    ttl: str = DEFAULT_TTL
    emulator: EmulatorSpec = Field(default_factory=EmulatorSpec)
    services: ServicesSpec = Field(default_factory=ServicesSpec)
    apps: dict[str, AppSpec] = Field(default_factory=dict)

    @field_validator("api_version")
    @classmethod
    def _check_api_version(cls, v: str) -> str:
        if v != API_VERSION:
            raise ValueError(f"unsupported apiVersion {v!r}; expected {API_VERSION}")
        return v

    @field_validator("seed")
    @classmethod
    def _check_seed(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("seed must be non-empty")
        return v

    @field_validator("namespace_prefix")
    @classmethod
    def _check_prefix(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _validate_dns_name(v)
            if len(v) > 30:
                raise ValueError("namespacePrefix too long (max 30 chars)")
        return v

    @field_validator("ttl")
    @classmethod
    def _check_ttl(cls, v: str) -> str:
        parse_ttl(v)
        return v

    @field_validator("apps")
    @classmethod
    def _check_app_names(cls, v: dict[str, AppSpec]) -> dict[str, AppSpec]:
        for name in v:
            _validate_dns_name(name)
        return v

    @model_validator(mode="after")
    def _check_alb_targets(self) -> "EnvSpec":
        for alb in self.services.alb:
            if alb.target_app not in self.apps:
                raise ValueError(
                    f"alb {alb.name!r}: targetApp {alb.target_app!r} is not in apps"
                )
        return self

    @property
    def ttl_delta(self) -> timedelta:
        return parse_ttl(self.ttl)

    def spec_hash(self) -> str:
        canonical = yaml.safe_dump(self.model_dump(by_alias=True), sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def load_spec(path: Union[str, Path]) -> EnvSpec:
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: spec must be a YAML mapping")
    return EnvSpec.model_validate(raw)
