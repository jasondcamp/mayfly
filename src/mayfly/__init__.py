"""mayfly — short lived ephemeral environment infrastructure."""

__version__ = "0.1.7"

MANAGED_LABEL = "mayfly.dev/managed"
SERVICE_LABEL = "mayfly.dev/service"  # marks provisioned service secrets
SEED_LABEL = "mayfly.dev/seed"
SPEC_HASH_LABEL = "mayfly.dev/spec-hash"
EXPIRES_AT_ANNOTATION = "mayfly.dev/expires-at"
CREATED_AT_ANNOTATION = "mayfly.dev/created-at"
FIELD_MANAGER = "mayfly"
