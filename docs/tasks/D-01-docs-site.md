# D-01 — Documentation site

Status: review · Phase: docs · Depends on: P0-17 · Parallel-safe with: every P2/P3 task · Size: L

## Goal
A public documentation site (`site/`, Astro + Starlight, deployed to GitHub Pages) with a
landing page, tutorials, concept pages with animations, integrations, an operations guide and a
generated reference. It stays in sync with the repository because facts are generated from the
code or embedded from real files, and tutorials are executed in CI.

## Read first
- docs/user/quickstart.md (moved by this task) and tests/e2e/test_docs.py
- docs/design/interfaces.md § 7 (rules), § 13 (toolchains), § 14 (CLI)
- docs/design/overview.md § Site configuration

## Scope (files)
- create `site/` (Astro project: config, styles, components, scenes, content)
- create `scripts/gen_site_reference.py`, `scripts/site_bench.py`
- create `examples/rule-plugin/` (installable example `verilator.lint` rule) and
  `examples/custom-toolchain/` (a tool tree declared as a toolchain; executed tutorial)
- modify `src/ebs/cli/main.py`: enable Typer's completion options (CONTRACT CHANGE, § 14)
- create `integrations/` (VS Code, JetBrains, Neovim, CI, Make)
- create `.github/workflows/site.yml`
- modify `tests/e2e/test_docs.py` (run every tutorial page), `docs/user/quickstart.md` (pointer),
  `.github/workflows/ci.yml` (path filter), `Makefile` (site targets), `pyproject.toml`
  (mypy/ruff paths for the example plugin), `docs/design/overview.md` (§ Dependencies),
  `.gitignore`

## Out of scope
- Documenting features whose task is not done (sandbox, S3, phase 3). The generated roadmap
  lists them. (P2-02…P2-08 are documented since 2026-10-18: imports and `flow.lock`, provenance,
  `--ci`/JUnit, `--detach`/`--resume`, the dashboard.)
- Versioned docs (one "latest" tracking `main` until 1.0).

## Requirements
- R1 Every `console` block of every page under `site/src/content/docs/{start,tutorials}/` is
  executed in CI against a fresh site and a fresh copy of `examples/`.
- R2 Every `yaml` block whose title ends in `flow.yaml` in the site content validates as a flow
  (JSON Schema and the flow model).
- R3 The generated CLI reference documents every command of the Typer app, with its options;
  adding a command without regenerating is impossible (the reference is generated at build).
- R4 The generated API reference lists every name in `ebs.rules.api.__all__` and the public
  toolchain API, with signatures and docstrings, and embeds interfaces.md § 7 and § 13.
- R5 The generated roadmap reflects the statuses in docs/tasks/README.md.
- R6 The example rule plugin implements `RulePlugin`, passes `RuleRegistry` checks, and its
  argv, classification and summary are tested.
- R7 The shipped integration files parse, and every `ebs` subcommand they invoke exists.
- R8 `scripts/site_bench.py` measures the examples (cold, warm, one-file change) and writes
  action counts and timings in a documented JSON shape the landing page reads.
- R9 The site builds with zero `astro check` errors and no broken internal links.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `tests/e2e/test_docs.py::test_page_blocks` | R1 | integration |
| `tests/unit/docs/test_site_snippets.py::test_flow_snippets_validate` | R2 | unit |
| `tests/unit/scripts/test_gen_site_reference.py::test_cli_reference_covers_every_command` | R3 | unit |
| `tests/unit/scripts/test_gen_site_reference.py::test_api_reference_lists_rule_api` | R4 | unit |
| `tests/unit/scripts/test_gen_site_reference.py::test_roadmap_statuses` | R5 | unit |
| `tests/unit/examples/test_rule_plugin.py` | R6 | unit |
| `tests/unit/docs/test_integrations.py` | R7 | unit |
| `tests/unit/scripts/test_site_bench.py` | R8 | unit |
| `tests/e2e/test_site_bench.py::test_parse_build_on_real_output` | R8 | integration |
| `make site-build` in `.github/workflows/site.yml` | R9 | CI |

## Done when
- [ ] `make check` passes (paste the summary line)
- [ ] every R has a test; reviewer subagent reports no correctness gaps
- [ ] Status set to `review` here and in docs/tasks/README.md

## Notes
- Node dependencies live only in `site/package.json` (docs site, not shipped):
  `astro`, `@astrojs/starlight` (docs framework, search, sidebar),
  `animejs` (scene animations), `starlight-links-validator` (R9), `@astrojs/check` and
  `typescript` (R9). No Python dependency is added.
- The repository admin must set Settings → Pages → Source to "GitHub Actions" once. **Done
  (2026-10-18).**
