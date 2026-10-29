"""Apply break scenarios to valid samples, producing invalid ones.

Public surface:

* :class:`SampleBreaker` — stateful breaker bound to a :class:`~.Schema`.
* :func:`apply_break_scenario` — functional shortcut for one-off use.

The engine operates on a **deep copy** of the input sample — the
original is never modified.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Set, Tuple

from jsonref import JsonRef

from .DefaultValueGenerator import DefaultValueGenerator
from .helpers.allof_handler import allof_merge
from .helpers.utils import (
    delete_value_at_path,
    get_value_at_path,
    parse_path,
    set_value_at_path,
    to_type,
)
from .models.break_models import BreakKind, BreakRule, BreakScenario
from .models.models import Schema

_dvg = DefaultValueGenerator()

_WRONG_TYPE_CANDIDATES = [
    "string",
    "integer",
    "number",
    "boolean",
    "array",
    "object",
    "null",
]

# Steps _resolve_fragment takes at one node before it gives up: each merges
# an allOf or takes a oneOf/anyOf branch.
_MAX_RESOLVE_STEPS = 32

# No sample value at the path (as opposed to a JSON null).
_NO_VALUE = object()

# Elements of an array sample compared with a candidate branch's items.
_ITEMS_COMPARED = 8

# The value written for a wrong type that the value generator has no
# default for.
_WRONG_TYPE_VALUES: Dict[str, Any] = {"array": [], "object": {}, "null": None}

_FORMAT_INVALID: Dict[str, str] = {
    "email": "not-an-email",
    "date-time": "not-a-datetime",
    "date": "not-a-date",
    "time": "not-a-time",
    "uri": "not a uri",
    "url": "not a url",
    "uuid": "not-a-uuid",
    "hostname": "not a hostname!",
    "ipv4": "999.999.999.999",
    "ipv6": "gggg::1",
}


def apply_break_scenario(
    schema: Schema,
    sample: dict,
    scenario: BreakScenario,
) -> dict:
    """Apply *scenario* to *sample* and return the mutated copy.

    Shortcut for ``SampleBreaker(schema).apply(sample, scenario)``.
    """
    return SampleBreaker(schema).apply(sample, scenario)


class SampleBreaker:
    """Applies :class:`~.BreakScenario` mutations to a valid sample.

    Parameters
    ----------
    schema:
        The JSON Schema that the input samples conform to.
    """

    def __init__(self, schema: Schema) -> None:
        self._schema = schema

    def apply(self, sample: dict, scenario: BreakScenario) -> dict:
        """Return a deep copy of *sample* with all *scenario* rules applied."""
        result = copy.deepcopy(sample)
        for rule in scenario.rules:
            self._apply_rule(result, rule)
        return result

    # ------------------------------------------------------------------
    # Rule dispatch
    # ------------------------------------------------------------------

    def _apply_rule(self, data: dict, rule: BreakRule) -> None:
        if rule.value is not None:
            set_value_at_path(rule.path, data, rule.value)
            return

        kind = rule.kind

        # For kinds that mutate an existing value, skip rules whose path
        # does not exist in the sample (e.g. a oneOf branch not taken).
        # ADDITIONAL_PROPERTY is exempt — it adds a new key to the parent.
        if kind != BreakKind.ADDITIONAL_PROPERTY and rule.path:
            existing = get_value_at_path(rule.path, data)
            if existing is None and not _path_exists(data, rule.path):
                return

        node, value = self._locate(data, rule.path)
        frag = _resolve_fragment(node, value)
        if not isinstance(frag, dict):
            frag = {}

        if kind == BreakKind.REMOVE_REQUIRED:
            delete_value_at_path(rule.path, data)

        elif kind == BreakKind.NULL_VALUE:
            set_value_at_path(rule.path, data, None)

        elif kind == BreakKind.WRONG_TYPE:
            invalid = self._wrong_type_value(node, frag)
            set_value_at_path(rule.path, data, invalid)

        elif kind == BreakKind.ENUM_VIOLATION:
            invalid = self._enum_violation(frag)
            set_value_at_path(rule.path, data, invalid)

        elif kind == BreakKind.CONST_VIOLATION:
            invalid = self._const_violation(frag)
            set_value_at_path(rule.path, data, invalid)

        elif kind == BreakKind.PATTERN_VIOLATION:
            current = get_value_at_path(rule.path, data) or ""
            set_value_at_path(rule.path, data, "!!!" + str(current))

        elif kind == BreakKind.MIN_LENGTH_VIOLATION:
            min_len = frag.get("minLength", 1)
            truncated = "" if min_len <= 1 else "x" * (min_len - 1)
            set_value_at_path(rule.path, data, truncated)

        elif kind == BreakKind.MAX_LENGTH_VIOLATION:
            max_len = frag.get("maxLength", 0)
            set_value_at_path(rule.path, data, "x" * (max_len + 1))

        elif kind == BreakKind.MIN_VIOLATION:
            bound = _get_numeric_bound(frag, "min")
            if bound is not None:
                set_value_at_path(rule.path, data, bound - 1)

        elif kind == BreakKind.MAX_VIOLATION:
            bound = _get_numeric_bound(frag, "max")
            if bound is not None:
                set_value_at_path(rule.path, data, bound + 1)

        elif kind == BreakKind.ADDITIONAL_PROPERTY:
            target = _resolve_dict_at(data, rule.path)
            if isinstance(target, dict):
                target["__break_extra__"] = "unexpected"

        elif kind == BreakKind.MIN_ITEMS_VIOLATION:
            min_items = frag.get("minItems", 1)
            current = get_value_at_path(rule.path, data)
            if isinstance(current, list):
                sliced = current[: max(min_items - 1, 0)]
                set_value_at_path(rule.path, data, sliced)

        elif kind == BreakKind.MAX_ITEMS_VIOLATION:
            max_items = frag.get("maxItems", 0)
            current = get_value_at_path(rule.path, data)
            if isinstance(current, list) and current:
                padded = list(current) + [current[-1]] * (
                    max_items + 1 - len(current)
                )
                set_value_at_path(rule.path, data, padded)
            elif isinstance(current, list):
                set_value_at_path(
                    rule.path, data, ["__break__"] * (max_items + 1)
                )

        elif kind == BreakKind.FORMAT_VIOLATION:
            fmt = frag.get("format", "")
            invalid = _FORMAT_INVALID.get(fmt, f"not-a-{fmt}")
            set_value_at_path(rule.path, data, invalid)

    # ------------------------------------------------------------------
    # Schema resolution helpers
    # ------------------------------------------------------------------

    def _resolve_schema(self, data: dict, path: str) -> dict:
        """Walk *path* in both *data* and the schema, returning the schema
        fragment, with ``allOf`` merged and the ``oneOf``/``anyOf`` branch
        the sample took chosen at every level (see
        :func:`_resolve_fragment`)."""
        node, value = self._locate(data, path)
        frag = _resolve_fragment(node, value)
        return frag if isinstance(frag, dict) else {}

    def _locate(self, data: Any, path: str) -> Tuple[Any, Any]:
        """The schema node at *path*, its own composition not resolved
        yet, and the sample value there. When the path leaves the sample,
        the node and value of the last level present."""
        node: Any = self._schema.data
        current: Any = data
        for segment in parse_path(path) if path else []:
            node = _resolve_fragment(node, current)
            if not isinstance(node, dict):
                return {}, _NO_VALUE
            if (
                isinstance(segment, str)
                and isinstance(current, dict)
                and segment in current
            ):
                props = node.get("properties")
                node = (
                    props.get(segment, {}) if isinstance(props, dict) else {}
                )
                current = current[segment]
            elif (
                isinstance(segment, int)
                and isinstance(current, list)
                and 0 <= segment < len(current)
            ):
                node = node.get("items", {})
                current = current[segment]
            else:
                # Path not found in data — return schema node as-is.
                break
        return _unwrap_node(node), current

    # ------------------------------------------------------------------
    # Value generators
    # ------------------------------------------------------------------

    def _wrong_type_value(self, node: Any, frag: dict) -> Any:
        """A value of a type the schema *node* does not accept: every
        ``oneOf`` and ``anyOf`` branch counts, and ``integer`` is a
        ``number``. A schema that does not restrict the type has no wrong
        type; it gets what it always got, a value of a type other than
        ``to_type`` of the resolved fragment *frag* (a string for an
        untyped ``pattern``, which a string can still break)."""
        accepted = _accepted_types(node)
        if accepted:
            if "number" in accepted:
                accepted.add("integer")
            candidates = [
                c for c in _WRONG_TYPE_CANDIDATES if c not in accepted
            ]
        else:
            current = to_type(frag)
            candidates = [c for c in _WRONG_TYPE_CANDIDATES if c != current]
        for candidate in candidates:
            if candidate in _WRONG_TYPE_VALUES:
                return copy.deepcopy(_WRONG_TYPE_VALUES[candidate])
            try:
                return _dvg({"type": candidate})()
            except Exception:
                continue
        return "WRONG_TYPE_FALLBACK"

    def _enum_violation(self, frag: dict) -> Any:
        enum_vals = frag.get("enum", [])
        sentinel = "__break_not_in_enum__"
        if sentinel not in enum_vals:
            return sentinel
        # Append a suffix to escape the enum exhaustively.
        return str(sentinel) + "_X"

    def _const_violation(self, frag: dict) -> Any:
        const = frag.get("const")
        if isinstance(const, str):
            return const + "_X"
        if isinstance(const, bool):
            return not const
        if isinstance(const, (int, float)):
            return const + 1
        return "__break_const__"


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _unwrap_node(node: Any) -> Any:
    if isinstance(node, JsonRef):
        try:
            return node.__subject__
        except Exception:
            pass
    return node


def _get_numeric_bound(frag: dict, direction: str) -> Optional[float]:
    """Return the inclusive bound for *direction* (``"min"`` or ``"max"``)."""
    if direction == "min":
        if "exclusiveMinimum" in frag:
            return float(frag["exclusiveMinimum"])
        if "minimum" in frag:
            return float(frag["minimum"])
    elif direction == "max":
        if "exclusiveMaximum" in frag:
            return float(frag["exclusiveMaximum"])
        if "maximum" in frag:
            return float(frag["maximum"])
    return None


def _merge(parts: List[Any]) -> Optional[dict]:
    """``allOf`` of *parts*, merged; ``None`` when the merge fails."""
    try:
        merged = allof_merge({"allOf": parts})
    except Exception:
        return None
    return merged if isinstance(merged, dict) else None


def _resolve_fragment(node: Any, sample: Any) -> Any:
    """*node* as *sample* sees it: ``allOf`` merged, and the ``oneOf`` or
    ``anyOf`` branch the sample took chosen and merged with the node's own
    keywords, the way the generator merges them, until neither is left."""
    for _ in range(_MAX_RESOLVE_STEPS):
        node = _unwrap_node(node)
        if not isinstance(node, dict):
            return node
        if "allOf" in node:
            merged = _merge([node])
            if merged is not None and "allOf" not in merged:
                node = merged
                continue
        for composite in ("oneOf", "anyOf"):
            variants = node.get(composite)
            if isinstance(variants, list) and variants:
                break
        else:
            return node
        branch = _pick_variant(variants, sample)
        if branch is None:
            return node
        siblings = {k: v for k, v in node.items() if k != composite}
        node = (_merge([siblings, branch]) or branch) if siblings else branch
    return node


def _json_type(value: Any) -> str:
    """The JSON Schema type of *value*; an integral float is an
    ``integer``."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "integer" if value.is_integer() else "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def _same_value(a: Any, b: Any) -> bool:
    """*a* and *b* are the same JSON value (``1`` and ``true`` differ)."""
    return _json_type(a) == _json_type(b) and a == b


def _accepted_types(node: Any) -> Optional[Set[str]]:
    """The JSON types *node* accepts, ``number`` standing for ``integer``
    too; ``None`` when it does not restrict them. ``type``, OpenAPI's
    ``nullable``, ``const``, ``enum``, ``allOf`` and every ``oneOf`` and
    ``anyOf`` branch count; a composition that leads back to a node being
    read counts as unrestricted."""
    known: Dict[int, Optional[Set[str]]] = {}
    reading: Set[int] = set()

    def read(node: Any) -> Optional[Set[str]]:
        node = _unwrap_node(node)
        if not isinstance(node, dict) or id(node) in reading:
            return None
        if id(node) in known:
            return known[id(node)]
        reading.add(id(node))
        try:
            accepted = _read_types(node, read)
        finally:
            reading.discard(id(node))
        known[id(node)] = accepted
        return accepted

    accepted = read(node)
    return None if accepted is None else set(accepted)


def _read_types(node: dict, read: Any) -> Optional[Set[str]]:
    """:func:`_accepted_types` of one node, *read* giving its parts'."""
    accepted: Optional[Set[str]] = None

    def narrow(types: Optional[Set[str]]) -> None:
        nonlocal accepted
        if types is None:
            return
        types = set(types)
        if "number" in types:
            types.add("integer")
        accepted = types if accepted is None else accepted & types

    declared = node.get("type")
    if isinstance(declared, str):
        narrow({declared})
    elif isinstance(declared, list):
        narrow({t for t in declared if isinstance(t, str)})
    if node.get("nullable") is True and accepted is not None:
        accepted.add("null")
    if "const" in node:
        narrow({_json_type(node["const"])})
    if isinstance(node.get("enum"), list):
        narrow({_json_type(v) for v in node["enum"]})
    for part in node.get("allOf") or []:
        narrow(read(part))
    for composite in ("oneOf", "anyOf"):
        branches = node.get(composite)
        if not isinstance(branches, list) or not branches:
            continue
        union: Optional[Set[str]] = set()
        for branch in branches:
            types = read(branch)
            if types is None:
                union = None
                break
            union |= types
        narrow(union)
    return accepted


def _pick_variant(variants: List[Any], sample_node: Any) -> Optional[dict]:
    """Pick the oneOf/anyOf variant *sample_node* took, each variant seen
    with its ``allOf`` merged (see :func:`_fit`). ``None`` for a value
    that is not an object when no variant accepts it."""
    best: Optional[dict] = None
    best_score: Optional[int] = None
    for v in variants:
        v = _unwrap_node(v)
        if not isinstance(v, dict):
            continue
        score = _fit(_merge([v]) or v, sample_node)
        if best_score is None or score > best_score:
            best_score = score
            best = v
    if not isinstance(sample_node, dict) and (best_score or 0) < 0:
        return None
    return best


def _fit(flat: dict, sample: Any, depth: int = 0) -> int:
    """How well *sample* fits the variant *flat*: far below zero when the
    variant rules out its type, or a value its ``const`` or ``enum`` does
    not hold; then, for an object, the variant's required keys and
    properties it has (as before); for an array, how its first elements
    fit the variant's ``items``."""
    accepted = _accepted_types(flat)
    score = 0 if accepted is None or _json_type(sample) in accepted else -1000
    if isinstance(sample, dict):
        required: List[str] = flat.get("required") or []
        props = flat.get("properties") or {}
        score += 10 * sum(1 for k in required if k in sample)
        score += sum(1 for k in props if k in sample)
        for key, prop in props.items():
            if key in sample:
                score += _agreement(prop, sample[key])
    elif isinstance(sample, list):
        items = _unwrap_node(flat.get("items"))
        if isinstance(items, dict):
            items = _merge([items]) or items
            for element in sample[:_ITEMS_COMPARED]:
                score += (
                    _fit(items, element, depth + 1)
                    if depth < 2
                    else _agreement(items, element)
                )
    else:
        score += _agreement(flat, sample)
    return score


def _agreement(schema: Any, value: Any) -> int:
    """Strongly negative when *schema*, its ``allOf`` merged, rules
    *value* out by its type, ``const`` or ``enum``; positive when its
    ``const`` or ``enum`` holds *value*; else zero."""
    schema = _unwrap_node(schema)
    if not isinstance(schema, dict):
        return 0
    accepted = _accepted_types(schema)
    if accepted is not None and _json_type(value) not in accepted:
        return -1000
    flat = (_merge([schema]) or schema) if "allOf" in schema else schema
    if "const" in flat:
        return 100 if _same_value(flat["const"], value) else -1000
    enum = flat.get("enum")
    if isinstance(enum, list):
        if any(_same_value(option, value) for option in enum):
            return 50
        return -1000
    return 0


def _resolve_dict_at(data: dict, path: str) -> Any:
    """Return the dict/list at *path*, or the root dict when *path* is empty."""
    if not path:
        return data
    return get_value_at_path(path, data)


def _path_exists(data: Any, path: str) -> bool:
    """Return True when *path* navigates to an existing key in *data*."""
    if not path:
        return True
    ref = data
    for segment in parse_path(path):
        if isinstance(segment, int):
            if not isinstance(ref, list) or segment >= len(ref):
                return False
        elif not isinstance(ref, dict) or segment not in ref:
            return False
        ref = ref[segment]
    return True
