"""Tests for action key documents and keys (task P0-08 R5, R6)."""

from __future__ import annotations

import dataclasses
import unicodedata

import pytest

from ebs.core.canon import digest_json
from ebs.core.digest import Digest, hash_bytes
from ebs.core.errors import PlanError
from ebs.plan.keys import (
    KEY_SCHEMA_VERSION,
    action_key_document,
    compute_key,
    nondeterministic_output_id,
    with_key,
)
from ebs.plan.types import ActionOutputInput, InputRef, SourceInput
from tests.helpers.plan import sample_spec


# R5
def test_key_document_has_exactly_the_contract_fields() -> None:
    spec = sample_spec()
    doc = action_key_document(spec)
    assert doc == {
        "schema": KEY_SCHEMA_VERSION,
        "rule": {"kind": "questa.sim", "version": "1"},
        "argv": ["vsim", "-c", "model/elab"],
        "params": {"seed": "1", "test": "smoke", "n": 3, "flag": True, "libs": ["a", "b"]},
        "env": {"UVM_VERBOSITY": "LOW"},
        "toolchain": str(hash_bytes(b"questa")),
        "config_files": {".ebs/script.sh": str(hash_bytes(b"echo hi\n"))},
        "inputs": {"model/elab": str(hash_bytes(b"model")), "rtl/a.sv": str(hash_bytes(b"a"))},
        "outputs": {
            "cov": {"path": "cov.ucdb", "type": "file"},
            "result": {"path": "result.json", "type": "file"},
        },
    }


# R5
def test_key_is_the_digest_of_the_document() -> None:
    spec = sample_spec()
    assert compute_key(spec) == digest_json(action_key_document(spec))


def test_no_toolchain_is_null() -> None:
    doc = action_key_document(sample_spec(toolchain=None))
    assert isinstance(doc, dict)
    assert doc["toolchain"] is None


# R6
def test_compute_key_refuses_unknown_input_ids() -> None:
    pending = InputRef("model/elab", "tree", ActionOutputInput("elab", "model"), None)
    spec = sample_spec(inputs=(pending,))
    with pytest.raises(PlanError, match=r"model/elab.*elab"):
        compute_key(spec)
    # with_key leaves the key unset instead.
    assert with_key(spec).key is None


def test_with_key_sets_the_key() -> None:
    spec = sample_spec()
    assert with_key(spec) == dataclasses.replace(spec, key=compute_key(spec))


def test_non_nfc_text_is_a_plan_error_naming_the_action() -> None:
    decomposed = unicodedata.normalize("NFD", "café")
    with pytest.raises(PlanError, match=r"sim\[seed=1,test=smoke\].*NFC"):
        compute_key(sample_spec(argv=("vsim", decomposed)))


# R6 (I14)
def test_nondeterministic_output_id_formula() -> None:
    k = hash_bytes(b"producer")
    assert nondeterministic_output_id(k, "worklib") == digest_json(
        {"nd": 1, "producer": str(k), "output": "worklib"}
    )
    assert nondeterministic_output_id(k, "worklib") != nondeterministic_output_id(k, "other")
    other: Digest = hash_bytes(b"other producer")
    assert nondeterministic_output_id(k, "worklib") != nondeterministic_output_id(other, "worklib")


def test_with_key_clears_stale_key() -> None:
    pending = InputRef("model/elab", "tree", ActionOutputInput("elab", "model"), None)
    spec = sample_spec(inputs=(pending,), key=hash_bytes(b"stale"))
    assert with_key(spec).key is None


def test_pending_message_lists_inputs_and_producers() -> None:
    inputs = (
        InputRef("a", "tree", ActionOutputInput("elab", "model"), None),
        InputRef("b", "file", SourceInput("srcs", "b"), None),
        InputRef("c", "tree", ActionOutputInput("comp", "lib"), None),
    )
    with pytest.raises(PlanError) as exc:
        compute_key(sample_spec(inputs=inputs))
    message = str(exc.value)
    assert "inputs a, b, c have no id yet" in message
    assert "until their producers ran (comp, elab)" in message
