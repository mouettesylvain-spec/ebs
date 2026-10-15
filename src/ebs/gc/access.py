"""Storing small objects so that a concurrent GC cannot lose them (P1-09 R1, I15).

A CAS put of bytes that already exist keeps the stored object; if GC claimed that object (a due
tombstone) just before, the bytes may vanish before the put is registered. Registering touches
the blob row, after which no claim succeeds, so checking the CAS once more after registering
closes the window: if the object went meanwhile, it is stored again.
"""

from __future__ import annotations

from ebs.cas.api import CAS
from ebs.core.digest import Digest
from ebs.meta.api import BlobRef, MetadataStore

__all__ = ["put_registered"]


def put_registered(cas: CAS, store: MetadataStore, domain: str, data: bytes) -> Digest:
    """Store `data`, register it, and make sure it is still there afterwards."""
    digest = cas.put_bytes(data)
    store.register_blobs(domain, [BlobRef(digest=digest, size=len(data), kind="file")])
    if not cas.has(digest):  # collected between the put and the registration
        cas.put_bytes(data)
    return digest
