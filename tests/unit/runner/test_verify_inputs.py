"""I10: the runner verifies every staged input against its expected digest before executing."""

from __future__ import annotations

from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.core.digest import Digest, hash_bytes
from ebs.plan.keys import nondeterministic_output_id
from ebs.plan.types import ActionOutputInput, InputRef, OutputSpec
from ebs.runner.main import EXIT_INFRA, EXIT_OK, EXIT_VERIFY
from tests.helpers.cas import write
from tests.helpers.runner import Harness, shell_spec, source_file, source_tree


def _corrupt(cas: FsCAS, d: Digest, data: bytes) -> None:
    blob = cas.blob_path(d)
    blob.chmod(0o644)
    blob.write_bytes(data)


def _marker_script(tmp_path: Path) -> tuple[str, Path]:
    marker = tmp_path / "tool-ran"
    return f"touch {marker}\n", marker


# R3 (I10)
@pytest.mark.parametrize("kind", ["file", "tree"])
def test_mismatch_exit_76_no_result(cas: FsCAS, tmp_path: Path, kind: str) -> None:
    if kind == "file":
        ref = source_file(cas, "rtl/top.sv", b"module top; endmodule\n")
        assert ref.id is not None
        _corrupt(cas, ref.id, b"module evil; endmodule\n")
    else:
        write(tmp_path / "lib" / "a.v", b"module a; endmodule\n")
        ref = source_tree(cas, "libs/core", tmp_path / "lib")
        _corrupt(cas, hash_bytes(b"module a; endmodule\n"), b"module b; endmodule\n")
    script, marker = _marker_script(tmp_path)
    h = Harness(tmp_path, cas)
    code, _build = h.run(shell_spec(script, inputs=[ref]))
    assert code == EXIT_VERIFY
    assert not marker.exists()  # verified before executing
    assert h.store.results == {}
    assert h.store.cache_puts == []
    assert h.store.events == []
    assert ref.logical_path in h.err.getvalue()
    assert h.scratch_entries() == []


# R3: the same plan with intact bytes runs (so the test above fails for the right reason).
def test_intact_inputs_run(cas: FsCAS, tmp_path: Path) -> None:
    ref = source_file(cas, "rtl/top.sv", b"module top; endmodule\n")
    script, marker = _marker_script(tmp_path)
    h = Harness(tmp_path, cas)
    code, _ = h.run(shell_spec(script, inputs=[ref]))
    assert code == EXIT_OK
    assert marker.exists()


def _nd_pair(cas: FsCAS, tmp_path: Path) -> tuple[Harness, Digest, InputRef]:
    """A producer with a `deterministic: false` tree output and a consumer of it."""
    producer = shell_spec(
        "true", action_id="p", outputs=[OutputSpec("out", "out", "dir", deterministic=False)]
    )
    assert producer.key is not None
    nd_id = nondeterministic_output_id(producer.key, "out")
    write(tmp_path / "wl" / "lib.dat", b"timestamped bytes")
    content = cas.put_tree(tmp_path / "wl")
    ref = InputRef("work", "tree", ActionOutputInput("p", "out"), nd_id)
    return Harness(tmp_path, cas), content, ref


# R3: a nondeterministic input is fetched by the content digest its producer recorded and
# verified against that digest (its id is derived from the producer key, not the bytes).
def test_nondeterministic_input_resolved_and_verified(cas: FsCAS, tmp_path: Path) -> None:
    from ebs.meta.api import OutputResult, ResourceUsage, ResultManifest, RunnerInfo
    from tests.helpers.runner import new_build

    h, content, ref = _nd_pair(cas, tmp_path)
    producer = shell_spec(
        "true", action_id="p", outputs=[OutputSpec("out", "out", "dir", deterministic=False)]
    )
    assert producer.key is not None
    earlier = new_build(h.store, "p", cache_mode="off")
    h.store.record_result(
        earlier,
        "p",
        ResultManifest(
            action_key=producer.key,
            status="passed",
            exit_code=0,
            inputs={},
            outputs={"out": OutputResult(digest=content, id=ref.id, type="tree", size=17)},  # type: ignore[arg-type]
            log=None,
            summary={},
            resources=ResourceUsage(max_rss_kb=0, cpu_s=0, wall_s=0),
            runner=RunnerInfo(version="t", host="n"),
        ),
    )
    consumer = shell_spec("cat work/lib.dat\n", action_id="c", inputs=[ref])
    code, build = h.run(producer, consumer, action_id="c")
    assert code == EXIT_OK
    manifest = h.result(build, "c")
    assert manifest is not None
    assert h.log_text(manifest) == "timestamped bytes"
    assert manifest.inputs == {"work": ref.id}  # the id as planned, feeding provenance

    _corrupt(cas, hash_bytes(b"timestamped bytes"), b"other bytes")
    code, _ = h.run(producer, consumer, action_id="c")
    assert code == EXIT_VERIFY


# R3: a nondeterministic input nobody recorded cannot be staged: temporary, retried.
def test_nondeterministic_input_unrecorded_is_infra(cas: FsCAS, tmp_path: Path) -> None:
    h, _, ref = _nd_pair(cas, tmp_path)
    producer = shell_spec(
        "true", action_id="p", outputs=[OutputSpec("out", "out", "dir", deterministic=False)]
    )
    code, _ = h.run(producer, shell_spec("true", action_id="c", inputs=[ref]), action_id="c")
    assert code == EXIT_INFRA
    assert h.store.results == {}


# R3: bytes missing from the CAS (GC, NFS trouble) are an infrastructure failure, not a mismatch.
def test_missing_input_blob_is_infra(cas: FsCAS, tmp_path: Path) -> None:
    ref = source_file(cas, "a.txt", b"gone")
    assert ref.id is not None
    cas.blob_path(ref.id).unlink()
    h = Harness(tmp_path, cas)
    code, _ = h.run(shell_spec("true", inputs=[ref]))
    assert code == EXIT_INFRA
    assert h.store.results == {}
    assert [e.type for e in h.store.events] == ["infra_failed"]


# R3: a deterministic producer's output is staged by its id (= content) without the store.
def test_deterministic_action_output_input(cas: FsCAS, tmp_path: Path) -> None:
    producer = shell_spec("true", action_id="p", outputs=[OutputSpec("out", "out", "dir")])
    write(tmp_path / "o" / "f.txt", b"det")
    ref = InputRef("dep", "tree", ActionOutputInput("p", "out"), cas.put_tree(tmp_path / "o"))
    h = Harness(tmp_path, cas)

    def no_store(*args: object) -> None:
        raise AssertionError("deterministic inputs must not be resolved through the store")

    h.store.resolve_output = no_store  # type: ignore[method-assign,assignment]
    code, build = h.run(producer, shell_spec("cat dep/f.txt\n", action_id="c", inputs=[ref]),
                        action_id="c")  # fmt: skip
    assert code == EXIT_OK
    manifest = h.result(build, "c")
    assert manifest is not None
    assert h.log_text(manifest) == "det"
