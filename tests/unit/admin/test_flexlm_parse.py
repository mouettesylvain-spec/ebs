"""P3-08 R1: `lmutil lmstat -a` output -> issued / in use per feature."""

from __future__ import annotations

from pathlib import Path

import pytest

from ebs.admin.flexlm_sync import (
    FeatureUsage,
    FlexlmError,
    FlexlmSyncConfig,
    parse_lmstat,
    parse_resources,
)
from ebs.core.errors import ConfigError
from tests.unit.admin.conftest import LICENSE, lmstat_fixture, make_config


# R1
def test_parses_issued_and_in_use_per_feature() -> None:
    usage = parse_lmstat(lmstat_fixture("lmstat_up.txt"))
    assert usage == {
        "simfeat": FeatureUsage(issued=10, in_use=3),
        "lintfeat": FeatureUsage(issued=1, in_use=0),  # singular "license issued"
        "gui_feat": FeatureUsage(issued=4, in_use=1),  # singular "license in use"
    }


# R1: uncounted (node-locked) features have no counts and are left out
def test_uncounted_features_are_skipped() -> None:
    assert "nodelocked" not in parse_lmstat(lmstat_fixture("lmstat_up.txt"))


# R1: the same feature served by two vendor daemons counts once with both totals
def test_duplicate_feature_lines_are_summed() -> None:
    text = (
        "Feature usage info:\n\n"
        "Users of simfeat:  (Total of 2 licenses issued;  Total of 1 license in use)\n"
        "Users of simfeat:  (Total of 3 licenses issued;  Total of 2 licenses in use)\n"
    )
    assert parse_lmstat(text) == {"simfeat": FeatureUsage(issued=5, in_use=3)}


# R1: user lines that merely mention "Users of" do not count
def test_only_feature_header_lines_count() -> None:
    text = (
        "Feature usage info:\n\n"
        "Users of simfeat:  (Total of 2 licenses issued;  Total of 1 license in use)\n"
        "    user1 Users of fake:  (Total of 9 licenses issued;  Total of 9 licenses in use)\n"
    )
    assert parse_lmstat(text) == {"simfeat": FeatureUsage(issued=2, in_use=1)}


# R1: "down" inside a host name is not a down server
def test_host_name_containing_down_parses() -> None:
    text = lmstat_fixture("lmstat_up.txt").replace("lic1.example", "lic-downtown.example")
    assert parse_lmstat(text)["simfeat"] == FeatureUsage(issued=10, in_use=3)


# R1: only the asked server's resource rows count (case of the server name ignored)
def test_parse_resources_keeps_only_the_given_server() -> None:
    text = "simfeat|vendorb|9\nsimfeat|FlexLM|2\nlintfeat|other|1\ngui_feat|flexlm|\n"
    assert parse_resources(text, "flexlm") == {"simfeat": 2, "gui_feat": None}


# R1
def test_output_without_a_feature_section_is_an_error() -> None:
    with pytest.raises(FlexlmError, match="Feature usage info"):
        parse_lmstat(lmstat_fixture("lmstat_truncated.txt"))


# R1: lmstat argv for a configured server
def test_lmstat_argv(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    (server,) = config.servers
    assert config.lmstat_argv(server) == ["lmutil", "lmstat", "-a", "-c", LICENSE]
    assert server.name == "flexlm"
    assert server.features == ("simfeat", "lintfeat", "gui_feat")


@pytest.mark.parametrize(
    "mapping",
    [
        {},  # no servers
        {"servers": {"flexlm": {"features": ["a"]}}},  # no license
        {"servers": {"flexlm": {"license": LICENSE, "features": []}}},
        {"servers": {"flexlm": {"license": LICENSE, "features": ["bad name"]}}},
        {"servers": {"bad server": {"license": LICENSE, "features": ["a"]}}},
        {"servers": {"flexlm": {"license": LICENSE, "features": ["a"], "typo": 1}}},
        {"servers": {"flexlm": {"license": LICENSE, "features": ["a"]}}, "timeout_s": 0},
        {"servers": {"flexlm": {"license": LICENSE, "features": ["a"]}}, "lmutl": "x"},
        {"servers": {"flexlm": {"license": LICENSE, "features": "simfeat"}}},  # not a list
        {"servers": {"flexlm": {"license": LICENSE, "features": ["a"]}}, "timeout_s": True},
        {"servers": {"flexlm": "x"}},
    ],
)
def test_invalid_config_is_rejected(mapping: dict[str, object]) -> None:
    with pytest.raises(ConfigError, match=r"\[flexlm\]"):
        FlexlmSyncConfig.from_mapping(mapping)
