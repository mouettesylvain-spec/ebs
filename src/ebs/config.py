"""Site and user configuration loading (ebs.toml) (layer L1).

Files are looked up in order (docs/design/overview.md § Site configuration): `$EBS_CONFIG`,
`./.ebs/config.toml`, `~/.config/ebs/config.toml`, `/etc/ebs/config.toml`. Earlier files override
later ones key by key within a section. Each consumer validates its own sections
(`MetadataConfig.from_mapping`, `RunnerSettings.from_config`, …).
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from ebs.core.errors import ConfigError

__all__ = ["CONFIG_ENV", "SYSTEM_CONFIG", "Config", "config_paths", "load_config"]

CONFIG_ENV: Final = "EBS_CONFIG"
SYSTEM_CONFIG: Final = Path("/etc/ebs/config.toml")

Config = dict[str, dict[str, object]]
"""Parsed configuration: section name -> key -> value (TOML types)."""


def config_paths(environ: Mapping[str, str], *, cwd: Path, home: Path) -> list[Path]:
    """Candidate config files, highest precedence first. An explicit `$EBS_CONFIG` must exist."""
    paths = [Path(environ[CONFIG_ENV])] if environ.get(CONFIG_ENV) else []
    if paths and not paths[0].is_file():
        raise ConfigError(f"${CONFIG_ENV} names {paths[0]}, which is not a file; fix or unset it")
    return [
        *paths,
        cwd / ".ebs" / "config.toml",
        home / ".config" / "ebs" / "config.toml",
        SYSTEM_CONFIG,
    ]


def load_config(paths: Sequence[Path]) -> Config:
    """Merge the existing files of `paths` (highest precedence first); missing ones are skipped."""
    merged: Config = {}
    for path in reversed(paths):
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(f"cannot read config file {path}: {exc}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid TOML in config file {path}: {exc}") from exc
        for name, section in data.items():
            if not isinstance(section, dict):
                raise ConfigError(
                    f"config file {path}: {name!r} must be a section ([{name}]), "
                    f"got a {type(section).__name__}"
                )
            merged.setdefault(name, {}).update(section)
    return merged
