from __future__ import annotations

import os
from pathlib import Path

from ebs.cas.fs import FsCAS
from ebs.runner.main import EXIT_OK
from ebs.runner.stage import Scratch, action_dir_name, stage_inputs, write_config_files
from tests.helpers.cas import write
from tests.helpers.runner import Harness, all_paths, shell_spec, source_file, source_tree

LIST_WORK = "find . -mindepth 1 | LC_ALL=C sort\n"


# R2
def test_scratch_layout(tmp_path: Path) -> None:
    s = Scratch.create(tmp_path / "scratch", "42", "sim[test=smoke,seed=1]")
    assert s.root == tmp_path / "scratch" / "42" / action_dir_name("sim[test=smoke,seed=1]")
    assert {p.name for p in s.root.iterdir()} == {"work", "home", "tmp", "logs"}
    assert (s.work, s.home, s.tmp, s.logs) == tuple(
        s.root / n for n in ("work", "home", "tmp", "logs")
    )
    assert all(not any(p.iterdir()) for p in (s.work, s.home, s.tmp, s.logs))
    # The directory name is a fixed-length hash: action ids hold '/', '[', '=' and so on.
    assert len(action_dir_name("a/b[c=d]")) == 16
    assert action_dir_name("a") != action_dir_name("b")
    s.remove()
    assert not (tmp_path / "scratch" / "42").exists()  # empty build dir goes too


# R2: a stale directory from an earlier attempt on this node is replaced, not reused.
def test_scratch_replaces_stale(tmp_path: Path) -> None:
    first = Scratch.create(tmp_path, "1", "a")
    (first.work / "leftover").write_text("x")
    second = Scratch.create(tmp_path, "1", "a")
    assert not (second.work / "leftover").exists()


# R2
def test_only_declared_inputs_present(cas: FsCAS, tmp_path: Path) -> None:
    tree = tmp_path / "lib"
    write(tree / "a.v", b"module a; endmodule\n")
    write(tree / "sub" / "b.v", b"module b; endmodule\n")
    inputs = [
        source_file(cas, "rtl/top.sv", b"module top; endmodule\n"),
        source_file(cas, "filelist.f", b"rtl/top.sv\n"),
        source_tree(cas, "libs/core", tree),
    ]
    h = Harness(tmp_path, cas)
    code, build = h.run(shell_spec(LIST_WORK, inputs=inputs))
    assert code == EXIT_OK
    manifest = h.result(build)
    assert manifest is not None
    listed = {line.removeprefix("./") for line in h.log_text(manifest).splitlines()}
    assert listed == {
        ".ebs",
        ".ebs/script.sh",
        "filelist.f",
        "libs",
        "libs/core",
        "libs/core/a.v",
        "libs/core/sub",
        "libs/core/sub/b.v",
        "rtl",
        "rtl/top.sv",
    }
    # Inputs are staged under their logical paths with their bytes; the result names them.
    assert manifest.inputs == {i.logical_path: i.id for i in inputs}


# R2
def test_stage_inputs_materializes_bytes(cas: FsCAS, tmp_path: Path) -> None:
    ref = source_file(cas, "deep/dir/x.txt", b"payload")
    work = tmp_path / "work"
    work.mkdir()
    stage_inputs(shell_spec("true", inputs=[ref]), cas, work, {ref.logical_path: ref.id})  # type: ignore[dict-item]
    assert (work / "deep/dir/x.txt").read_bytes() == b"payload"
    assert all_paths(work) == {"deep", "deep/dir", "deep/dir/x.txt"}


# R2
def test_config_files_written(cas: FsCAS, tmp_path: Path) -> None:
    spec = shell_spec(
        "cat modelsim.ini cfg/opts.f\n",
        config_files={"modelsim.ini": "[Library]\nwork = work\n", "cfg/opts.f": "-sv\n"},
    )
    work = tmp_path / "work"
    work.mkdir()
    write_config_files(spec, work)
    assert (work / "modelsim.ini").read_text() == "[Library]\nwork = work\n"
    assert (work / "cfg/opts.f").read_text() == "-sv\n"
    assert (work / ".ebs/script.sh").read_text() == "cat modelsim.ini cfg/opts.f\n"
    assert all_paths(work) == {".ebs", ".ebs/script.sh", "cfg", "cfg/opts.f", "modelsim.ini"}
    # Config files are part of the key; the tool must not be able to edit them in place.
    assert not os.access(work / "modelsim.ini", os.W_OK) or os.geteuid() == 0

    h = Harness(tmp_path / "h", cas)
    code, build = h.run(spec)
    assert code == EXIT_OK
    manifest = h.result(build)
    assert manifest is not None
    assert h.log_text(manifest) == "[Library]\nwork = work\n-sv\n"
