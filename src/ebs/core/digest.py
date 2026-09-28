"""Content digests: the `Digest` type and streaming hashing (docs/design/interfaces.md § 1).

A digest is written `<algo>:<hex>` with lowercase hex, e.g. `sha256:e3b0…`. SHA-256 comes from
hashlib (OpenSSL, SHA-NI accelerated); BLAKE3 needs the optional `blake3` package.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol, get_args

from ebs.core.errors import DigestError

Algo = Literal["sha256", "blake3"]

ALGOS: Final[frozenset[str]] = frozenset(get_args(Algo))
_HEX_RE: Final = re.compile(r"[0-9a-f]{64}")
_QUOTE_LIMIT: Final = 80


def _quote(value: str) -> str:
    """repr() of a user-supplied value, truncated so error messages stay readable."""
    if len(value) <= _QUOTE_LIMIT:
        return repr(value)
    return repr(value[:_QUOTE_LIMIT]) + f"… ({len(value)} chars)"


@dataclass(frozen=True, slots=True, order=True)
class Digest:
    """An algorithm-prefixed content digest; construct with `parse` or the hash functions."""

    algo: Algo
    hex: str  # lowercase, 64 chars for both algorithms

    def __post_init__(self) -> None:
        if self.algo not in ALGOS:
            raise DigestError(
                f"unsupported digest algorithm {_quote(str(self.algo))}; "
                f"expected one of {sorted(ALGOS)}"
            )
        if not isinstance(self.hex, str) or _HEX_RE.fullmatch(self.hex) is None:
            raise DigestError(
                f"invalid {self.algo} digest value {_quote(str(self.hex))}: "
                "expected 64 lowercase hex characters"
            )

    @classmethod
    def parse(cls, s: str) -> Digest:
        """Parse `<algo>:<hex>`; raises DigestError quoting the bad value."""
        # Without ':' the whole string lands in `algo` and hex_ is empty, so this rejects it.
        algo, _, hex_ = s.partition(":")
        if algo not in ALGOS or _HEX_RE.fullmatch(hex_) is None:
            raise DigestError(
                f"invalid digest {_quote(s)}: expected '<algo>:<64 lowercase hex>' "
                f"with algo in {sorted(ALGOS)}, e.g. 'sha256:{'0' * 64}'"
            )
        return cls(algo, hex_)  # type: ignore[arg-type]  # algo checked against ALGOS

    def __str__(self) -> str:
        return f"{self.algo}:{self.hex}"

    def shard(self) -> tuple[str, str, str]:
        """Path components for CAS fan-out: `("ab", "cd", "<hex>")`."""
        return self.hex[0:2], self.hex[2:4], self.hex


class _Hasher(Protocol):
    def update(self, data: bytes, /) -> object: ...
    def hexdigest(self) -> str: ...


def _new_hasher(algo: Algo) -> _Hasher:
    if algo == "sha256":
        return hashlib.sha256()
    if algo == "blake3":
        try:
            from blake3 import blake3
        except ImportError as exc:
            raise DigestError(
                "blake3 not installed: install the optional extra (`pip install 'ebs[blake3]'`) "
                "or use the sha256 algorithm"
            ) from exc
        return blake3()
    raise DigestError(
        f"unsupported digest algorithm {_quote(str(algo))}; expected one of {sorted(ALGOS)}"
    )


class StreamingHasher:
    """Incremental hasher; `finish()` returns the digest and the number of bytes seen."""

    __slots__ = ("_algo", "_hasher", "_size")

    def __init__(self, algo: Algo = "sha256") -> None:
        self._algo: Algo = algo
        self._hasher = _new_hasher(algo)
        self._size = 0

    def update(self, b: bytes) -> None:
        self._hasher.update(b)
        self._size += len(b)

    def finish(self) -> tuple[Digest, int]:
        return Digest(self._algo, self._hasher.hexdigest()), self._size


def hash_bytes(data: bytes, algo: Algo = "sha256") -> Digest:
    hasher = StreamingHasher(algo)
    hasher.update(data)
    return hasher.finish()[0]


def hash_file(path: Path, algo: Algo = "sha256", *, chunk: int = 1 << 20) -> tuple[Digest, int]:
    """Hash a file in `chunk`-sized reads (never all at once); returns `(digest, size)`."""
    if chunk <= 0:
        raise DigestError(f"hash_file chunk must be a positive number of bytes, got {chunk}")
    hasher = StreamingHasher(algo)
    with open(path, "rb", buffering=0) as f:
        while block := f.read(chunk):
            hasher.update(block)
    return hasher.finish()
