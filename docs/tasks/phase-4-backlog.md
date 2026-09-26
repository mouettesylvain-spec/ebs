# Phase 4 backlog (outlines)

Expand each into a full task file (see `_TEMPLATE.md`) before starting. All follow the rule-pack pattern of
P1-08: argv golden tests, fake tools in `tests/fakes/bin/`, transcript/report parsers with synthetic fixtures,
`classify` (license ⇒ INFRA), and `vendor`-marked tests on the grid.

| ID | Outline | Key tests to plan |
| --- | --- | --- |
| P4-01 | `vcs.compile` / `vcs.sim` rule pack (vlogan/vcs/simv, `-ntb_opts uvm`, `+ntb_random_seed`) | argv golden; simv transcript parsing; nondeterministic `simv.daidir` |
| P4-02 | `spyglass` rule pack (VC SpyGlass lint/CDC goals), normalized violation reports enabling early cutoff | report normalization property tests; lint comparator reuse (P3-05) |
| P4-03 | `dc` / `fusion_compiler` synthesis rule pack; 100+ GB outputs as trees; QoR still out of scope | large-output handling via materialize thresholds; Tcl params file reuse (P0-12) |
| P4-04 | `primetime` STA rule pack; per-corner matrix | corner matrix expansion; report parsing |
| P4-05 | Formality equivalence as a revalidation comparator for netlists | comparator verdict mapping |
| P4-06 | Content-defined chunking for large blobs (FastCDC; restic/casync approach), chunk manifests in CAS, transparent to `CAS` users | dedup ratio test on synthetic 1 GB files with small edits; I8/I9 extended to chunks |
| P4-07 | LSF executor (`bsub`/`bjobs -json`), FakeLSF, contract suite | executor contract; license (`rusage`) mapping |
