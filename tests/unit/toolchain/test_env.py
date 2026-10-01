"""Toolchain environment capture and toolchain ids (P0-07 R3, R4)."""

from __future__ import annotations

import random
import shlex
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ebs.core.canon import digest_json
from ebs.core.digest import hash_bytes
from ebs.core.errors import ToolchainError
from ebs.core.types import JsonValue
from ebs.toolchain.env import VOLATILE_VARS, RunResult, capture_env
from ebs.toolchain.model import toolchain_id

FP = hash_bytes(b"install tree")


# R3
def test_clean_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EBS_LEAK_CHECK", "from the caller")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/leak")
    env = capture_env([], base_env={"MODULEPATH": "/opt/modules"})
    assert env == {
        "PATH": "/usr/bin:/bin",
        "HOME": "$HOME",
        "LANG": "C.UTF-8",
        "MODULEPATH": "/opt/modules",
    }


# R3
def test_clean_start_home_is_fresh_and_removed(tmp_path: Path) -> None:
    listing, home = tmp_path / "listing.txt", tmp_path / "home.txt"
    script = (
        f'ls -A "$HOME" > {shlex.quote(str(listing))}; printf %s "$HOME" > {shlex.quote(str(home))}'
    )
    capture_env(["eval", script], base_env={})
    assert listing.read_text() == ""  # empty HOME: no dotfiles leak in
    assert Path(home.read_text()).is_absolute()
    assert not Path(home.read_text()).exists()  # cleaned up afterwards


# R3
def test_base_env_overrides_defaults() -> None:
    env = capture_env([], base_env={"PATH": "/opt/tool/bin:/usr/bin:/bin", "LANG": "C"})
    assert env["PATH"] == "/opt/tool/bin:/usr/bin:/bin"
    assert env["LANG"] == "C"


# R3
def test_base_env_cannot_set_home() -> None:
    with pytest.raises(ToolchainError, match="HOME"):
        capture_env([], base_env={"HOME": "/home/someone"})


# R3
def test_setup_command_changes_are_captured() -> None:
    # argv runs in the same shell as `env -0`: builtins like export/source/module persist.
    env = capture_env(
        ["export", "QUESTA_HOME=/opt/questa", "PATH=/opt/questa/bin:/usr/bin:/bin"], base_env={}
    )
    assert env["QUESTA_HOME"] == "/opt/questa"
    assert env["PATH"] == "/opt/questa/bin:/usr/bin:/bin"


# R3
def test_argv_not_interpolated(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    payload = f"$(touch {marker})"
    backticks = f"`touch {marker}`"
    env = capture_env(["export", f"EVIL={payload}", f"TICKS={backticks}"], base_env={})
    assert not marker.exists()
    assert env["EVIL"] == payload  # stored verbatim, never expanded
    assert env["TICKS"] == backticks


# R3
def test_argv_not_interpolated_as_command(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    with pytest.raises(ToolchainError, match="status 127"):
        capture_env([f"true; touch {marker}"], base_env={})  # one word: no such command
    assert not marker.exists()


# R3
def test_base_env_values_not_interpolated(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    env = capture_env([], base_env={"X": f"$(touch {marker})"})
    assert not marker.exists()
    assert env["X"] == f"$(touch {marker})"


# R3
def test_volatile_removed() -> None:
    env = capture_env(
        ["export", "RANDOM", "SECONDS", "OLDPWD=/x", "KEEP=1"],
        base_env={},
    )
    assert env["KEEP"] == "1"
    for name in ("PWD", "OLDPWD", "SHLVL", "_", "RANDOM", "SECONDS"):
        assert name in VOLATILE_VARS
        assert name not in env


# R3
def test_failing_setup_is_actionable() -> None:
    with pytest.raises(ToolchainError, match=r"(?s)status 3.*no such module"):
        capture_env(["eval", "echo 'no such module' >&2; exit 3"], base_env={})


# R3
def test_setup_stdout_does_not_corrupt_env() -> None:
    env = capture_env(["echo", "A=1"], base_env={})
    assert "A" not in env


# R3
def test_timeout_is_actionable() -> None:
    with pytest.raises(ToolchainError, match="timed out"):
        capture_env(["sleep", "5"], base_env={}, timeout_s=0.2)


# R3
def test_values_with_newlines_and_equals() -> None:
    env = capture_env(["export", "MULTI=a\nb=c"], base_env={})
    assert env["MULTI"] == "a\nb=c"


def _id(env: dict[str, str]) -> str:
    return str(toolchain_id("questa/2025.2", "2025.2", FP, env))


# R4
def test_id_user_independent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # argv is not interpolated, so the setup script itself expands HOME (as modulefiles do).
    argv = ["eval", 'export TOOL_CFG="$HOME/.tool" ALSO="x:$HOME:y"']
    ids = []
    for user in ("alice", "bob"):
        base = tmp_path / user
        base.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(base))
        env = capture_env(argv, base_env={"MODULEPATH": "/opt/modules"})
        assert env["TOOL_CFG"] == "$HOME/.tool"
        assert env["ALSO"] == "x:$HOME:y"
        assert env["HOME"] == "$HOME"
        ids.append(_id(env))
    assert ids[0] == ids[1]


# R4
def test_id_is_digest_of_documented_document() -> None:
    env = {"PATH": "/usr/bin:/bin", "HOME": "$HOME"}
    env_json: dict[str, JsonValue] = dict(env)
    expected = digest_json(
        {"module": "questa/2025.2", "version": "2025.2", "fingerprint": str(FP), "env": env_json}
    )
    assert toolchain_id("questa/2025.2", "2025.2", FP, env) == expected


# R4
@pytest.mark.parametrize(
    "args",
    [
        ("questa/2025.3", "2025.2", FP, {"A": "1"}),
        ("questa/2025.2", "2025.3", FP, {"A": "1"}),
        ("questa/2025.2", "2025.2", hash_bytes(b"patched"), {"A": "1"}),
        ("questa/2025.2", "2025.2", FP, {"A": "2"}),
        ("questa/2025.2", "2025.2", FP, {"A": "1", "B": "1"}),
    ],
)
def test_id_sensitive_to_every_part(args: tuple[str, str, object, dict[str, str]]) -> None:
    base = toolchain_id("questa/2025.2", "2025.2", FP, {"A": "1"})
    module, version, fp, env = args
    assert isinstance(fp, type(FP))
    assert toolchain_id(module, version, fp, env) != base


# R4
def test_id_env_order_independent() -> None:
    assert _id({"A": "1", "B": "2"}) == _id({"B": "2", "A": "1"})


# R4
def test_id_nfc_normalizes_env() -> None:
    assert _id({"NAME": "café"}) == _id({"NAME": "café"})


class RecordingRunner:
    """Fake process boundary: records the command and answers with canned `env -0` output."""

    def __init__(self, env: dict[str, str] | None = None, returncode: int = 0) -> None:
        self.env = env
        self.returncode = returncode
        self.calls: list[tuple[list[str], dict[str, str], str, float]] = []

    def __call__(
        self, cmd: Sequence[str], env: Mapping[str, str], cwd: str, timeout_s: float
    ) -> RunResult:
        self.calls.append((list(cmd), dict(env), cwd, timeout_s))
        out = self.env if self.env is not None else {**env, "PWD": cwd, "SHLVL": "1"}
        stdout = b"".join(f"{k}={v}".encode() + b"\0" for k, v in out.items())
        return RunResult(self.returncode, stdout, b"")


# R3
def test_fixed_script_with_positional_argv() -> None:
    runner = RecordingRunner()
    argv = ["module", "load", "questa/2025.2; rm -rf /"]
    capture_env(argv, base_env={"X": "1"}, timeout_s=7, runner=runner)
    second = RecordingRunner()
    capture_env(["other"], base_env={}, runner=second)
    ((cmd, env, cwd, timeout),) = runner.calls
    assert Path(cmd[0]).name == "bash"
    assert cmd[1:4] == ["--noprofile", "--norc", "-c"]
    assert cmd[4] == second.calls[0][0][4]  # the script never depends on argv
    assert "questa" not in cmd[4]
    assert cmd[6:] == argv  # positional parameters, verbatim
    assert env == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": cwd, "X": "1"}
    assert timeout == 7


# R3
@settings(deadline=None)  # each example creates and removes a temporary HOME
@given(
    st.dictionaries(
        st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,20}", fullmatch=True),
        st.text(st.characters(exclude_characters="\0", exclude_categories=["Cs"])),
        max_size=8,
    )
)
def test_env_output_round_trip(variables: dict[str, str]) -> None:
    runner = RecordingRunner(env={"PWD": "/x", **variables})
    expected = {k: v for k, v in variables.items() if k not in VOLATILE_VARS}
    assert capture_env([], base_env={}, runner=runner) == expected


# R3
def test_setup_exiting_shell_is_actionable() -> None:
    with pytest.raises(ToolchainError, match="must not call `exit`"):
        capture_env(["exit", "0"], base_env={})


# R3
def test_non_utf8_value_is_actionable() -> None:
    def runner(cmd: Sequence[str], env: Mapping[str, str], cwd: str, t: float) -> RunResult:
        return RunResult(0, b"OK=1\0BAD=\xff\0", b"")

    with pytest.raises(ToolchainError, match=r"'BAD'.*not UTF-8"):
        capture_env([], base_env={}, runner=runner)


# R3
@pytest.mark.parametrize("name", ["", "A=B", "A\0B", "1A", "BASH_FUNC_true%%", "A-B"])
def test_invalid_base_env_name(name: str) -> None:
    with pytest.raises(ToolchainError, match="invalid environment variable"):
        capture_env([], base_env={name: "x"}, runner=RecordingRunner())


# R4
@given(
    st.dictionaries(st.text(min_size=1, max_size=5), st.text(max_size=5), max_size=6),
    st.randoms(use_true_random=False),
)
def test_id_env_order_independent_property(env: dict[str, str], rnd: random.Random) -> None:
    items = list(env.items())
    rnd.shuffle(items)
    assert _id(env) == _id(dict(items))


# R3
@pytest.mark.parametrize(
    "name",
    [
        "BASH_ENV",
        "ENV",
        "SHELLOPTS",
        "BASHOPTS",
        "PS4",
        "CDPATH",
        "GLOBIGNORE",
        "IFS",
        "BASH_XTRACEFD",
    ],
)
def test_shell_control_vars_rejected(tmp_path: Path, name: str) -> None:
    marker = tmp_path / "pwned"
    with pytest.raises(ToolchainError, match=f"must not set {name}"):
        capture_env([], base_env={name: f"$(touch {marker})"})
    assert not marker.exists()


# R3
def test_setup_cannot_shadow_env_dump() -> None:
    env = capture_env(["eval", "command() { echo X=1; }; export KEEP=1"], base_env={})
    assert env["KEEP"] == "1"
    assert "X" not in env


# R4
def test_id_rejects_normalization_clash() -> None:
    with pytest.raises(ToolchainError, match="Unicode normalization"):
        _id({"cafe\u0301": "1", "caf\u00e9": "2"})
