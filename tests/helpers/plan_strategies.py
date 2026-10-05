"""Hypothesis strategies for plan types (action specs and their parts)."""

from __future__ import annotations

from hypothesis import strategies as st

from ebs.core.digest import hash_bytes
from ebs.plan.types import (
    ActionOutputInput,
    ActionSpec,
    DebugSpec,
    InputRef,
    OutputSpec,
    RuleRef,
    SourceInput,
    ToolchainRef,
)
from tests.helpers.plan import resources

ALPHABET = "abcxyz019-_.=+/"
names = st.text(alphabet="abcxyz_", min_size=1, max_size=6)
texts = st.text(alphabet=ALPHABET, min_size=1, max_size=8)
paths = (
    st.lists(st.text(alphabet="abcxyz019_.", min_size=1, max_size=5), min_size=1, max_size=3)
    .map("/".join)
    .filter(lambda p: all(part not in {".", ".."} for part in p.split("/")))
)
digests = st.binary(max_size=8).map(hash_bytes)
param_values = st.one_of(texts, st.integers(-1000, 1000), st.booleans(), st.tuples(texts, texts))
sources = st.one_of(
    st.builds(SourceInput, names, texts), st.builds(ActionOutputInput, texts, names)
)


@st.composite
def specs(draw: st.DrawFn) -> ActionSpec:
    input_paths = draw(st.lists(paths, max_size=4, unique=True))
    output_names = draw(st.lists(names, max_size=3, unique=True))
    return ActionSpec(
        action_id=draw(texts),
        step=draw(names),
        rule=RuleRef(draw(names), draw(texts)),
        argv=tuple(draw(st.lists(texts, min_size=1, max_size=4))),
        params=draw(st.dictionaries(names, param_values, max_size=4)),
        env=draw(st.dictionaries(names.map(str.upper), texts, max_size=3)),
        toolchain=draw(st.none() | st.builds(ToolchainRef, names, texts, digests)),
        inputs=tuple(
            InputRef(p, draw(st.sampled_from(["file", "tree"])), draw(sources), draw(digests))
            for p in sorted(input_paths)
        ),
        outputs=tuple(
            OutputSpec(n, draw(paths), draw(st.sampled_from(["file", "dir"])), draw(st.booleans()))
            for n in sorted(output_names)
        ),
        config_files=draw(st.dictionaries(paths, st.text(ALPHABET + " \n"), max_size=2)),
        resources=resources(1, 1 << 30, 60),
        licenses=draw(st.dictionaries(names, st.integers(1, 4), max_size=2)),
        debug=DebugSpec(),
        domain="test",
        key=None,
    )
