"""Revalidation of a release from its sign-off bundle (P3-05 R1, R3)."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from ebs.cas.api import CAS
from ebs.core.clock import FakeClock
from ebs.core.digest import Digest
from ebs.core.errors import ReleaseError, ToolchainError
from ebs.core.types import JsonValue
from ebs.driver.build import run_build
from ebs.driver.retry import ReasonRetry, RetrySettings
from ebs.driver.scheduler import DriverConfig
from ebs.meta.api import CacheMode, ToolchainCreate
from ebs.plan.planner import Planner
from ebs.plan.types import Plan
from ebs.release.archive import Bundle, archive_release, open_bundle
from ebs.release.revalidate import (
    BuildRunner,
    OverrideToolchains,
    RerunOutcome,
    Revalidation,
    RevalidationSettings,
    _Compared,
    _digest_compare,
    _recorded_results,
    _verdict,
    parse_overrides,
    revalidate,
)
from ebs.rules.comparators.api import Verdict
from ebs.rules.comparators.lint import LintComparator
from ebs.rules.comparators.sim import SimComparator
from ebs.toolchain.model import Toolchain
from tests.helpers.driver import Outcome, ScriptedExecutor
from tests.helpers.signoff import SignedOff, hermetic_git, signed_off

FINGERPRINT_2 = "sha256:" + "2" * 64


def _bundle(s: SignedOff, tmp_path: Path) -> Bundle:
    rec = archive_release(
        s.services.memory_store, s.cas, s.release, to=tmp_path / "cold", repo=s.site.proj,
        git=hermetic_git(), toolchains=s.services.toolchains(s.site.proj), by="alice",
    )  # fmt: skip
    return open_bundle(Path(rec.location), tmp_path / "open")


def _advance(s: SignedOff, seconds: float) -> None:
    clock = s.services.clock
    assert isinstance(clock, FakeClock)
    clock.advance(seconds)


def _register(s: SignedOff, module: str, fingerprint: str) -> Toolchain:
    from ebs.core.digest import Digest

    name, version = module.split("/")
    tc = Toolchain.from_parts(
        name=name, module=module, version=version, install_roots=[Path(f"/opt/{name}")],
        env={"FAKESIM_MODE": "batch"}, fingerprint=Digest.parse(fingerprint),
    )  # fmt: skip
    s.services.memory_store.register_toolchain(
        ToolchainCreate(
            id=tc.id, name=name, module=module, version=version, fingerprint=tc.fingerprint,
            env=dict(tc.env), install_roots=[f"/opt/{name}"], registered_by="admin",
        ),
        b"",
    )  # fmt: skip
    _advance(s, 60)
    return tc


class Runner:
    """Runs a plan as a build with the scripted executor, as `ebs release revalidate` runs it
    with the site's executor."""

    def __init__(self, s: SignedOff, scripts: Mapping[str, tuple[Any, ...]] = {}) -> None:
        self.s = s
        self.scripts = dict(scripts)
        self.modes: list[CacheMode] = []
        self.executor: ScriptedExecutor | None = None

    def __call__(
        self, plan: Plan, refiner: Planner, cas: CAS, cache_mode: CacheMode
    ) -> RerunOutcome:
        self.modes.append(cache_mode)
        store = self.s.services.memory_store
        self.executor = ScriptedExecutor(store, cas, record_jobs=True)
        for action_id, (outcomes, outputs) in self.scripts.items():
            self.executor.script(action_id, *outcomes, outputs=outputs)
        outcome = run_build(
            plan, refiner=refiner, cas=cas, store=store, executor=self.executor,
            workdir=self.s.site.proj, user="ebs-revalidate",
            config=DriverConfig(cache_mode=cache_mode, keep_going=True),
            clock=self.s.services.clock, retry=ReasonRetry(RetrySettings()),
        )  # fmt: skip
        return RerunOutcome(build=outcome.build, status=outcome.status, plan=outcome.plan)


def _run(
    s: SignedOff,
    tmp_path: Path,
    runner: BuildRunner,
    *,
    overrides: Mapping[str, str] | None = None,
    settings: RevalidationSettings | None = None,
    cache_mode: CacheMode = "read",
    name: str = "w",
) -> Revalidation:
    return revalidate(
        _bundle(s, tmp_path / name),
        store=s.services.memory_store, cas=s.cas, rules=s.services.rules(),
        git=hermetic_git(), work=tmp_path / name / "work", run=runner,
        overrides=overrides or {}, settings=settings or RevalidationSettings(),
        cache_mode=cache_mode, by="ebs-revalidate", clock=s.services.clock,
    )  # fmt: skip


def _report(s: SignedOff, r: Revalidation) -> dict[str, Any]:
    with s.cas.open(r.record.report) as f:
        doc: dict[str, Any] = json.loads(f.read())
    return doc


# R1: `fake=latest` re-plans with the newest registered toolchain of that tool (recorded in the
# report and the record), and the re-run is a normal build that never writes the action cache.
def test_override_recorded_and_no_cache_write(tmp_path: Path) -> None:
    s = signed_off(tmp_path)
    store = s.services.memory_store
    old = _register(s, "fakesim/1.0", "sha256:" + "1" * 64)
    new = _register(s, "fakesim/2.0", FINGERPRINT_2)
    _register(s, "othersim/9.0", "sha256:" + "9" * 64)  # another tool: never "latest" here
    runner = Runner(s)
    result = _run(s, tmp_path, runner, overrides=parse_overrides(["fake=latest"]))

    assert runner.modes == ["read"]
    build = store.get_build(result.record.build)  # type: ignore[arg-type]
    assert build.cache_mode == "read"
    assert runner.executor is not None
    lint = next(a for a in runner.executor.submitted_specs if a.step == "lint")
    assert lint.toolchain is not None
    assert (lint.toolchain.module, lint.toolchain.id) == ("fakesim/2.0", new.id)
    for spec in runner.executor.submitted_specs:  # nothing went into the action cache
        assert spec.key is not None
        assert store.cache_peek("test", spec.key) is None

    doc = _report(s, result)
    assert doc["toolchains"]["fake"]["override"] == "latest"
    assert doc["toolchains"]["fake"]["used"] == {"module": "fakesim/2.0", "id": str(new.id)}
    assert doc["toolchains"]["fake"]["recorded"]["module"] == "fakesim/1.0"
    assert doc["toolchains"]["fake"]["recorded"]["id"] != str(new.id)
    assert doc["cache_mode"] == "read"
    assert result.record.toolchains == {"fake": "fakesim/2.0"}
    assert "fakesim/1.0 → fakesim/2.0 (override: latest)" in result.markdown
    assert old.id != new.id

    # An explicit module; `--no-cache` re-runs everything; write mode is refused.
    explicit = _run(s, tmp_path, Runner(s), overrides={"fake": "fakesim/1.0"}, name="w2",
                    cache_mode="off")  # fmt: skip
    assert _report(s, explicit)["toolchains"]["fake"]["used"]["id"] == str(old.id)
    assert store.get_build(explicit.record.build).cache_mode == "off"  # type: ignore[arg-type]
    with pytest.raises(ReleaseError, match="never writes the action cache"):
        _run(s, tmp_path, Runner(s), cache_mode="write", name="w3")
    with pytest.raises(ReleaseError, match="no toolchain 'questa'"):
        _run(s, tmp_path, Runner(s), overrides={"questa": "latest"}, name="w4")
    with pytest.raises(ToolchainError, match=r"fakesim/9\.9"):
        _run(s, tmp_path, Runner(s), overrides={"fake": "fakesim/9.9"}, name="w5")


# R1: override syntax
def test_parse_overrides() -> None:
    assert parse_overrides(["questa=latest", "spyglass=vc_spyglass/2026.03"]) == {
        "questa": "latest", "spyglass": "vc_spyglass/2026.03",
    }  # fmt: skip
    for bad in ["questa", "=latest", "questa=", "a=latest,b=latest"]:
        with pytest.raises(ReleaseError, match="NAME=latest"):
            parse_overrides([bad])
    with pytest.raises(ReleaseError, match="twice"):
        parse_overrides(["questa=latest", "questa=questa/2026.1"])


def _scripted(outcome: Outcome, outputs: Mapping[str, bytes] | None = None) -> tuple[Any, ...]:
    return ((outcome,), outputs or {})


# R3: reproduced (every action equivalent), recorded, attached, stored in the CAS, rooted.
def test_report_and_verdict(tmp_path: Path) -> None:
    s = signed_off(tmp_path)
    store = s.services.memory_store
    same = _run(s, tmp_path, Runner(s), name="same")
    assert same.record.verdict == "reproduced"
    assert same.record.counts == {"equivalent": 3, "changed": 0, "incomparable": 0}
    assert store.list_revalidations(s.release.id) == [same.record]
    doc = _report(s, same)
    assert doc["verdict"] == "reproduced"
    assert doc["release"] == {"domain": "test", "name": "verif/cpu", "version": "2026.10.1",
                              "id": int(s.release.id)}  # fmt: skip
    assert [(a["action_id"], a["verdict"]) for a in doc["actions"]] == [
        ("lint", "equivalent"), ("sim[test=random]", "equivalent"),
        ("sim[test=smoke]", "equivalent"),
    ]  # fmt: skip
    with s.cas.open(same.record.markdown) as f:
        assert f.read().decode() == same.markdown
    assert "# Revalidation of verif/cpu 2026.10.1: reproduced" in same.markdown
    roots = store.gc_roots("test", s.services.clock.now()).released
    assert {same.record.report, same.record.markdown} <= roots

    # A changed output of lint: differences, and the report names it.
    diff = _run(s, tmp_path, Runner(s, {"lint": _scripted("pass", {"report": b"other"})}),
                name="diff")  # fmt: skip
    assert diff.record.verdict == "differences"
    assert diff.record.counts["changed"] == 1
    lint = next(a for a in _report(s, diff)["actions"] if a["action_id"] == "lint")
    assert lint["verdict"] == "changed"
    assert lint["comparator"] == "digest"
    assert any(d.startswith("output report:") for d in lint["details"])
    assert "| `lint` | digest | changed |" in diff.markdown

    # A test that now fails is a difference too (a cached test failure is a result).
    failing = _run(s, tmp_path, Runner(s, {"sim[test=smoke]": _scripted("fail")}), name="fail")
    assert failing.record.verdict == "differences"
    smoke = next(a for a in _report(s, failing)["actions"] if a["action_id"] == "sim[test=smoke]")
    assert "status: passed -> failed" in smoke["details"]

    # An infrastructure failure leaves an action without a result: the re-run failed.
    infra: tuple[Any, ...] = ((("infra", "node_fail"),) * 5, {})
    broken = _run(s, tmp_path, Runner(s, {"lint": infra}), name="broken")
    assert broken.record.verdict == "failed"
    assert broken.record.build is not None
    lint = next(a for a in _report(s, broken)["actions"] if a["action_id"] == "lint")
    assert lint["verdict"] == "incomparable"
    assert "no result" in lint["details"][0]

    # A plan that cannot be made: failed without a build, with the error in the report.
    def no_run(plan: Plan, refiner: Planner, cas: CAS, cache_mode: CacheMode) -> RerunOutcome:
        raise AssertionError("must not run")

    bundle = _bundle(s, tmp_path / "bad")
    (bundle.root / "git" / "flow.bundle").write_bytes(b"not a git bundle")
    bad = revalidate(
        bundle, store=store, cas=s.cas, rules=s.services.rules(), git=hermetic_git(),
        work=tmp_path / "bad" / "work", run=no_run, overrides={},
        settings=RevalidationSettings(), cache_mode="read", by="ebs-revalidate",
        clock=s.services.clock,
    )  # fmt: skip
    assert (bad.record.verdict, bad.record.build) == ("failed", None)
    assert _report(s, bad)["error"]
    assert [r.verdict for r in store.list_revalidations(s.release.id)] == [
        "reproduced",
        "differences",
        "differences",
        "failed",
        "failed",
    ]


# R2 + R3: a comparator chosen by step name decides; its "incomparable" is a difference.
def test_comparator_by_step(tmp_path: Path) -> None:
    s = signed_off(tmp_path)
    s.cas.put_bytes(b"lint/report")  # the recorded report's bytes (the scripted run's default)
    report = b"W1 rtl/a.sv:3: signal 'x' unused\n"
    s.cas.put_bytes(report)
    settings = RevalidationSettings(comparators={"lint": LintComparator()})
    changed = _run(s, tmp_path, Runner(s, {"lint": _scripted("pass", {"report": report})}),
                   settings=settings, name="lint")  # fmt: skip
    lint = next(a for a in _report(s, changed)["actions"] if a["action_id"] == "lint")
    assert (lint["comparator"], lint["verdict"]) == ("lint", "changed")
    assert "+ W1 rtl/a.sv:3: signal <*> unused" in lint["details"]
    assert changed.record.verdict == "differences"

    # Other bytes, same (empty) violation set: equivalent for the lint comparator, where the
    # digest comparison would report a change.
    clean = b"lint run: no violations\n"
    s.cas.put_bytes(clean)
    same = _run(s, tmp_path, Runner(s, {"lint": _scripted("pass", {"report": clean})}),
                settings=settings, name="lint2")  # fmt: skip
    lint = next(a for a in _report(s, same)["actions"] if a["action_id"] == "lint")
    assert (lint["comparator"], lint["verdict"]) == ("lint", "equivalent")
    assert same.record.verdict == "reproduced"

    # Without the recorded bytes in any CAS the comparator cannot run: incomparable, which a
    # chosen comparator counts as a difference.
    gone = _run(s, tmp_path, Runner(s, {"lint": _scripted("pass", {"report": b"unstored"})}),
                settings=settings, name="lint3")  # fmt: skip
    lint = next(a for a in _report(s, gone)["actions"] if a["action_id"] == "lint")
    assert (lint["comparator"], lint["verdict"]) == ("lint", "incomparable")
    assert gone.record.verdict == "differences"


# R3: settings from the site config
def test_settings_from_config() -> None:
    settings = RevalidationSettings.from_config({
        "revalidate": {
            "toolchain_overrides": {"questa": "latest"},
            "comparators": {"lint_legacy": "lint", "questa.sim": "sim"},
            "comparator": {"sim": {"coverage_tolerance": 1.0}},
        },
    })  # fmt: skip
    assert settings.toolchain_overrides == {"questa": "latest"}
    assert {k: c.name for k, c in settings.comparators.items()} == {
        "lint_legacy": "lint", "questa.sim": "sim",
    }  # fmt: skip
    default = RevalidationSettings.from_config({})
    assert {k: c.name for k, c in default.comparators.items()} == {"questa.sim": "sim"}
    from ebs.core.errors import ConfigError

    with pytest.raises(ConfigError, match=r"\[revalidate.comparators\]"):
        RevalidationSettings.from_config({"revalidate": {"comparators": {"x": "nope"}}})
    with pytest.raises(ConfigError, match="unknown key"):
        RevalidationSettings.from_config({"revalidate": {"bogus": 1}})

    # A site's choice wins over the default, and its comparator settings are applied.
    site = RevalidationSettings.from_config({"revalidate": {
        "comparators": {"questa.sim": "lint", "sim": "sim"},
        "comparator": {"sim": {"coverage_tolerance": 0.0}, "lint": {"max_details": 3}},
    }})  # fmt: skip
    lint, sim = site.comparators["questa.sim"], site.comparators["sim"]
    assert isinstance(lint, LintComparator)
    assert lint.max_details == 3
    assert isinstance(sim, SimComparator)
    assert sim.coverage_tolerance == 0.0


def _with_outputs(s: SignedOff, tmp_path: Path) -> Bundle:
    rec = archive_release(
        s.services.memory_store, s.cas, s.release, to=tmp_path / "cold", repo=s.site.proj,
        git=hermetic_git(), toolchains=s.services.toolchains(s.site.proj), by="alice",
        with_outputs=True,
    )  # fmt: skip
    return open_bundle(Path(rec.location), tmp_path / "open")


def _direct(
    s: SignedOff, bundle: Bundle, tmp_path: Path, run: Any, settings: RevalidationSettings
) -> Revalidation:
    return revalidate(
        bundle, store=s.services.memory_store, cas=s.cas, rules=s.services.rules(),
        git=hermetic_git(), work=tmp_path / "work", run=run, overrides={}, settings=settings,
        by="ebs-revalidate", clock=s.services.clock,
    )  # fmt: skip


def _action(s: SignedOff, r: Revalidation, action_id: str) -> dict[str, Any]:
    return next(a for a in _report(s, r)["actions"] if a["action_id"] == action_id)


# R2 + R3: a chosen comparator that finds the outputs equivalent still reports a status change.
def test_comparator_status_change(tmp_path: Path) -> None:
    s = signed_off(tmp_path)
    s.cas.put_bytes(b"lint/report")
    settings = RevalidationSettings(comparators={"lint": LintComparator()})
    r = _run(s, tmp_path, Runner(s, {"lint": _scripted("fail", {"report": b"lint/report"})}),
             settings=settings)  # fmt: skip
    lint = _action(s, r, "lint")
    assert (lint["comparator"], lint["verdict"]) == ("lint", "changed")
    assert "status: passed -> failed" in lint["details"]
    assert r.record.verdict == "failed"  # and the sims it fed were skipped: no result


# R3: an action that completed in the build but has no result in a completed re-run (skipped
# after a test failure) makes the revalidation failed, not just different.
def test_skipped_action_is_failed(tmp_path: Path) -> None:
    s = signed_off(tmp_path)
    r = _run(s, tmp_path, Runner(s, {"lint": _scripted("fail")}))
    doc = _report(s, r)
    assert doc["build_status"] == "failed"
    assert r.record.verdict == "failed"
    smoke = _action(s, r, "sim[test=smoke]")
    assert (smoke["verdict"], smoke["counted"]) == ("incomparable", True)
    assert "no result in the re-run" in smoke["details"][0]


# R3: a recorded action the re-planned flow no longer has is a difference.
def test_action_gone_from_replan(tmp_path: Path) -> None:
    s = signed_off(tmp_path)
    inner = Runner(s)

    def run(plan: Plan, refiner: Planner, cas: CAS, cache_mode: CacheMode) -> RerunOutcome:
        out = inner(plan, refiner, cas, cache_mode)
        kept = tuple(a for a in out.plan.actions if a.action_id != "sim[test=smoke]")
        return dataclasses.replace(out, plan=dataclasses.replace(out.plan, actions=kept))

    r = _run(s, tmp_path, run)
    assert _action(s, r, "sim[test=smoke]")["verdict"] == "changed"
    assert r.record.verdict == "differences"


# R2: the recorded outputs are read from the bundle when the domain CAS no longer has them
# (the years-later case: the release's bytes were archived, then collected).
def test_recorded_outputs_from_bundle(tmp_path: Path) -> None:
    s = signed_off(tmp_path)
    bundle = _with_outputs(s, tmp_path)
    s.cas.delete(_recorded_results(bundle)["sim[test=smoke]"].outputs["result"].digest)
    clean = b"no violations\n"
    s.cas.put_bytes(clean)
    settings = RevalidationSettings(comparators={"sim": LintComparator()})
    run = Runner(s, {"sim[test=smoke]": _scripted("pass", {"result": clean})})
    r = _direct(s, bundle, tmp_path, run, settings)
    assert _action(s, r, "sim[test=smoke]")["verdict"] == "equivalent"


# R3: a broken bundle found after the re-run ran is still recorded, as failed with its error.
def test_compare_error_is_recorded(tmp_path: Path) -> None:
    s = signed_off(tmp_path)
    bundle = _bundle(s, tmp_path)
    bundle.member("actions.json").chmod(0o644)
    bundle.member("actions.json").write_text("{}")
    r = _direct(s, bundle, tmp_path, Runner(s), RevalidationSettings())
    assert (r.record.verdict, r.record.build is not None) == ("failed", True)
    assert "comparing the re-run build" in _report(s, r)["error"]
    assert s.services.memory_store.list_revalidations(s.release.id) == [r.record]


# R1: `latest` takes the current toolchain, not a superseded one registered later.
def test_latest_skips_superseded(tmp_path: Path) -> None:
    s = signed_off(tmp_path)
    a = _register(s, "fakesim/2.0", "sha256:" + "a" * 64)
    b = _register(s, "fakesim/2.0", "sha256:" + "b" * 64)
    again = _register(s, "fakesim/2.0", "sha256:" + "a" * 64)  # a reverted install
    assert again.id == a.id != b.id
    r = _run(s, tmp_path, Runner(s), overrides={"fake": "latest"})
    assert _report(s, r)["toolchains"]["fake"]["used"]["id"] == str(a.id)


# R1: nested module names (`vendor/tool/version`) resolve `latest` by the tool's full name.
def test_latest_nested_module(tmp_path: Path) -> None:
    s = signed_off(tmp_path)
    store = s.services.memory_store
    tc = Toolchain.from_parts(
        name="q", module="mentor/questa/2026.1", version="2026.1",
        install_roots=[Path("/opt/questa")], env={}, fingerprint=Digest.parse(FINGERPRINT_2),
    )  # fmt: skip
    store.register_toolchain(
        ToolchainCreate(
            id=tc.id, name="mentor/questa", module="mentor/questa/2026.1", version="2026.1",
            fingerprint=tc.fingerprint, env={}, install_roots=["/opt/questa"],
            registered_by="admin",
        ),
        b"",
    )  # fmt: skip
    records: dict[str, dict[str, JsonValue]] = {"q": {"module": "mentor/questa/2025.2"}}
    resolver = OverrideToolchains(records, {"q": "latest"}, store)
    assert resolver.chosen["q"].id == tc.id
    # A site-wide override for a toolchain this plan does not have is skipped when optional.
    skipped = OverrideToolchains(records, {"q": "latest", "spyglass": "latest"}, store,
                                 optional={"spyglass"})  # fmt: skip
    assert set(skipped.overrides) == {"q"}
    with pytest.raises(ReleaseError, match="no toolchain 'spyglass'"):
        OverrideToolchains(records, {"spyglass": "latest"}, store)


# R2: without a comparator, nondeterministic outputs are not compared; an action with only those
# is incomparable and not counted, and the release can still be reproduced.
def test_nondeterministic_outputs(tmp_path: Path) -> None:
    s = signed_off(tmp_path)
    bundle = _bundle(s, tmp_path)
    lint = next(a for a in bundle.plan().actions if a.action_id == "lint")
    old = _recorded_results(bundle)["lint"]
    other = old.outputs["report"].model_copy(update={"digest": Digest.parse(FINGERPRINT_2)})
    new = old.model_copy(update={"outputs": {"report": other}})
    nondet = dataclasses.replace(lint, outputs=tuple(
        dataclasses.replace(o, deterministic=False) for o in lint.outputs))  # fmt: skip
    verdict, counted = _digest_compare(nondet, old, new)
    assert (verdict.kind, counted) == ("incomparable", False)
    verdict, counted = _digest_compare(lint, old, new)
    assert (verdict.kind, counted) == ("changed", True)
    assert _verdict([_Compared("lint", "lint", "shell", "digest", Verdict.incomparable("x"),
                               counted=False)], True) == "reproduced"  # fmt: skip
    assert _verdict([_Compared("lint", "lint", "shell", "lint", Verdict.incomparable("x"))],
                    True) == "differences"  # fmt: skip


# R2: a step-name comparator wins over a rule-kind one.
def test_step_name_wins_over_kind(tmp_path: Path) -> None:
    s = signed_off(tmp_path)
    s.cas.put_bytes(b"lint/report")
    settings = RevalidationSettings(
        comparators={"lint": LintComparator(), "shell": SimComparator()}
    )
    r = _run(s, tmp_path, Runner(s), settings=settings)
    assert _action(s, r, "lint")["comparator"] == "lint"
    assert _action(s, r, "sim[test=smoke]")["comparator"] == "sim"
