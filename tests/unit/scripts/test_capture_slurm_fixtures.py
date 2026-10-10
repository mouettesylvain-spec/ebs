from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from ebs.exec.slurm.models import SacctResponse, SqueueResponse

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "capture_slurm_fixtures.py"


@pytest.fixture(scope="module")
def capture() -> ModuleType:
    spec = importlib.util.spec_from_file_location("capture_slurm_fixtures", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["capture_slurm_fixtures"] = module
    spec.loader.exec_module(module)
    return module


SECRETS = [
    "jdoe",
    "asmith",
    "chipdesign",
    "proj-falcon",
    "gpu-long",
    "highprio",
    "hpc-west",
    "node-a17",
    "node-a18",
    "lmhost.corp.example",
    "/home/jdoe",
    "falcon_top",
    "login-03",
    "ssh-cert-xyz",
]


def _raw() -> dict[str, Any]:
    job = {
        "job_id": 77,
        "job_state": ["RUNNING"],
        "user_name": "jdoe",
        "user_id": 51234,
        "group_name": "chipdesign",
        "group_id": 6001,
        "account": "proj-falcon",
        "partition": "gpu-long",
        "qos": "highprio",
        "nodes": "node-a[17-18]",
        "batch_host": "node-a17",
        "allocating_node": "login-03",
        "licenses": "msimhdlsim@lmhost.corp.example:1",
        "command": "/home/jdoe/falcon_top/run.sh",
        "current_working_directory": "/home/jdoe/falcon_top",
        "standard_output": "/home/jdoe/falcon_top/out.log",
        "comment": "ssh-cert-xyz",
        "name": "falcon_top-sim",
        "state_reason": "ReqNodeNotAvail, UnavailableNodes:node-a18",
        "array_job_id": {"set": True, "infinite": False, "number": 0},
    }
    return {
        "jobs": [job],
        "meta": {
            "client": {"source": "/dev/pts/1", "user": "jdoe", "group": "chipdesign"},
            "slurm": {"cluster": "hpc-west", "release": "26.05.0"},
            "command": ["squeue", "--json"],
        },
    }


def test_anonymize_removes_every_identifying_string(capture: ModuleType) -> None:
    out = capture.anonymize(_raw())
    text = json.dumps(out)
    for secret in SECRETS:
        assert secret not in text, secret
    job = out["jobs"][0]
    assert job["user_name"] == "u1"
    assert job["group_name"] == "g1"
    assert job["account"] == "a1"
    assert job["partition"] == "p1"
    assert job["qos"] == "q1"
    assert job["batch_host"] == "n001"
    assert job["licenses"] == "msimhdlsim@flexlm:1"
    assert job["state_reason"] == "ReqNodeNotAvail, UnavailableNodes:n002"
    assert out["meta"]["slurm"]["cluster"] == "c1"
    assert out["meta"]["slurm"]["release"] == "26.05.0"
    assert job["job_id"] == 77
    assert job["user_id"] != 51234


def test_anonymize_is_consistent_across_documents(capture: ModuleType) -> None:
    anon = capture.Anonymizer()
    a = anon.apply(_raw())
    b = anon.apply(_raw())
    assert a == b


def test_anonymized_fixtures_still_parse(capture: ModuleType) -> None:
    fixtures = SCRIPT.parents[1] / "tests" / "fixtures" / "slurm" / "26.05"
    for path in sorted(fixtures.glob("*.json")):
        data = capture.anonymize(json.loads(path.read_text()))
        model = SqueueResponse if path.name.startswith("squeue") else SacctResponse
        model.model_validate(data)


def test_capture_runs_commands_and_writes_files(capture: ModuleType, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str]) -> str:
        calls.append(argv)
        return json.dumps(_raw())

    capture.capture(["77", "78_1"], tmp_path, prefix="mixed", run=run)
    assert calls == [
        ["squeue", "--json", "--states=all", "--jobs=77,78_1"],
        ["sacct", "--json", "--jobs=77,78_1"],
    ]
    written = sorted(p.name for p in tmp_path.iterdir())
    assert written == ["sacct_mixed.json", "squeue_mixed.json"]
    assert "jdoe" not in (tmp_path / "squeue_mixed.json").read_text()


def test_anonymize_keeps_signal_and_step_names(capture: ModuleType) -> None:
    raw = json.loads(
        (
            SCRIPT.parents[1] / "tests" / "fixtures" / "slurm" / "26.05" / "sacct_finished.json"
        ).read_text()
    )
    out = capture.anonymize(raw)
    by_id = {j["job_id"]: j for j in out["jobs"]}
    assert by_id[2008]["exit_code"]["signal"]["name"] == "KILL"
    assert by_id[2001]["steps"][0]["step"]["name"] == "batch"
    assert by_id[2001]["name"] == "job1"
