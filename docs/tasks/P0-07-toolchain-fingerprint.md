# P0-07 — Toolchain fingerprint and environment capture

Status: todo · Phase: 0 · Depends on: P0-02 · Parallel-safe with: P0-03, P0-05, P0-10 · Size: S

## Goal
Compute an immutable toolchain id from an install tree and a captured environment, so tool
versions become a hashed input of every action. Modulefile loading and the registry come in P1-07.

## Read first
- docs/architecture.md § "Hashing, action keys and caching" → "Toolchains"

## Scope (files)
- create `src/ebs/toolchain/fingerprint.py`, `src/ebs/toolchain/env.py`, `src/ebs/toolchain/model.py`
- create `tests/unit/toolchain/test_fingerprint.py`, `test_env.py`

## Out of scope
`module load`, persistence, CLI (P1-07). The P0 planner uses a `StaticToolchainResolver` defined here
that reads toolchains from a YAML file (`.ebs/toolchains.yaml`) for tests and the e2e demo.

## Contract
```python
@dataclass(frozen=True) class Toolchain:
    name: str; module: str; version: str; install_roots: tuple[Path, ...]; env: Mapping[str, str]
    fingerprint: Digest; id: Digest
def fingerprint_roots(roots: Sequence[Path], *, content_hash: Callable[[Path], bool]) -> Digest
def capture_env(argv: Sequence[str], *, base_env: Mapping[str, str]) -> dict[str, str]   # runs `env -0` after argv in a clean shell
def toolchain_id(module: str, version: str, fingerprint: Digest, env: Mapping[str, str]) -> Digest
class ToolchainResolver(Protocol):
    def resolve(self, name: str, module: str) -> Toolchain: ...
class StaticToolchainResolver: ...
```

## Requirements
- R1 Fingerprint covers every file under each root: relative path, size, mtime_ns, executable bit; and
  full content hash for files where `content_hash(path)` is true (default: executable bit set or ELF
  magic or suffix in `.so`, `.sh`, `.tcl`, `.py`). Order independent; symlinks recorded by target.
- R2 Changing any recorded attribute changes the fingerprint; changing atime or ownership does not.
- R3 `capture_env` starts from an env containing only `PATH=/usr/bin:/bin`, `HOME=<tmp>`, `LANG=C.UTF-8`
  plus explicitly passed keys, runs the given argv followed by `env -0` in one `bash --noprofile --norc -c`
  invocation built from a fixed script with argv passed as positional args (no interpolation), and
  returns the resulting variables minus volatile ones (`PWD`, `OLDPWD`, `SHLVL`, `_`, `RANDOM`-style).
- R4 `toolchain_id` = `digest_json({"module","version","fingerprint","env"})`; env values containing the
  temporary HOME path are normalized to `$HOME` so ids are reproducible across users.
- R5 Fingerprinting a 20,000-file synthetic tree runs in < 10 s on CI with content hashing limited to executables.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_fingerprint.py::test_covers_attributes[...]`, `::test_order_independent` | R1 | unit |
| `test_fingerprint.py::test_sensitivity`, `::test_insensitive_to_atime_owner` | R2 | unit |
| `test_env.py::test_clean_start`, `::test_argv_not_interpolated` (argv containing `$(touch x)`), `::test_volatile_removed` | R3 | unit |
| `test_env.py::test_id_user_independent` | R4 | unit |
| `test_fingerprint.py::test_large_tree_perf` (mark `slow`) | R5 | unit |

## Done when
- [ ] `make check` passes
