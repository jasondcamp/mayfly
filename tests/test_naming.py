from mayfly.naming import env_name, namespace_for


def test_deterministic():
    assert env_name("poc-1") == env_name("poc-1")


def test_distinct_seeds_distinct_names():
    assert env_name("poc-1") != env_name("poc-2")


def test_hash_suffix_present():
    name = env_name("poc-1")
    parts = name.split("-")
    assert len(parts) == 4
    assert len(parts[3]) == 4
    int(parts[3], 16)  # suffix is hex


def test_namespace_prefix():
    assert namespace_for("x").startswith("env-")


def test_poc_compatibility_words():
    # Same wordlists and indexing as the bash POC: poc-1 -> vivid-hearty-okapi
    assert env_name("poc-1").startswith("vivid-hearty-okapi-")
