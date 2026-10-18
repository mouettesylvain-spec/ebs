"""Creating releases (P2-02 R1): output selection, build and action rules, D4, uniqueness."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from ebs.core.clock import FakeClock
from ebs.core.errors import MetadataError, ReleaseError
from ebs.meta.api import BuildId, ReleaseCreate, ReleaseOutput, release_output_name
from ebs.meta.memory import InMemoryMetadataStore
from ebs.release.releases import create_release, parse_selection
from tests.helpers.release import Action, finish, out

LIB_CORE, LIB_FPU, REPORT = out(b"core"), out(b"fpu", deterministic=False), out(b"lint")


def _store() -> InMemoryMetadataStore:
    return InMemoryMetadataStore(clock=FakeClock())


def _build(store: InMemoryMetadataStore, **kwargs: object) -> BuildId:
    return finish(
        store,
        {
            "compile[lib=core]": Action({"worklib": LIB_CORE}),
            "compile[lib=fpu]": Action({"worklib": LIB_FPU}),
            "lint": Action({"report": REPORT}),
        },
        **kwargs,  # type: ignore[arg-type]
    )


# R1: the selectors pick actions like flow references do; each output keeps its recorded result.
@pytest.mark.parametrize(
    ("selection", "names"),
    [
        ("lint.report", ["lint.report"]),
        ("compile.worklib[*]", ["compile[lib=core].worklib", "compile[lib=fpu].worklib"]),
        ("compile.worklib[lib=fpu]", ["compile[lib=fpu].worklib"]),
    ],
)
def test_select(selection: str, names: list[str]) -> None:
    store = _store()
    build = _build(store)
    rel = create_release(
        store, name="rtl-team/cpu", version="2026.09.1", build=build, outputs=[selection],
        by="alice",
    )  # fmt: skip
    assert sorted(rel.outputs) == names
    assert rel.domain == "test"
    assert rel.build == build
    assert rel.created_by == "alice"
    if "compile[lib=fpu].worklib" in names:
        fpu = rel.outputs["compile[lib=fpu].worklib"]
        assert (fpu.digest, fpu.id, fpu.type, fpu.size) == (
            LIB_FPU.digest, LIB_FPU.id, "file", 3,
        )  # fmt: skip
        assert fpu.id != fpu.digest  # a nondeterministic output keeps its passed-down id
    assert store.get_release("test", "rtl-team/cpu", "2026.09.1") == rel


def _create(store: InMemoryMetadataStore, build: BuildId, **kwargs: object) -> object:
    args: dict[str, object] = {
        "name": "rtl-team/cpu", "version": "1.0", "build": build, "outputs": ["lint.report"],
        "by": "alice",
    }  # fmt: skip
    args.update(kwargs)
    return create_release(store, **args)  # type: ignore[arg-type]


Case = Callable[[InMemoryMetadataStore], object]


def _running(store: InMemoryMetadataStore) -> object:
    return _create(store, _build(store, status=None))


def _failed_action(store: InMemoryMetadataStore) -> object:
    build = finish(store, {"lint": Action({"report": REPORT}, passed=False)}, status="failed")
    return _create(store, build)


def _matrix_without_selector(store: InMemoryMetadataStore) -> object:
    return _create(store, _build(store), outputs=["compile.worklib"])


def _no_match(store: InMemoryMetadataStore) -> object:
    return _create(store, _build(store), outputs=["compile.worklib[lib=gpu]"])


def _unknown_output(store: InMemoryMetadataStore) -> object:
    return _create(store, _build(store), outputs=["lint.nothing"])


def _unknown_step(store: InMemoryMetadataStore) -> object:
    return _create(store, _build(store), outputs=["elab.model"])


def _bad_version(store: InMemoryMetadataStore) -> object:
    return _create(store, _build(store), version="-1")


def _bad_name(store: InMemoryMetadataStore) -> object:
    return _create(store, _build(store), name="RTL Team")


def _empty_reason(store: InMemoryMetadataStore) -> object:
    return _create(store, _build(store), allow_failed=" ")


def _no_outputs(store: InMemoryMetadataStore) -> object:
    return _create(store, _build(store), outputs=[])


# R1: everything a release must satisfy, refused with an actionable message.
@pytest.mark.parametrize(
    ("case", "message"),
    [
        (_running, "still running"),
        (_failed_action, "did not pass.*--allow-failed"),
        (_matrix_without_selector, r"has 2 actions; use compile.worklib\[\*\]"),
        (_no_match, "no action of step 'compile' matches"),
        (_unknown_output, "no recorded output 'nothing'"),
        (_unknown_step, "no step 'elab'"),
        (_bad_version, "invalid release version '-1'"),
        (_bad_name, "invalid release name"),
        (_empty_reason, "needs a reason"),
        (_no_outputs, "at least one output"),
    ],
)
def test_create_rules(case: Case, message: str) -> None:
    store = _store()
    with pytest.raises(ReleaseError, match=message):
        case(store)
    assert store.list_releases("test") == []


# R1: --allow-failed releases outputs of failed actions and stores the reason.
def test_allow_failed_reason_stored() -> None:
    store = _store()
    build = finish(store, {"lint": Action({"report": REPORT}, passed=False)}, status="failed")
    rel = _create(store, build, allow_failed="waiver W-12: known lint noise")
    assert rel.allow_failed == "waiver W-12: known lint noise"  # type: ignore[attr-defined]


# R1, D4: the version rule.
@pytest.mark.parametrize("version", ["2026.09.3", "1.0.0-rc.1", "v2+build.7", "a" * 64])
def test_versions_accepted(version: str) -> None:
    store = _store()
    assert _create(store, _build(store), version=version).version == version  # type: ignore[attr-defined]


@pytest.mark.parametrize("version", ["", ".1", "1 0", "a" * 65, "1/2", "é"])
def test_versions_refused(version: str) -> None:
    store = _store()
    with pytest.raises(ReleaseError, match="invalid release version"):
        _create(store, _build(store), version=version)


# R1: (domain, name, version) is unique; the same name and version may exist in another domain.
def test_unique() -> None:
    store = _store()
    build = _build(store)
    _create(store, build)
    with pytest.raises(ReleaseError, match="already exists"):
        _create(store, build)
    spec = store.get_release("test", "rtl-team/cpu", "1.0")
    assert spec is not None
    with pytest.raises(MetadataError, match="already exists"):  # the store's own check
        store.create_release(ReleaseCreate.model_validate(spec.model_dump(
            include=set(ReleaseCreate.model_fields))), by="bob")  # fmt: skip
    other = _build(store, domain="other")
    rel = _create(store, other)
    assert rel.domain == "other"  # type: ignore[attr-defined]
    assert [r.version for r in store.list_releases("test")] == ["1.0"]


def _spec(build: BuildId, output: ReleaseOutput, **kwargs: object) -> ReleaseCreate:
    fields: dict[str, object] = {
        "domain": "test", "name": "rtl-team/cpu", "version": "1.0", "build": build,
        "outputs": {release_output_name(output.action_id, output.output): output},
    }  # fmt: skip
    fields.update(kwargs)
    return ReleaseCreate.model_validate(fields)


# R1: the store checks every output against the build itself (an HTTP caller is not trusted).
@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"digest": out(b"forged").digest}, "does not match what build"),
        ({"action_id": "sim"}, "has no action 'sim'"),
        ({"output": "other"}, "recorded no output 'other'"),
        ({"size": 999}, "does not match"),
    ],
)
def test_store_checks_outputs(change: dict[str, object], message: str) -> None:
    store = _store()
    build = _build(store)
    good = ReleaseOutput(action_id="lint", output="report", **REPORT.model_dump())
    bad = good.model_copy(update=change)
    with pytest.raises(MetadataError, match=message):
        store.create_release(_spec(build, bad), by="alice")
    with pytest.raises(MetadataError, match="must be named"):
        store.create_release(_spec(build, good, outputs={"x": good}), by="alice")
    with pytest.raises(MetadataError, match="not 'other'"):
        store.create_release(_spec(build, good, domain="other"), by="alice")
    failed = finish(store, {"lint": Action({"report": REPORT}, passed=False)}, status="failed")
    with pytest.raises(MetadataError, match="did not pass"):
        store.create_release(_spec(failed, good), by="alice")
    assert store.list_releases("test") == []
    assert store.create_release(_spec(build, good), by="alice").outputs == {"lint.report": good}


def test_parse_selection() -> None:
    sel = parse_selection("compile.worklib[lib=core,opt=fast]")
    assert (sel.step, sel.output, sel.selector) == (
        "compile", "worklib", (("lib", "core"), ("opt", "fast")),
    )  # fmt: skip
    for bad in ["compile", "compile.worklib[", "a.b[c]", "a.b.c"]:
        with pytest.raises(ReleaseError, match="invalid --output"):
            parse_selection(bad)
