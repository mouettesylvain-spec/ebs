"""`ebs release …` and `ebs channel …` (P2-02 R1-R4 through the CLI)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ebs.core.errors import ExitCode
from ebs.meta.api import OutputResult
from tests.helpers.attest import released, team_key
from tests.helpers.cli import Site, TestServices, write_site
from tests.helpers.release import Action, finish, out


def _site(tmp_path: Path) -> Site:
    write_site(tmp_path, config_extra='[release.gates]\nstable = "cpu-gate"\n')
    return Site(tmp_path, TestServices(tmp_path))


# R1, R2: create, show, list, yank; failures exit 2 with the rule that was broken.
def test_release_commands(tmp_path: Path) -> None:
    site = _site(tmp_path)
    store = site.services.memory_store
    build = finish(store, {"sim[test=a]": Action({"r": out(b"a")}),
                           "sim[test=b]": Action({"r": out(b"b")}, passed=False)},
                   status="failed")  # fmt: skip
    args = ["release", "create", "rtl-team/cpu", "2026.10.1", "--build", str(build)]
    refused = site.invoke([*args, "--output", "sim.r[*]"])
    assert refused.exit_code == ExitCode.USAGE
    assert "did not pass: sim[test=b]" in refused.output
    created = site.invoke([*args, "--output", "sim.r[test=a]", "--notes", "first", "--json"])
    assert created.exit_code == ExitCode.OK, created.output
    doc = json.loads(created.stdout)
    assert list(doc["outputs"]) == ["sim[test=a].r"]
    assert doc["created_by"] == "alice"
    again = site.invoke([*args, "--output", "sim.r[test=a]"])
    assert again.exit_code == ExitCode.USAGE
    assert "already exists" in again.output

    show = site.invoke(["release", "show", "rtl-team/cpu", "2026.10.1", "--domain", "test"])
    assert show.exit_code == ExitCode.OK, show.output
    assert "sim[test=a].r" in show.stdout
    assert "first" in show.stdout
    nodomain = site.invoke(["release", "show", "rtl-team/cpu", "2026.10.1"])
    assert nodomain.exit_code == ExitCode.USAGE
    assert "--domain" in nodomain.output
    missing = site.invoke(["release", "show", "rtl-team/cpu", "9", "--domain", "test"])
    assert missing.exit_code == ExitCode.USAGE
    assert "no release rtl-team/cpu 9" in missing.output

    yank = site.invoke(["release", "yank", "rtl-team/cpu", "2026.10.1", "--domain", "test",
                        "--reason", "wrong seed"])  # fmt: skip
    assert yank.exit_code == ExitCode.OK, yank.output
    listed = site.invoke(["release", "list", "--domain", "test", "--json"])
    [row] = json.loads(listed.stdout)["releases"]
    assert row["yank_reason"] == "wrong seed"
    text = site.invoke(["release", "list", "--domain", "test"])
    assert "YANKED" in text.stdout


# R3: channels through the CLI, with the configured gate.
def test_channel_commands(tmp_path: Path) -> None:
    site = _site(tmp_path)
    store = site.services.memory_store
    lib = out(b"lib", deterministic=False)
    build = finish(store, {"compile": Action({"worklib": lib})})
    for version in ("1", "2"):
        made = site.invoke(["release", "create", "cpu", version, "--build", str(build),
                            "--output", "compile.worklib"])  # fmt: skip
        assert made.exit_code == ExitCode.OK, made.output
    base = ["--domain", "test"]
    assert site.invoke(["channel", "set", "cpu", "nightly", "1", *base]).exit_code == 0
    gated = site.invoke(["channel", "set", "cpu", "stable", "2", *base])
    assert gated.exit_code == ExitCode.USAGE
    assert "'cpu-gate'" in gated.output
    gate = finish(store, {"sim": Action({"r": out(b"ok")}, inputs={"lib": lib.id})},
                  project="cpu-gate")  # fmt: skip
    moved = site.invoke(["channel", "set", "cpu", "stable", "2", *base, "--json"])
    assert moved.exit_code == ExitCode.OK, moved.output
    assert json.loads(moved.stdout)["gate"] == {"flow": "cpu-gate", "build": gate}
    site.invoke(["channel", "set", "cpu", "nightly", "2", *base])
    show = site.invoke(["channel", "show", "cpu", "nightly", *base])
    assert "cpu:nightly -> 2" in show.stdout
    history = site.invoke(["channel", "history", "cpu", "nightly", *base, "--json"])
    assert [m["version"] for m in json.loads(history.stdout)["moves"]] == ["1", "2"]
    text = site.invoke(["channel", "history", "cpu", "stable", *base])
    assert f"gate cpu-gate: build {gate}" in text.stdout
    unset = site.invoke(["channel", "show", "cpu", "dev", *base])
    assert "not set" in unset.stdout


# R4: export through the CLI copies into the target domain's CAS and records the release.
def test_release_export(tmp_path: Path) -> None:
    site = _site(tmp_path)
    store = site.services.memory_store
    cas = site.services.cas("test")
    blob = cas.put_bytes(b"report")
    site.services.cas("partner").put_bytes(b"other")  # the target domain's CAS directory exists
    build = finish(store, {"lint": Action({"report": OutputResult(
        digest=blob, id=blob, type="file", size=6)})})  # fmt: skip
    site.invoke(["release", "create", "cpu", "1", "--build", str(build), "--output", "lint.report"])
    result = site.invoke(["release", "export", "cpu", "1", "--domain", "test", "--to", "partner"])
    assert result.exit_code == ExitCode.OK, result.output
    assert site.services.cas("partner").has(blob)
    exported = store.get_release("partner", "cpu", "1")
    assert exported is not None
    assert exported.exported_from is not None
    nowhere = site.invoke(["release", "export", "cpu", "1", "--domain", "test", "--to", "gone"])
    assert nowhere.exit_code == ExitCode.USAGE
    assert "not a member of 'gone'" in nowhere.output


# P3-03 R2, R3: `ebs release attest` signs with the configured team key; `verify-signature`
# checks the envelope and blobs with no metadata store at all.
def test_release_attest_and_verify(tmp_path: Path) -> None:
    key = team_key(tmp_path / "keys")
    write_site(tmp_path, config_extra=f'[release.signing]\nkey = "{key.key}"\n')
    site = Site(tmp_path, TestServices(tmp_path))
    rel = released(site.services.cas("test"), tmp_path / "work", site.services.memory_store)
    made = site.invoke(["release", "attest", "verif/cpu", "2026.10.1", "--domain", "test",
                        "--json"])  # fmt: skip
    assert made.exit_code == ExitCode.OK, made.output
    doc = json.loads(made.stdout)
    envelope = Path(doc["envelope"])
    assert envelope == tmp_path / "proj" / "verif_cpu_2026.10.1.intoto.json"
    assert doc["subjects"] == ["lint.report", "pack.srcs"]

    offline = Site(tmp_path, TestServices(tmp_path, with_store=False))
    args = ["release", "verify-signature", str(envelope),
            "--allowed-signers", str(key.allowed_signers)]  # fmt: skip
    ok = offline.invoke([*args, "--blob", str(rel.report), "--blob", str(rel.tree)])
    assert ok.exit_code == ExitCode.OK, ok.output
    assert f"verified verif/cpu 2026.10.1: signed by {key.principal}" in ok.stdout
    assert "pack.srcs  matches" in ok.stdout
    as_json = offline.invoke([*args, "--json"])
    assert json.loads(as_json.stdout)["principals"] == [key.principal]

    rel.report.write_bytes(b"1 violation\n")
    changed = offline.invoke([*args, "--blob", str(rel.report), "--blob", str(rel.tree)])
    assert changed.exit_code == ExitCode.USAGE
    assert "subject lint.report" in changed.output
    text = json.loads(envelope.read_text())
    text["payload"] = text["payload"][:-8] + "AAAAAAA="
    envelope.write_text(json.dumps(text))
    forged = offline.invoke(args)
    assert forged.exit_code == ExitCode.USAGE
    assert "no signature" in forged.output

    nokey = Site(tmp_path, TestServices(tmp_path))
    nokey.services.memory_store = site.services.memory_store
    (tmp_path / "config.toml").write_text(f'[cas]\nroot = "{tmp_path / "cas"}"\n')
    missing = nokey.invoke(["release", "attest", "verif/cpu", "2026.10.1", "--domain", "test"])
    assert missing.exit_code == ExitCode.USAGE
    assert "[release.signing] key" in missing.output

    # --key and -o override the defaults; allowed_signers comes from config when not given.
    other = team_key(tmp_path / "other", principal="b-team@example.invalid")
    made = nokey.invoke(["release", "attest", "verif/cpu", "2026.10.1", "--domain", "test",
                         "--key", str(other.key), "-o", "out.json"])  # fmt: skip
    assert made.exit_code == ExitCode.OK, made.output
    written = tmp_path / "proj" / "out.json"
    fingerprint = subprocess.run(["ssh-keygen", "-l", "-f", str(other.key)], capture_output=True,
                                 check=True).stdout.split()[1].decode()  # fmt: skip
    assert json.loads(written.read_text())["signatures"][0]["keyid"] == fingerprint
    (tmp_path / "config.toml").write_text(
        f'[release.signing]\nallowed_signers = "{other.allowed_signers}"\n'
    )
    configured = Site(tmp_path, TestServices(tmp_path, with_store=False))
    ok = configured.invoke(["release", "verify-signature", str(written)])
    assert ok.exit_code == ExitCode.OK, ok.output
    assert "signed by b-team@example.invalid" in ok.stdout
