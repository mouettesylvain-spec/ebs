"""Rule registry: step kind -> `RulePlugin`, loaded from `ebs.rules` entry points.

Each entry point's name is the step kind and its value a `RuleFactory` (usually the plugin
class), called with the site's `RuleSettings`:

    [project.entry-points."ebs.rules"]
    shell = "ebs.rules.shell:ShellRule"
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable
from importlib.metadata import entry_points as installed_entry_points
from typing import Protocol

from ebs.core.errors import EbsError, RuleError
from ebs.flow.model import KIND_PATTERN
from ebs.rules.api import ENTRY_POINT_GROUP, RulePlugin, RuleSettings

__all__ = ["EntryPointLike", "RuleRegistry"]

_KIND_RE = re.compile(KIND_PATTERN, re.ASCII)
_METHODS = ("validate", "expand", "classify", "summarize")


class EntryPointLike(Protocol):
    """The parts of `importlib.metadata.EntryPoint` the registry uses."""

    @property
    def name(self) -> str: ...

    @property
    def value(self) -> str: ...

    def load(self) -> object: ...


def _check_plugin(plugin: object, origin: str) -> RulePlugin:
    kind = getattr(plugin, "kind", None)
    if not isinstance(kind, str) or not _KIND_RE.fullmatch(kind):
        raise RuleError(
            f"rule plugin from {origin} has an invalid kind {kind!r}: expected a string matching "
            f"{KIND_PATTERN} (e.g. 'questa.sim')"
        )
    version = getattr(plugin, "version", None)
    if not isinstance(version, str) or not version.strip():
        raise RuleError(
            f"rule {kind!r} from {origin} has version {version!r}: the version must be a "
            "non-empty string (it is part of every action key; bump it when argv changes)"
        )
    missing = [m for m in _METHODS if not callable(getattr(plugin, m, None))]
    if missing:
        raise RuleError(
            f"rule {kind!r} from {origin} does not implement {', '.join(missing)} "
            "(see ebs.rules.api.RulePlugin)"
        )
    return plugin  # type: ignore[return-value]  # checked structurally above


class RuleRegistry:
    """Rule plugins by kind. Kinds are unique; versions are non-empty strings."""

    def __init__(self, plugins: Iterable[RulePlugin] = ()) -> None:
        self._plugins: dict[str, RulePlugin] = {}
        self._origins: dict[str, str] = {}
        for plugin in plugins:
            self.register(plugin)

    @classmethod
    def from_entry_points(
        cls,
        *,
        settings: RuleSettings | None = None,
        entry_points: Iterable[EntryPointLike] | None = None,
    ) -> RuleRegistry:
        """Load every `ebs.rules` entry point (or the given ones, in tests)."""
        settings = settings or RuleSettings()
        if entry_points is None:
            entry_points = installed_entry_points(group=ENTRY_POINT_GROUP)
        registry = cls()
        for ep in sorted(entry_points, key=lambda e: (e.name, e.value)):
            origin = f"entry point {ep.name!r} ({ep.value})"
            try:
                factory = ep.load()
                plugin = factory(settings)  # type: ignore[operator]
            except EbsError:
                raise  # e.g. ConfigError for a bad [rules] setting: not a plugin fault
            except Exception as exc:
                raise RuleError(
                    f"could not load rule plugin {ep.name!r} ({ep.value}): {exc}; "
                    "fix or uninstall the package that provides it"
                ) from exc
            checked = _check_plugin(plugin, origin)
            if checked.kind != ep.name:
                raise RuleError(
                    f"entry point {ep.name!r} ({ep.value}) provides kind {checked.kind!r}: "
                    "the entry point name must equal the rule's kind"
                )
            registry.register(checked, origin=origin)
        return registry

    def register(self, plugin: RulePlugin, *, origin: str | None = None) -> None:
        """Add `plugin`; `origin` names where it came from in error messages."""
        origin = origin or type(plugin).__qualname__
        checked = _check_plugin(plugin, origin)
        if checked.kind in self._plugins:
            raise RuleError(
                f"duplicate rule kind {checked.kind!r}: provided by {self._origins[checked.kind]} "
                f"and {origin}; uninstall one of them"
            )
        self._plugins[checked.kind] = checked
        self._origins[checked.kind] = origin

    def get(self, kind: str) -> RulePlugin:
        plugin = self._plugins.get(kind)
        if plugin is not None:
            return plugin
        message = f"unknown step kind {kind!r}"
        close = difflib.get_close_matches(kind, self._plugins, n=1, cutoff=0.6)
        if close:
            message += f"; did you mean {close[0]!r}?"
        message += f" (available kinds: {', '.join(self.kinds()) or 'none'})"
        raise RuleError(message)

    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._plugins))

    def plugins(self) -> list[RulePlugin]:
        """All plugins sorted by kind (`ebs rules list`)."""
        return [self._plugins[k] for k in self.kinds()]
