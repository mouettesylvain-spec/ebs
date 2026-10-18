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
  License names match `LICENSE_PATTERN = ^[A-Za-z0-9_.-]+(@[A-Za-z0-9_.-]+)?$` (`feature` or
  `feature@server`; tightened in P1-03). The planner merges them with
  `ebs.flow.model.merge_licenses(toolchain, step)`: per feature (the name before `@`), the step wins.
- `debug` is a `DebugDef(collect, max_size, on_success)`.
- `OutputDef(file | dir, deterministic=True, optional=False)` accepts the short form `name: path`.
- `StepDef.inputs` values (`InputValue`) are a pattern or `${…}` reference, a list of them, or (P1-04)
  `InputDef(from_: str | tuple[str, ...], writable=False)`, YAML `{from: …, writable: true}`:
  `writable` makes the runner stage private writable copies. `input_texts(name, value)` lists the
  `(field path, text)` pairs of any form (`inputs.x`, `inputs.x[1]`, `inputs.x.from[0]`).
- `Resources.cpus`, `mem` and `time` are an `int` (count, bytes, seconds) or, when the value holds a `${…}` reference, the verbatim string.
- `Resources.max_parallel` (P1-02) is a literal positive `int`: the SLURM array throttle (`%N`) for the step's job arrays, overriding `[slurm].array_throttle`; a toolchain's `resources.max_parallel` is a default like `cpus`. Like all resources it is not in the action key.
- `Resources.constraint` (P1-03) is a literal string of SLURM node features joined by `&`
  (`CONSTRAINT_PATTERN = ^[A-Za-z0-9_.:-]+(&[A-Za-z0-9_.:-]+)*$`, e.g. `avx512&bigmem`). The
  planner ANDs a toolchain's and the step's (`ebs.flow.model.and_constraints(*exprs)`: in order,
  no repeats); the SLURM executor ANDs the result with `[slurm].os_constraint`.
  plan.json writes it only when set. Not in the action key.
- `Resources.retry_on_timeout` (P1-05) is a literal `bool` or None: whether an attempt killed at
  its time limit is retried (`ReasonRetry`); None defers to `[retry].retry_on_timeout`. A
  toolchain's value is a default the step overrides. plan.json writes it only when set. Not in
  the action key.
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
    writable: bool = False             # P1-04; NOT in key: stage a writable copy, never a CAS link

@dataclass(frozen=True, slots=True)
class ActionSpec:
    action_id: str                      # "compile[lib=core]"; stable across runs
    step: str
    rule: RuleRef                       # kind + rule implementation version
    argv: tuple[str, ...]               # fully expanded; paths are logical (relative to scratch)
    params: Mapping[str, str | int | bool | tuple[str, ...]]   # resolved values as written (P0-08)
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
    runtime_env: Mapping[str, str] = {} # NOT in key; the rule's runtime env, expanded by the runner

def action_key_document(spec: ActionSpec) -> JsonValue: ...   # exactly the fields below
def compute_key(spec: ActionSpec) -> Digest: ...              # raises if an input id is None
def nondeterministic_output_id(producer_key: Digest, output_name: str) -> Digest: ...
def with_key(spec: ActionSpec) -> ActionSpec: ...             # key set, or None while ids are unknown
```

Supporting types (P0-08, `ebs.plan.types`): `RuleRef(kind, version)`, `ToolchainRef(name, module,
id)`, `SourceInput(input, pattern)`, `ActionOutputInput(action_id, output)`, `ImportInput(name,
path, content)` (P2-03; **CONTRACT CHANGE:** `content` added: the digest of the bytes to stage,
the id except for a whole `deterministic: false` release output; not in the key), `ImportPin(domain,
name, version, channel, release, outputs: {name: ImportedOutput(id, digest, type)})` and
`PlanLock(digest, imports: {alias: ImportPin})` (P2-03, what `ebs.release.lock.plan_lock` builds), `OutputSpec(name, path, type: "file"|"dir", deterministic, optional)`,
`DebugSpec(collect, max_size: bytes | None, on_success)`, `FlowInfo(path, git: GitInfo | None)`,
`GitInfo(repo, commit, dirty)`, `PlanToolchain(module, id, env)`, `Plan(ebs_version, domain, project,
flow, toolchains, actions, edges, lock, key_schema, v)` with `Plan.action(id)`,
`ActionSpec.input(path)` / `.output(name)`, and `ActionDiff(action_id, status, changed, pending)`
with status `added | removed | unchanged | changed | unknown`.
plan.json `inputs` entries are `{"path", "kind", "id", "source"}` plus `"writable": true` only for a
writable input (P1-04; read-only inputs encode as before, so existing plan digests are unchanged).
The planner marks an input writable when it is staged through a flow input declared
`writable: true` (source files and output references alike; a path staged by both a writable
and a read-only declaration is writable). Rule-generated inputs are never writable.
`PlanToolchain.env` (P0-13) is the toolchain's captured env, HOME-normalized (literal `$HOME`,
§ 13); the runner applies it with `$HOME` replaced by the action's scratch home. It is already
hashed into the toolchain id, so it changes no action key; plan.json `toolchains` entries are
`{"module", "id", "env"}`.

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
- Input logical paths: each file a source glob matches is a `file` input at its path relative to
  the flow directory (id: snapshot digest). An output reference `${steps.S.outputs.O[sel]}`
  stages the producer instance's output at `O/<instance_id>` (a `tree`) or
  `O/<instance_id>/<file name>` (a `file`), and renders to that path. Inputs, config files and
  outputs of one action must not share or nest paths. Symlinks matched by a source glob are a
  `PlanError` for now.
- Sandbox v1 (R4): output, config file and debug paths must be relative and stay in the work
  dir; source patterns must be relative to the flow directory; an argv element (or a part of it
  after `=`, `+`, `,`, `:` or an option prefix such as `-I`) naming an absolute or `~` path is
  rejected unless it lies under the toolchain's install roots or `planner.SYSTEM_PATHS`; so is a
  relative path leaving the work dir (`../x`). `http(s)://` and `ftp://` URLs are not paths;
  `file://` URLs are. `decode` rejects an action whose stored key does not match its fields.

Plan file (`plan.json`, canonical JSON, stored in CAS; its digest is the build's plan digest):

```json
{"v": 1, "ebs_version": "…", "key_schema": 1, "flow": {"path": "...", "git": {"repo": "...", "commit": "...", "dirty": false}},
 "lock": "sha256:…|null", "domain": "cpu-nda", "project": "rv32x-cpu",
 "toolchains": {"questa": {"module": "questa/2025.2", "id": "sha256:…", "env": {…}}},
 "actions": [ActionSpec…], "edges": [["from_action_id", "output", "to_action_id"]]}
```

`actions` are in topological order (producers first; steps in declaration order on ties), edges
sorted. An action's `resources` is `{"cpus", "mem", "time"}` plus `"max_parallel"` only when set
(P1-02), so plans that do not use it encode as before. Action JSON layout: `ebs.plan.planfile` module docstring; `encode`/`decode`/`plan_digest`,
`to_json`/`from_json`, `spec_to_json`/`spec_from_json`. `decode` rejects another plan format
version or key schema (re-plan).

```python
class Planner:
    def __init__(self, cas: CAS, sources: Snapshotter, toolchains: ToolchainResolver, rules: RuleRegistry) -> None: ...
    def plan(self, flow: Flow, *, base: Path, info: FlowInfo | None = None,
             targets: Sequence[str] = (), rehash: bool = False) -> Plan: ...
    def refine(self, plan: Plan, produced: Mapping[tuple[str, str], Digest]) -> Plan: ...  # (action_id, output) -> id
def diff_plans(old: Plan, new: Plan) -> list[ActionDiff]: ...   # per action: which key fields changed

class Snapshotter(Protocol):        # implemented by ebs.sources.SourceSnapshotter
    @property
    def rehash(self) -> bool: ...
    def snapshot(self, base: Path, pattern: str, *, optional: bool = False) -> SnapshotResult: ...
```

- `base` is the flow directory (tables and source globs resolve against it); `info` is recorded
  as plan.json `flow` (default `{"path": "flow.yaml", "git": null}`). `rehash=True` requires a
  snapshotter with `rehash` set.
- Imports (P2-03; **CONTRACT CHANGE:** `plan(…, lock: PlanLock | None = None)`): every flow
  import needs a pin whose `name` equals `from`, whose `domain` is the flow's, and whose
  `channel` (or `version`) is the one the flow asks for; else `PlanError` naming
  `ebs lock update <alias>`. The planner never resolves channels. `${imports.A}` renders
  `imports/A` (`${imports.A/x}`: `imports/A/x`). As an input value it must come first:
  `${imports.A}` stages every release output whole at `imports/A/<output>` (kind `tree`/`file`,
  id = the release's id), `${imports.A}/<output>` one of them, any other `${imports.A}/<glob>`
  matches the release's files (`ebs.sources.globs.match_paths`, the `resolve_glob` semantics)
  read from the tree manifests in the consumer domain's CAS (missing: `PlanError` suggesting
  `ebs release export`); each match is a `file` input at `imports/A/<output>/<path>` with the
  manifest's digest as id: nothing is re-hashed (a file output keeps the release's id). A
  pattern starting with an exact output name and `/` globs inside that output only, so output
  names with `[k=v]` are literal there; a glob matching a symlink is a `PlanError` (import the
  whole output). Whole outputs keep keys stable across re-releases of the same producer key;
  globbed files follow their bytes. plan.json `lock` is the lock digest when the
  flow has imports, else null. Import source JSON: `{"type": "import", "name", "path",
  "content"}`.
- `refine` is idempotent and processes actions in plan order, so one call propagates through
  chains of nondeterministic outputs; ids of nondeterministic outputs always come from the
  producer's key, never from `produced`.
- `diff_plans` lists the new plan's actions in order, then removed ones. Changed fields are named
  `schema`, `rule`, `argv`, `toolchain`, `params.<n>`, `env.<n>`, `outputs.<n>`,
  `config_files["<path>"]`, `inputs["<path>"]`; `unknown` actions carry
  `pending = ("depends on <producer action_id>", …)`.

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
also has `blob_path`, `tree_path`, `iter_objects() -> Iterator[(Digest, size, "file" | "tree")]`
and `cleanup_tmp(max_age=24h, *, dry_run=False) -> int` (GC, P1-09; `dry_run` only counts).

## 6. Metadata — `ebs.meta.api`

```python
class MetadataStore(Protocol):
    # action cache
    def cache_get(self, domain: str, key: Digest) -> ResultManifest | None: ...     # also touches last_access
    def cache_put(self, domain: str, key: Digest, result: ResultManifest) -> bool: ...  # False if already present
    def cache_replace_failed(self, domain: str, key: Digest, result: ResultManifest) -> bool: ...  # P0-15: --rerun-failed
    # builds
    def create_build(self, b: BuildCreate) -> BuildId: ...
    def add_actions(self, build: BuildId, actions: Sequence[ActionRow]) -> None: ...
    def set_action_state(self, build: BuildId, action_id: str, state: ActionState, **fields: object) -> None: ...
    def record_result(self, build: BuildId, action_id: str, result: ResultManifest) -> None: ...  # also writes provenance edges; never changes state
    def get_result(self, build: BuildId, action_id: str) -> ResultManifest | None: ...  # P0-15: last recorded manifest
    def finish_build(self, build: BuildId, status: BuildStatus) -> None: ...
    def get_build(self, build: BuildId) -> BuildView: ...
    def list_actions(self, build: BuildId, *, state: ActionState | None = None) -> list[ActionRow]: ...
    # access (P0), leases and GC (P1-09)
    def touch(self, domain: str, digests: Iterable[Digest]) -> None: ...
    def resolve_output(self, domain: str, object_id: Digest) -> Digest | None: ...   # P0-13
    def register_blobs(self, domain: str, blobs: Iterable[BlobRef]) -> None: ...      # P1-09: every CAS put
    def acquire_lease(self, domain: str, build: BuildId, digests: Iterable[Digest], ttl_s: int) -> LeaseId: ...
    def renew_lease(self, build: BuildId, lease: LeaseId, ttl_s: int, *, add: Iterable[Digest] = ()) -> None: ...
    def pin_build(self, build: BuildId, *, by: str, reason: str = "") -> None: ...
    def unpin_build(self, build: BuildId) -> bool: ...
    # driver lease (P2-08)
    def claim_driver(self, build: BuildId, owner: str, ttl_s: int) -> DriverLease: ...
    def release_driver(self, build: BuildId, owner: str) -> None: ...
    # events
    def emit(self, event: Event) -> None: ...
    # toolchain registry (P1-07)
    def register_toolchain(self, spec: ToolchainCreate, manifest: bytes) -> RegisterOutcome: ...
    def get_toolchain(self, toolchain_id: Digest) -> ToolchainRecord | None: ...
    def current_toolchain(self, module: str) -> ToolchainRecord | None: ...
    def list_toolchains(self, *, module: str | None = None, current_only: bool = False) -> list[ToolchainRecord]: ...
    def toolchain_manifest(self, toolchain_id: Digest) -> bytes | None: ...
    def touch_toolchain(self, toolchain_id: Digest) -> None: ...
```

Implementations: `PgMetadataStore` (SQLAlchemy), `InMemoryMetadataStore` (tests), `HttpMetadataStore`
(client of the FastAPI service, P1-06, § 6.1). All three must pass `tests/contract/test_metadata_store.py`.
Every method is one short transaction and raises `MetadataError`; models are frozen, strict and
re-validated on write (values must also pass `canonical_json`).

- `cache_replace_failed` (P0-15) replaces an entry only while it holds a `failed` result (new
  `created_at`/`last_access`, hits kept); it never inserts and never overwrites a passed result.
  The driver calls it after a `--rerun-failed` run in cache mode `write`, because the runner's
  `cache_put` keeps the old failure.
- `cache_put` is insert-if-absent and requires `result.action_key == key`. `cache_get` counts a
  hit on every call and rewrites `last_access` at most once per hour (`TOUCH_INTERVAL_S`); `touch`
  applies the same throttle to known `blobs` rows and ignores unknown digests.
- `ActionRow.rule` (rule kind), `params` (`ParamJson` values: str, int, bool, list of str) and
  `toolchain` (toolchain id) are what the plan says the action is (**CONTRACT CHANGE (P2-04)**:
  new optional fields, None on rows written before; migration 0010). The driver sets them in
  `add_actions` (`ebs.driver.build.action_row`); they never change afterwards.
- Domains are registered implicitly by the first `create_build`/`cache_put` naming them
  (`[A-Za-z0-9][A-Za-z0-9._-]{0,127}`, as the fs CAS); P2-01 adds the admin side.
- `InfraReason = oom | timeout | node_fail | preempted | license | runner_crash |
  input_verification | other` (`input_verification` = runner exit 76, added by P0-14 with
  migration `0002`); `PendingReason = licenses | resources | priority | other`.
- `ActionState` transitions (`TRANSITIONS`): `queued → pending|running|cached|cancelled|infra_failed|skipped`,
  `pending → pending|running|infra_failed|cancelled`, `running → done|failed|infra_failed|cancelled`,
  `infra_failed → queued` (retry); `done`, `failed`, `cached`, `cancelled`, `skipped` are final.
  `skipped` (P0-15, migration `0003`) = never started: a producer did not succeed, an input was
  not produced, or the build stopped after a failure without `--keep-going`. The store
  stamps `queued_at`/`started_at`/`finished_at`, counts `attempts` on entering `running`, sets
  `cached` on `cached`, and a retry clears the previous attempt's job id, reasons and times.
  `**fields` accepts `key` (only while unset or unchanged), `result_key`, `slurm_job_id`, `pending_reason` (state `pending` only),
  `infra_reason` (state `infra_failed` only).
- `record_result` sets the action's `key` (if still unset; a different key is refused) and
  `result_key`, writes one `in` provenance edge per `inputs` entry and one `out` edge per output
  (`object_id` = passed-down id, `content_digest` = content), and registers output blobs, all in
  one transaction and idempotently. It also stores the manifest on the action row (P0-15,
  migration `0003`, column `actions.result`); a later manifest for the same key replaces it. It
  does not change the state: the runner may post before the driver has seen `running`; the
  driver sets `done`/`failed` after reading the result with `get_result`.
- `get_result(build, action_id)` (P0-15) returns the last manifest recorded for that action of
  that build (also for cache hits, which the driver records), or None; unknown build or action
  ⇒ `MetadataError`. It is how the driver learns pass/fail and output ids in every cache mode.
- `resolve_output(domain, object_id)` (P0-13) returns the `content_digest` of an `out` edge
  whose passed-down id is `object_id`, else None (also for `in` edges and other domains). The
  runner uses it to fetch the bytes of a `deterministic: false` input, whose id is derived from
  the producer key. `out` edges are never overwritten, so the first recorded bytes win.
- `BuildStatus = running | passed | failed | infra_failed | cancelled`; `finish_build` takes a
  final one, once. `BuildView` = `BuildCreate` fields + `id, status, created_at, finished_at,
  pinned, action_counts` (per-state counts for `ebs status`).
- `Event` (§ 10) is defined in `ebs.meta.api`, because meta (L2) cannot import the driver (L4);
  `ebs.driver.events` re-exports it.
- Toolchain registry (P1-07, migration `0004`). `ToolchainCreate = {id, name, module, version,
  fingerprint, env: {[A-Za-z_][A-Za-z0-9_]*: str}, install_roots: [absolute path, ≥1],
  registered_by}`; `ToolchainRecord` adds `registered_at`, `superseded_by`, `last_used_at`, the
  only fields that ever change. `id` must be `toolchain_id(module, version, fingerprint, env)`
  (§ 13): the registry computes it and re-checks it on every read; stores do not (the runner
  imports the store and must not import `ebs.toolchain`). `register_toolchain` inserts a new id
  with its manifest (opaque bytes, table `toolchain_manifests`), or leaves an existing id and
  its manifest untouched; either way that id becomes the module's current toolchain: other
  non-superseded rows of the module get `superseded_by = id`, and a superseded id registered
  again (reverted install) is un-superseded. `RegisterOutcome = {inserted, superseded: ids}`.
  Registrations of one module are serialized (PG: transaction advisory lock on the module), so a
  module has at most one current row. `current_toolchain` = the non-superseded row;
  `list_toolchains` orders by (module, registered_at, id); `touch_toolchain` sets
  `last_used_at` with the `touch` throttle and ignores unknown ids.
- GC bookkeeping (P1-09, migration `0006`). `register_blobs(domain, [BlobRef{digest, size,
  kind: file|tree}])` inserts `blobs` rows (`created_at = last_access = now`) and touches known
  rows with the `touch` throttle: storing an object again is a use. `record_result` registers
  its outputs the same way (it used to leave known rows alone). The runner registers every
  object it stores (output files, output trees with all their manifests and blobs, the log) and
  touches the inputs it stages; the driver registers the plans it stores and touches the outputs
  and logs of cache hits (batched per scheduling pass); `ebs fetch` touches what it fetches.
- Leases (P1-09 R2): `acquire_lease` (the build must be running and in `domain`; `1 ≤ ttl_s ≤
  MAX_LEASE_TTL_S = 86400`) protects digests until `now + ttl_s`; `renew_lease(build, lease,
  ttl_s, add=…)` extends a live lease of that build and adds digests, else `MetadataError`
  (acquire a new one). `finish_build` drops the build's leases. **CONTRACT CHANGE (P1-09):**
  `renew_lease` takes the build (so the service can authorize its owner) and `add`. The driver's
  `ebs.gc.leases.LeaseKeeper` leases the plan and every known input id at build start, adds
  cache-hit and produced outputs, logs and refined plans, and renews every `ttl / 3`
  (`LEASE_TTL_S = 900`); lease errors are logged, never fatal.
- Driver lease (P2-08 R3), **CONTRACT CHANGE (P2-08):** one live driver per build.
  `claim_driver(build, owner, ttl_s)` atomically takes the lease when the build has none, it
  expired, or `owner` already holds it (a renewal), setting it to expire at `now + ttl_s`, and
  returns `DriverLease{build, owner, expires_at}` as it now stands: an `owner` other than the
  caller's means another driver holds it. The build must be running (else `MetadataError`);
  `1 ≤ ttl_s ≤ MAX_LEASE_TTL_S`; `owner` is 1-256 printable characters (`host:pid:token`).
  `release_driver(build, owner)` drops it if `owner` holds it, else does nothing. The driver's
  `ebs.driver.build.DriverLeaseKeeper` claims it before driving and renews it every `ttl / 3`
  (`DRIVER_LEASE_TTL_S = 300`); a lost lease stops the driver (`DriverBusyError`) without
  cancelling the jobs, which belong to the new driver.
- Pins: `pin_build(build, by=, reason=)` is idempotent (the first author and reason are kept)
  and sets `BuildView.pinned`; `unpin_build` returns whether it was pinned.
- `GcStore` (P1-09; `PgMetadataStore` and `InMemoryMetadataStore` only: GC is a service account
  with direct database access, never over HTTP): `gc_policy(domain) -> DomainPolicy{ttl_days,
  quota_bytes, high_water, low_water}` (defaults without a `domains` row), `gc_blobs`,
  `gc_cache_entries` (`GcCacheEntry{key, last_access, outputs: content digests, log}`),
  `gc_roots(domain, now) -> GcRoots{pinned, leased, released}` (pinned: plan, outputs and logs
  of pinned builds; leased: live leases; released: release outputs, P2-02), `gc_tombstones`,
  `gc_attribution` (`GcAttribution{digest, build, project, step}` for every output and log of a
  recorded result), `register_blobs`, `gc_expire_leases(domain, now) -> int`,
  `gc_delete_cache_entries(domain, keys, *, unused_before) -> deleted keys` (re-checks
  `last_access <= unused_before`), `gc_tombstone(domain, digests, *, now, delete_after)`
  (existing tombstones kept), `gc_drop_tombstones`, `gc_claim(domain, digest, now) -> bool`
  (atomically: a due tombstone whose blob row was not touched since `deleted_rows_at` is
  deleted with the row; PG locks the blob row first, like `touch`), `gc_record_run(GcRun)`.
- `ReleaseStore` (P2-02; PostgreSQL, in-memory and HTTP stores; `MetadataBackend` = both
  protocols, what `open_metadata_store` returns). **CONTRACT CHANGE (P2-02):** new protocol and
  models; `SiteServices.store()` and the factory return `MetadataBackend`; `GcRoots.released`
  is now filled. `create_release(ReleaseCreate{domain, name, version, build, outputs: {"<action
  id>.<output>": ReleaseOutput{action_id, output, digest, id, type, size}}, notes,
  allow_failed: reason | None}, *, by) -> ReleaseView` (+ `id, created_at, created_by,
  exported_from, yanked_at, yanked_by, yank_reason`): the store checks the build is in
  `domain` and finished, and each output equals what its action recorded, from an action in
  `done`/`cached` with a `passed` result unless `allow_failed`; `(domain, name, version)` is
  unique. Names `RELEASE_NAME_PATTERN` (`team/product`), versions `VERSION_PATTERN` (D4),
  channels `CHANNEL_PATTERN`. `get_release(domain, name, version)`, `get_release_by_id`,
  `list_releases(domain, *, name=None)` (by name, created_at, id), `yank_release(id, *, by,
  reason)` (idempotent: the first yank is kept). There is no update or delete (I19; in
  PostgreSQL a trigger refuses DELETE and any UPDATE other than setting the yank fields once).
  `set_channel(domain, name, channel, release, *, by, gate: GateCheck{flow, build} | None) ->
  ChannelMove{id, domain, name, channel, release, version, moved_at, moved_by, gate}` (the
  release must have this domain and name and not be yanked), `get_channel -> ChannelView{…,
  release: ReleaseView, updated_at, updated_by} | None`, `channel_history` (oldest first,
  append-only), `gate_builds(domain, flow, ids) -> [BuildId]` (passed builds of project `flow`
  with an action whose `in` provenance edge holds one of `ids`, newest first; cache hits count,
  edges belong to the action key), `record_export(release, to_domain, *, by) -> ReleaseView`
  (the same release in `to_domain` with `exported_from`, plus an `ExportRecord{id, release,
  exported, to_domain, exported_at, exported_by}` audit row, atomically), `list_exports(release)`
  (rows naming it as source or copy). Release outputs (content digests, also of yanked releases)
  are `GcRoots.released`.
- `ProvenanceStore` (P2-04; PostgreSQL as one recursive CTE, in-memory, HTTP; part of
  `MetadataBackend`). **CONTRACT CHANGE (P2-04):** new protocol and models. `provenance_upstream(
  domain, *, ids, keys, depth)` / `provenance_downstream(...)` -> `ProvenanceGraph{domain, edges:
  [ProvenanceEdge{action_key, direction, logical_path, object_id, content_digest}] (by key,
  direction, path), actions: [ProvenanceAction{key, build, action_id, step, rule, params,
  toolchain, state, cached, status (of the recorded result), finished_at, project, flow_repo,
  flow_commit, flow_dirty, user_name, build_created_at}] (by build, action id; every row holding
  a walked key, cache hits included), releases: [ProvenanceRelease{release, name, version, output,
  object_id, exported_from, yanked}] (outputs whose id is in the graph), truncated}`. Walks stay
  in one domain and count action levels, `1..PROVENANCE_DEPTH_MAX` (200): upstream level 1 is
  `keys` plus the producers of `ids` (as passed-down id or content digest), level n+1 the
  producers of level n's inputs; downstream level 1 is `keys` plus the consumers of `ids`, level
  n+1 the consumers of level n's outputs. `truncated`: a level past `depth` has unwalked keys.
- `ebs.release.provenance` (L3, P2-04): `why(store, domain, *, ids, keys, depth) -> Lineage{graphs,
  links: [ExportLink{domain, release, source_domain, source, export}], hidden: [Hidden{domain |
  None, release, reason}]}` (an id with no producer that is an output of an exported release
  continues in the source domain, found only through the export's audit row, `list_exports`);
  `downstream(store, release)` (the release's domain, then through the exports of it and of every
  release found downstream); `used_by(store, release) -> UsedBy{builds, releases (via "outputs" |
  "export"), hidden, truncated}` (not the release's own build); `sim_runs(store, release) ->
  [TestRun{domain, build, action_id, step, rule, test, seed, status, state, cached, toolchain,
  finished_at, project}]` (rules `*.sim`); `latest(runs)` (newest per (test, seed));
  `sources(lineage)`, `lineage_doc`, `to_dot`, `junit_xml(runs, *, suite)`. Over HTTP each walk
  is a domain-scoped route call, so what the caller cannot read is in `hidden`, never data (R4).
- Release imports (P2-03, `MetadataStore`; **CONTRACT CHANGE:** new methods):
  `record_release_imports(build, [ReleaseImport{alias, release}])` (all or nothing; every
  release in the build's domain; the same row again is a no-op, another release for a recorded
  alias is refused) and `list_release_imports(build)` (by alias). `run_build(…,
  release_imports=)` records them right after `create_build`. Aliases match
  `IMPORT_ALIAS_PATTERN`. PostgreSQL: table `release_imports` (revision 0009, append-only).
- `ebs.release.lock` (L3, P2-03): `flow.lock` models `LockFile{version: 1, imports: {alias:
  LockEntry{domain, name, version, channel | None, release, outputs: {name: LockedOutput{digest,
  id, size, type}}}}}`; `parse_lock(text)` / `load_lock(path) -> LockFile | None` (`ReleaseError`
  when invalid), `dump_lock(lock) -> str` (header comment + YAML with keys sorted at every
  level; `dump(parse(t)) == t`), `lock_digest(lock)` (canonical JSON of the content),
  `release_digest(view)` (`{"v": 1, name, version, outputs}`: no domain, so an exported copy
  pins the same), `plan_lock(lock) -> PlanLock`, `resolve_lock(store, flow, old, aliases=())`
  (channels and versions resolved in the flow's domain; yanked releases refused; with aliases
  the others keep their pins), `diff_locks(old, new) -> [LockChange]` (`describe()`,
  `to_json()`), `import_rows(store, flow, lock) -> [ReleaseImport]` (checks each pinned release
  still has its digest).
- `ebs.release` (L3, P2-02): `releases.create_release(store, *, name, version, build, outputs:
  ["step.output", "step.output[*]", "step.output[col=value,…]"], by, notes, allow_failed)`
  (selectors as in flow references; a matrix step needs one; `ReleaseError` for a broken rule),
  `channels.GatePolicy.from_section([release])` (`[release.gates]`: `channel` or
  `"name:channel"` -> gate flow project; the specific key wins), `check_gate(store, release,
  flow, *, gate_build=None) -> GateCheck`, `move_channel(store, gates, release, channel, *, by,
  gate_build=None)`, `export.export_release(store, release, *, src, dst, access, by)` (copies
  every blob and manifest through `dst`'s atomic write path, children before manifests,
  registers them in the target domain, then `record_export`; `access: DomainAccess` must accept
  both domains, by default `UnixGroupAccess(cas_root)`: owner or group of `<cas_root>/<domain>`).

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

### 6.1 Metadata HTTP service and client (P1-06)

`ebs.meta.service` (L4) serves a MetadataStore over REST; `ebs.meta.client.HttpMetadataStore` (L2)
is the drop-in client and passes the contract suite (`http` over the memory store, `http-pg` over
PostgreSQL). `ebs.meta.factory.open_metadata_store(section)` picks the client for an `http(s)`
`[metadata].url` and `PgMetadataStore` for a `postgresql` one (development, and the service).

Routes, under `API_PREFIX = "/api/v1"`; bodies and responses are `ebs.meta.api` models, parsed
in JSON mode (`model_validate_json`):

| Method and path | Store call | Body → response |
| --- | --- | --- |
| `GET /domains/{d}/cache/{key}` | `cache_get` | → `OptionalResult` |
| `PUT /domains/{d}/cache/{key}` | `cache_put` | `ResultManifest` → `Changed` |
| `POST /domains/{d}/cache/{key}/replace-failed` | `cache_replace_failed` | `ResultManifest` → `Changed` |
| `POST /domains/{d}/cache/lookup` | `cache_get` per key | `DigestBatch` → `CacheHits` (hits only) |
| `POST /domains/{d}/touch` | `touch` | `DigestBatch` → 204 |
| `POST /domains/{d}/blobs` | `register_blobs` | `BlobBatch` → 204 (P1-09, `cache_write`) |
| `GET /domains/{d}/outputs/{id}` | `resolve_output` | → `OptionalDigest` |
| `POST /builds` | `create_build` | `BuildCreate` → `CreatedBuild` |
| `GET /builds/{b}` | `get_build` | → `BuildView` |
| `POST /builds/{b}/actions` | `add_actions` | `ActionBatch` → 204 |
| `GET /builds/{b}/actions[?state=]` | `list_actions` | → `ActionList` (paged) |
| `POST /builds/{b}/actions/{a}/state` | `set_action_state` | `StateChange` → 204 |
| `PUT /builds/{b}/actions/{a}/result` | `record_result` | `ResultManifest` → 204 |
| `GET /builds/{b}/actions/{a}/result` | `get_result` | → `OptionalResult` |
| `POST /builds/{b}/finish` | `finish_build` | `BuildFinish` → 204 |
| `POST /builds/{b}/leases` | `acquire_lease` | `LeaseRequest{domain, digests, ttl_s}` → `CreatedLease` (P1-09) |
| `POST /builds/{b}/leases/{id}/renew` | `renew_lease` | `LeaseRequest{digests, ttl_s}` → 204 |
| `POST /builds/{b}/driver` | `claim_driver` | `DriverClaim{owner, ttl_s}` → `DriverLease` (P2-08) |
| `POST /builds/{b}/driver/release` | `release_driver` | `DriverRelease{owner}` → 204 |
| `PUT /builds/{b}/pin` | `pin_build` (by = caller) | `PinRequest{reason}` → 204 |
| `DELETE /builds/{b}/pin` | `unpin_build` | → `Changed` |
| `POST /builds/{b}/events` | `emit` | `Event` → 204 |
| `POST /builds/{b}/release-imports` | `record_release_imports` | `ReleaseImportBatch{imports}` → 204 (P2-03, `build_write`) |
| `GET /builds/{b}/release-imports` | `list_release_imports` | → `ReleaseImportList` (paged) |
| `GET /builds/{b}/events` | `events_after(build=)` | → `text/event-stream` (P2-05, § 6.2) |
| `GET /domains/{d}/events` | `events_after(domain=)` | → `text/event-stream` (P2-05) |
| `POST /toolchains` | `register_toolchain` | `ToolchainRegistration` (manifest base64) → `RegisterOutcome` |
| `GET /toolchains[?module=&current_only=]` | `list_toolchains` | → `ToolchainList` (paged) |
| `GET /toolchains/current?module=` | `current_toolchain` | → `ToolchainList` (0 or 1) |
| `GET /toolchains/{id}` | `get_toolchain` | → `ToolchainList` (0 or 1) |
| `GET /toolchains/{id}/manifest` | `toolchain_manifest` | → `ToolchainManifest` |
| `POST /toolchains/{id}/touch` | `touch_toolchain` | → 204 |
| `POST /releases` | `create_release` (by = caller) | `ReleaseCreate` → `ReleaseView` (P2-02, `release_write`) |
| `GET /releases?domain=[&name=][&version=]` | `list_releases` / `get_release` | → `ReleaseList` (paged) |
| `GET /releases/{id}` | `get_release_by_id` | → `ReleaseList` (0 or 1) |
| `POST /releases/{id}/yank` | `yank_release` (by = caller) | `YankRequest{reason}` → `ReleaseView` |
| `POST /releases/{id}/exports` | `record_export` (by = caller) | `ExportRequest{to_domain}` → `ReleaseView` (`release_export`) |
| `GET /releases/{id}/exports` | `list_exports` | → `ExportList` (paged) |
| `PUT /channels` | `move_channel` with the service's `[release.gates]` | `ChannelSet{domain, name, channel, release, gate_build}` → `ChannelMove` |
| `GET /channels?domain=&name=&channel=` | `get_channel` | → `ChannelList` (0 or 1) |
| `GET /channels/history?domain=&name=&channel=` | `channel_history` | → `ChannelHistory` (paged) |
| `POST /domains/{d}/gate-builds` | `gate_builds` | `GateQuery{flow, ids}` → `BuildList` |
| `POST /domains/{d}/provenance/upstream` | `provenance_upstream` (P2-04) | `ProvenanceQuery{ids, keys, depth}` → `ProvenanceGraph` (`read` of `d`) |
| `POST /domains/{d}/provenance/downstream` | `provenance_downstream` (P2-04) | `ProvenanceQuery{ids, keys, depth}` → `ProvenanceGraph` (`read` of `d`) |
| `GET /whoami` | — | → caller's principal |

`/healthz` (process) and `/readyz` (database reachable, schema at the migrations' head, else 503)
need no credentials. Errors are `ErrorBody {detail}`: 401 missing or invalid credentials, 403
not allowed, 404 no such object, 413 body too large, 422 invalid request, 429 rate limited
(P2-05), 409 any other `MetadataError` (the store's message), 503 a database outage, 500 a bug.
The client raises `MetadataError` with the detail.

- **Batches:** `ActionBatch` and `DigestBatch` carry at most `BATCH_MAX = 500` items (422
  otherwise). `HttpMetadataStore.add_actions` refuses duplicate ids over the whole sequence before
  sending and then sends batches of 500; each batch is atomic, but a failure in a later batch
  (build finished meanwhile, an id added concurrently) leaves the earlier batches added.
  `HttpMetadataStore.cache_get_many(domain, keys)` (client only, not in the protocol yet) looks
  keys up 500 at a time. `register_blobs` sends `BlobBatch`es of 500; a lease of more than 500
  digests is acquired with the first 500 and grown by renewals of 500 each (P1-09).
- **Retries:** each request times out after 10 s (`[metadata].timeout_s`). Connect errors are
  retried for every call; 5xx and read timeouts only for idempotent calls (reads, `cache_put`,
  `record_result`, `touch`, `cache_get_many`, `register_toolchain`, `touch_toolchain`,
  `register_blobs`, `renew_lease`, `pin_build`, `unpin_build`), with exponential backoff (0.5 s doubling, capped at
  8 s, `[metadata].retries = 4`). Each attempt sends fresh credentials. A retried `cache_put`
  whose first attempt landed returns False.
- **Authentication (D1, decided 2026-10-11):** MUNGE for users on cluster hosts, bearer tokens
  for CI and services. `X-EBS-Munge` carries a credential whose payload is
  `ebs.meta.client.munge_payload(audience, method, path)` = compact JSON
  `["ebs-meta/1", audience, METHOD, path]` (`path` below `API_PREFIX`, percent-decoded;
  `audience` = `[meta_service].audience`, by default the `host[:port]` of the client's
  `[metadata].url`). The service decodes it with `unmunge --numeric` (replays, expired and
  rewound credentials refused), parses only the metadata block, takes exactly `LENGTH` payload
  characters, refuses a payload other than the request's (so credentials seen in other MUNGE
  traffic, or minted for another request, are useless), and maps the uid to a user name via NSS.
  `Authorization: Bearer <token>`: only `sha256:` hashes stored in `api_tokens`, kinds
  `user | ci | service`. Pluggable `Authenticator`s (`authenticate(RequestInfo)`), tried in
  order by `ChainAuthenticator`.
- **Authorization (R4):** every API route calls the one hook `ServiceDeps.authorize(principal,
  op, *, domain, owner)` with `op = read | cache_write | build_write | toolchain_write |
  release_write | release_export` (P2-02: like `build_write` on the release's build; an
  export's `domain` is the target; a failed channel gate is a 409), the
  domain, and the build's `BuildOwner(user_name, project, domain, build)` for build routes. The
  default `Policy(toolchain_admins)`: a build write is allowed to its owner (`principal ==
  user_name`) or to a `ci` token whose `team` is the build's project, and `service` tokens never
  write builds; `toolchain_write` (registering makes a toolchain current for every later build)
  only to `service` tokens and the users in `[meta_service].toolchain_admins`.
- **Domains (P2-01, I20):** routes call `ServiceDeps.check(...)`, which calls the hook and
  records the authorization on `Principal.authorized`; a 2xx answer without one becomes a 500
  (fail closed), and every OpenAPI route depends on `current_principal`, which arms that check
  (`tests/unit/meta/service/test_authz.py`). The deployed hook is
  `ebs.meta.service.authz.DomainPolicy(base, domains, served, groups, token_domains)`: the
  route's domain and the domain of the build it names must be in `[meta_service].domains`
  (required, each in `[[domains]]`) and the caller a member (MUNGE user: NSS groups via
  `NssGroups(ttl_s=[meta_service].group_ttl_s)`, `getgrouplist`; bearer principal:
  `[meta_service].token_domains`, else the NSS groups of that name), then `base` (the owner
  rules above). Refusal is `DomainDenied` (403); routes that name an object by id answer 404
  (`NotFoundError`, as for an id that does not exist — **CONTRACT CHANGE (P2-01):** an unknown
  build id is now 404, not 409), cache reads (`cache_get`, `lookup`, `resolve_output`,
  `GET /releases/{id}`) answer a miss; a release export also checks the source domain. Only
  `/whoami` and the toolchain registry are domain-free.

### 6.2 Service hardening (P2-05)

- **Dashboard login (R1, `ebs.meta.service.oidc`):** with `[meta_service.oidc]` (`issuer`,
  `client_id`, `client_secret_file`, `redirect_uri`, `session_secret_file`, `username_claim =
  "preferred_username"`, `scopes`, `session_ttl_s = 28800`, `login_ttl_s = 600`,
  `cookie_secure = true`) the service mounts `GET /auth/login?next=`, `GET /auth/callback` and
  `POST /auth/logout` (outside the API and the OpenAPI document). Authorization code + PKCE
  (S256) against GitLab; the login state is a signed cookie on `/auth`; the ID token is taken
  from the token endpoint over TLS (OIDC Core § 3.1.3.7) and its `iss`, `aud`/`azp`, `exp` and
  `nonce` are checked (so `issuer`, `redirect_uri` and every discovered endpoint must be https
  unless `allow_insecure_http`, for tests); the user name is `username_claim` (ID token, else userinfo with the same
  `sub`) and must match `[a-z_][a-z0-9_.-]{0,31}`. The session cookie `ebs_session` (HMAC-SHA256
  over JSON `{user, csrf, exp}`; keys one per line, the first signs) authenticates as
  **`Principal(via="session")`** (**CONTRACT CHANGE:** `via` gains `"session"`; `whoami` too),
  authorized like a MUNGE user of that name (NSS groups). Requests other than GET/HEAD/OPTIONS
  with a session must send the `ebs_csrf` cookie's value in `X-CSRF-Token`, else 403.
- **Event stream (R2, `ebs.meta.service.events_stream`):** `GET /builds/{b}/events` (404 outside
  the caller's domains) and `GET /domains/{d}/events` (403) answer `text/event-stream`: first
  `retry: <retry_ms>`, then one frame per event, `id: <row id>`, `event: <type>`, `data:
  <StoredEvent JSON {id, event}>`, and `: keepalive` comments when idle. The stream resumes
  after the later of `Last-Event-ID` and `?after=` (EventSource reconnects to the same URL). The source is the new store protocol
  **`EventReader.events_after(after, *, build | domain, limit ≤ EVENT_PAGE_MAX = 1000) ->
  list[StoredEvent]`** (PostgreSQL and memory stores; not over HTTP). Ids commit in order within a
  domain: `PgMetadataStore.emit` takes a per-domain advisory lock until commit, so resuming never
  skips a row. Waiting streams are woken by `Wakeup` (in-process, from the `emit` and `finish`
  routes) and by `PgEventListener` (LISTEN `ebs_events`; every PostgreSQL `emit` and
  `finish_build` sends NOTIFY `"<domain> <build>"`), with a slow poll (`poll_s`) as a fallback.
  A build's stream ends when the build is finished and drained; any stream ends after
  `max_stream_s` and the client reconnects (`[meta_service.events]`: `poll_s = 15`,
  `keepalive_s = 15`, `max_stream_s = 300`, `retry_ms = 3000`).
- **Pagination (R3, `ebs.meta.service.pagination`):** the list routes marked "paged" take
  `limit` (1..`max_page_size`, the default) and `cursor`, and answer **`next`** (**CONTRACT
  CHANGE:** `ActionList`, `ReleaseList`, `ToolchainList`, `ChannelHistory`, `ExportList` gain
  `next: str | None`). A cursor is opaque (the last item's sort key and a query fingerprint):
  a page resumes after that key, and a cursor of another query is a 422. Order: actions by id,
  releases by (name, created_at, id), toolchains by (module, registered_at, id), channel moves and
  exports by id. `HttpMetadataStore` follows `next`, so its list methods still return everything.
- **Limits (R4, `ebs.meta.service.limits`):** a token bucket per principal (`user:<name>` for
  MUNGE and session callers, `token:<name>`) checked
  after authentication: 429 with `Retry-After` (whole seconds). Bodies over `max_body_bytes`
  answer 413 before any route runs. `[meta_service.limits]`: `rate_per_s = 100`, `burst = 2000`,
  `max_body_bytes = 16 MiB`, `max_page_size = 1000`. Buckets are per process. **CONTRACT
  CHANGE:** the client retries a 429 for every call (nothing ran), waiting `Retry-After` (at most
  60 s) within `[metadata].retries`.
- **Observability (R5, `ebs.meta.service.metrics`):** `GET /metrics` (no credentials) in the
  Prometheus text format: `ebs_http_request_duration_seconds{method,route,status}` (histogram per
  route template; methods outside HTTP's own count as `other`), `ebs_cache_lookups_total{result}`
  (hit ratio) and `ebs_db_pool_size`,
  `_checked_out`, `_overflow`; no ids or domain names in labels. Every response carries
  `X-Request-ID` (the caller's if it matches `[A-Za-z0-9._-]{1,64}`, else a new one), bound into
  every log line of the request, plus one `request` access line; `serve` logs JSON lines.
- **Event retention (R6):** revision `0008_events_partitioned` adds `stored_at` (the store's
  clock at insert) and partitions `events` by its UTC month (`events_yYYYYmMM`, primary key
  `(id, stored_at)`, ids from `event_id_seq`, partitions created by
  `ebs.ensure_events_partition(ts)` on first use). The client's `ts` never picks a partition. **`EventReader.prune_events(before, *,
  dry_run) -> list["YYYY-MM"]`** drops every month of stored events that ends at or before `before`;
  `python -m ebs.meta.service events prune [--dry-run]` uses `[meta_service.events].
  retention_days = 180` and creates next month's partition (daily timer in `deploy/meta`).

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
The runner calls `classify` once per run, after the tool exits and before it collects the
outputs, with the outputs that exist (name -> path in the work dir); a rule may finalize an
output file there (P1-08: `questa.sim` writes its `result.json`), and what is then on disk is
what gets collected. `summarize` runs after collection.

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
- Questa rule pack (`ebs.rules.questa`, P1-08): `questa.compile` (`vlib` + `vlog`/`vcom` per
  library; params `lib`, `lang`, `vlog_opts`, `vcom_opts`; inputs `filelist`/`files`; output
  `worklib`), `questa.opt` (`vlib` + `vopt … <top>… -o opt`; params `top`, `vopt_opts`; output
  `model`) and `questa.sim` (`vsim -c -batch … -sv_seed`; params `seed`, `test`, `plusargs`,
  `vsim_opts`; input `model`; outputs `result`, optional `cov`). Each runs a generated
  `.ebs/questa_*.sh` with `-modelsimini modelsim.ini` (at the work-dir root, so relative library
  paths mean the same to the tools as to the runner); every `${steps.…}` input other than
  `filelist`/`files` (compile) and `model` (sim) is an upstream library, mapped under the
  producer's `lib` matrix value (or its step name without a matrix), so `questa.compile`
  requires its `lib` param to equal that name. Pass a sim step the libraries its model was
  optimized from as well as `model`. Generated
  `worklib`/`model` outputs are `deterministic: false`. `questa.sim` writes a stub
  `result.json` before `exec vsim`; its `classify` (runner side, before outputs are collected)
  parses the `transcript` (`ebs.rules.questa.transcript`) and replaces the stub with the final,
  timestamp-free document. PASSED iff exit 0, no fatals, UVM_ERROR = 0 and a `$finish` marker:
  a bare `** Error` (e.g. an SVA `$error`) is counted but does not fail a test. Exit 126/127
  (tool not found) and the tools' own `** Fatal: (SIG…)` crash reports are INFRA.
- The planner merges `env`/`inputs`/`outputs`/`config_files` into the `ActionSpec` and puts
  `runtime_env` in a non-key field (P0-08); declared and generated names must not collide.

## 8. Executors — `ebs.exec.api`

```python
class Executor(Protocol):
    name: str
    def submit(self, batch: SubmitBatch) -> list[JobHandle]: ...   # one handle per action, in batch order; may raise ExecutorBusy
    def poll(self, handles: Sequence[JobHandle]) -> dict[JobHandle, JobStatus]: ...   # ONE backend call per poll; never blocks
    def cancel(self, handles: Sequence[JobHandle]) -> None: ...   # finished jobs keep their state

@dataclass(frozen=True)
class JobHandle:                          # hashable value
    executor: str                         # Executor.name
    job_id: str                           # "17" locally, "1234_5" for a SLURM array element
    action_id: str

@dataclass(frozen=True)
class SubmitBatch:                        # non-empty; one step, one domain, unique action ids (else ExecutorError)
    plan: Digest                          # plan.json in the domain's CAS
    domain: str
    build: BuildId | None                 # None: the runner posts nothing and prints the result
    actions: tuple[ActionSpec, ...]
    exclude_nodes: tuple[str, ...] = ()   # P1-05: hosts (NODE_PATTERN) the jobs must avoid

class JobStatus(BaseModel):               # frozen, strict
    state: Literal["pending", "running", "done", "infra_failed", "cancelled"]
    pending_reason: PendingReason | None = None   # only for `pending`
    infra_reason: InfraReason | None = None       # exactly for `infra_failed`
    exit_code: int | None = None                  # the runner's; negative = killed by that signal
    node: NodeName | None = None                  # P1-05: host it ran on, if known (never pending)
```

`NODE_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$"`: one host name, never a host list. Executors
without nodes (local) ignore `exclude_nodes` and report no node; the SLURM executor reads the
node from `squeue`/`sacct` (`nodes`; a host list or "None assigned" is no node) and passes
`exclude_nodes` as `sbatch --exclude=`.

`ExecutorBusy(ExecutorError)` (P1-02) carries `retry_after_s`: `submit` started nothing because the
backend cannot take the batch now (rate limit, too many jobs in flight). It is not a failure of
the actions: the driver leaves them queued, submits nothing for `max(retry_after_s, poll_min_s)`
(polling goes on) and does not read the cache again for them.

`done` means the runner finished and posted a ResultManifest (passed or failed); the driver reads
the result from MetadataStore, not from the executor. Every executor maps runner exit codes
(§ 9) the same way (`ebs.exec.api.status_for_exit`): `0` ⇒ `done`; `75` ⇒ `infra_failed` with the
reason from the runner's infra line on stderr (default `other`); `76` ⇒
`infra_failed("input_verification")`; any other code or death by a signal ⇒
`infra_failed("runner_crash")`. `cancel` sends SIGTERM to a running runner (which removes its
scratch) and drops queued work; the job reports `cancelled` from then on, whatever the runner's
exit code. All executors pass `tests/contract/test_executor.py`.

SLURM adapter (P1-01, `ebs.exec.slurm`; used by the SLURM executor, P1-02):

```python
SlurmCli(runner: CommandRunner | None = None, clock: Clock | None = None, bin_dir: Path | None = None,
         max_retries: int = 5, timeout_s: float = 60.0)
    .submit(script: Path, opts: SbatchOptions) -> JobId          # sbatch --parsable
    .query(job_ids: Iterable[JobId | ArrayTaskId]) -> dict[JobId | ArrayTaskId, SlurmJobInfo]
    .cancel(job_ids, *, signal: str | None = None) -> None       # one scancel; ended jobs are no error
    .version() -> str                                            # scontrol --version
SbatchOptions(job_name, partition=, account=, qos=, cpus_per_task=, mem_mb=, time_limit_s=,
              licenses={name[@server]: n}, constraint=, exclude=(node, …),
              array=ArraySpec(indices, throttle=),
              output=, error=, workdir=, export_file=, requeue=False, comment=)   # frozen, validated
write_env_file(path, env)                 # NUL-separated NAME=value for --export-file
states.job_status(info: SlurmJobInfo, *, infra_reason: InfraReason | None = None) -> JobStatus
```

- `comment` (P1-02) becomes `--comment=…`: the build UUID, read by admin tooling and the
  scratch-cleanup epilog (P1-04).
- argv is always a list; options are single `--name=value` elements; `--export=NONE` always
  (the job's environment is the env file only); `--no-requeue` unless `requeue=True` (the driver
  retries, a SLURM requeue would run the action twice).
- `query` makes one `squeue --json --states=all --jobs=<base ids>` call and one
  `sacct --json --jobs=…` call for the references squeue no longer knows; jobs unknown to both
  are absent from the result. Array tasks still pending are one SLURM record (`2-9%2`); the
  adapter answers each task from it. Unknown JSON fields are ignored; a missing required one
  (`job_id`, `job_state`, sacct `state.current`) is an `ExecutorError` naming the SLURM release.
- Transient failures (socket timeout, controller/slurmdbd unreachable, our own command timeout)
  are retried up to `max_retries` times with backoff 1, 2, 4, 8, 16 s (cap 30 s); any other
  non-zero exit raises `ExecutorError` with SLURM's message at once.
- State mapping: PENDING/REQUEUED ⇒ `pending` with reason `Licenses`⇒licenses,
  `Resources`/`ReqNodeNotAvail…`⇒resources, `Priority`⇒priority, else other; RUNNING-like states
  and any state flagged COMPLETING (epilog still cleaning) ⇒ `running`; OUT_OF_MEMORY⇒oom,
  TIMEOUT/DEADLINE⇒timeout, NODE_FAIL/BOOT_FAIL⇒node_fail, PREEMPTED⇒preempted (all
  `infra_failed`); CANCELLED ⇒ `cancelled`; COMPLETED/FAILED ⇒ `status_for_exit(runner exit code)`
  (negative when a signal ended it); an unknown state ⇒ `infra_failed("other")` with a warning.

SLURM executor (P1-02, `ebs.exec.slurm.executor`):

```python
SlurmSettings(debug_root=Path("/debug"), default_partition=None, max_array_size=1000, array_throttle=200,
              max_pending_jobs=2000, max_submits_per_min=60, poll_min_s=5.0, poll_max_s=60.0, bin_dir=None,
              license_server="flexlm", license_fuzzy=False, account_map={}, qos=QosSettings(),
              os_constraint=None)                       # last five: P1-03
SlurmSettings.from_config(config) -> SlurmSettings      # [slurm] (D5 defaults) + [debug].root; ConfigError
SlurmExecutor(*, cli: SlurmCli, cas: CAS, settings: SlurmSettings, store: MetadataStore | None = None,
              runner_argv=("ebs-runner",), env: Mapping[str, str] | None = None, clock: Clock | None = None,
              project: str | None = None, build_kind: BuildKind = "dev")   # P1-03; ConfigError if unmapped
    .submit / .poll / .cancel                         # the Executor protocol; name = "slurm"
    .job_ref(handle) -> JobId | ArrayTaskId           # the SLURM job behind a handle
    .adopt(build, handles) -> None                    # P2-08: track jobs a dead driver submitted
                                                      # (handles from actions.slurm_job_id)
    .in_flight() -> int                               # unfinished jobs, array tasks counted one by one
# ebs.exec.slurm.arrays
plan_jobs(actions, *, max_array_size, throttle) -> list[JobPlan(actions, is_array, throttle)]
array_throttle(actions, default) -> int               # strictest resources.max_parallel, else default
store_array_map(cas, plan, action_ids) -> Digest;  load_array_map(cas, d) / decode_array_map(bytes) -> ArrayMap
# ebs.exec.slurm.licenses (P1-03)
merge_licenses(toolchain, step) -> dict[str, int]     # re-export of ebs.flow.model.merge_licenses
FEATURE_PATTERN = r"^[A-Za-z0-9_.-]+$";  DEFAULT_LICENSE_SERVER = "flexlm"
check_license_server(server) -> str                   # ConfigError unless FEATURE_PATTERN
license_request(licenses, *, server, fuzzy) -> dict[str, int]   # {"feat@server"|"feat": n}; validates
render_licenses(request) -> str                       # "a@flexlm:1,b@flexlm:2", sorted
batch_licenses(actions) -> dict[str, int]             # largest count per license over a job's actions
license_error(exc, request, *, step) -> ExecutorError | None    # names the features SLURM rejected
# ebs.exec.slurm.accounts (P1-03)
BuildKind = Literal["dev", "ci", "release"];  BUILD_KINDS;  QosSettings(default=None, ci=None, release=None)
parse_account_map(v) / parse_qos(v) / parse_os_constraint(v)   # [slurm] parsing; ConfigError names the key
resolve_account(account_map, project) -> str | None   # None if no map; ConfigError lists known projects
select_qos(qos, kind) -> str | None                   # ci ⇒ qos.ci, release ⇒ qos.release, else/unset ⇒ default
job_constraint(os_constraint, step_constraints) -> str | None   # features ANDed, no repeats
JobPolicy.resolve(*, account_map, qos, os_constraint, project, build_kind) -> JobPolicy(account, qos, os_constraint)
# ebs.exec.slurm.jobscript
runner_arguments(runner_argv, *, plan, domain, build, action=None | array_map=None) -> list[str]
render_jobscript(argv) -> str                         # fixed template, every value shlex.quote'd
```

- One executor per domain (its CAS's). A batch of one action is one plain job; a larger batch is
  one job array, split into parts of at most `max_array_size` (a part of one action is a plain
  job). Handles are `"<job>"` or `"<array job>_<task>"`. The array throttle is
  `array_throttle(actions, [slurm].array_throttle)`, per array part.
- The job script is `#!/bin/bash`, `set -euo pipefail`, `exec <runner argv>` (§ 9 form, with
  `--domain` and, for a build, `--build`); sbatch gets `--export=NONE` and `--export-file` with
  `env` (default: the caller's `PATH` and `EBS_CONFIG`). stdout+stderr go to
  `<debug_root>/<domain>/<build id|local>/slurm/%A_%a.out` (arrays) or `%j.out`; scripts and env
  files sit in its `jobs/` subdirectory. Job name `ebs:<build uuid[:8]|local>:<step>`, comment
  the build UUID. Partition, cpus, memory (MiB, rounded up) and time (`resources.time` +
  `JOB_TIME_GRACE_S` = 120 s, so the runner's own timeout fires first) come from the batch's
  largest request. P1-03: `--licenses` is `license_request(batch_licenses(actions))` (feature names
  `^[A-Za-z0-9_.-]+$`, an explicit `feat@server` kept as written); `--account`/`--qos` come from
  the executor's `project`/`build_kind`; `--constraint` is `os_constraint` ANDed with the
  actions' `resources.constraint`. An sbatch failure mentioning licenses is re-raised naming the
  requested features. None of these is in the action key (I3).
- R7: before `submit` returns, each job id is recorded with
  `set_action_state(build, action, "pending", slurm_job_id=…)`; a build needs a store. If an
  sbatch call or a record fails, the batch's jobs already submitted are scancelled and `submit`
  raises ExecutorError.
- `poll` makes at most one `SlurmCli.query` (one squeue, plus one sacct for what squeue forgot)
  for the unfinished handles it is given; finished jobs answer from memory, a job SLURM does
  not know is left out of the result. Runner exit 75: the infra reason is read from the tail of
  the job's output file. `cancel` makes one query (finished jobs keep their state), then one
  scancel (whole arrays by their id when all their unfinished tasks are cancelled).
- R8: `submit` raises `ExecutorBusy` when its sbatch calls would exceed `max_submits_per_min` in
  the last 60 s (`retry_after_s` = until the oldest call leaves the window), or when the jobs in
  flight plus the batch would exceed `max_pending_jobs` (`retry_after_s = poll_min_s`). Neither
  applies when nothing is in flight / no call is in the window, so an oversized batch runs alone.

`LocalExecutor(log_dir=, runner_argv=("ebs-runner",), max_parallel=None, env=None)` (P0-14) runs
runner subprocesses on this machine: at most `max_parallel` slots (default `os.cpu_count()`), an
action taking `resources.cpus` slots (default 1; more than `max_parallel` is capped, so it runs
alone, with a warning), started FIFO from `submit`/`poll` — a head action that does not fit
blocks those behind it; queued actions are `pending` with `pending_reason="resources"`. Memory
requests are advisory (logged with `enforced=False`, never applied). Runners start in their own
session with stdin closed; stdout/stderr go to files under `<log_dir>/local-*/` (`log_paths`).
`close(grace_s=30)` cancels everything and waits, SIGKILLing a runner that outlives the grace.

## 9. Runner — `ebs-runner`

```
ebs-runner --plan sha256:<plan digest> --action <action_id> [--build <id>] [--domain <d>]
ebs-runner --plan sha256:<plan digest> --array-map sha256:<map digest> [--build <id>] [--domain <d>]   # uses $SLURM_ARRAY_TASK_ID
```

Array map (P1-02, a blob in the domain's CAS, canonical JSON; `ebs.exec.slurm.arrays`):
`{"v": 1, "plan": "sha256:<plan digest>", "actions": [<action id of task 0>, <task 1>, …]}`.
Task `$SLURM_ARRAY_TASK_ID` runs `actions[i]`; its `plan` must equal `--plan` (P1-04 reads it).

Exit codes: `0` result posted (passed or failed), `64` bad usage, `70` internal error,
`75` infra failure (temporary; driver retries), `76` input verification failed (digest mismatch).
With exit 75 the runner's last stderr line is `ebs-runner: infra_failed {"detail": …, "reason":
<InfraReason>}` (JSON on one line; `ebs.runner.errors.format_infra_line`/`parse_infra_reason`),
so executors learn the reason with or without `--build` (P0-14). The tool's own output goes to
`logs/tool.log`, never to this stream; the reason only picks a retry policy, never a cache entry.
Lifecycle and guarantees: architecture.md "Runner lifecycle on a node". P0-13 details:

- Options: `--domain` is required without `--build` (else taken from the build, must match);
  `--keep-scratch` keeps the scratch dir and prints `scratch kept at <path>` on stderr. Without
  `--build` nothing is posted and the ResultManifest is printed as JSON on stdout. `--array-map`
  is P1-04 (exit 64 until then). Config: `[cas].root`, `[scratch].dir`, `[runner]`, `[rules]`,
  `[metadata]` (only with `--build`), via `ebs.config.load_config`.
- Exit 64 also for: a plan not in the CAS or of another domain, an unknown action id, an
  action whose key or input ids are unset (the driver must refine the plan), a rule kind or
  version that differs from the planner's, a toolchain id missing from plan `toolchains`, an
  unexpanded `resources.time`. A plan whose bytes do not hash to `--plan` is 76. An explicit
  `$EBS_CONFIG` that is not a file is a ConfigError (exit 64).
- Scratch `<scratch>/<build|local>/<sha256(action_id)[:16]>/{work,home,tmp,logs}`; a stale dir
  is replaced. Under SLURM the dir first gets `.ebs-job` holding `$SLURM_JOB_ID` (P1-04), the
  key of the node epilog `deploy/slurm/epilog.d/ebs-scratch-cleanup.sh`. Inputs are
  materialized at `work/<logical path>` with `CAS.materialize(mode="auto",
  copy_threshold=[cas].copy_threshold (256 MiB), writable=ref.writable)`: larger files are
  symlinked read-only from the CAS, writable inputs always copied (P1-04 R1); config files are
  written read-only; each input is re-hashed
  (file: blob digest, tree: manifest digest) against its content digest: the input id, or
  `resolve_output(id)` for a nondeterministic producer output (I10).
- Env (I11), later layers winning: toolchain env (`$HOME` replaced) → declared env → runtime
  env (expanded with the runner's vars) → `HOME`, `TMPDIR`, `EBS_CPUS` (`resources.cpus`, else
  1), `EBS_ACTION_ID`, `EBS_SCRATCH`. `PATH` defaults to `/usr/bin:/bin` if no layer sets it.
  Caller variables matching `[runner].passthrough_env` (default `LM_LICENSE_FILE`,
  `*_LICENSE_FILE`, `SLURM_*`; case-sensitive globs) only fill names no layer set.
- The tool runs in its own session with stdin closed; stdout+stderr go to `logs/tool.log`,
  capped at `[runner].max_log` (default 2 GiB) keeping the tail after a
  `[ebs-runner: N bytes of log dropped …]` line. Timeout `resources.time`: SIGTERM to the group,
  SIGKILL after `[runner].kill_grace_s` (default 30). Leftover group members are killed when
  the tool exits. Usage from `wait4` rusage: `max_rss_kb`, `cpu_s` (user+sys), `wall_s`.
- Classification: an exec failure (program not found: treated as a node/toolchain install
  problem, never cached as FAILED), a log write error (scratch disk full), a timeout (even if
  the tool then exits 0) or the rule's `INFRA(reason)` ⇒ exit 75 with an
  `infra_failed` event `data = {reason: InfraReason, detail, exit_code, log}` (rule reasons
  other than `license`/`timeout` map to `other`, kept in `detail`); the log is uploaded, no
  result or cache entry. Any other exit-75 failure (CAS or metadata unreachable, input bytes
  missing, scratch not creatable, SIGTERM/SIGINT ⇒ `preempted`; repeated signals are ignored
  during cleanup) emits the same event when it can. Outputs that are symlinks count as missing. Otherwise outputs
  are uploaded; a missing required output (or one of the wrong type) makes the result `failed`
  with `summary.missing_output = "<name>[,<name>…]"`; `record_result`, then `cache_put` if the
  build's cache mode is `write`.

P1-04 (grid) details:

- Before the tool starts the runner emits one `running` event, `data = {host, job_id}`
  (`job_id` = `$SLURM_JOB_ID` or null). With `--build` it holds its own GC lease (P1-09) on
  the content digests it staged: `acquire_lease(domain, build, digests, LEASE_TTL_S)` on the
  first heartbeat, then `renew_lease` every `[runner].heartbeat_s` (60 s) while the tool runs,
  re-acquiring if the lease expired; it lapses after its TTL or at `finish_build`. Lease
  errors are logged, never fatal. (`Runner(..., renew_lease=…)` replaces this for tests.) `run_tool` takes `tee`
  (every output chunk) and `on_poll` (each wait-loop turn, ≤ 0.25 s) hooks.
- Debug area (`[debug].root`; none if unset): `<root>/<domain>/<build|local>/<action>/` with
  `<action>` = `ebs.runner.collect.debug_action_dir` (the action id, `/` percent-encoded).
  The domain dir is the admin's; dirs below it are created mode 2750 and files 0640 in the
  domain dir's group, whatever the umask (R6). If the area cannot be set up the runner warns
  on stderr and writes nothing there; the result is unaffected.
- `live.log` (D2): the tool's output appended while it runs, flushed every 5 s (sooner past
  1 MiB buffered), at most 256 MiB then a note; removed after a passed run unless
  `debug.on_success`.
- Debug collection on failed/infra (always) or passed (`debug.on_success`): `command.json`
  `{action_id, key, argv, env, inputs: [{path, kind, id}], host, slurm_job_id}`, `tool.log`,
  and the `debug.collect` matches below `work/` copied to `work/<path>` (a matched dir gives
  all its files; symlinks, staged inputs, unreadable files and patterns with `..` are
  skipped). The whole dir, `live.log` included, stays ≤ `debug.max_size` (else
  `[debug].max_size`, 2 GiB): `live.log` is dropped if over half of it (`tool.log` has the
  same output), `command.json` is always whole, the largest files are cut to their tail
  after a `TRUNCATED_NOTE` line; a retry replaces the earlier attempt's files and restarts
  `live.log`. After SIGTERM only `command.json` and the log tail
  (≤ 16 MiB) are kept, so cleanup fits in SLURM's KillWait.
- A SIGTERM that lands during debug collection or scratch removal does not stop the removal
  (I16); a job killed with SIGKILL is cleaned by the epilog (deploy/slurm/README.md).
- Shared with `ebs reproduce` (P1-10; `ebs.runner.stage`, `ebs.runner.env`):
  `resolve_content(plan, spec, ref, store) -> Digest | None` (the bytes to stage for one input;
  None = a nondeterministic producer's result is not recorded), `prepare_work(spec, cas, work,
  contents, *, copy_threshold)` (stage, write config files, verify: I10),
  `toolchain_env(plan, spec)`, `remove_tree(path)`. The runner itself goes through them.

## 10. Driver events — `ebs.driver.events`

The `Event` model is defined in `ebs.meta.api` (§ 6) and re-exported here.

`Event` = `{v, ts, build, type, action_id?, data}` with types: `build_started`, `plan_ready`,
`cache_hit`, `submitted`, `pending`, `running`, `finished`, `infra_failed`, `retrying`,
`build_finished`, `stat_audit_mismatch`, `build_resumed` (P2-08) (the runner also emits `running {host, job_id}` once per
attempt, § 9). Emitted to MetadataStore and to a local JSONL file
(`<workdir>/.ebs/builds/<uuid>/events.jsonl`, written before the store so an outage leaves a
trace) used by `ebs status` / `ebs logs` when offline; `read_events(path)` parses it. Data (no
floats): `submitted {job_id, attempt, batch_size}`, `pending {reason}`, `running {job_id}`,
`cache_hit {key, status}`, `infra_failed {reason, attempt, exit_code?, detail?, node?,
excluded_node?}`, `retrying {attempt, delay_ms}`, `finished {state ∈
done|failed|infra_failed|skipped|cancelled, exit_code?, attempts?, reasons?, reason?, hint?}`,
`build_finished {status, counts}`, `build_started {uuid, user, cache_mode, ci?}` (`ci`: the
`run_build(ci=…)` metadata, P2-07 R1), `build_resumed {attached, rerun, finished}` (P2-08: a new
driver took over the build: jobs re-attached, actions run again, actions already final). P1-05
R2: `attempt` numbers the submissions of an action
(1, 2, …) in `submitted` and the driver's `infra_failed`; once any attempt failed for an
infrastructure reason, the action's `finished` event has `attempts` (submissions) and
`reasons` (one per failed attempt, in order). `attempt_history(events) -> {action_id:
[Attempt(attempt, job_id, node, reason)]}` rebuilds them (`ebs status`). The runner's own
`infra_failed` event has no `attempt`.

Driver API (P0-15; used by the CLI, P0-16, and retry tuning, P1-05):

```python
def run_build(plan: Plan, *, refiner: Refiner, cas: CAS, store: MetadataStore, executor: Executor,
              workdir: Path, user: str, config: DriverConfig | None = None, ci_job: str | None = None,
              ci: Mapping[str, str] | None = None,   # P2-07: CI job metadata -> build_started.ci
              clock: Clock | None = None, retry: RetryPolicy | None = None,
              handle_sigint: bool = False, handle_sigterm: bool = False,
              owner: str | None = None) -> BuildOutcome: ...   # = start_build + drive_build
# BuildOutcome(build, uuid, status, exit_code, states: {action_id: final state}, plan (refined), events_path)
# P2-08: start_build(plan, *, cas, store, workdir, user, cache_mode="read", ci_job=None, clock=None)
#   -> StartedBuild(build, uuid, plan_digest): build record + queued actions + build_started/plan_ready.
# drive_build(plan, started, *, refiner, cas, store, executor, workdir, config, clock, retry,
#   handle_sigint, handle_sigterm, owner, restore=None) -> BuildOutcome: claims the driver lease
#   (DriverBusyError if held), runs the scheduler; SIGINT/SIGTERM cancel the jobs (exit 130).
# ebs.driver.resume.resume_build(build, *, cas, store, executor, workdir, config=None, clock=None,
#   retry=None, owner=None, refiner=None, handle_sigint=False, handle_sigterm=False) -> BuildOutcome:
#   plan from the CAS, the build's own cache mode; reconcile(rows, finished=, can_attach=) keeps
#   final states, re-attaches pending/running jobs with a job id (`Reattaching.adopt`), reruns
#   the others and infra_failed actions awaiting a retry (and refuses, DriverBusyError, when
#   SLURM jobs run but the executor cannot adopt them). Scheduler.restore(states, results,
#   attached) loads that state before `run` (restored outputs join the GC lease).
# ebs.plan.planner.refine_plan(plan, produced) -> Plan: what Planner.refine does, without a planner.

@dataclass(frozen=True)
class DriverConfig:                 # ebs.driver.scheduler
    cache_mode: CacheMode = "read"; rerun_failed: bool = False; keep_going: bool = False
    max_retries: int = 2; max_batch: int = 1000; poll_min_s: float = 0.5; poll_max_s: float = 10.0

class RetryPolicy(Protocol):        # ebs.driver.retry
    def decide(self, spec: ActionSpec, attempt: int, reason: InfraReason,
               earlier: tuple[InfraReason, ...] = ()) -> RetryDecision | None: ...
    # earlier (P1-05): reasons of the action's previous failed attempts, oldest first
RetryDecision(delay_s: float, resources: Resources | None = None, avoid_node: bool = False)
    # resources: e.g. more mem after OOM; avoid_node: the next attempt excludes the failed node
BackoffRetry(max_retries=2, base_s=10.0, factor=2.0, max_s=300.0)   # run_build's default
RetrySettings(max_retries=2, backoff_s=10.0, backoff_factor=2.0, backoff_max_s=300.0,
              oom_mem_factor=1.5, max_mem=None, license_backoff_s=60.0, retry_on_timeout=False,
              input_verification_retries=1)    # .from_config(config): `[retry]`, ConfigError
ReasonRetry(settings=RetrySettings())   # P1-05 R1, used by `ebs build`; see ebs.driver.retry
grown_memory(mem: int, factor: float, cap: int | None) -> int | None
CachePolicy(mode, rerun_failed=False)   # .reads, .writes, .accept(result)

# ebs.driver.resources_tuning (P1-05 R4): hints, never enforcement
usage_path(workdir) -> Path                     # <workdir>/.ebs/usage.jsonl
UsageSample(step, max_rss_kb, cpu_s, wall_s); UsageLog(path, *, max_samples=200, max_lines=20000)
    .record(samples) / .samples() / .by_step() -> {step: [max_rss_kb, …]}   # most recent last
record_usage(log, samples)          # best effort (OSError is logged); Scheduler.usage holds them
p95(values) -> int                                           # nearest rank
memory_hints(plan, usage, *, factor=4.0, min_samples=5) -> list[MemoryHint]
MemoryHint(step, requested, p95, samples, suggested)         # bytes; .message(), .to_json()
```

`ReasonRetry` (P1-05 R1): every reason is bounded by `max_retries`; `oom` ⇒ memory ×
`oom_mem_factor` rounded up to a MiB, capped at `max_mem` (no request, or at the cap: final);
`timeout` ⇒ final unless `resources.retry_on_timeout` (else `[retry].retry_on_timeout`) is true;
`node_fail`/`preempted`/`runner_crash`/`other` ⇒ same resources after the backoff; `license` ⇒
`license_backoff_s` × attempt; `input_verification` ⇒ `input_verification_retries` times
(counted over `earlier` input_verification failures, whatever came before), each
avoiding the failed node, then final with a `hint` in its `finished` event. A test failure
(`failed` result) never reaches the policy (I12). The scheduler excludes a node from every later
submission of the build once it had `NODE_FAIL_LIMIT` (2) `node_fail`s, of any actions.

Driver rules: an action is looked up in the cache and, on a miss, submitted only once all its
producers succeeded and its key is known; the refined plan is stored in the CAS before each
submit (the runner needs keys). Batches group one step with identical resources, licenses and
nodes to avoid. After the build is finished, `run_build` appends the usage of the actions that
ran and passed (collected by the scheduler from their manifests) to `.ebs/usage.jsonl` (P1-05 R4).
One `poll` per loop iteration; the wait doubles from `poll_min_s` to `poll_max_s` while nothing
changes and also ends when a retry is due. Executor results: `done` ⇒ `get_result` (none posted
⇒ infra `runner_crash`); `infra_failed` ⇒ retry policy; `cancelled` not requested by the driver,
or a job missing from 10 polls in a row ⇒ infra `other`. A waiting retry stays `infra_failed`
until resubmitted; if the build stops it is abandoned in that state. Ctrl-C (`handle_sigint`)
cancels submitted work, keeps results of jobs that already finished, marks the rest
`cancelled`, and restores the previous handler so a second Ctrl-C interrupts at once.

## 11. Errors and CLI exit codes — `ebs.core.errors`

`EbsError` → `ConfigError`, `FlowError` (has `file`, `line`, `col`), `DigestError`, `CanonError`,
`TreeError`, `PlanError`, `CasError`, `MetadataError`, `ExecutorError`, `RuleError`, `SandboxError`,
`SourceError` (→ `SourceEscapeError`), `ToolchainError`, `ReleaseError` (P2-02: a broken release,
channel or export rule; exit 2), `DriverBusyError` (P2-08: the build has another live driver, or
is finished; exit 2).
CLI exit codes: `0` ok, `1` build finished with failed actions, `2` usage/flow error,
`3` infrastructure error (retries exhausted), `4` internal bug (with "please report" hint),
`130` cancelled by the user (Ctrl-C; 128 + SIGINT, P0-15).
Build status → exit code (`ebs.driver.build.EXIT_CODES`, P0-15): `passed` 0, `failed` 1,
`infra_failed` 3, `cancelled` 130. `cancelled` beats
`infra_failed`, which beats `failed`; `infra_failed` only when some action really used up its
retries (an infra failure seen while the build was already stopping counts as `failed`).

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
    @property
    def rehash(self) -> bool: ...                                   # strict mode (P0-08)
    def snapshot(self, base: Path, pattern: str, *, optional: bool = False) -> SnapshotResult: ...
    def snapshot_file(self, path: Path) -> Digest: ...              # follows symlinks
    audit_mismatches: list[Path]

class StatCache:                # sqlite (WAL); default_statcache_path(uid) -> /var/tmp/ebs-<uid>/statcache.sqlite (P0-16)
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
- Location (P0-16 R7, human decision 2026-10-05): `[stat_cache].path` if set, else
  `default_statcache_path(os.getuid())`, never `~/.cache`. The CLI creates the default's
  directory with mode 0700 and uses it only if it is a directory owned by the user with no
  group/other bits (`/var/tmp` is shared). If that check or opening fails, the CLI warns and uses
  an in-memory stat cache (`StatCache(Path(":memory:"))`): every source is rehashed, the plan
  never fails because of the stat cache.
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
def fingerprint_entries(entries: Sequence[FingerprintEntry]) -> Digest: ...  # P1-07: digest of scan_roots output

# ebs.toolchain.modules (P1-07, L3)
@dataclass(frozen=True, slots=True)
class ModuleSystem: flavour: Literal["envmodules", "lmod"]; version: str; command: Path; home: Path
def detect_module_system(environ: Mapping[str, str], *, runner: EnvRunner = run_subprocess,
                         timeout_s: float = 30.0) -> ModuleSystem: ...
@dataclass(frozen=True, slots=True)
class ModuleSnapshot: module: str; version: str; env: Mapping[str, str]; install_roots: tuple[Path, ...]; system: ModuleSystem
def snapshot_module(module: str, *, system: ModuleSystem, environ: Mapping[str, str], user: str,
                    extra_roots: Sequence[Path] = (), timeout_s: float = 120.0,
                    runner: EnvRunner = run_subprocess) -> ModuleSnapshot: ...
def derive_install_roots(env, *, modulepath=(), extra=()) -> tuple[Path, ...]: ...

# ebs.toolchain.registry (P1-07, L3)
def register(store: MetadataStore, *, module: str, version: str, env: Mapping[str, str],
             install_roots: Sequence[Path], user: str, content_hash=default_content_hash) -> Registration: ...
    # Registration: toolchain, status (registered|unchanged|reinstated), previous, superseded,
    # changes (first DRIFT_LIST_LIMIT=20 FileChange by path), changes_total, env_changes
def format_drift(reg: Registration) -> str | None: ...
def check(store: MetadataStore, *, days: int, now: datetime, content_hash=...) -> list[CheckResult]: ...
class RegistryToolchainResolver:  # ToolchainResolver over the store
    def __init__(self, store: MetadataStore) -> None: ...
    def resolve(self, name: str, module: str) -> Toolchain: ...   # current id; touches last_used_at
    def get(self, toolchain_id: Digest, *, name: str) -> Toolchain: ...  # any id, superseded too
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
- **Module snapshots (P1-07)**: Environment Modules ≥ 4 (`$MODULESHOME/libexec/modulecmd.tcl`)
  or Lmod (`$LMOD_CMD`, else `$MODULESHOME/libexec/lmod`), version from `<cmd> bash --version`.
  `capture_env` runs `source <pkg>/module_load.bash <cmd> <module>`, which evals the output of
  `<cmd> bash purge` then `<cmd> bash load <module>` (module name only ever an argument), with
  `base_env = {MODULESHOME, MODULEPATH}` from the caller. The module must be `<name>/<version>`.
  The recorded env is the **diff** against the same clean shell without the load (unset
  variables are dropped), minus site variables matching `[runner]`'s default passthrough
  (`LM_LICENSE_FILE`, `*_LICENSE_FILE`, `SLURM_*`: never in a key, the runner passes the
  caller's). The caller's real HOME (and its realpath) is normalized to `$HOME` where a `:`-path
  starts with it; a path element naming the registering user (as a whole word) is refused unless
  it lies inside an install root (a service account owning the tool tree: warning only), so ids
  are user independent. Install roots:
  existing absolute entries (split on `:`) of changed variables, a file by its directory, a
  `bin`/`lib`/`share`/`etc`… directory by its parent, skipping module bookkeeping variables
  (`MODULEPATH`, `LOADEDMODULES`, `_LMFILES_`, `__LMOD*`, `_ModuleTable*`, `LMOD_*`,
  `__MODULES_*`, `MODULES_*`), system dirs and modulefile dirs; plus `--root`s; nested roots
  collapse into their ancestor.
- **Registry (P1-07)**: the manifest is `zlib(concat(FingerprintEntry.line()))`; drift lists
  compare manifests by (root, path). Planning without `.ebs/toolchains.yaml` uses
  `RegistryToolchainResolver` when `[metadata].url` is set; the plan records the id, so old
  plans keep pointing at the old snapshot. `check` re-fingerprints current toolchains whose
  `last_used_at` (else `registered_at`) is within `days`, and never writes.

## 14. CLI — `ebs.cli` (P0-16)

Commands: `ebs plan`, `build`, `status`, `logs`, `outputs`, `fetch` (P1-13), `debug`,
`reproduce` (P1-10), `rules list`,
`seeds gen` (P1-11), `toolchain register|list|show|check` (P1-07); global `--debug` (tracebacks,
debug logs). Exit codes: § 11. Every command has `--json`; `NO_COLOR` disables colors. The
working directory holds `.ebs/` (config lookup, `builds/`, executor logs); `-f` names the flow
(default `./flow.yaml`). `ebs.cli._context.SiteServices` builds the CAS, metadata store,
executor, rules (`[rules]`) and stat cache from ebs.toml; tests pass a subclass as the Typer
context object.

**Errors.** One line on stderr: `ebs: error: <message>` (a `FlowError` message starts with
`file:line:col`); internal bugs: `ebs: internal error: …; … please report …`. With `--json`,
stdout also gets `{"error": {"type", "message", "file"?, "line"?, "col"?}, "exit_code"}`.
Usage/flow/config/plan/rule/source/toolchain/digest errors exit 2; metadata/CAS/executor/
sandbox errors 3; anything else 4.

**Build record** (`.ebs/builds/<uuid>/build.json`, next to the driver's `events.jsonl`), written
by `ebs build` on `build_started` and rewritten when the build ends:

```json
{"v": 1, "uuid": "…", "build": 7, "domain": "…", "project": "…", "flow": "/abs/flow.yaml",
 "plan": "sha256:… (as submitted)", "final_plan": "sha256:…|null (as refined, stored in CAS)",
 "status": "running|passed|failed|infra_failed|cancelled", "actions": {"<action_id>": "<step>"},
 "created_at": "…", "host": "<driver host>", "pid": 1234,
 "ci": {"project_path", "pipeline_id", "job_url", "commit"} | null}
```

`ci` (P2-07 R1) is set by `ebs build --ci` from `CI_PROJECT_PATH`, `CI_PIPELINE_ID`, `CI_JOB_URL`
and `CI_COMMIT_SHA` (only those present); the store's build gets `ci_job` = the job URL (else
"<project> pipeline <id>", at most 256 characters).

The build's status is that of its `build_finished` event, else the record's; a `running` build
whose driver (`host`, `pid`) is this host and no longer alive shows as `interrupted`, so
`status --watch` and `logs -f` end when `ebs build` was killed without a trace.

`ebs status` and `ebs logs` read it plus the events, so they work without the metadata store.
A BUILD argument is a build id, else a UUID or a unique UUID prefix; the default is the latest
build in the directory (for `plan`: the latest build of the same flow file). Action states
folded from events: `queued` (no event yet), `submitted`, `pending: <reason>`, `running`,
`infra_failed`, `retrying`, `cached`, then `finished.state`.

**`ebs plan --json`** (v1; keys in this order):

```json
{"v": 1, "flow": "<plan.json flow.path>", "domain": "…", "project": "…", "plan": "sha256:…",
 "cache": "available|unavailable", "baseline": {"build": "<uuid>|null"},
 "totals": {"actions": 3, "hit": 0, "miss": 1, "unknown": 2},
 "steps": [{"step": "gen", "actions": 1, "hit": 0, "miss": 1, "unknown": 0}],
 "actions": [{"action_id": "…", "step": "…", "key": "sha256:…|null",
              "cache": "hit|miss|unknown", "cached_status": "passed|failed|null",
              "change": {"status": "added|changed|unchanged|unknown|null", "fields": […],
                         "pending": ["depends on <action_id>"]}}],
 "removed": ["<action_id>"], "warnings": ["…"],
 "hints": [{"step", "requested_mem": "16G", "p95_mem": "1G", "samples": 20,
            "suggested_mem": "1536M", "message": "…"}]}
```

`hints` (P1-05 R4) lists steps whose largest memory request is more than 4 × the p95 of the
peak memory of their recent successful runs (`.ebs/usage.jsonl`, written by `run_build`; at
least 5 samples). Text mode prints each as `hint: <message>`. Hints never change the plan.

Prediction mirrors the driver: keyed actions are looked up with `cache_get` (which counts a hit
and touches `last_access`); the output ids of passed hits refine the plan, repeated until no new
key appears. `cache` is `unknown` for actions still unkeyed, or for all without
`[metadata].url`. `change` comes from `diff_plans(baseline final plan, predicted plan)`;
`status` is null without a baseline.

**`ebs build --json`**: one line per driver event (`Event` JSON), then
`{"build", "uuid", "status", "exit_code", "counts": {state: n}}`. Text mode prints
`build <uuid> (id <n>)` first, then one line per event (non-TTY) or a live per-step table (TTY),
then `build <uuid>: build <status> (<state>=<n>, …)`.

**`ebs build --ci`** (P2-07; `ebs.ci`): implies `--cache write` (another `--cache` or
`--no-cache` exits 2), `--rehash`, line output even on a TTY, and build kind `ci` for the
executor (`SiteServices.open_executor(…, project=, build_kind=)`, the CI QOS of P1-03). With an
HTTP `[metadata].url` it needs a bearer token (`$EBS_META_TOKEN` or `[metadata].token_file`;
exit 2 otherwise). After the build it writes, into `$CI_PROJECT_DIR` (else the working
directory): `ebs-junit.xml` (`ebs.ci.junit`: one testcase per action with a `test` param,
`classname` = step, `name` = `test[seed]`, failure message = summary `signature` |
`first_error` | exit code, `<system-out>` = last 16 KiB of the log (redacted before the cut), cached results carry
property `ebs.cached=true`; infra failures are `<error>`, never-run actions `<skipped>`),
`ebs-summary.md` (link from `[ci].build_url`, a template with `{build}`/`{uuid}`; per-step
counts; up to 20 failed tests) and `ebs.env` (`EBS_BUILD_ID=<id>`, a GitLab dotenv report).
The values of `EBS_META_TOKEN`, `CI_JOB_TOKEN`, `CI_REGISTRY_PASSWORD`, `CI_DEPLOY_PASSWORD`,
`CI_JOB_JWT`, `CI_JOB_JWT_V2` are replaced by `[MASKED]` in those files.

**`ebs build --detach`** (P2-08): plans and starts the build (record written with `host: ""`,
`pid: 0`), then submits `ebs build --resume-into <uuid> --executor <x> [-k] [--rerun-failed]`
as a SLURM job (`ebs:<uuid8>:driver`, 1 CPU, `[driver]` partition/QOS/time, else
`[slurm].default_partition` and the build kind's QOS; output `.ebs/builds/<uuid>/driver.out`,
script and 0600 env file next to it) and exits 0 after printing the build line and
`driver job <id> submitted: …` (`--json`: `{"build", "uuid", "status": "running", "driver_job"}`).
It needs `--executor slurm` and does not combine with `--ci` (usage errors otherwise). If the submission fails the build is
finished `infra_failed`. A driver job cancelled before it started leaves the build `running`
without a driver: `ebs build --resume BUILD` takes it over. **`ebs build --resume BUILD`**
drives a running build whose driver died (`--resume-into` is the same, used by the driver job);
it takes no steps, `--cache`, `--no-cache` or `--rehash` (the build's plan and cache mode
apply), rewrites the record's `host`/`pid`, and prints `build <uuid> (id <n>): resuming`.
`--executor slurm` (P2-08) runs actions as SLURM jobs (`[slurm]`, `[debug].root`).

**`ebs status --json`**: `{"v": 1, "uuid", "build", "status", "flow", "created_at", "steps":
[{"step", "actions", "states": {state: n}}], "totals": {state: n}, "retries": [{"action_id",
"state", "attempts": [{"attempt", "job_id", "node", "reason"}]}]}`. `retries` (P1-05 R2) lists the
actions with at least one infrastructure failure, with every attempt (`reason` null for one that
did not fail that way); text mode prints `<action>: <state> after <n> attempts (<reason> on
<node>, …)` under `infrastructure failures:`. `--watch` redraws every `--interval` seconds until
the build is no longer `running`.

**`ebs logs ACTION`**: ACTION is an action id or a unique prefix. Finished actions: the log blob
of `get_result(build, action)` (or of the last `infra_failed` event's `log`), read from the CAS.
Running actions (local executor): the runner's live files in
`<[scratch].dir>/<build id>/<action dir>/logs/` — `tool.log.cur` (opened unbuffered), renamed to
`tool.log.prev` at the `[runner].max_log` cap, joined into `tool.log` when the tool ends.
`-f` keeps `.cur` open (renames do not disturb it), switches to a new `.cur` after reading the
old one to its end (rotation; or a retry, marked by a separator line), and stops when the
action is final or the build has ended; an action that wrote nothing while followed gets its
stored log. Limits at the size cap: `ebs.cli.logs` docstring. `--json`: `{"action_id", "build",
"uuid", "state", "log"}`; `-f` with `--json` is a usage error.

**`ebs outputs` / `ebs fetch`** (P1-13; `ebs.cli.outputs`):

```
ebs outputs [ACTION…] [--build B] [--step S] [--json]
ebs fetch   ACTION[.OUTPUT]… [--build B] [-o|--output-dir DIR] [--flat] [--mode copy|hardlink|symlink] [--force] [--json]
```

Both need `[metadata].url` and only read (no cache, store, build record or CAS writes); a
successful `fetch` only `touch`es what it copied, so GC sees the use (P1-09 R1). `outputs` lists one row per output of every action of the build with a
result (`get_result`: done, cached and test-failed actions), in plan order then by output name;
ACTION arguments (`match_action`: id or unique prefix) and `--step` filter. The declared
logical `path` comes from the build's `final_plan` (else `plan`) in the CAS, matched by output
name; an output the plan does not declare has `path: null` (`-` in the table). `--json` (v1):

```json
{"v": 1, "uuid": "…", "build": 7, "domain": "…",
 "outputs": [{"action_id": "…", "step": "…", "state": "done|cached|failed",
              "name": "report", "path": "lint/reports/alu.rpt|null", "type": "file|tree",
              "size": 123, "digest": "sha256:…", "id": "sha256:…"}]}
```

`fetch` materializes each output (`CAS.materialize`, `--mode`, default `copy`; files read-only)
at `DIR/<action dir>/<path>` (default `DIR` = `./ebs-out`; `--flat`: `DIR/<path>`; an output
without a declared path uses its name). `<action dir>` is the action id with `%` → `%25` and
`/` → `%2F` (`.`/`..` fully encoded). An argument is an exact action id, else a step name (all
its actions), else a unique prefix; only if none matches is it split at its last `.` into
ACTION and OUTPUT. Before anything is written, every destination is checked: inside `DIR` after
normalization and through no symlink leaving it, no two the same (with `--flat`: "drop
--flat"), not existing unless `--force` (removes it first), and every object present in the
CAS. Errors (exit 2): unknown/ambiguous action, unknown output (lists the action's outputs),
no result yet (states the action's state), no `[metadata].url`; an object missing from the CAS
exits 3 ("may have been garbage-collected; `ebs build --no-cache <step>`"). Text: one line per
output, `<action_id>.<name> -> <dest>`. `--json`: `{"v": 1, "uuid", "build", "fetched":
[{"action_id", "name", "digest", "dest"}]}` (`dest` as printed: `DIR` as given).

**`ebs debug` / `ebs reproduce`** (P1-10; `ebs.cli.debug`, `ebs.cli.reproduce`,
`ebs.runner.reproduce`):

```
ebs debug     ACTION [--build B] [--json]
ebs reproduce ACTION [--build B] [--dir D] [--run | --interactive] [--salloc] [--json]
```

Both read the build record and events, the build's `final_plan` (else `plan`) from the CAS and,
with `[metadata].url`, `get_result` and `resolve_output`; ACTION is an id or unique prefix. An
action whose input ids the recorded plan lacks takes them from its result's `inputs`.
`debug --json` (v1): `{"v": 1, "uuid", "build", "action_id", "step", "state", "reason",
"exit_code", "first_error", "signature", "key", "argv", "command" (shell-quoted), "env"
(declared only), "toolchain": null | {"name", "module", "id"}, "inputs": [{"path", "kind", "id",
"content", "origin": {"type": "source", "input", "pattern"} | {"type": "action", "action_id",
"output"} | {"type": "import", "name", "path"}}], "slurm": {"job_id", "node"}, "attempts":
[{"attempt", "job_id", "node", "reason"}], "debug_dir", "log"}`. `first_error`/`signature` come
from the result's `summary`; `slurm` from the result's `runner` (else the last attempt);
`debug_dir` is the action's debug dir if it exists. Store fields are null without a store.

`reproduce` builds the sandbox with the runner's own code (`resolve_content`, `prepare_work`,
`toolchain_env`, `build_env`; § 9) in `--dir D` (new or empty, else exit 2) or a new
directory `<[scratch].dir>/repro-<uid>/<action dir>-<random>` (0700; an earlier sandbox is
never reused or removed), with the scratch layout `{work,home,tmp,logs}` plus `repro.json`
`{v, kind: "repro", cache_mode: "off", build, build_id, plan, action_id, key}`, and keeps it.
Under SLURM (`$SLURM_JOB_ID` set, e.g. the inner command of `--salloc`) the default directory
gets the `.ebs-job` marker, so the node epilog removes it with the job (I16). Inputs whose bytes
are gone (not in the CAS, any blob or manifest of a tree missing, a nondeterministic input
without a recorded result, or an id unknown because its producer never ran) are all named
before anything is created, with a hint to `ebs build --no-cache`; exit 3. `--run` runs the command with `run_tool` (runner timeout and log cap),
classifies the exit like the runner (exec or log error, timeout, the rule's `classify`, a missing
required output ⇒ `failed`) and compares outputs with the result (R3): `same`,
`different`, `missing`, `not_compared` (`deterministic: false`), `not_recorded`. `--interactive`
runs `bash --norc` in `work/` with the sandbox env (+ `PS1`). `--salloc` (needs `--run` or
`--interactive`) runs `salloc --job-name=ebs:repro:<step> [--partition --account --qos
--cpus-per-task --mem=<MiB>M --time=<time + JOB_TIME_GRACE_S> --licenses --constraint] srun
--pty ebs reproduce ACTION --build <uuid> --run|--interactive [--dir D] [--json]` (options as the SLURM
executor would give the action alone; account/QOS for build kind `dev`), so the sandbox is
built on the node. Reproduce never writes to the metadata store: no build, result, cache entry,
event, lease or touch (R4). `--json`: `{"v": 1, "uuid", "build", "action_id", "dir", "work",
"argv", "env", "interactive", "ran": null | {"exit_code", "status", "timed_out", "error", "log",
"outputs": [{"name", "status", "recorded", "reproduced"}], "identical"}}`; with `--salloc`
the outer command prints the salloc line on stderr and the inner one the document. The exit
code is 0 once the sandbox is built, whatever the tool or the comparison gave.

**`ebs rules list --json`**: `{"v": 1, "rules": [{"kind", "version", "impl"}]}`, sorted by kind.

**`ebs seeds gen TABLE --json`**: `{"v": 1, "table": "<TABLE as given>", "appended": n,
"filled": n}`. Options `--per-test N`, `--replace`, `--column` (default `seed`),
`--test-column` (default `test`), `--base-seed` (default 0); algorithm and formatting rules:
`ebs.flow.seeds` docstring (task P1-11).

**`ebs toolchain`** (P1-07; needs `[metadata].url`). `register MODULE [--root DIR]… [--timeout S]`
snapshots and registers (stdout `registered|unchanged|reinstated MODULE` + id, fingerprint,
roots, env names, module system; a drift warning with the first 20 changed files on stderr);
`--json`: `{"v": 1, "status", "id", "module", "version", "fingerprint", "install_roots", "env",
"previous", "superseded", "drift": null | {"total", "files": [{"path", "change"}], "env"},
"module_system": {"flavour", "version"}}`. `list [--all]` (current only by default; `--json`:
`{"v": 1, "toolchains": [record]}`), `show MODULE|ID` (`{"v": 1, "toolchain": record}`).
`check [--days N=30]` re-fingerprints, changes nothing and exits 1 if any toolchain drifted or
could not be fingerprinted (`--json`: `{"v": 1, "days", "results": [{"id", "module", "status":
ok|drift|error, "fingerprint", "registered_fingerprint", "changes", "error"}]}`).

**`ebs domain list|check [--min-free PCT] [--json]`** (P2-01; `ebs.domains`): the `[[domains]]`
with group, roots, TTL, quota and your membership; `check` verifies `<cas>/<name>` (group, 2750),
its `blobs`/`trees`/`tmp` and `<debug>/<name>` (2770) and free space, exit 3 on a problem
(the layout `deploy/admin/create_domain.sh NAME GROUP` makes, idempotently). With `[[domains]]`
configured, `ebs plan`/`build` refuse a flow whose domain you are not in (its `unix_group`)
before anything is hashed (exit 2); `ebs gc` collects only `[gc].domain` and `[gc].domains`.
`SiteServices.cas(domain)` and the runner use the domain's `cas_root`/`debug_root`
(`RunnerSettings.cas_root_for/debug_root_for`).

**`ebs release create NAME VERSION --build B --output SEL… [--notes] [--allow-failed REASON]
[--json]`**, `release show|yank|export NAME VERSION [--domain D]` (`yank --reason R`, `export
--to D2`), `release list [NAME]`; **`ebs channel set NAME CHANNEL VERSION [--gate-build ID] [--if-passing]`**,
`channel show|history NAME CHANNEL` (P2-02; `ebs.release`). `--domain` defaults to the domain of
the latest build recorded here; B is a recorded build's UUID prefix or id, or any store build id.
A broken rule (selector, version, failed action, gate, membership) exits 2. `--if-passing` (P2-07
R4, a CI gate job): the gate build (`--gate-build`, else `$EBS_BUILD_ID`; neither exits 2) must
be in the release's domain and have `passed` (checked by the CLI), else exit 2 without moving; it
is checked against the channel's gate policy, and without one is recorded as the move's gate
(`{flow: <its project>, build}`) on a direct database. Through the HTTP service only `gate_build`
is sent; the stored gate is whatever the service's own policy decides.

**`ebs why TARGET [--domain D] [--build B] [--depth N] [--json | --dot]`**, **`ebs used-by NAME
VERSION`**, **`ebs tests NAME VERSION [--latest] [--junit FILE]`** (P2-04; `ebs.release.provenance`,
all with `--domain D`, `--depth N`, `--json`). TARGET: a digest (object id, content digest or
action key), `NAME@VERSION[:OUTPUT]`, or an action id of `--build` (default the latest build
here). `why` prints the upstream tree (action, rule, result, build, project@commit, toolchain,
params, inputs down to `(source)`), `--json` `lineage_doc` (v1: `truncated, graphs, links,
hidden, sources, target, domain`), `--dot` a Graphviz digraph; `used-by` lists builds and
releases; `tests` lists the runs (domain, build, test, seed, status, toolchain, finished, action),
`--json` `{v: 1, release, tests}`; `--junit` writes one JUnit testsuite (failed result: failure,
no result: skipped). An unknown release or action exits 2.
**`ebs lock update [ALIAS…] [-f FLOW] [--json]`**, **`ebs lock check [-f FLOW] [--json]`**
(P2-03; `ebs.release.lock`): `update` resolves the flow's imports with the metadata store,
prints one line per changed alias (`rtl: rtl-team/cpu 1 → 2 (channel stable); changed
pack.srcs`, `rtl: added …`, `rtl: removed (was …)`) and rewrites `flow.lock` next to the flow
when its text changes; with aliases only those move. `check` exits 1 when `update` would change
the file (content or layout, or no file), printing the same lines, and writes nothing. `--json`:
`{"changes": [{"alias", "change": "added|removed|changed", "name", "old_version", "new_version",
"outputs_changed", "outputs_added", "outputs_removed"}], "written"}`. `ebs plan`/`build` of a
flow with imports read `flow.lock` (missing or stale: exit 2); `build` records the
`release_imports` rows.

**`ebs gc [--dry-run] [--domain D] [--json]`** (P1-09; `ebs.gc`): collects one domain (default
`[gc].domain`) as the GC service account; needs `[metadata].url` = a `postgresql` URL (direct
database access, `SiteServices.gc_store()`), else exit 2. Prints the report (`ebs.gc.report.render`);
`--json`: `GcReport` (v1: `domain, dry_run, started_at, objects, bytes, marked, kept, adopted,
cache_rows_deleted, tombstoned, tombstoned_bytes, resurrected, deleted, deleted_bytes, tmp_removed,
quota: null | {quota_bytes, usage_bytes, pending_bytes, high_water_bytes, low_water_bytes, evicted,
evicted_bytes}, reclaimable: {objects, bytes, by_step, by_project, by_build}`), also stored in
`gc_runs.report`. `--dry-run` changes nothing (stale tmp files are only counted) and reports what
the real run would do. `ebs pin BUILD [--reason R]` / `ebs unpin BUILD` (BUILD: a UUID prefix or
id recorded here, or any numeric build id) call `pin_build` (by = `$USER`) / `unpin_build`.
