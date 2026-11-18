"""Enumerate scenarios that exercise every oneOf/anyOf branch in a schema.

Two strategies are provided:

* :func:`cartesian_scenarios` emits the full cartesian product of variants
  across every discovered site — useful when every combination matters.
* :func:`minimal_scenarios` emits a 1-wise covering set of size
  ``max(site.count)`` so that every variant of every site appears in at
  least one scenario — the smallest fixture set that still touches every
  branch.

Both strategies return :class:`Scenario` objects with ``oneof_selectors``
pre-filled. Selector keys are regex patterns so wildcard array paths like
``items[*]`` match the concrete ``items[0]``, ``items[1]``, ... paths the
generator produces at runtime.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from jsonref import JsonRef

from .helpers.allof_handler import allof_merge
from .helpers.discriminator import discriminator_name
from .helpers.utils import (
    DEFAULT_MAX_DEPTH,
    MAX_REENTRIES,
    allof_ref_markers,
    join_path,
    schema_marker,
)
from .models import Scenario, Schema

_DEFAULT_MAX_DEPTH = DEFAULT_MAX_DEPTH
_DEFAULT_MAX_SCENARIOS = 10_000


@dataclass(frozen=True)
class VariantSite:
    """A location in a schema where a branch must be chosen.

    Attributes:
        path: Property path to the composite node (empty string at root).
            Array items use ``[*]`` as the index placeholder.
        kind: ``"oneOf"`` or ``"anyOf"``.
        count: Number of variants at this site.
        names: Best-effort human labels for each variant — the variant's
            ``title``, else a discriminator-mapping key, else
            ``f"variant_{i}"``.
    """

    path: str
    kind: str
    count: int
    names: Tuple[str, ...]


def collect_variant_sites(
    schema: Schema, *, max_depth: int = _DEFAULT_MAX_DEPTH
) -> List[VariantSite]:
    """Walk ``schema`` and return every oneOf/anyOf site it contains.

    The walker recurses through ``properties``, array ``items``, merged
    ``allOf`` blocks, and into each variant of a oneOf/anyOf so nested
    sites are also discovered. Duplicate (path, kind) pairs — which can
    arise when two sibling variants of an outer oneOf both contain a
    nested oneOf at the same relative path — are collapsed to the first
    occurrence.
    """
    sites: List[VariantSite] = []
    seen: Dict[Tuple[str, str], int] = {}
    _walk(schema.data, "", 0, max_depth, sites, seen)
    return sites


def cartesian_scenarios(
    schema: Schema,
    *,
    name_prefix: str = "cartesian",
    base: Optional[Scenario] = None,
    max_scenarios: int = _DEFAULT_MAX_SCENARIOS,
) -> List[Scenario]:
    """Build one :class:`Scenario` per variant combination.

    The total count is ``prod(site.count for site in sites)`` (``1`` when
    there are no sites). Raises :class:`ValueError` if that product
    exceeds ``max_scenarios`` — lift the cap explicitly if you really
    want the explosion.
    """
    sites = collect_variant_sites(schema)
    if not sites:
        return [_build_scenario((), [], f"{name_prefix}_0", "", base)]

    total = 1
    for site in sites:
        total *= site.count
    if total > max_scenarios:
        raise ValueError(
            f"cartesian product would produce {total} scenarios "
            f"(max_scenarios={max_scenarios}); pass a higher "
            "max_scenarios or use minimal_scenarios()"
        )

    combos = itertools.product(*(range(s.count) for s in sites))
    width = max(len(str(total - 1)), 1)
    return [
        _build_scenario(
            combo,
            sites,
            f"{name_prefix}_{idx:0{width}d}",
            _describe(sites, combo),
            base,
        )
        for idx, combo in enumerate(combos)
    ]


def minimal_scenarios(
    schema: Schema,
    *,
    name_prefix: str = "minimal",
    base: Optional[Scenario] = None,
) -> List[Scenario]:
    """Build the smallest scenario set covering every variant once.

    Returns ``N = max(site.count)`` scenarios. For ``s in range(N)``, site
    ``i`` selects index ``s % site_i.count`` — a standard 1-wise covering
    construction. Every variant of every site is guaranteed to appear in
    at least one scenario (subject to reachability: a nested site is only
    actually exercised when its enclosing variant is selected).
    """
    sites = collect_variant_sites(schema)
    if not sites:
        return [_build_scenario((), [], f"{name_prefix}_0", "", base)]

    n = max(site.count for site in sites)
    width = max(len(str(n - 1)), 1)
    result: List[Scenario] = []
    for s_idx in range(n):
        combo = tuple(s_idx % site.count for site in sites)
        result.append(
            _build_scenario(
                combo,
                sites,
                f"{name_prefix}_{s_idx:0{width}d}",
                _describe(sites, combo),
                base,
            )
        )
    return result


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _unwrap(node: Any) -> Any:
    if isinstance(node, JsonRef):
        try:
            return node.__subject__
        except Exception:
            return node
    return node


def _variant_label(node: Any, variant: Any, index: int) -> str:
    """The name the generator gives the branch: its discriminator value,
    else its ``title``, else ``variant_<index>``."""
    named = discriminator_name(node, variant)
    if named is not None:
        return named[1]
    variant = _unwrap(variant)
    if isinstance(variant, dict):
        title = variant.get("title")
        if isinstance(title, str) and title:
            return title
    return f"variant_{index}"


def _walk(
    node: Any,
    path: str,
    depth: int,
    max_depth: int,
    out: List[VariantSite],
    seen: Dict[Tuple[str, str], int],
    ref_path: Tuple[Any, ...] = (),
    recursive: bool = False,
) -> None:
    if depth > max_depth:
        return
    # The generator's recursion rule: a node that recurs on the path gets
    # its required members only; a node that recurs again is cut.
    via_ref = isinstance(node, JsonRef)
    markers = [schema_marker(node)] if via_ref else []
    node = _unwrap(node)
    if not isinstance(node, dict):
        return
    markers.append(schema_marker(node))
    recurrences = max(ref_path.count(m) for m in markers)
    if recurrences >= MAX_REENTRIES:
        return
    recursive = recursive or recurrences >= 1
    ref_path = ref_path + tuple(markers)

    if "allOf" in node:
        # The merged schema is a fresh dict: carry the parts' $ref markers
        # for an inline allOf. The body of a type reached through a $ref
        # has its own marker already; its parts are mixins, not edges.
        part_markers = [] if via_ref else allof_ref_markers(node)
        if any(ref_path.count(m) >= MAX_REENTRIES for m in part_markers):
            return
        recursive = recursive or any(m in ref_path for m in part_markers)
        ref_path = ref_path + tuple(part_markers)
        try:
            merged = allof_merge(node)
        except Exception:
            merged = node
        _walk(
            merged, path, depth + 1, max_depth, out, seen, ref_path, recursive
        )
        return

    for kind in ("oneOf", "anyOf"):
        variants = node.get(kind)
        if not isinstance(variants, list) or not variants:
            continue
        names = tuple(
            _variant_label(node, v, i) for i, v in enumerate(variants)
        )
        key = (path, kind)
        if key not in seen:
            seen[key] = len(out)
            out.append(
                VariantSite(
                    path=path,
                    kind=kind,
                    count=len(variants),
                    names=names,
                )
            )
        for v in variants:
            _walk(
                v, path, depth + 1, max_depth, out, seen, ref_path, recursive
            )

    props = node.get("properties")
    required = node.get("required") or []
    if isinstance(props, dict):
        for prop_name, prop_schema in props.items():
            if recursive and prop_name not in required:
                continue
            child_path = join_path(path, str(prop_name))
            _walk(
                prop_schema,
                child_path,
                depth + 1,
                max_depth,
                out,
                seen,
                ref_path,
                recursive,
            )

    items = node.get("items")
    if isinstance(items, (dict, JsonRef)) and not (
        recursive and (node.get("minItems") or 0) < 1
    ):
        _walk(
            items,
            f"{path}[*]",
            depth + 1,
            max_depth,
            out,
            seen,
            ref_path,
            recursive,
        )


def _site_to_regex_key(path: str) -> str:
    """Convert an internal path (with ``[*]`` wildcards) to a regex key."""
    escaped = re.escape(path)
    # ``re.escape`` turns ``[*]`` into ``\[\*\]``; swap for ``\[\d+\]``.
    return escaped.replace(re.escape("[*]"), r"\[\d+\]")


def _describe(sites: List[VariantSite], combo: Tuple[int, ...]) -> str:
    parts = []
    for site, idx in zip(sites, combo):
        label = site.names[idx] if 0 <= idx < len(site.names) else idx
        path = site.path or "<root>"
        parts.append(f"{path}={label}")
    return "; ".join(parts)


def _build_scenario(
    combo: Tuple[int, ...],
    sites: List[VariantSite],
    name: str,
    description: str,
    base: Optional[Scenario],
) -> Scenario:
    if base is None:
        overrides: Dict[str, Any] = {}
        pattern_overrides: List[Tuple[str, Any]] = []
        default_data: Dict[str, Any] = {}
        selectors: Dict[str, Any] = {}
    else:
        overrides = dict(base.overrides)
        pattern_overrides = list(base.pattern_overrides)
        default_data = dict(base.default_data)
        selectors = dict(base.oneof_selectors)

    for site, idx in zip(sites, combo):
        # Exact-path key if the path has no wildcard, else a regex key.
        if "[*]" in site.path:
            selectors[_site_to_regex_key(site.path)] = idx
        else:
            selectors[site.path] = idx

    return Scenario(
        name=name,
        description=description or None,
        overrides=overrides,
        pattern_overrides=pattern_overrides,
        oneof_selectors=selectors,
        default_data=default_data,
    ).normalize()
