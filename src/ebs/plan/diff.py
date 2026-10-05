"""`diff_plans`: why each action of a new plan will (or will not) rerun.

Actions are matched by `action_id`. A matched action is `unchanged` when its key is equal,
`changed` when it differs (with the key-document fields that differ), and `unknown` while its
new key cannot be computed yet; `pending` then names the producers it waits for.

Field names: `schema`, `rule`, `argv`, `toolchain`, `params.<name>`, `env.<name>`,
`outputs.<name>`, `config_files["<path>"]`, `inputs["<logical path>"]`.
"""

from __future__ import annotations

from ebs.core.canon import canonical_json
from ebs.core.types import JsonValue
from ebs.plan.keys import action_key_document
from ebs.plan.types import ActionDiff, ActionOutputInput, ActionSpec, Plan

__all__ = ["diff_plans"]

_DOTTED = ("params", "env", "outputs")
_QUOTED = ("config_files", "inputs")


def _as_dict(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value


_ABSENT = "\x00absent"  # never a canonical JSON value of a key document entry


def _differs(a: JsonValue, b: JsonValue) -> bool:
    # Canonical bytes, not ==: in Python 1 == True, but they are different key inputs.
    return canonical_json(a) != canonical_json(b)


def _changed_fields(old: ActionSpec, new: ActionSpec) -> tuple[str, ...]:
    """Key-document fields that differ; inputs whose new id is still unknown are skipped."""
    a, b = _as_dict(action_key_document(old)), _as_dict(action_key_document(new))
    unknown = {ref.logical_path for ref in new.inputs if ref.id is None}
    fields: list[str] = []
    for name in b:
        if name in _DOTTED or name in _QUOTED:
            sub_a, sub_b = _as_dict(a[name]), _as_dict(b[name])
            for sub in sorted(sub_a.keys() | sub_b.keys()):
                if name == "inputs" and sub in unknown:
                    continue
                if _differs(sub_a.get(sub, _ABSENT), sub_b.get(sub, _ABSENT)):
                    fields.append(f"{name}.{sub}" if name in _DOTTED else f'{name}["{sub}"]')
        elif _differs(a[name], b[name]):
            fields.append(name)
    return tuple(fields)


def _pending(spec: ActionSpec) -> tuple[str, ...]:
    producers = {
        ref.source.action_id
        for ref in spec.inputs
        if ref.id is None and isinstance(ref.source, ActionOutputInput)
    }
    return tuple(f"depends on {p}" for p in sorted(producers))


def diff_plans(old: Plan, new: Plan) -> list[ActionDiff]:
    """One entry per action of either plan: new plan's order first, then removed actions."""
    before = {spec.action_id: spec for spec in old.actions}
    after = {spec.action_id for spec in new.actions}
    diffs: list[ActionDiff] = []
    for spec in new.actions:
        prev = before.get(spec.action_id)
        if prev is None:
            diffs.append(ActionDiff(spec.action_id, "added"))
        elif spec.key is None:
            diffs.append(
                ActionDiff(spec.action_id, "unknown", _changed_fields(prev, spec), _pending(spec))
            )
        elif spec.key == prev.key:
            diffs.append(ActionDiff(spec.action_id, "unchanged"))
        else:
            # Equal documents with different keys can only come from another key schema.
            fields = _changed_fields(prev, spec) or ("schema",)
            diffs.append(ActionDiff(spec.action_id, "changed", fields))
    diffs.extend(
        ActionDiff(spec.action_id, "removed") for spec in old.actions if spec.action_id not in after
    )
    return diffs
