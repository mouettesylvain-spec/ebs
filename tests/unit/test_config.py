from __future__ import annotations

from pathlib import Path

import pytest

from ebs.config import config_paths, load_config
from ebs.core.errors import ConfigError


def test_config_paths_order(tmp_path: Path) -> None:
    (tmp_path / "explicit.toml").write_text("")
    paths = config_paths(
        {"EBS_CONFIG": str(tmp_path / "explicit.toml")},
        cwd=tmp_path / "repo",
        home=tmp_path / "home",
    )
    assert paths == [
        tmp_path / "explicit.toml",
        tmp_path / "repo" / ".ebs" / "config.toml",
        tmp_path / "home" / ".config" / "ebs" / "config.toml",
        Path("/etc/ebs/config.toml"),
    ]
    assert config_paths({}, cwd=tmp_path, home=tmp_path)[0] == tmp_path / ".ebs" / "config.toml"


def test_earlier_files_override_later_per_key(tmp_path: Path) -> None:
    high, low = tmp_path / "high.toml", tmp_path / "low.toml"
    high.write_text('[cas]\nroot = "/high"\n')
    low.write_text('[cas]\nroot = "/low"\ncopy_threshold = "1MiB"\n[debug]\nroot = "/dbg"\n')
    assert load_config([high, tmp_path / "missing.toml", low]) == {
        "cas": {"root": "/high", "copy_threshold": "1MiB"},
        "debug": {"root": "/dbg"},
    }


@pytest.mark.parametrize(
    ("text", "message"),
    [("[cas\n", "invalid TOML"), ('cas = "x"\n', "section")],
)
def test_bad_config(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "c.toml"
    path.write_text(text)
    with pytest.raises(ConfigError, match=message) as exc:
        load_config([path])
    assert str(path) in str(exc.value)


# An explicit $EBS_CONFIG that does not exist is a typo, not "no config".
def test_missing_explicit_config(tmp_path: Path) -> None:
    missing = tmp_path / "typo.toml"
    with pytest.raises(ConfigError, match="EBS_CONFIG"):
        load_config(config_paths({"EBS_CONFIG": str(missing)}, cwd=tmp_path, home=tmp_path))
