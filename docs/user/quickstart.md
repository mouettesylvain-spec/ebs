# Quickstart

This walk-through runs the two examples in `examples/` on your machine with the local executor.
They need no EDA tool or license: each ships a small fake tool written in bash and awk, and each
also has a `flow.verilator.yaml` for real [Verilator](https://verilator.org).

- `examples/lint-make`: an existing lint Makefile, wrapped as-is with `kind: make`, one action per
  RTL block, plus a `summary` step that merges the reports.
- `examples/verilator-sim`: compile a model once, simulate a regression table (one action per test
  and seed), aggregate the results.

The commands in the `console` blocks below are run by `tests/e2e/test_docs.py` in CI (ready pull
requests and `main`), so they stay correct. The `sed -i` edits assume GNU sed (Linux). Lines with `…` stand for text that changes from run to run.

## 1. Set up

You need Python 3.11+, [uv](https://docs.astral.sh/uv/) and a PostgreSQL 16+ database you can
create tables in (`deploy/postgres` will provide a production setup).

```sh
git clone <this repository> ebs && cd ebs
uv sync
source .venv/bin/activate     # puts `ebs` and `ebs-runner` on PATH
python -c 'from ebs.meta import migrations; migrations.upgrade("postgresql+psycopg://me@dbhost/ebs")'
```

Tell `ebs` where the metadata database and the content-addressed store (CAS) are. Put this in
`~/.config/ebs/config.toml` (or point `$EBS_CONFIG` at another file):

```toml
[metadata]
url = "postgresql+psycopg://me@dbhost/ebs"

[cas]
root = "/path/to/ebs-cas"      # shared by everyone who should reuse each other's results
```

## 2. Wrap a Makefile and build it twice

`examples/lint-make/lint/Makefile` is an ordinary Makefile. The flow runs it once per row of
`blocks.csv` (`make -C lint lint BLOCK=<block>`), and declares what it reads and writes:

```yaml
  lint:
    kind: make
    workdir: lint
    target: lint
    matrix: { table: blocks.csv }
    inputs:
      rtl: "rtl/${row.block}/*.sv"
      linter: tools/fake-lint
    params: { BLOCK: "${row.block}", LINTER: fake }
    outputs:
      report: "lint/reports/${row.block}.rpt"
```

`ebs plan` shows what would run, without running anything:

```console
$ cd examples/lint-make
$ ebs plan
plan … of flow.yaml: 3 actions, 0 hit, 2 miss, 1 unknown (no previous build)
```

`summary` is "unknown" because its key depends on the bytes of the reports, which do not exist
yet. Build it. `--cache write` stores the results in the shared cache; the default, `read`, only
reuses results, which is what personal sandboxes should do once CI fills the cache.

```console
$ ebs build --cache write
build … (id …)
submitted lint[block=alu] …
submitted lint[block=counter] …
submitted summary …
build …: build passed (done=3)
$ ebs logs summary
lint summary
alu: 0 violation(s)
counter: 0 violation(s)
0 violation(s)
```

Nothing changed, so the second build runs nothing: every action is a cache hit.

```console
$ ebs build --cache write
cache_hit lint[block=alu] …
cache_hit lint[block=counter] …
cache_hit summary …
build …: build passed (cached=3)
```

## 3. Change a comment: early cutoff

Edit the comment at the top of `rtl/alu/alu.sv`. The file's content changed, so `lint[block=alu]`
must run again, and `ebs plan` says why. Its report, though, comes out byte-identical (the linter
ignores comments and its report is normalized), so `summary` keeps its key and is not rerun:

```console
$ sed -i 's|// Combinational ALU: add, subtract, and, or.|// Combinational ALU (add/sub/and/or).|' rtl/alu/alu.sv
$ ebs plan
  lint[block=alu]: inputs["rtl/alu/alu.sv"]
$ ebs build --cache write
cache_hit lint[block=counter] …
submitted lint[block=alu] …
cache_hit summary …
build …: build passed (cached=2, done=1)
```

## 4. Change the code: only what is affected reruns

Add a signal nobody uses. The `counter` block is untouched and stays cached; the `alu` report now
differs, so `summary` reruns:

```console
$ sed -i 's|^  always_comb begin$|  logic spare_dbg;\n  always_comb begin|' rtl/alu/alu.sv
$ ebs build --cache write
cache_hit lint[block=counter] …
submitted lint[block=alu] …
submitted summary …
build …: build passed (cached=1, done=2)
$ ebs logs summary
alu: 1 violation(s)
  rtl/alu/alu.sv:10: UNUSED: signal 'spare_dbg' is never used
$ ebs status
build … (id …): passed, 3 actions
```

Sources are copied into the CAS when the build is planned, and actions run on that copy: an edit
you make while a build is running never leaks into it.

## 5. A regression table

`examples/verilator-sim/tests.csv` lists tests and seeds. Seeds are values in the table, never
generated on the fly, so every run of a row is reproducible. The compiled model is marked
`deterministic: false` (a real model embeds build details): the `sim` keys use the `compile`
action's key rather than the model's bytes, and stay stable.

```console
$ cd ../verilator-sim
$ ebs build --cache write
submitted compile …
submitted sim[seed=1,test=smoke] …
submitted sim[seed=7,test=random] …
submitted sim[seed=42,test=random] …
build …: build passed (done=5)
$ ebs logs report
3 passed, 0 failed
```

Changing one seed in `tests.csv` reruns that one simulation and the `report`, nothing else.

## 6. With real Verilator

`flow.verilator.yaml` runs the same steps with Verilator, declared as a **toolchain**: its
version, install tree and environment are part of every action key, so a result of the fake tool
is never reused for Verilator (or for another Verilator version). Until `ebs toolchain register`
exists (P1-07), describe it in `.ebs/toolchains.yaml` next to the flow:

```yaml
version: 1
toolchains:
  verilator/system:
    version: "5.020"                                  # verilator --version
    install_roots: ["/usr/share/verilator"]           # verilator --getenv VERILATOR_ROOT
    env: { PATH: "/usr/bin:/bin" }                    # not VERILATOR_ROOT: see below
```

```sh
ebs build --cache write -f flow.verilator.yaml
```

`verilator-sim` builds a C++ model (`verilator --binary`), so it also needs a C++ compiler.
Do not set `VERILATOR_ROOT` in `env` for a distro package: Debian and Ubuntu install
`verilator_bin` in `/usr/bin`, outside the root, and the `verilator` wrapper then fails with
exit code 127.

## What next

- `ebs plan --diff <build>` explains every difference from an earlier build.
- `ebs build -k` keeps going past failed actions; `ebs logs <action>` shows any action's log, by
  a unique prefix of its id.
- Flow syntax: `docs/architecture.md` § Flow description format.
