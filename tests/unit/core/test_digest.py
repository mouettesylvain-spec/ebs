from __future__ import annotations

import hashlib
import io
import itertools
import sys
from pathlib import Path
from typing import get_args

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ebs.core import digest as digest_mod
from ebs.core.digest import Algo, Digest, StreamingHasher, hash_bytes, hash_file
from ebs.core.errors import DigestError

ALGOS: tuple[Algo, ...] = get_args(Algo)
HEX_A = "a" * 64
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
EMPTY_BLAKE3 = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"
ABC_BLAKE3 = "6437b3ac38465133ffb63b75273a8db548c558465d79db03fd359c6cd5bd9d85"

hex64 = st.binary(min_size=32, max_size=32).map(bytes.hex)


def _blake3_available() -> bool:
    try:
        import blake3  # noqa: F401
    except ImportError:
        return False
    return True


AVAILABLE_ALGOS: tuple[Algo, ...] = ALGOS if _blake3_available() else ("sha256",)


# R1
@given(algo=st.sampled_from(ALGOS), hex_=hex64)
def test_roundtrip(algo: Algo, hex_: str) -> None:
    d = Digest(algo, hex_)
    assert str(d) == f"{algo}:{hex_}"
    assert Digest.parse(str(d)) == d


# R1
@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("sha256:" + "A" * 64, id="uppercase"),
        pytest.param("sha256:" + "Ab" * 32, id="mixed-case"),
        pytest.param("sha256:" + "a" * 63, id="short"),
        pytest.param("sha256:" + "a" * 65, id="long"),
        pytest.param("md5:" + HEX_A, id="unknown-algo"),
        pytest.param("SHA256:" + HEX_A, id="uppercase-algo"),
        pytest.param(HEX_A, id="missing-prefix"),
        pytest.param(":" + HEX_A, id="empty-algo"),
        pytest.param("sha256:", id="empty-hex"),
        pytest.param("", id="empty"),
        pytest.param("sha256:" + "g" * 64, id="non-hex"),
        pytest.param("sha256:" + HEX_A + "\n", id="trailing-newline"),
        pytest.param(" sha256:" + HEX_A, id="leading-space"),
        pytest.param("sha256::" + HEX_A, id="double-colon"),
        pytest.param("sha256:" + "\u0661" * 64, id="non-ascii-digits"),
    ],
)
def test_parse_rejects(bad: str) -> None:
    with pytest.raises(DigestError) as excinfo:
        Digest.parse(bad)
    assert repr(bad) in str(excinfo.value)


# R1
def test_parse_error_quotes_values_up_to_the_limit_in_full() -> None:
    bad = "sha256:" + "z" * 73  # exactly 80 characters
    with pytest.raises(DigestError) as excinfo:
        Digest.parse(bad)
    assert repr(bad) in str(excinfo.value)
    assert "chars)" not in str(excinfo.value)


# R1
def test_parse_error_truncates_long_values() -> None:
    bad = "sha256:" + "z" * 500
    with pytest.raises(DigestError) as excinfo:
        Digest.parse(bad)
    message = str(excinfo.value)
    assert repr(bad[:80]) in message
    assert bad[:81] not in message


# R1
@pytest.mark.parametrize(
    ("algo", "hex_"),
    [
        ("sha256", "A" * 64),
        ("sha256", "a" * 10),
        ("sha256", "a" * 65),
        ("sha256", HEX_A + "\n"),
        ("sha256", b"a" * 64),
        ("sha256", None),
        ("md5", HEX_A),
    ],
)
def test_constructor_validates(algo: str, hex_: object) -> None:
    with pytest.raises(DigestError):
        Digest(algo, hex_)  # type: ignore[arg-type]


# R1
def test_digests_are_ordered_and_hashable() -> None:
    a, b = Digest("sha256", "0" * 64), Digest("sha256", "1" * 64)
    assert a < b
    assert len({a, Digest.parse(str(a))}) == 1


# R8
def test_shard() -> None:
    hex_ = "abcd" + "0" * 60
    assert Digest("sha256", hex_).shard() == ("ab", "cd", hex_)


# R2, R7
@pytest.mark.parametrize(
    ("data", "expected"),
    [(b"", EMPTY_SHA256), (b"abc", ABC_SHA256)],
)
def test_known_sha256_vectors(data: bytes, expected: str) -> None:
    assert hash_bytes(data) == Digest("sha256", expected)
    hasher = StreamingHasher()
    hasher.update(data)
    assert hasher.finish() == (Digest("sha256", expected), len(data))


class _ReadSpy(io.RawIOBase):
    """File wrapper that records the size argument of every read call."""

    def __init__(self, inner: io.FileIO) -> None:
        self.inner = inner
        self.sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.sizes.append(size)
        return self.inner.read(size)

    def readinto(self, buffer: object) -> int:
        raise AssertionError("hash_file is expected to use read(chunk)")

    def readall(self) -> bytes:
        raise AssertionError("hash_file must never read the whole file")

    def close(self) -> None:
        self.inner.close()
        super().close()


# R2
def test_hash_file_streams(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "data.bin"
    data = bytes(range(256)) * 40  # 10240 bytes
    path.write_bytes(data)
    spies: list[_ReadSpy] = []

    def spy_open(file: Path, mode: str = "r", *args: object, **kwargs: object) -> _ReadSpy:
        assert "b" in mode
        assert kwargs.get("buffering") == 0  # unbuffered: each read is exactly one chunk
        spy = _ReadSpy(io.FileIO(file, "r"))
        spies.append(spy)
        return spy

    monkeypatch.setattr(digest_mod, "open", spy_open, raising=False)
    result = hash_file(path, chunk=4096)
    assert result == (hash_bytes(data), len(data))
    (spy,) = spies
    assert spy.closed
    assert spy.sizes
    assert all(size == 4096 for size in spy.sizes), spy.sizes
    assert len(spy.sizes) == 4  # 4096 + 4096 + 2048 + EOF


# R2
@pytest.mark.parametrize("algo", AVAILABLE_ALGOS)
@pytest.mark.parametrize("size", [0, 1, 4095, 4096, 4097, 3 * 4096])
def test_hash_file_matches_hash_bytes(tmp_path: Path, algo: Algo, size: int) -> None:
    path = tmp_path / "f"
    path.write_bytes(bytes(i % 251 for i in range(size)))
    assert hash_file(path, algo, chunk=4096) == (hash_bytes(path.read_bytes(), algo), size)


# R2
def test_empty(tmp_path: Path) -> None:
    path = tmp_path / "empty"
    path.touch()
    assert hash_file(path) == (Digest("sha256", EMPTY_SHA256), 0)


# R2
@pytest.mark.parametrize("chunk", [0, -1])
def test_hash_file_rejects_non_positive_chunk(tmp_path: Path, chunk: int) -> None:
    path = tmp_path / "f"
    path.touch()
    with pytest.raises(DigestError, match="chunk"):
        hash_file(path, chunk=chunk)


# R2
def test_hash_file_accepts_one_byte_chunks(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"abc")
    assert hash_file(path, chunk=1) == (Digest("sha256", ABC_SHA256), 3)


# R2
@pytest.mark.slow
def test_sparse_3gib(tmp_path: Path) -> None:
    size = 3 << 30
    path = tmp_path / "sparse"
    with path.open("wb") as f:
        f.truncate(size)
    expected = hashlib.sha256()
    zeros = bytes(1 << 24)
    for _ in range(size // len(zeros)):
        expected.update(zeros)
    assert hash_file(path) == (Digest("sha256", expected.hexdigest()), size)


# R3
def test_blake3_vectors() -> None:
    pytest.importorskip("blake3", reason="missing dependency: install the `blake3` extra")
    assert hash_bytes(b"", "blake3") == Digest("blake3", EMPTY_BLAKE3)
    assert hash_bytes(b"abc", "blake3") == Digest("blake3", ABC_BLAKE3)


# R3
def test_blake3_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate the optional package being absent, whether or not it is installed here.
    monkeypatch.setitem(sys.modules, "blake3", None)
    with pytest.raises(DigestError, match="blake3 not installed"):
        hash_bytes(b"abc", "blake3")
    with pytest.raises(DigestError, match="blake3 not installed"):
        StreamingHasher("blake3")
    # Parsing a blake3 digest never needs the package.
    assert Digest.parse("blake3:" + HEX_A).algo == "blake3"
    assert hash_bytes(b"abc") == Digest("sha256", ABC_SHA256)


# R3
def test_unknown_algorithm_is_rejected() -> None:
    with pytest.raises(DigestError, match="md5"):
        hash_bytes(b"", "md5")  # type: ignore[arg-type]


# R7
@given(
    algo=st.sampled_from(AVAILABLE_ALGOS),
    data=st.binary(max_size=4096),
    cuts=st.lists(st.integers(min_value=0, max_value=4096), max_size=8),
)
def test_streaming_split_invariance(algo: Algo, data: bytes, cuts: list[int]) -> None:
    bounds = sorted({0, len(data), *(c for c in cuts if c <= len(data))})
    hasher = StreamingHasher(algo)
    for start, end in itertools.pairwise(bounds):
        hasher.update(data[start:end])
    assert hasher.finish() == (hash_bytes(data, algo), len(data))
