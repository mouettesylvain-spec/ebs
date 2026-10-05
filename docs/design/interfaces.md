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

### Tables, matrix expansion, rendering (P0-05)

```python
# ebs.flow.tables
@dataclass(frozen=True) class Table: columns: tuple[str, ...]; rows: tuple[Mapping[str, str], ...]; source: Path; digest: Digest; lines: tuple[int, ...] = ()
def load_table(path: Path) -> Table        # .csv (RFC 4180, UTF-8, header row) or .yaml/.yml (list of flat maps)

# ebs.flow.interp
@dataclass(frozen=True) class SourceLocation: step: str | None = None; field: str | None = None; file: str | None = None
@dataclass(frozen=True) class Ref: kind: Literal["row","params","env","imports","steps"]; name: str; glob: str | None; output: str | None; selector: "*" | tuple[tuple[str, str], ...] | None
@dataclass(frozen=True) class Template: parts: tuple[Literal | Ref, ...]      # str(t) re-serializes; parse(str(t)) == t
class Resolver(Protocol):
    def resolve(self, ref: Ref, *, where: SourceLocation) -> str | list[str]: ...
def parse_template(s: str, *, where: SourceLocation = ...) -> Template
def render(t: Template, resolver: Resolver, *, where: SourceLocation = ...) -> str | list[str]
class Scope(Resolver)      # one instance: row/params/env locally, imports/steps via an outer Resolver

# ebs.flow.matrix
@dataclass(frozen=True) class StepInstance: name: str; step: StepDef; row: Mapping[str, str]; instance_id: str
    params: Mapping[str, str | int | bool | tuple[str, ...]]; env: Mapping[str, str]; resources: Resources; row_origin: str | None
    def render(self, text: str, *, field: str, resolver: Resolver | None = None) -> str | list[str]
def expand_matrix(step: StepDef, tables: Mapping[str, Table], *, name: str, resolver: Resolver | None = None) -> list[StepInstance]
def load_matrix_tables(matrix: MatrixDef, base: Path) -> dict[str, Table]   # keyed by the path as written
def make_instance_id(name: str, row: Mapping[str, str], keys: Sequence[str]) -> str
```

- Table values are the source text (no type guessing); `Table.digest` is the digest of the file bytes.
- `instance_id` is `name[k=v,…]` over the key columns (`matrix.id`, default all) sorted, with keys and
  values percent-encoded (`urllib.parse.quote(safe="")`), so it contains no `/` and is injective. A step
  without a matrix has one instance whose id is the step name. Expansion that yields no rows is an error.
- `StepInstance.resources` holds parsed values (cpus count, bytes, seconds) once `${…}` is resolved.
- A resolved param is its YAML literal (str/int/bool) or, for a sole list-valued ref, a tuple; inside text,
  ints render as decimal and bools as `true`/`false`.

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
    def put_manifest(self, manifest: TreeManifest) -> Digest: ...  # children must be stored (I9)
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
    def record_result(self, build: BuildId, action_id: str, result: ResultManifest) -> None: ...  # also writes provenance edges; never changes state
    def finish_build(self, build: BuildId, status: BuildStatus) -> None: ...
    def get_build(self, build: BuildId) -> BuildView: ...
    def list_actions(self, build: BuildId, *, state: ActionState | None = None) -> list[ActionRow]: ...
    # access (P0), leases and GC (P1-09)
    def touch(self, domain: str, digests: Iterable[Digest]) -> None: ...
    def acquire_lease(self, domain: str, build: BuildId, digests: Iterable[Digest], ttl_s: int) -> LeaseId: ...
    def renew_lease(self, lease: LeaseId, ttl_s: int) -> None: ...
    # events
    def emit(self, event: Event) -> None: ...
```

Implementations: `PgMetadataStore` (SQLAlchemy), `InMemoryMetadataStore` (tests), `HttpMetadataStore`
(client of the FastAPI service, P1). All three must pass `tests/contract/test_metadata_store.py`.
Every method is one short transaction and raises `MetadataError`; models are frozen, strict and
re-validated on write (values must also pass `canonical_json`).

- `cache_put` is insert-if-absent and requires `result.action_key == key`. `cache_get` counts a
  hit on every call and rewrites `last_access` at most once per hour (`TOUCH_INTERVAL_S`); `touch`
  applies the same throttle to known `blobs` rows and ignores unknown digests.
- Domains are registered implicitly by the first `create_build`/`cache_put` naming them
  (`[A-Za-z0-9][A-Za-z0-9._-]{0,127}`, as the fs CAS); P2-01 adds the admin side.
- `ActionState` transitions (`TRANSITIONS`): `queued → pending|running|cached|cancelled|infra_failed`,
  `pending → pending|running|infra_failed|cancelled`, `running → done|failed|infra_failed|cancelled`,
  `infra_failed → queued` (retry); `done`, `failed`, `cached`, `cancelled` are final. The store
  stamps `queued_at`/`started_at`/`finished_at`, counts `attempts` on entering `running`, sets
  `cached` on `cached`, and a retry clears the previous attempt's job id, reasons and times.
  `**fields` accepts `key` (only while unset or unchanged), `result_key`, `slurm_job_id`, `pending_reason` (state `pending` only),
  `infra_reason` (state `infra_failed` only).
- `record_result` sets the action's `key` (if still unset; a different key is refused) and
  `result_key`, writes one `in` provenance edge per `inputs` entry and one `out` edge per output
  (`object_id` = passed-down id, `content_digest` = content), and registers output blobs, all in
  one transaction and idempotently. It does not change the state: the runner may post before the
  driver has seen `running`; the driver sets `done`/`failed` after reading the result.
- `BuildStatus = running | passed | failed | infra_failed | cancelled`; `finish_build` takes a
  final one, once. `BuildView` = `BuildCreate` fields + `id, status, created_at, finished_at,
  pinned, action_counts` (per-state counts for `ebs status`).
- `Event` (§ 10) is defined in `ebs.meta.api`, because meta (L2) cannot import the driver (L4);
  `ebs.driver.events` re-exports it.

```python
class ResultManifest(BaseModel):          # frozen
    v: Literal[1] = 1
    action_key: Digest
    status: Literal["passed", "failed"]   # failed = tool ran and reported failure (cacheable)
    exit_code: int
    inputs: dict[str, Digest]             # logical path -> input id as staged (feeds `in` provenance edges)
    outputs: dict[str, OutputResult]      # name -> {digest(content), id(passed downstream), type, size}
    log: Digest | None                    # full combined log blob
    summary: dict[str, str | int]         # rule-specific (e.g. sim: uvm_errors, first_error)
    resources: ResourceUsage              # max_rss_kb, cpu_s, wall_s (whole numbers: canon has no floats)
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

Supporting types (P0-12):

```python
@dataclass(frozen=True) class Classification: status: Literal["passed","failed","infra"]; reason: str | None
PASSED, FAILED: Final[Classification]; def INFRA(reason: str) -> Classification
@dataclass(frozen=True) class RuleSettings: license_error_patterns: tuple[str, ...]   # config [rules]
@dataclass(frozen=True) class ExpandContext: instance: StepInstance; resolver: Resolver | None = None
    def render(self, text: str, *, field: str) -> str | list[str]; def render_str(...) -> str
@dataclass(frozen=True) class ActionTemplate:
    argv: tuple[str, ...]                 # logical paths, relative to the work dir
    env: Mapping[str, str]                # declared env for the tool (IN the key)
    inputs: Mapping[str, str]             # implicit inputs: name -> source glob (flow-relative); names "ebs.<x>"
    outputs: Mapping[str, OutputDef]      # implicit outputs, added to the declared ones
    config_files: Mapping[str, str]       # logical path -> content; generated ones live under ".ebs/"
    runtime_env: Mapping[str, str]        # set by the runner, NOT in the key; "$EBS_CPUS" etc. expanded
def expand_runtime_env(runtime_env, values: Mapping[str, str]) -> dict[str, str]   # runner side
def default_classify(exit_code, log_tail, patterns) -> Classification
RuleFactory = Callable[[RuleSettings], RulePlugin]   # what an `ebs.rules` entry point loads
def compile_license_patterns(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]   # ConfigError if invalid
def render_command(command: tuple[str, ...], ctx: ExpandContext, *, start: int = 0) -> list[str]  # sole list refs spliced
def param_text(value: ParamValue) -> str        # int decimal, bool true/false, list space-joined
class BaseRule: ...                             # default validate/classify/summarize; __init__(settings=None)
ENTRY_POINT_GROUP = "ebs.rules"; GENERATED_DIR = ".ebs"
CRASH_SIGNALS = {SIGKILL, SIGSEGV, SIGBUS}; DEFAULT_LICENSE_ERROR_PATTERNS: tuple[str, ...]   # generic FlexLM shapes

class RuleRegistry:                                  # ebs.rules.registry
    def __init__(self, plugins: Iterable[RulePlugin] = ()) -> None: ...
    @classmethod
    def from_entry_points(cls, *, settings: RuleSettings | None = None, entry_points=None) -> RuleRegistry: ...
    def register(self, plugin: RulePlugin, *, origin: str | None = None) -> None: ...   # RuleError: duplicate/bad kind, empty version
    def get(self, kind: str) -> RulePlugin: ...             # RuleError with "did you mean"
    def kinds(self) -> tuple[str, ...]: ...; def plugins(self) -> list[RulePlugin]: ...   # sorted by kind
```

- Entry point name = step kind; its value is a `RuleFactory` (usually the plugin class).
- Default `classify`: exit 0 ⇒ PASSED; killed by SIGKILL/SIGSEGV/SIGBUS (`-N`, or `128+N` as
  reported by a shell or make) ⇒ INFRA("tool_crash"); a `license_error_patterns` match in the log
  tail ⇒ INFRA("license"); otherwise FAILED.
- Built-ins: `shell` (`script:` ⇒ `bash --noprofile --norc -eo pipefail .ebs/script.sh`, or
  `command:` argv), `make` (`make -C <workdir> [target] VAR=value…`, params sorted; implicit input
  `ebs.workdir` = `<workdir>/**`; `MAKEFLAGS`/`MFLAGS`/`MAKELEVEL` dropped from env; runtime env
  `MAKEFLAGS=-j$EBS_CPUS`), `tcl` (`command: [tool, script, args…]` ⇒ `tool .ebs/ebs_main.tcl args…`,
  which sources `.ebs/ebs_params.tcl` with `set ::ebs(name) {value}` and then the script;
  values with characters above U+FFFF are a RuleError, since Tcl 8.6 cannot represent them).
- The planner merges `env`/`inputs`/`outputs`/`config_files` into the `ActionSpec` and puts
  `runtime_env` in a non-key field (P0-08); declared and generated names must not collide.

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

The `Event` model is defined in `ebs.meta.api` (§ 6) and re-exported here.

`Event` = `{v, ts, build, type, action_id?, data}` with types: `build_started`, `plan_ready`,
`cache_hit`, `submitted`, `pending`, `running`, `finished`, `infra_failed`, `retrying`,
`build_finished`, `stat_audit_mismatch`. Emitted to MetadataStore and to a local JSONL file
(`.ebs/builds/<id>/events.jsonl`) used by `ebs status` / `ebs logs` when offline.

## 11. Errors and CLI exit codes — `ebs.core.errors`

`EbsError` → `ConfigError`, `FlowError` (has `file`, `line`, `col`), `DigestError`, `CanonError`,
`TreeError`, `PlanError`, `CasError`, `MetadataError`, `ExecutorError`, `RuleError`, `SandboxError`,
`SourceError` (→ `SourceEscapeError`), `ToolchainError`.
CLI exit codes: `0` ok, `1` build finished with failed actions, `2` usage/flow error,
`3` infrastructure error (retries exhausted), `4` internal bug (with "please report" hint).

## 12. Sources — `ebs.sources`

```python
@dataclass(frozen=True, slots=True)
class SnapshotResult:
    digest: Digest              # tree manifest of the matched paths, relative to base
    files: tuple[str, ...]      # matched relative paths (files and symlinks), sorted
    size: int                   # total bytes of the regular files

class SourceSnapshotter:        # one per plan: git state is read once per repo and cached
    def __init__(self, cas: CAS, statcache: StatCache, *, clock: Clock, rehash: bool = False,
                 audit_fraction: float = 0.01, untrusted_mounts: Sequence[Path] = (),
                 git: GitIds | Literal["auto"] | None = "auto",     # None disables the git guard
                 rng: random.Random | None = None) -> None: ...     # audit sampling (seed in tests)
    def snapshot(self, base: Path, pattern: str, *, optional: bool = False) -> SnapshotResult: ...
    def snapshot_file(self, path: Path) -> Digest: ...              # follows symlinks
    audit_mismatches: list[Path]

class StatCache:                # sqlite (WAL); default_statcache_path(env) -> ~/.cache/ebs/statcache.sqlite
    def __init__(self, db_path: Path, *, racy_window_s: float = 3.0, busy_timeout_s: float = 30.0,
                 clock: Clock | None = None) -> None: ...   # retries a locked first open until the timeout
    def lookup(self, st: os.stat_result, path: Path) -> Digest | None: ...
    def store(self, st: os.stat_result, path: Path, d: Digest, recorded_at: float) -> None: ...
    def lookup_git_blob(self, blob: str) -> tuple[Digest, int] | None: ...   # git blob id -> (sha256, size)
    def store_git_blob(self, blob: str, d: Digest, size: int) -> None: ...
    def forget_git_blob(self, blob: str) -> None: ...
    def close(self) -> None: ...            # also a context manager

class GitIds:                   # injected runner: Callable[[Sequence[str], Path], bytes]
    def __init__(self, runner: GitRunner = run_git) -> None: ...
    def blob_id(self, path: Path) -> str | None: ...   # clean tracked regular file ("H" entry, stage 0)

def resolve_glob(base: Path, pattern: str, *, optional: bool = False) -> list[str]: ...
```

- Stat cache I/O failures after opening are logged and treated as misses / skipped writes; an
  unopenable database raises `SourceError`. Keep the database on local disk (WAL needs shared
  memory; it is unsafe on NFS).
- The git guard ignores assume-unchanged and skip-worktree entries (`ls-files -v` tags other
  than `H`) and runs git with `core.fsmonitor=false`.
- A symlink whose target is inside the base but not matched by the pattern is snapshotted as a
  dangling link; the tool fails loudly unless another input provides the target.

- Globs: `**` = zero or more directories; `*`, `?`, `[…]` within a segment; hidden names match
  only a segment starting with `.`; a matched directory means `dir/**`; `**` never follows
  symlinks. No match raises `SourceError` unless `optional`. Absolute patterns, escaping `..` and
  symlinks resolving outside `base` raise `SourceEscapeError` (pattern + resolved path).
- File id order: git (clean tracked regular file; blob id mapped through the persistent map, size
  must match) → stat cache (key `(dev, ino, size, mtime_ns, ctime_ns, path)`; miss if
  `max(mtime, ctime) > recorded_at - racy_window_s`) → read. `rehash` or an untrusted mount
  (compared lexically and after `realpath`) skips git and the stat cache.
- A read is `CAS.put_file` (hash while copying); it counts only if the file's stat is identical
  before and after, else it is retried (3 attempts, then `SourceError`). `recorded_at` is taken
  before the read. A git mapping is stored only after the CAS bytes re-hash to that git blob id.
- Audit: `audit_fraction` of git/stat hits are re-read; a mismatch is appended to
  `audit_mismatches`, the read digest is used and the stale stat/git entry replaced or dropped.
- Trees are stored with `CAS.put_manifest`, children first; symlinks keep their (relative) target.

## 13. Toolchains — `ebs.toolchain` (P0-07)

```python
# ebs.toolchain.fingerprint (L1)
@dataclass(frozen=True, slots=True)
class FingerprintEntry:         # root, path (relative, "/"), kind: file|dir|symlink|other,
    ...                         # size, mtime_ns, executable, content: Digest | None, target
def default_content_hash(path: Path) -> bool: ...   # x bit, ELF magic, or .so/.sh/.tcl/.py
def scan_roots(roots: Sequence[Path], *, content_hash=default_content_hash) -> list[FingerprintEntry]: ...
def fingerprint_roots(roots: Sequence[Path], *, content_hash=default_content_hash) -> Digest: ...

# ebs.toolchain.env (not importable from L0-L2)
def capture_env(argv: Sequence[str], *, base_env: Mapping[str, str], timeout_s: float = 60.0,
                runner: EnvRunner = run_subprocess) -> dict[str, str]: ...
EnvRunner = Callable[[Sequence[str], Mapping[str, str], str, float], RunResult]  # cmd, env, cwd, timeout

# ebs.toolchain.model
@dataclass(frozen=True, slots=True)
class Toolchain:
    name: str; module: str; version: str; install_roots: tuple[Path, ...]
    env: Mapping[str, str]      # read-only, sorted
    fingerprint: Digest; id: Digest
    @classmethod
    def from_parts(cls, *, name, module, version, install_roots, env, fingerprint) -> Toolchain: ...
def toolchain_id(module: str, version: str, fingerprint: Digest, env: Mapping[str, str]) -> Digest: ...
class ToolchainResolver(Protocol):
    def resolve(self, name: str, module: str) -> Toolchain: ...
class StaticToolchainResolver:  # reads .ebs/toolchains.yaml (P0); P1-07 adds the registry
    def __init__(self, path: Path) -> None: ...
```

- **Fingerprint**: SHA-256 over a header line `["ebs-toolchain-fingerprint", 1]` and one
  `json.dumps(ensure_ascii=True)` line per entry, sorted by (normalized absolute root, path):
  `[root, path, kind, size, mtime_ns, executable, content|null, target|null]`. Not canonical JSON,
  so non-UTF-8 and non-NFC file names stay byte-exact. Roots must be absolute; duplicates count
  once; symlinks are recorded by target and never followed. atime, uid/gid and the other mode
  bits are not recorded. Changing the line format requires bumping `FINGERPRINT_VERSION`.
- **Env capture**: starts from `PATH=/usr/bin:/bin`, `LANG=C.UTF-8`, a fresh empty `HOME`, plus
  `base_env` (may override PATH/LANG; rejects HOME, names outside `[A-Za-z_][A-Za-z0-9_]*`,
  `BASH_*` and variables bash acts on at startup: `BASH_ENV`, `ENV`, `SHELLOPTS`, `BASHOPTS`,
  `PS4`, `CDPATH`, `GLOBIGNORE`, `IFS`). Runs `bash --noprofile --norc -c <fixed script>
  ebs-capture-env <argv…>`; the script runs `"$@"` (stdout → stderr) then
  `builtin command -p env -0` in the same shell. Drops `VOLATILE_VARS` (`PWD`, `OLDPWD`, `SHLVL`,
  `_`, `RANDOM`, `SECONDS`, …) and replaces the temporary HOME path with the literal `$HOME` in
  every value.
- **Toolchain id**: `digest_json({"module", "version", "fingerprint": str, "env"})`, with every
  string NFC-normalized; env names that collide after normalization raise `ToolchainError`. The
  flow-level name is not part of the id.
- `.ebs/toolchains.yaml` is keyed by module: `{version: 1, toolchains: {<module>: {version?,
  install_roots?, env?, fingerprint?}}}`. Relative roots resolve against the file's directory;
  `version` defaults to the text after the last `/`; a `fingerprint` pins the digest instead of
  walking the roots. Fingerprints are computed once per resolver. A tree checked out from version
  control gets fresh mtimes in every clone, so demos and fixtures that need a stable id across
  checkouts pin `fingerprint`.
