# Interfaces (cross-package contracts)

These contracts let tasks be implemented in parallel. Signatures are normative; names of private
helpers are not. A change here is a CONTRACT CHANGE (see CLAUDE.md).

## 1. Digests and canonical JSON — `ebs.core.digest`, `ebs.core.canon`

```python
Algo = Literal["sha256", "blake3"]

@dataclass(frozen=True, slots=True, order=True)
class Digest:
    algo: Algo
    hex: str                                   # lowercase, 64 chars for both algos

    @classmethod
    def parse(cls, s: str) -> Digest: ...      # "sha256:<hex>"; raises DigestError
    def __str__(self) -> str: ...              # "sha256:<hex>"
    def shard(self) -> tuple[str, str, str]:   # ("ab", "cd", hex) for CAS paths
        ...

def hash_bytes(data: bytes, algo: Algo = "sha256") -> Digest: ...
def hash_file(path: Path, algo: Algo = "sha256", *, chunk: int = 1 << 20) -> tuple[Digest, int]: ...
class StreamingHasher:                          # used by the runner while copying outputs
    def update(self, b: bytes) -> None: ...
    def finish(self) -> tuple[Digest, int]: ...

def canonical_json(obj: JsonValue) -> bytes: ...   # RFC 8785 (JCS) subset, see below
def digest_json(obj: JsonValue, algo: Algo = "sha256") -> Digest: ...
```

Canonical JSON rules (RFC 8785 subset): UTF-8, no insignificant whitespace, object keys sorted by
UTF-16 code units, strings NFC-normalized **by the caller** (canon rejects non-NFC input with
`CanonError` rather than silently normalizing), integers only (floats rejected: parameters are
strings or ints), `true/false/null`, no NaN. Duplicate keys impossible (dict input).
Integers must lie within ±(2**53 − 1), the range JCS numbers represent exactly, so the output is
byte-identical to any RFC 8785 implementation; larger values are passed as strings. Tuples encode
exactly like lists. Rejections raise `CanonError` naming the JSON path (`$.params.seed`).

```python
# ebs.core.types
JsonValue: TypeAlias = (
    "dict[str, JsonValue] | list[JsonValue] | tuple[JsonValue, ...] | str | int | bool | None"
)
```

## 2. Tree manifests — `ebs.core.tree`

```python
EntryType = Literal["file", "dir", "symlink"]

@dataclass(frozen=True, slots=True)
class TreeEntry:
    name: str               # single path component, NFC, no "/" or NUL, not "." / ".."
    type: EntryType
    digest: Digest | None   # file: content digest; dir: child tree digest; symlink: None
    size: int               # file: bytes; dir: total bytes below; symlink: 0
    executable: bool        # files only; other mode bits are not recorded
    target: str | None      # symlink only; must be relative and stay inside the tree

@dataclass(frozen=True, slots=True)
class TreeManifest:
    entries: tuple[TreeEntry, ...]           # sorted by name (bytewise UTF-8)
    def to_json(self) -> JsonValue: ...      # {"v":1,"entries":[…]}
    def digest(self, algo: Algo = "sha256") -> Digest: ...   # digest_json(to_json())

def build_tree(root: Path, hasher: Callable[[Path], tuple[Digest, int]]) -> tuple[Digest, dict[Digest, TreeManifest]]: ...
```

Nested directories are separate manifests (Merkle). The field set maps onto REAPI `Directory`
(files/directories/symlinks, `is_executable`) so a REAPI backend can be added later; the on-disk
encoding is our canonical JSON, not protobuf. A REAPI backend must re-derive dir sizes (ours is the
total of file bytes below; REAPI's is the serialized child size) and manifest digests. Entry JSON
holds only the fields of its type: file `name,type,digest,size,executable`; dir
`name,type,digest,size`; symlink `name,type,target`. Manifests are hashed with sha256.
`build_tree` raises `TreeError` for special files, non-UTF-8/non-NFC names, symlinks whose target
is absolute or leaves the root (lexically or through other links), and unreadable paths.
`from_json` output is not checked against the filesystem, so consumers that materialize a tree
(P0-09) must not follow symlinks while writing.

## 3. Flow model — `ebs.flow`

```python
def load_flow(path: Path, *, lock: Path | None = None) -> Flow: ...     # raises FlowError with file:line:col
def flow_json_schema() -> JsonValue: ...                                # generated from the Pydantic models
```

`Flow`, `StepDef`, `OutputDef`, `ToolchainRef`, `MatrixDef`, `ImportDef`, `Resources` are Pydantic v2
models (`model_config = ConfigDict(extra="forbid", frozen=True)`). Field names follow the YAML in
architecture.md "Flow description format". Additional P0 fields: `env: dict[str,str]` per step,
`script: str | None` (shell), `command: list[str] | None`, `workdir`, `target` (make), `debug`,
`config_files: dict[str, str]` (tool config files written into scratch, e.g. `modelsim.ini`).

Model details (P0-04):
- YAML lists become tuples (`command`, `debug.collect`, `matrix.cross`/`zip_`/`id`).
- Python names differ where YAML keys are keywords or builtins: `ImportDef.from_` (`from`) and `MatrixDef.zip_` (`zip`). `MatrixDef.tables` returns the table paths in order.
- Steps also have `licenses: dict[str, int]`, and `ToolchainRef` has `licenses` and `resources`.
- `debug` is a `DebugDef(collect, max_size, on_success)`.
- `OutputDef(file | dir, deterministic=True, optional=False)` accepts the short form `name: path`.
- `Resources.cpus`, `mem` and `time` are an `int` (count, bytes, seconds) or, when the value holds a `${…}` reference, the verbatim string.
- The planner parses a resolved resource string with `ebs.flow.model.parse_memory` / `parse_duration`.
- `model_dump()` emits the YAML spelling again (`8G`, `30m`, `from`, `zip`), so the result re-validates.
- `ebs.flow.model.check_ref_syntax(s) -> str | None` validates the grammar below without resolving anything.

### Interpolation grammar (`ebs.flow.interp`)

`${…}` references, resolved at plan time, never by a shell:

```
ref      := "${" path "}"
path     := "row." IDENT
          | "params." IDENT
          | "imports." IDENT ("/" globtail)?
          | "steps." IDENT ".outputs." IDENT ("[*]" | "[" rowsel "]")?
          | "env." IDENT                       # only declared env of the same step
rowsel   := IDENT "=" VALUE ("," IDENT "=" VALUE)*
```

In `steps.` refs, step and output names use the name rule `[a-z][a-z0-9_]{0,62}`. A `VALUE` has no
`,`, `[`, `]`, `{`, `}`, `$`, `=` or whitespace. A `globtail` is non-empty. Strings are scanned left
to right: `$${` escapes a literal `${`, and every other `${` must be closed by the next `}`. Unknown refs are errors with a "did you mean" suggestion. Output
refs create DAG edges. `[*]` means "all matrix instances of that step" (fan-in).

## 4. Plan — `ebs.plan`

```python
KEY_SCHEMA_VERSION: Final = 1

@dataclass(frozen=True, slots=True)
class InputRef:
    logical_path: str                  # normalized, relative, POSIX; where the runner stages it
    kind: Literal["file", "tree"]
    source: SourceInput | ActionOutputInput | ImportInput
    id: Digest | None                  # None only when produced by a not-yet-run deterministic output

@dataclass(frozen=True, slots=True)
class ActionSpec:
    action_id: str                      # "compile[lib=core]"; stable across runs
    step: str
    rule: RuleRef                       # kind + rule implementation version
    argv: tuple[str, ...]               # fully expanded; paths are logical (relative to scratch)
    params: Mapping[str, str | int]
    env: Mapping[str, str]              # declared env only
    toolchain: ToolchainRef | None      # name + toolchain id
    inputs: tuple[InputRef, ...]        # sorted by logical_path
    outputs: tuple[OutputSpec, ...]     # name, logical path, type, deterministic, optional
    config_files: Mapping[str, str]     # logical path -> content (hashed via inputs)
    resources: Resources                # NOT in key
    licenses: Mapping[str, int]         # NOT in key
    debug: DebugSpec                    # NOT in key
    domain: str
    key: Digest | None                  # None while any input id is None

def action_key_document(spec: ActionSpec) -> JsonValue: ...   # exactly the fields below
def compute_key(spec: ActionSpec) -> Digest: ...              # raises if an input id is None
def nondeterministic_output_id(producer_key: Digest, output_name: str) -> Digest: ...
```

Key document (the only thing hashed):

```json
{"schema": 1, "rule": {"kind": "...", "version": "..."}, "argv": [...], "params": {...},
 "env": {...}, "toolchain": "sha256:…|null", "config_files": {"path": "sha256:…"},
 "inputs": {"<logical_path>": "sha256:…"}, "outputs": {"<name>": {"path": "...", "type": "..."}}}
```

- `nondeterministic_output_id(k, name) = digest_json({"nd": 1, "producer": str(k), "output": name})`.
- Deterministic outputs pass their **content** digest downstream (early cutoff). Their consumers'
  keys are therefore unknown until the producer ran or hit the cache; the planner leaves
  `key=None` and the driver finalizes keys as results arrive (`Planner.refine(plan, results)`).
- `domain` is not in the key: the action cache is keyed by `(domain, key)`.

Plan file (`plan.json`, canonical JSON, stored in CAS; its digest is the build's plan digest):

```json
{"v": 1, "ebs_version": "…", "key_schema": 1, "flow": {"path": "...", "git": {"repo": "...", "commit": "...", "dirty": false}},
 "lock": "sha256:…|null", "domain": "cpu-nda", "project": "rv32x-cpu",
 "toolchains": {"questa": {"module": "questa/2025.2", "id": "sha256:…"}},
 "actions": [ActionSpec…], "edges": [["from_action_id", "output", "to_action_id"]]}
```

```python
class Planner:
    def __init__(self, cas: CAS, sources: SourceSnapshotter, toolchains: ToolchainResolver, rules: RuleRegistry) -> None: ...
    def plan(self, flow: Flow, *, targets: Sequence[str] = (), rehash: bool = False) -> Plan: ...
    def refine(self, plan: Plan, produced: Mapping[tuple[str, str], Digest]) -> Plan: ...  # (action_id, output) -> id
def diff_plans(old: Plan, new: Plan) -> list[ActionDiff]: ...   # per action: which key fields changed
```

## 5. CAS — `ebs.cas.api`

```python
class CAS(Protocol):
    domain: str
    def has(self, d: Digest) -> bool: ...
    def put_bytes(self, data: bytes) -> Digest: ...
    def put_file(self, path: Path, *, expected: Digest | None = None) -> Digest: ...
    def put_tree(self, root: Path) -> Digest: ...          # uploads blobs first, manifests last
    def get_tree(self, d: Digest) -> TreeManifest: ...
    def open(self, d: Digest) -> BinaryIO: ...
    def local_path(self, d: Digest) -> Path | None: ...    # fs backend only; for read-only bind/symlink
    def materialize(self, d: Digest, kind: Literal["file", "tree"], dest: Path,
                    mode: Literal["copy", "hardlink", "symlink", "auto"] = "auto",
                    copy_threshold: int = 256 << 20, *, writable: bool = False) -> None: ...
    def verify(self, d: Digest) -> bool: ...
    def delete(self, d: Digest) -> None: ...               # GC only
    def iter_digests(self) -> Iterator[tuple[Digest, int]]: ...   # GC only
```

fs layout: `<root>/<domain>/blobs/<algo>/<ab>/<cd>/<hex>`, `…/trees/<algo>/<ab>/<cd>/<hex>.json`,
`…/tmp/`. Blobs mode 0444, dirs 2750 group `<domain>` (group set by the deployment, not by code).

`has` is true for a blob or a tree manifest; `open`/`local_path` address blobs, `get_tree`
manifests; `verify`, `delete` and `iter_digests` cover both. `materialize` requires that `dest`
does not exist. `auto`: a file ≤ `copy_threshold` is copied, larger ones symlinked read-only from
the CAS; tree files are hard links when on the same filesystem, else chosen by the same threshold.
Files are read-only, directories writable; `writable=True` copies every file (0644/0755).
Executable files are always copied (0555), because a link would share the blob's inode and mode.
Tree symlinks are created last and must resolve inside `dest`, else `CasError`. The fs backend
also has `blob_path`, `tree_path` and `cleanup_tmp(max_age=24h) -> int` (GC).

## 6. Metadata — `ebs.meta.api`

```python
class MetadataStore(Protocol):
    # action cache
    def cache_get(self, domain: str, key: Digest) -> ResultManifest | None: ...     # also touches last_access
    def cache_put(self, domain: str, key: Digest, result: ResultManifest) -> bool: ...  # False if already present
    # builds
    def create_build(self, b: BuildCreate) -> BuildId: ...
    def add_actions(self, build: BuildId, actions: Sequence[ActionRow]) -> None: ...
    def set_action_state(self, build: BuildId, action_id: str, state: ActionState, **fields: object) -> None: ...
    def record_result(self, build: BuildId, action_id: str, result: ResultManifest) -> None: ...  # also writes provenance edges
    def finish_build(self, build: BuildId, status: BuildStatus) -> None: ...
    def get_build(self, build: BuildId) -> BuildView: ...
    def list_actions(self, build: BuildId, *, state: ActionState | None = None) -> list[ActionRow]: ...
    # access, leases, GC (P1)
    def touch(self, domain: str, digests: Iterable[Digest]) -> None: ...
    def acquire_lease(self, domain: str, build: BuildId, digests: Iterable[Digest], ttl_s: int) -> LeaseId: ...
    def renew_lease(self, lease: LeaseId, ttl_s: int) -> None: ...
    # events
    def emit(self, event: Event) -> None: ...
```

Implementations: `PgMetadataStore` (SQLAlchemy), `InMemoryMetadataStore` (tests), `HttpMetadataStore`
(client of the FastAPI service, P1). All three must pass `tests/contract/test_metadata_store.py`.

```python
class ResultManifest(BaseModel):          # frozen
    v: Literal[1] = 1
    action_key: Digest
    status: Literal["passed", "failed"]   # failed = tool ran and reported failure (cacheable)
    exit_code: int
    outputs: dict[str, OutputResult]      # name -> {digest(content), id(passed downstream), type, size}
    log: Digest | None                    # full combined log blob
    summary: dict[str, str | int]         # rule-specific (e.g. sim: uvm_errors, first_error)
    resources: ResourceUsage              # max_rss_kb, cpu_s, wall_s
    runner: RunnerInfo                    # version, host, slurm_job_id (not part of cache identity)
```

Infrastructure failures never produce a ResultManifest; they produce an `InfraFailure` event.

## 7. Rules — `ebs.rules.api`

```python
class RulePlugin(Protocol):
    kind: str                     # "shell", "make", "questa.sim"
    version: str                  # bump when generated argv/outputs change -> keys change
    def validate(self, step: StepDef) -> None: ...                 # extra per-kind schema checks
    def expand(self, step: StepDef, ctx: ExpandContext) -> ActionTemplate: ...   # argv, config files, implicit outputs
    def classify(self, exit_code: int, log_tail: str, outputs: Mapping[str, Path]) -> Classification: ...
    def summarize(self, outputs: Mapping[str, Path], log: Path) -> dict[str, str | int]: ...
```

`Classification` = `PASSED | FAILED | INFRA(reason)`; rules decide e.g. that a Questa license
checkout error is INFRA, a UVM_ERROR is FAILED. Plugins register via entry point group `ebs.rules`.

## 8. Executors — `ebs.exec.api`

```python
class Executor(Protocol):
    name: str
    def submit(self, batch: SubmitBatch) -> list[JobHandle]: ...   # batch = same step, compatible resources
    def poll(self, handles: Sequence[JobHandle]) -> dict[JobHandle, JobStatus]: ...   # ONE backend call per poll
    def cancel(self, handles: Sequence[JobHandle]) -> None: ...

class JobStatus(BaseModel):
    state: Literal["pending", "running", "done", "infra_failed", "cancelled"]
    pending_reason: Literal["licenses", "resources", "priority", "other"] | None
    infra_reason: Literal["oom", "timeout", "node_fail", "preempted", "license", "runner_crash", "other"] | None
    exit_code: int | None
```

`done` means the runner finished and posted a ResultManifest (passed or failed); the driver reads
the result from MetadataStore, not from the executor.

## 9. Runner — `ebs-runner`

```
ebs-runner --plan sha256:<plan digest> --action <action_id> [--build <id>] [--domain <d>]
ebs-runner --plan sha256:<plan digest> --array-map sha256:<map digest>    # uses $SLURM_ARRAY_TASK_ID
```

Exit codes: `0` result posted (passed or failed), `64` bad usage, `70` internal error,
`75` infra failure (temporary; driver retries), `76` input verification failed (digest mismatch).
Lifecycle and guarantees: architecture.md "Runner lifecycle on a node".

## 10. Driver events — `ebs.driver.events`

`Event` = `{v, ts, build, type, action_id?, data}` with types: `build_started`, `plan_ready`,
`cache_hit`, `submitted`, `pending`, `running`, `finished`, `infra_failed`, `retrying`,
`build_finished`, `stat_audit_mismatch`. Emitted to MetadataStore and to a local JSONL file
(`.ebs/builds/<id>/events.jsonl`) used by `ebs status` / `ebs logs` when offline.

## 11. Errors and CLI exit codes — `ebs.core.errors`

`EbsError` → `ConfigError`, `FlowError` (has `file`, `line`, `col`), `DigestError`, `CanonError`,
`TreeError`, `PlanError`, `CasError`, `MetadataError`, `ExecutorError`, `RuleError`, `SandboxError`.
CLI exit codes: `0` ok, `1` build finished with failed actions, `2` usage/flow error,
`3` infrastructure error (retries exhausted), `4` internal bug (with "please report" hint).
