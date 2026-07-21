"""Deterministic environment naming: adjective-adjective-animal.

The seed string hashes to a memorable name. Distinct seeds can collide onto
the same words (~64k combinations); `mayfly up` guards against this by
refusing a namespace whose recorded seed label differs from the spec's.
"""

import hashlib
from typing import Optional

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

def env_name(seed: str) -> str:
    """Derive the deterministic environment name from a seed string."""
    digest = hashlib.sha256(seed.encode()).hexdigest()
    a1 = int(digest[0:2], 16) % len(ADJECTIVES)
    a2 = int(digest[2:4], 16) % len(ADJECTIVES)
    an = int(digest[4:6], 16) % len(ANIMALS)
    return f"{ADJECTIVES[a1]}-{ADJECTIVES[a2]}-{ANIMALS[an]}"


def namespace_for(seed: str, prefix: Optional[str] = None) -> str:
    """Namespace for a seed: `<prefix>-<name>` with a prefix, bare name without."""
    name = env_name(seed)
    return f"{prefix}-{name}" if prefix else name
