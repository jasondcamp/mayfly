"""Deterministic environment naming: adjective-adjective-animal-hash.

The seed string hashes to a memorable name. A 4-hex-char suffix removes
the collision risk of the word combination alone (~64k combos).
"""

import hashlib

ADJECTIVES = [
    "obvious", "frustrated", "wobbly", "gentle", "rapid", "blonde", "sleepy", "brave",
    "dizzy", "quiet", "mellow", "spicy", "foggy", "shiny", "rustic", "bold",
    "calm", "eager", "fuzzy", "grumpy", "witty", "vivid", "tidy", "sturdy",
    "snappy", "plucky", "nimble", "merry", "lively", "keen", "jolly", "humble",
    "hearty", "gleeful", "frank", "earnest", "daring", "cheery", "breezy", "agile",
]

ANIMALS = [
    "weasel", "frog", "heron", "badger", "otter", "lynx", "crane", "mole",
    "finch", "newt", "marmot", "shrew", "stoat", "bison", "egret", "gecko",
    "krill", "loon", "okapi", "quail", "raven", "seal", "tapir", "vole",
    "wren", "yak", "zebra", "ibis", "jay", "kiwi", "lemur", "mink",
    "narwhal", "ocelot", "puffin", "robin", "skink", "toad", "urchin", "walrus",
]

NAMESPACE_PREFIX = "env-"


def env_name(seed: str) -> str:
    """Derive the deterministic environment name from a seed string."""
    digest = hashlib.sha256(seed.encode()).hexdigest()
    a1 = int(digest[0:2], 16) % len(ADJECTIVES)
    a2 = int(digest[2:4], 16) % len(ADJECTIVES)
    an = int(digest[4:6], 16) % len(ANIMALS)
    suffix = digest[6:10]
    return f"{ADJECTIVES[a1]}-{ADJECTIVES[a2]}-{ANIMALS[an]}-{suffix}"


def namespace_for(seed: str) -> str:
    return f"{NAMESPACE_PREFIX}{env_name(seed)}"
