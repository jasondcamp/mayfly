from mayfly.naming import env_name, namespace_for


def test_deterministic():
    assert env_name("poc-1") == env_name("poc-1")


def test_distinct_seeds_distinct_names():
    assert env_name("poc-1") != env_name("poc-2")


def test_three_words_no_suffix():
    assert len(env_name("poc-1").split("-")) == 3


def test_namespace_bare_without_prefix():
    assert namespace_for("poc-1") == env_name("poc-1")


def test_namespace_with_prefix():
    assert namespace_for("poc-1", "env") == f"env-{env_name('poc-1')}"
    assert namespace_for("poc-1", "team-a") == f"team-a-{env_name('poc-1')}"


def test_poc_compatibility_words():
    # Same wordlists and indexing as the bash POC: poc-1 -> vivid-hearty-okapi
    assert env_name("poc-1") == "vivid-hearty-okapi"
