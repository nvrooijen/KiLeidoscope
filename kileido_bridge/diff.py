"""Fingerprints of raw KiCad items, taken before any conversion.

Hashing the serialized protos costs ~10 ms for a whole board, versus ~120 ms for
hashing converted records as JSON. Per-item digests also key
the reader's conversion cache, so only new or changed items are converted.
"""

import hashlib
from collections.abc import Iterable

Key = tuple[str, str]  # (canonical layer or "", kind)


def item_bytes(item: object) -> bytes:
    """Serialized proto for real kipy items; repr() for synthetic test doubles."""
    proto = getattr(item, "proto", None)
    serialize = getattr(proto, "SerializeToString", None)
    if serialize is not None:
        return serialize(deterministic=True)
    return repr(item).encode("utf-8")


def item_digest(item: object) -> bytes:
    return hashlib.blake2b(item_bytes(item), digest_size=16).digest()


def combine(digests: Iterable[bytes]) -> bytes:
    """Order-independent digest of a group of item digests."""
    total = hashlib.blake2b(digest_size=16)
    for digest in sorted(digests):
        total.update(digest)
    return total.digest()


def fingerprint(items: Iterable[object]) -> bytes:
    return combine(item_digest(item) for item in items)
