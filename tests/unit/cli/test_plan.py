from __future__ import annotations

import json
import re
from typing import Any

from ebs.cas.fs import FsCAS
from ebs.core.digest import Digest
from ebs.core.errors import ExitCode
from tests.helpers.cli import FLOW, Site, poison_statcache

SMOKE = "sim[test=smoke]"
RANDOM = "sim[test=random]"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _plan_json(site: Site, *args: str) -> dict[str, Any]:
    result = site.invoke(["plan", "--json", *args])
    assert result.exit_code == ExitCode.OK, result.output
    doc = json.loads(result.stdout)
    assert isinstance(doc, dict)
    return doc


def _build(site: Site, *args: str) -> None:
    result = site.invoke(["build", "--cache", "write", *args])
    assert result.exit_code == ExitCode.OK, result.output


def _by_id(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {a["action_id"]: a for a in doc["actions"]}


# R1
def test_summary(site: Site) -> None:
    result = site.invoke(["plan"])
    assert result.exit_code == ExitCode.OK, result.output
    out = result.stdout
    # per-step counts: gen has 1 action (a miss), sim 2 (keys wait for gen's output)
    assert re.search(r"gen\s+1\s+0\s+1\s+0", out), out
    assert re.search(r"sim\s+2\s+0\s+0\s+2", out), out
    assert "3 actions" in out
    assert "no previous build" in out

    doc = _plan_json(site)
    assert doc["totals"] == {"actions": 3, "hit": 0, "miss": 1, "unknown": 2}
    assert [s["step"] for s in doc["steps"]] == ["gen", "sim"]
    assert _by_id(doc)[SMOKE]["change"]["pending"] == ["depends on gen"]


# R1
def test_summary_after_build_predicts_hits_through_refinement(site: Site) -> None:
    _build(site)
    doc = _plan_json(site)
    # gen hits; its cached output id completes sim's keys, which hit as well
    assert doc["totals"] == {"actions": 3, "hit": 3, "miss": 0, "unknown": 0}
    assert all(a["cached_status"] == "passed" for a in doc["actions"])


# R1
def test_without_store_cache_is_unknown(site: Site) -> None:
    site.services.with_store = False  # no [metadata] in the config
    doc = _plan_json(site)
    assert doc["totals"] == {"actions": 3, "hit": 0, "miss": 0, "unknown": 3}
    assert doc["cache"] == "unavailable"


# R1
def test_diff_reasons(site: Site) -> None:
    _build(site)
    (site.proj / "src" / "a.txt").write_text("changed\n")
    result = site.invoke(["plan"])
    assert result.exit_code == ExitCode.OK, result.output
    assert 'gen: inputs["src/a.txt"]' in result.stdout

    doc = _plan_json(site)
    gen = _by_id(doc)["gen"]
    assert gen["cache"] == "miss"
    assert gen["change"] == {"status": "changed", "fields": ['inputs["src/a.txt"]'], "pending": []}
    assert _UUID.fullmatch(doc["baseline"]["build"])


# R1
def test_diff_reasons_param_change_vs_explicit_build(site: Site) -> None:
    _build(site)
    first = _plan_json(site)["baseline"]["build"]
    flow = site.proj / "flow.yaml"
    flow.write_text(flow.read_text().replace('"${row.test}"', '"x-${row.test}"'))
    _build(site)
    doc = _plan_json(site, "--diff", first[:8])  # a unique prefix names the build
    assert doc["baseline"]["build"] == first
    smoke = _by_id(doc)[SMOKE]
    assert smoke["cache"] == "hit"  # built in the second build
    assert smoke["change"] == {"status": "changed", "fields": ["params.test"], "pending": []}


# R1
def test_diff_unknown_build_is_usage_error(site: Site) -> None:
    result = site.invoke(["plan", "--diff", "deadbeef"])
    assert result.exit_code == ExitCode.USAGE
    assert "deadbeef" in result.stderr


# R1
def test_targets_limit_steps(site: Site) -> None:
    doc = _plan_json(site, "gen")
    assert [a["action_id"] for a in doc["actions"]] == ["gen"]


# R1
def test_json_schema_stable(site: Site) -> None:
    doc = json.loads(
        _DIGEST.sub("<digest>", json.dumps(_plan_json(site)))
    )  # digests depend on the ebs version (plan.json) but not their positions
    assert doc == {
        "v": 1,
        "flow": "flow.yaml",
        "domain": "test",
        "project": "demo",
        "plan": "<digest>",
        "cache": "available",
        "baseline": {"build": None},
        "totals": {"actions": 3, "hit": 0, "miss": 1, "unknown": 2},
        "steps": [
            {"step": "gen", "actions": 1, "hit": 0, "miss": 1, "unknown": 0},
            {"step": "sim", "actions": 2, "hit": 0, "miss": 0, "unknown": 2},
        ],
        "actions": [
            {
                "action_id": "gen",
                "step": "gen",
                "key": "<digest>",
                "cache": "miss",
                "cached_status": None,
                "change": {"status": None, "fields": [], "pending": []},
            },
            *(
                {
                    "action_id": action_id,
                    "step": "sim",
                    "key": None,
                    "cache": "unknown",
                    "cached_status": None,
                    "change": {"status": None, "fields": [], "pending": ["depends on gen"]},
                }
                for action_id in (SMOKE, RANDOM)
            ),
        ],
        "removed": [],
        "warnings": [],
    }


def _gen_key(doc: dict[str, Any]) -> str:
    return str(_by_id(doc)["gen"]["key"])


def _poison(site: Site) -> None:
    poison_statcache(site.root / "statcache.sqlite", site.proj / "src" / "a.txt", site.root / "cas")


# R1 (test-critic: --rehash must reach the snapshotter, not just be accepted)
def test_rehash_ignores_poisoned_stat_cache(site: Site) -> None:
    honest = _gen_key(_plan_json(site))
    _poison(site)
    assert _gen_key(_plan_json(site)) != honest  # control: the stat cache is trusted
    assert _gen_key(_plan_json(site, "--rehash")) == honest


# R2 (test-critic: the same for builds)
def test_build_rehash_ignores_poisoned_stat_cache(site: Site) -> None:
    _build(site)
    _poison(site)
    result = site.invoke(["build", "--rehash", "--json"])
    assert result.exit_code == ExitCode.OK, result.output
    assert json.loads(result.stdout.splitlines()[-1])["counts"] == {"cached": 3}


# R1 (test-critic: a cached failure produces nothing downstream may use)
def test_failed_hit_does_not_refine(site: Site) -> None:
    site.services.scripts = {"gen": ("fail",)}
    assert site.invoke(["build", "--cache", "write"]).exit_code == ExitCode.ACTIONS_FAILED
    actions = _by_id(_plan_json(site))
    assert (actions["gen"]["cache"], actions["gen"]["cached_status"]) == ("hit", "failed")
    for action_id in (SMOKE, RANDOM):
        assert actions[action_id]["cache"] == "unknown"
        assert actions[action_id]["key"] is None
        assert actions[action_id]["change"]["pending"] == ["depends on gen"]


# R1 (test-critic: the baseline is the last build of the same flow file)
def test_baseline_is_same_flow(site: Site) -> None:
    _build(site)
    first = _plan_json(site)["baseline"]["build"]
    other = FLOW.replace("project: demo", "project: other")
    (site.proj / "other.yaml").write_text(other)
    _build(site, "-f", "other.yaml")
    assert _plan_json(site)["baseline"]["build"] == first
    assert _plan_json(site, "-f", "other.yaml")["baseline"]["build"] != first


# R1 (test-critic: removed actions are reported)
def test_removed_actions_listed(site: Site) -> None:
    _build(site)
    (site.proj / "tests.csv").write_text("test\nsmoke\n")
    assert _plan_json(site)["removed"] == [RANDOM]
    assert f"removed: {RANDOM}" in site.invoke(["plan"]).stdout


# R1 (test-critic: an explicit --diff whose plan cannot be read is an error, not "no baseline")
def test_diff_with_missing_plan_fails(site: Site) -> None:
    _build(site)
    uuid = _plan_json(site)["baseline"]["build"]
    record = json.loads((site.proj / ".ebs" / "builds" / uuid / "build.json").read_text())
    for digest in {record["plan"], record["final_plan"]}:
        FsCAS(site.root / "cas", "test").delete(Digest.parse(digest))
    result = site.invoke(["plan", "--diff", uuid])
    assert result.exit_code != ExitCode.OK
    assert "ebs: error" in result.stderr


# R1
def test_rehash_flag_is_accepted(site: Site) -> None:
    assert _plan_json(site, "--rehash")["totals"]["actions"] == 3
