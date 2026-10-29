from __future__ import annotations

import copy
import math
import random
import re
import warnings
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    cast,
)

from faker import Faker
from jsonref import JsonRef, jsonloader, replace_refs
from proxytypes import LazyProxy

from .DefaultValueGenerator import DefaultValueGenerator
from .helpers import allof_merge, to_type
from .helpers.utils import (
    DEFAULT_MAX_DEPTH,
    MAX_REENTRIES,
    allof_ref_markers,
    join_path,
    parse_path,
    path_startswith,
    schema_marker,
)
from .models import Context, Scenario, Schema
from .SchemaGeneratorBuilder import SchemaGeneratorBuilder

_original_lazy_subject = LazyProxy.__subject__


def _safe_lazy_subject(self):
    try:
        return object.__getattribute__(self, "cache")
    except AttributeError:
        pass
    factory = getattr(self, "factory", None)
    if callable(factory):
        cache = factory()
    else:
        try:
            cache = object.__getattribute__(self, "__wrapped__")
        except AttributeError:
            fget = getattr(_original_lazy_subject, "fget", None)
            if fget is None:
                raise AttributeError("LazyProxy.__subject__ missing fget")
            cache = fget(self)
    object.__setattr__(self, "cache", cache)
    return cache


LazyProxy.__subject__ = property(_safe_lazy_subject)

fake = Faker()

GeneratorFunctionType = Callable[[Dict[str, Any]], Callable[[], Any]]

# Redraws attempted for an array item that duplicates an earlier one when
# ``uniqueItems`` is set and the item schema has an open domain.
UNIQUE_ITEMS_ATTEMPTS = 64


def _ordinal(n: int) -> str:
    """``1st``, ``2nd``, ``3rd``, ``4th``, ``11th``..."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


class _NoDistinctItem(Exception):
    """Raised inside ``_ensure_unique_item`` when no value distinct from
    the earlier items can be drawn; ``_handle_array`` decides whether that
    violates ``minItems`` or just ends the array."""


def _json_key(value: Any) -> Any:
    """A hashable key under which two values are equal as JSON values:
    ``1`` and ``true`` differ, ``1`` and ``1.0`` do not."""
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, float):
        # 1.0 == 1 as JSON; keep large ints exact (no float round trip)
        return ("num", int(value) if value.is_integer() else value)
    if isinstance(value, int):
        return ("num", value)
    if isinstance(value, str):
        return ("str", value)
    if value is None:
        return ("null",)
    if isinstance(value, list):
        return ("list", tuple(_json_key(v) for v in value))
    if isinstance(value, dict):
        return (
            "dict",
            tuple(sorted((str(k), _json_key(v)) for k, v in value.items())),
        )
    return ("other", repr(value))


def _subject(node: Any) -> Any:
    """The schema a :class:`JsonRef` stands for, else *node* itself; a
    reference that cannot be resolved is treated as a leaf (``None``)."""
    if isinstance(node, JsonRef):
        try:
            return node.__subject__
        except Exception:
            return None
    return node


def _contains_itself(data: Any) -> bool:
    """True when a dict or list in *data* contains itself, directly or
    through a :class:`JsonRef`. That is how ``Schema.from_oas`` and
    ``Schema.from_raw_data`` hand over a recursive ``$ref``: resolved up
    front, as a structure that refers back to itself."""
    finished: Set[int] = set()
    on_path: Set[int] = set()
    stack: List[Tuple[Any, bool]] = [(data, False)]
    while stack:
        node, leaving = stack.pop()
        if leaving:
            on_path.discard(id(node))
            finished.add(id(node))
            continue
        node = _subject(node)
        if not isinstance(node, (dict, list)):
            continue
        if id(node) in on_path:
            return True
        if id(node) in finished:
            continue
        on_path.add(id(node))
        stack.append((node, True))
        children = node.values() if isinstance(node, dict) else node
        stack.extend((child, False) for child in children)
    return False


def _holds_ref(data: Any) -> bool:
    """True when *data* still holds a ``{"$ref": "..."}`` to resolve."""
    seen: Set[int] = set()
    stack: List[Any] = [data]
    while stack:
        node = _subject(stack.pop())
        if not isinstance(node, (dict, list)) or id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, dict):
            if isinstance(node.get("$ref"), str):
                return True
            stack.extend(node.values())
        else:
            stack.extend(node)
    return False


# Where _as_references writes the schemas a resolved schema shares.
_SHARED_KEY = "x-sample-generator-shared"


class _Unbroken(Exception):
    """A cycle that no ``$ref`` breaks: it passes through no shared dict."""


def _is_root_ref(node: Any, target: Any, root: Any) -> bool:
    """True for a :class:`JsonRef` written ``{"$ref": "#"}`` to *root*."""
    return target is root and node.__reference__.get("$ref") == "#"


def _walk_once(
    root: Any,
) -> Tuple[Dict[int, Any], Dict[int, int], Set[int], bool]:
    """Every dict and list reachable from *root*, ``JsonRef`` subjects
    included, by ``id``; how often each is reached other than through a
    ``JsonRef``; the ids of the ``JsonRef`` subjects (a ``"#"`` to *root*
    aside); and whether any ``JsonRef`` was met."""
    walked: Dict[int, Any] = {}
    reached: Dict[int, int] = {}
    referred: Set[int] = set()
    any_ref = False
    stack: List[Tuple[Any, bool]] = [(root, False)]
    while stack:
        node, by_ref = stack.pop()
        if isinstance(node, JsonRef):
            any_ref = True
            target = _subject(node)
            if target is None:
                continue
            if not _is_root_ref(node, target, root):
                referred.add(id(target))
            stack.append((target, True))
            continue
        if not isinstance(node, (dict, list)):
            continue
        if not by_ref:
            reached[id(node)] = reached.get(id(node), 0) + 1
        if id(node) in walked:
            continue
        walked[id(node)] = node
        children = node.values() if isinstance(node, dict) else node
        stack.extend((child, False) for child in children)
    return walked, reached, referred, any_ref


def _as_references(
    data: Dict[Any, Any], components: Sequence[Tuple[Any, str]] = ()
) -> Optional[Dict[Any, Any]]:
    """*data*, a schema that contains itself, written back as a tree of
    ``$ref``, the way it was written before it was resolved: each schema
    it refers to is written once and referred to wherever it occurs, and
    the root is written in place, as the root of a document is.

    Where *data* still holds its ``JsonRef``s (``Schema.from_raw_data``),
    each becomes a ``$ref`` again, ``"#"`` one too. *components* are the
    component schemas ``Schema.from_oas`` resolved, with their names:
    those are written under ``components/schemas`` and referred to as
    ``#/components/schemas/<name>``, as in the document. Anything else
    reached more than once is written under ``_SHARED_KEY``; and when
    nothing tells what was referred to, every ``allOf`` is taken for a
    referred component, so that inheritance keeps its meaning. ``None``
    when a cycle passes through no such dict."""
    walked, _, _, _ = _walk_once(data)
    # Schema.from_oas and pydantic hand the root over as a shallow copy of
    # the schema the references point at: start from that schema, so the
    # dicts and lists the two share do not count as shared.
    root: Dict[Any, Any] = data
    for node in walked.values():
        if (
            node is not data
            and isinstance(node, dict)
            and len(node) == len(data)
            and all(k in node and node[k] is v for k, v in data.items())
        ):
            root = node
            break
    walked, reached, referred, any_ref = _walk_once(root)
    known = {id(c): name for c, name in components if walked.get(id(c)) is c}
    referred_to = {
        key
        for key, node in walked.items()
        if key in referred
        or (isinstance(node, dict) and reached.get(key, 0) > 1)
    }
    named = referred_to | (set(known) - {id(root)})
    if not any_ref and not known:
        named |= {
            key
            for key, node in walked.items()
            if node is not root and isinstance(node, dict) and "allOf" in node
        }
    if id(root) in referred_to:
        named.add(id(root))

    shared_key = _SHARED_KEY
    while shared_key in root:
        shared_key += "_"
    in_components = "components" not in root
    order = [k for k in walked if k in named]
    names: Dict[int, str] = {}
    for i, k in enumerate(order):
        if k in known and in_components:
            escaped = known[k].replace("~", "~0").replace("/", "~1")
            names[k] = f"#/components/schemas/{escaped}"
        else:
            names[k] = f"#/{shared_key}/{i}"

    on_path: Set[int] = set()

    def write(node: Any, body: bool = False) -> Any:
        if isinstance(node, JsonRef):
            target = _subject(node)
            if target is None:
                return node
            if _is_root_ref(node, target, root):
                return {"$ref": "#"}
            return {"$ref": names[id(target)]}
        if not isinstance(node, (dict, list)):
            return node
        if not body and id(node) in names:
            return {"$ref": names[id(node)]}
        if id(node) in on_path:
            raise _Unbroken()
        on_path.add(id(node))
        try:
            if isinstance(node, dict):
                return {k: write(v) for k, v in node.items()}
            return [write(v) for v in node]
        finally:
            on_path.discard(id(node))

    try:
        tree = write(root, body=True)
        definitions = {k: write(walked[k], body=True) for k in order}
    except _Unbroken:
        return None
    for k, definition in definitions.items():
        if names[k].startswith("#/components/schemas/"):
            schemas = tree.setdefault("components", {}).setdefault(
                "schemas", {}
            )
            schemas[known[k]] = definition
        else:
            tree.setdefault(shared_key, {})[
                names[k].rsplit("/", 1)[-1]
            ] = definition
    return tree


# Keywords next to a oneOf/anyOf that do not constrain the instance and
# therefore need not be merged into the chosen branch. Vendor extensions
# (``x-...``) are treated the same way.
_VARIANT_ANNOTATIONS = frozenset(
    {
        "discriminator",
        "title",
        "description",
        "$comment",
        "examples",
        "example",
        "default",
        "deprecated",
        "readOnly",
        "writeOnly",
        "$schema",
        "$id",
        "definitions",
        "$defs",
        "components",
        "xml",
        "externalDocs",
        "nullable",
    }
)


class JSONSchemaGenerator:
    """
    JSON schema sample data generator that creates example data based on
    JSON Schema definitions and custom scenarios.
    """

    def __init__(
        self,
        schema: Schema,
        scenario: Optional[Scenario] = None,
        allof_merger: Callable[[Dict[str, Any]], Dict[str, Any]] = allof_merge,
        max_depth: int = DEFAULT_MAX_DEPTH,
        default_value_generator: GeneratorFunctionType = DefaultValueGenerator(),
        loader=jsonloader,
        generator_max_items: Optional[int] = None,
    ):
        # Schema.from_oas and Schema.from_raw_data resolve every $ref up
        # front, so a recursive one arrives as a structure that contains
        # itself, which replace_refs, walking it as a tree, would never
        # finish. Such a schema is written back as $refs first, and is then
        # generated the way the same schema written with $ref is.
        data = schema.data
        shared = (
            _as_references(data, schema._components)
            if _contains_itself(data) and not _holds_ref(data)
            else None
        )
        self.schema = Schema(
            data=cast(
                Dict[Any, Any],
                copy.deepcopy(data if shared is None else shared),
            ),
            base_uri=schema.base_uri,
        )
        self.max_depth = max_depth
        self.generator_max_items = generator_max_items

        uri = (
            self.schema.base_uri
            if self.schema.base_uri is not None
            else "file://dummy.json"
        )
        if shared is not None:
            # Its references are local, and jsonref resolves a local one
            # only against a base URI without a fragment.
            uri = uri.split("#", 1)[0]

        loaded = replace_refs(self.schema.data, base_uri=uri, loader=loader)
        # Ensure schema.data is a dictionary after replacing references
        if isinstance(loaded, dict):
            self.schema.data = cast(Dict[Any, Any], loaded)
        else:
            self.schema.data = cast(Dict[Any, Any], dict(cast(Any, loaded)))

        self.scenario = scenario or Scenario(name="default")
        self.default_value_generator = default_value_generator
        self.allof_merger = allof_merger

    def generate(self, scenario: Optional[Scenario] = None) -> Any:
        """
        Generate a JSON sample based on the schema and scenario.

        This method is thread-safe and can be called in parallel with different scenarios.

        Args:
            scenario: Optional scenario to use for this generation.
                     If provided, overrides the scenario set in the constructor.

        Returns:
            Generated JSON sample data
        """
        # Use the provided scenario or fall back to the default one
        active_scenario = scenario or self.scenario

        active_scenario = active_scenario.normalize()

        # Create a new builder for this generation
        builder = SchemaGeneratorBuilder()

        # Initialize result with scenario default data if provided
        if active_scenario.default_data:
            # Merge filtered default_data into builder.generated
            from .helpers.utils import deep_merge

            filtered_defaults = self._filter_default_data(
                self.schema.data, active_scenario.default_data
            )
            builder.generated = deep_merge(
                builder.generated, filtered_defaults
            )

        # Build the initial context
        ctx = builder.build_context(self.schema)

        # Start the generation process
        self._generate_node(ctx, active_scenario, builder)
        self._resolve_pending_fields(active_scenario, builder)

        return builder.get_result()

    def _filter_default_data(
        self, schema_fragment: Dict[str, Any], default_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Filter scenario default data so only known top-level properties are merged."""

        if not isinstance(default_data, dict):
            return default_data

        properties = schema_fragment.get("properties")
        if not isinstance(properties, dict):
            return {}

        return {k: v for k, v in default_data.items() if k in properties}

    def _scenario_defined(self, path: str, scenario: Scenario) -> bool:
        """
        Check if a scenario is defined for the given path.

        Args:
            path: The property path to check
            scenario: The scenario to check against

        Returns:
            True if a scenario is defined for this path
        """
        return path in scenario.overrides or any(
            pattern in path for pattern, _ in scenario.pattern_overrides
        )

    def _apply_scenario(self, ctx: Context, scenario: Scenario) -> Any:
        """
        Apply the scenario override for the given context.

        Args:
            ctx: The current generation context
            scenario: The scenario to apply

        Returns:
            The value from the scenario override

        Raises:
            ValueError: If no scenario override is defined for the path
        """
        path = ctx.prop_path
        if path in scenario.overrides:
            return scenario.overrides[path](ctx)

        for pattern, override in scenario.pattern_overrides:
            if pattern in path:
                return override(ctx)

        raise ValueError(
            f"Scenario override not defined for path: {path}. "
            "Please define a scenario for this path."
        )

    def _resolve_fragment(
        self, schema: Dict[str, Any], fragment: str
    ) -> Dict[str, Any]:
        """
        Resolve a schema fragment from a URI.

        Args:
            schema: The root schema to resolve the fragment from.
            fragment: The fragment to resolve (e.g., '#/definitions/MySchemaModel').

        Returns:
            The resolved schema fragment.

        Raises:
            KeyError: If the fragment cannot be resolved.
        """
        parts = fragment.lstrip("#/").split("/")
        for part in parts:
            if part not in schema:
                print(f"DEBUG: Current schema: {schema}")
                print(f"DEBUG: Missing part: {part}")
                raise KeyError(f"Fragment '{fragment}' not found in schema.")
            schema = schema[part]
        return schema

    def _generate_node(
        self,
        ctx: Context,
        scenario: Scenario,
        builder: SchemaGeneratorBuilder,
        via_ref: bool = False,
    ) -> Any:
        """
        Generate a node in the JSON sample based on the schema and context.

        Args:
            ctx: The current generation context
            scenario: The scenario to use for this generation
            builder: The builder instance for this generation
            via_ref: True when the node is the body of a ``$ref`` that was
                just resolved (its own marker is already on the path)

        Returns:
            The generated value for this node
        """
        schema = ctx.schema_data

        if self._depth(ctx.prop_path) > self.max_depth:
            self._warn_cut(ctx, builder, f"max_depth={self.max_depth} reached")
            return None

        if isinstance(ctx.schema_data, JsonRef):
            ref = ctx.schema_data.__reference__["$ref"]
            if self._reentry_cut(ctx, ref, builder):
                return None
            resolved_schema = ctx.schema_data.__subject__
            ctx = ctx.copy(
                schema_data=resolved_schema,
                schema_path=ref,
                parent_schema=resolved_schema,
                ref_path=ctx.ref_path + (ref,),
                recursive=ctx.recursive or ref in ctx.ref_path,
            )
            return self._generate_node(ctx, scenario, builder, via_ref=True)

        path = ctx.prop_path

        if self._scenario_defined(path, scenario):
            try:
                val = self._apply_scenario(ctx, scenario)
                return builder.set_value_at_path(path, val)
            except (KeyError, IndexError):
                # The override reads data that is not generated yet
                # (ctx.data is the live result): retry in the second pass.
                builder.add_pending_field(ctx)
                return None

        if "allOf" in schema:
            return self._generate_all_of(ctx, scenario, builder, via_ref)
        if "anyOf" in schema:
            return self._generate_any_of(ctx, scenario, builder)
        if "oneOf" in schema:
            return self._generate_one_of(ctx, scenario, builder)

        typ = to_type(schema)

        if typ == "object":
            return self._handle_object(ctx, scenario, builder)
        elif typ == "array":
            return self._handle_array(ctx, scenario, builder)
        else:
            # If scenario default_data already provides a value at this path, keep it
            if builder.has_value_at_path(path):
                return builder.get_value_at_path(path)
            if scenario.scenario_only:
                return None
            val = self._default_value(schema)
            return builder.set_value_at_path(path, val)

    @staticmethod
    def _depth(path: str) -> int:
        """Object nesting depth of *path*: the number of property keys
        below the root. Array indices do not add a level."""
        keys = sum(isinstance(s, str) for s in parse_path(path))
        return max(keys - 1, 0)

    def _reentry_cut(
        self, ctx: Context, marker: Any, builder: SchemaGeneratorBuilder
    ) -> bool:
        """True, after warning if the member is required, when *marker*
        already recurs ``MAX_REENTRIES`` times on the path: the node is
        cut so that required recursion cannot branch without bound."""
        if ctx.ref_path.count(marker) < MAX_REENTRIES:
            return False
        self._warn_cut(
            ctx,
            builder,
            f"recursion limit reached (a schema node occurs for the "
            f"{_ordinal(MAX_REENTRIES + 1)} time on the path)",
        )
        return True

    def _warn_cut(
        self, ctx: Context, builder: SchemaGeneratorBuilder, reason: str
    ) -> None:
        """Warn once per generate() when a cut (the ``max_depth`` hard cap
        or the re-entry limit) drops a property the schema requires, or an
        array item needed for ``minItems``. Recursion normally ends through
        the cycle rule (see ``Context.recursive``); a cut means the schema
        requires its recursion all the way down, so no finite sample can
        validate."""
        if builder.depth_warned:
            return
        segments = parse_path(ctx.prop_path)
        parent = ctx.parent_schema
        if not segments or not hasattr(parent, "get"):
            return
        last = segments[-1]
        if isinstance(last, str):
            required = last in (parent.get("required") or [])
        else:
            required = last < (parent.get("minItems") or 0)
        if not required:
            return
        builder.depth_warned = True
        warnings.warn(
            f"{reason} at '{ctx.prop_path}': this required property is "
            "left out, so the sample will not validate against the schema. "
            "The schema requires its recursion all the way down, or nests "
            "deeper than max_depth: pass a larger max_depth to "
            "JSONSchemaGenerator in the latter case. Further cuts in this "
            "generate() call are not reported.",
            UserWarning,
            stacklevel=2,
        )

    def _generate_all_of(
        self,
        ctx: Context,
        scenario: Scenario,
        builder: SchemaGeneratorBuilder,
        via_ref: bool = False,
    ):
        """
        Generate a node that satisfies all schemas in an allOf array.

        Args:
            ctx: The current generation context
            scenario: The scenario to use for generation
            builder: The builder instance for this generation

        Returns:
            The generated value
        """
        # Use the original allOf schema (with potential JsonRef children) as the parent schema
        origin_parent = ctx.schema_data
        # The merged schema is a fresh dict. For an inline allOf (a property
        # value such as {"allOf": [$ref Node, ...]}, user-written or built
        # by allof_merge for a property defined twice) the $ref parts are
        # the edges recursion travels, so their markers are carried so the
        # recursion rule and the re-entry limit see through the merge. The
        # body of a type reached through a $ref already has its own marker
        # on the path; its parts are mixins (a shared Base) and must not
        # count, or OpenAPI inheritance would look like a cycle.
        markers = [] if via_ref else allof_ref_markers(origin_parent)
        for marker in markers:
            if self._reentry_cut(ctx, marker, builder):
                return None
        recursive = ctx.recursive or any(m in ctx.ref_path for m in markers)
        result = self.allof_merger(origin_parent)
        return self._generate_node(
            ctx.copy(
                schema_data=result,
                parent_schema=origin_parent,
                ref_path=ctx.ref_path + tuple(markers),
                recursive=recursive,
            ),
            scenario,
            builder,
        )

    @staticmethod
    def _property_schema_path(value: Any, inherited: Optional[str]) -> Any:
        """The ``schema_path`` for a property or item schema: its ``$ref``
        when it is a reference, the last ``$ref`` part when it is an
        ``allOf`` of references (a re-declared or refined reference), else
        the inherited path."""
        if isinstance(value, JsonRef):
            return value.__reference__["$ref"]
        if isinstance(value, dict) and isinstance(value.get("allOf"), list):
            refs = [
                part.__reference__["$ref"]
                for part in value["allOf"]
                if isinstance(part, JsonRef)
            ]
            if refs:
                return refs[-1]
        return inherited

    def _generate_any_of(
        self, ctx: Context, scenario: Scenario, builder: SchemaGeneratorBuilder
    ):
        return self._generate_variant(ctx, scenario, builder, "anyOf")

    def _generate_one_of(
        self, ctx: Context, scenario: Scenario, builder: SchemaGeneratorBuilder
    ):
        return self._generate_variant(ctx, scenario, builder, "oneOf")

    def _generate_variant(
        self,
        ctx: Context,
        scenario: Scenario,
        builder: SchemaGeneratorBuilder,
        kind: str,
    ) -> Any:
        """Generate a node from the branch chosen for a oneOf/anyOf.

        JSON Schema evaluates the keywords next to ``oneOf``/``anyOf``
        together with the chosen branch, so when the parent carries
        constraints of its own (``type``, ``properties``, ``required``,
        ``format``...) the two are merged the way ``allOf`` is before
        generating. A parent that holds nothing but the branch list and
        annotations (``discriminator``, ``title``...) recurses into the
        branch directly, which keeps ``$ref`` identity for
        ``schema_path`` tracking.

        For ``oneOf`` the instance must match exactly one branch, so the
        properties that only the other branches require are recorded on
        the context (``excluded_properties``) and left out by the object
        handler unless the merged schema requires them or the scenario
        names them. Their definitions stay in place, so a nested
        ``oneOf`` that requires one of them can still generate it.
        """
        parent = ctx.schema_data
        schemas = parent.get(kind, [])
        selected = self._resolve_variant(ctx, schemas, scenario, kind)

        siblings = {
            k: v
            for k, v in parent.items()
            if k != kind
            and k not in _VARIANT_ANNOTATIONS
            and not k.startswith("x-")
        }
        if not siblings:
            # Recurse into the branch itself (keeps $ref identity and
            # schema_path), still leaving out what only the other oneOf
            # branches require.
            excluded = ctx.excluded_properties
            if kind == "oneOf":
                flat = self.allof_merger({"allOf": [selected]})
                excluded = excluded + self._required_only_elsewhere(
                    schemas, selected, flat if isinstance(flat, dict) else {}
                )
            return self._generate_node(
                ctx.copy(schema_data=selected, excluded_properties=excluded),
                scenario,
                builder,
            )

        merged = self.allof_merger({"allOf": [siblings, selected]})
        excluded = ctx.excluded_properties
        if kind == "oneOf":
            excluded = excluded + self._required_only_elsewhere(
                schemas, selected, merged
            )

        updates: Dict[str, Any] = {
            "schema_data": merged,
            "parent_schema": parent,
            "excluded_properties": excluded,
        }
        if isinstance(selected, JsonRef):
            updates["schema_path"] = selected.__reference__["$ref"]
        return self._generate_node(ctx.copy(**updates), scenario, builder)

    def _required_only_elsewhere(
        self, schemas: List[Any], selected: Any, merged: Dict[str, Any]
    ) -> Tuple[str, ...]:
        """Names the non-selected ``oneOf`` branches require that the
        merged schema does not. Each branch is flattened through the
        allOf merger, so ``required`` nested in ``allOf`` and behind
        ``$ref`` counts too."""
        required = set(merged.get("required") or [])
        names: List[str] = []
        for branch in schemas:
            if branch is selected:
                continue
            flat = self.allof_merger({"allOf": [branch]})
            if not isinstance(flat, dict):
                continue
            for name in flat.get("required") or []:
                if name not in required and name not in names:
                    names.append(name)
        return tuple(names)

    def _resolve_variant(
        self,
        ctx: Context,
        schemas: List[Dict[str, Any]],
        scenario: Scenario,
        kind: str,
    ) -> Dict[str, Any]:
        """Pick one branch from a oneOf/anyOf candidate list.

        Selector lookup in ``scenario.oneof_selectors``:
          1. exact match on ``ctx.prop_path``;
          2. else first regex (``re.fullmatch``) in insertion order.

        Selector return values:
          * ``int`` — index into ``schemas`` (``bool`` rejected).
          * ``str`` — resolved against candidate ``title``, then the
            OpenAPI discriminator value (mapping key or the const/enum/
            default of the property named by ``discriminator.propertyName``).
          * ``dict`` — taken as the selected schema fragment.
        """
        if not schemas:
            raise ValueError(
                f"{kind} has no candidates at path "
                f"'{ctx.prop_path or '<root>'}'"
            )

        selector = self._lookup_variant_selector(ctx.prop_path, scenario)
        if selector is None:
            candidates = schemas
            if ctx.recursive:
                # On a cycle, prefer branches that do not re-enter a node
                # already on the path, so the recursion escapes to a leaf.
                escaping = [
                    s for s in schemas if not self._reenters(s, ctx.ref_path)
                ]
                if escaping:
                    candidates = escaping
            return candidates[random.randint(0, len(candidates) - 1)]

        sel_res = selector(ctx, schemas)

        if isinstance(sel_res, bool):
            raise TypeError(
                f"{kind} selector at '{ctx.prop_path}' returned a bool; "
                "expected int, str, or dict"
            )
        if isinstance(sel_res, int):
            if sel_res < 0 or sel_res >= len(schemas):
                raise IndexError(
                    f"{kind} selector at '{ctx.prop_path}' returned "
                    f"out-of-range index {sel_res} "
                    f"(valid: 0..{len(schemas) - 1})"
                )
            return schemas[sel_res]
        if isinstance(sel_res, str):
            return self._resolve_variant_by_name(sel_res, schemas, ctx, kind)
        if isinstance(sel_res, dict):
            return sel_res

        raise TypeError(
            f"{kind} selector at '{ctx.prop_path}' returned "
            f"{type(sel_res).__name__}; expected int, str, or dict"
        )

    @classmethod
    def _reenters(
        cls, candidate: Any, ref_path: Tuple[Any, ...], depth: int = 3
    ) -> bool:
        """True if *candidate* is, or holds within *depth* levels of
        ``properties``, ``items`` or ``allOf``, a schema node already on
        the path."""
        on_path = set(ref_path)
        if schema_marker(candidate) in on_path:
            return True
        node = (
            candidate.__subject__
            if isinstance(candidate, JsonRef)
            else candidate
        )
        if not isinstance(node, dict):
            return False
        if schema_marker(node) in on_path:
            return True
        if depth <= 0:
            return False
        children: List[Any] = []
        props = node.get("properties")
        if isinstance(props, dict):
            children.extend(props.values())
        if "items" in node:
            children.append(node["items"])
        children.extend(node.get("allOf") or [])
        return any(
            cls._reenters(child, ref_path, depth - 1) for child in children
        )

    @staticmethod
    def _lookup_variant_selector(
        path: str, scenario: Scenario
    ) -> Optional[Callable[[Context, List[Dict[str, Any]]], Any]]:
        selectors = scenario.oneof_selectors
        if path in selectors:
            return selectors[path]
        for pattern, sel in selectors.items():
            if pattern == path:
                continue
            try:
                if re.fullmatch(pattern, path) is not None:
                    return sel
            except re.error:
                # Treat invalid regex as non-matching rather than crashing.
                continue
        return None

    @staticmethod
    def _resolve_variant_by_name(
        name: str,
        schemas: List[Dict[str, Any]],
        ctx: Context,
        kind: str,
    ) -> Dict[str, Any]:
        # 1. title match
        for s in schemas:
            if isinstance(s, dict) and s.get("title") == name:
                return s

        # 2. discriminator-based match
        disc = None
        if isinstance(ctx.schema_data, dict):
            disc = ctx.schema_data.get("discriminator")
        if disc is None and isinstance(ctx.parent_schema, dict):
            disc = ctx.parent_schema.get("discriminator")

        prop_name = None
        mapping: Dict[str, Any] = {}
        if isinstance(disc, dict):
            prop_name = disc.get("propertyName")
            mapping = disc.get("mapping") or {}

        # 2a. discriminator.mapping lookup
        target_ref = mapping.get(name)
        if target_ref is not None:
            for s in schemas:
                if not isinstance(s, dict):
                    continue
                # Schemas may still carry a $ref (jsonref inlined) — check
                # either the raw ref or a title match on the referenced name.
                if s.get("$ref") == target_ref:
                    return s
                # Fall back to matching by the last path segment of the ref.
                ref_tail = str(target_ref).rsplit("/", 1)[-1]
                if s.get("title") == ref_tail:
                    return s

        # 2b. match by the discriminator property's const/default/enum[0]
        if prop_name:
            for s in schemas:
                if not isinstance(s, dict):
                    continue
                prop = (s.get("properties") or {}).get(prop_name)
                if not isinstance(prop, dict):
                    continue
                if prop.get("const") == name:
                    return s
                if prop.get("default") == name:
                    return s
                enum = prop.get("enum")
                if isinstance(enum, list) and name in enum:
                    return s

        # Build a helpful error listing what was available.
        available_titles = [
            s.get("title")
            for s in schemas
            if isinstance(s, dict) and s.get("title")
        ]
        raise ValueError(
            f"{kind} selector at '{ctx.prop_path}' returned string "
            f"{name!r}, but no candidate matched. "
            f"Available titles: {available_titles or 'none'}; "
            f"discriminator.propertyName: {prop_name!r}; "
            f"discriminator.mapping keys: {list(mapping)}"
        )

    def _subtree_has_coverage(
        self,
        child_path: str,
        scenario: Scenario,
        builder: SchemaGeneratorBuilder,
    ) -> bool:
        """Return True if the scenario covers child_path or any descendant.

        Coverage = exact override, pattern_override that could match a
        descendant path, default_data seed, or oneof_selector entry.
        """
        for key in scenario.overrides:
            if path_startswith(child_path, key):
                return True
        for key in scenario.oneof_selectors:
            if path_startswith(child_path, key):
                return True
        if builder.has_value_at_path(child_path):
            return True
        for pat, _ in scenario.pattern_overrides:
            if pat in child_path or child_path in pat:
                return True
        return False

    def _should_include_optional(
        self,
        child_path: str,
        scenario: Scenario,
        builder: SchemaGeneratorBuilder,
    ) -> bool:
        """Return True if an optional property at *child_path* should be
        included when :attr:`Scenario.minimal_mode` is active.

        A field is included when any of the following is true:

        * An entry in ``scenario.overrides`` equals or starts with
          *child_path* (exact match or a descendant path).
        * ``scenario.default_data`` already seeded a value at *child_path*
          (checked via the builder's ``has_value_at_path``).
        * An entry in ``scenario.oneof_selectors`` equals or starts with
          *child_path*.

        ``pattern_overrides`` are intentionally excluded — they apply to
        fields that survive filtering but do not force optional fields in.
        """
        for key in scenario.overrides:
            if path_startswith(child_path, key):
                return True
        if builder.has_value_at_path(child_path):
            return True
        for key in scenario.oneof_selectors:
            if path_startswith(child_path, key):
                return True
        return False

    def _handle_array(
        self, ctx: Context, scenario: Scenario, builder: SchemaGeneratorBuilder
    ) -> List[Any]:
        """
        Handle array type schema by generating array elements.

        Args:
            ctx: The current generation context
            scenario: The scenario to use for generation
            builder: The builder instance for this generation

        Returns:
            List of generated array items
        """
        schema = ctx.schema_data
        items = schema.get("items", {})

        if self._reentry_cut(ctx, schema_marker(schema), builder):
            return None
        recursive = ctx.recursive or schema_marker(schema) in ctx.ref_path
        min_items = schema.get("minItems", 0)
        bounds = [
            b
            for b in (schema.get("maxItems"), self.generator_max_items)
            if b is not None
        ]
        max_items = min(bounds) if bounds else max(min_items, 2)

        unique = bool(schema.get("uniqueItems"))
        domain = self._unique_domain(items) if unique else None
        if unique:
            # A closed domain bounds how many distinct items can exist.
            size = (
                len(domain)
                if domain is not None
                else self._integer_domain_size(items)
            )
            if size is not None:
                if min_items > size:
                    raise ValueError(
                        f"uniqueItems at '{ctx.prop_path}': minItems="
                        f"{min_items} exceeds the {size} distinct value(s) "
                        "the item schema allows"
                    )
                max_items = min(max_items, size)

        if recursive:
            # The array schema recurs on the path: only the items the
            # schema insists on, so that the recursion ends.
            count = min_items
        else:
            count = random.randint(min_items, max_items)

        # Derive schema_path for array items when items is a $ref (or an
        # allOf of refs)
        item_schema_path = self._property_schema_path(items, None)

        # The root container is never an uncovered optional field, so it
        # is written even in scenario_only mode.
        if not scenario.scenario_only or not ctx.prop_path:
            builder.ensure_container(ctx.prop_path, [])

        child_markers = ctx.ref_path + (schema_marker(schema),)
        result: List[Any] = []
        for i in range(count):
            child_ctx = ctx.copy(
                prop_path=join_path(ctx.prop_path, i),
                schema_data=items,
                parent_schema=schema,
                schema_path=item_schema_path or ctx.schema_path,
                data=builder.generated,
                ref_path=child_markers,
                recursive=recursive,
                excluded_properties=(),
            )
            value = self._generate_node(child_ctx, scenario, builder)
            if unique:
                try:
                    value = self._ensure_unique_item(
                        ctx, child_ctx, i, value, domain, scenario, builder
                    )
                except _NoDistinctItem as exc:
                    if i < min_items:
                        raise ValueError(
                            f"uniqueItems at '{ctx.prop_path}': minItems="
                            f"{min_items} cannot be met, {exc}"
                        ) from None
                    # The domain is exhausted (a shape the cap cannot
                    # size): end the array here rather than fail.
                    stored = builder.get_value_at_path(ctx.prop_path)
                    if isinstance(stored, list) and len(stored) > i:
                        del stored[i:]
                    break
            result.append(value)

        return result

    @staticmethod
    def _unique_domain(items: Any) -> Optional[List[Any]]:
        """The distinct values an item schema with a closed domain
        (``const``, ``enum`` or boolean) can take, else ``None``."""
        node = items.__subject__ if isinstance(items, JsonRef) else items
        if not isinstance(node, dict):
            return None
        if "const" in node:
            return [node["const"]]
        if isinstance(node.get("enum"), list):
            seen: Dict[Any, Any] = {}
            for value in node["enum"]:
                seen.setdefault(_json_key(value), value)
            return list(seen.values())
        if node.get("type") == "boolean":
            return [True, False]
        return None

    @staticmethod
    def _integer_domain_size(items: Any) -> Optional[int]:
        """Number of integers a bounded integer item schema allows, or
        ``None`` when the schema is not an integer with both bounds (or
        has ``multipleOf``)."""
        node = items.__subject__ if isinstance(items, JsonRef) else items
        if (
            not isinstance(node, dict)
            or node.get("type") != "integer"
            or "multipleOf" in node
        ):
            return None

        def number(value: Any) -> Optional[float]:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            return value

        low = number(node.get("minimum"))
        high = number(node.get("maximum"))
        ex_low = node.get("exclusiveMinimum")
        ex_high = node.get("exclusiveMaximum")
        if number(ex_low) is not None:
            ex = math.floor(number(ex_low)) + 1
            low = ex if low is None else max(math.ceil(low), ex)
        elif low is not None:
            low = math.floor(low) + 1 if ex_low is True else math.ceil(low)
        if number(ex_high) is not None:
            ex = math.ceil(number(ex_high)) - 1
            high = ex if high is None else min(math.floor(high), ex)
        elif high is not None:
            high = math.ceil(high) - 1 if ex_high is True else math.floor(high)
        if low is None or high is None:
            return None
        return max(int(high) - int(low) + 1, 0)

    def _ensure_unique_item(
        self,
        ctx: Context,
        child_ctx: Context,
        index: int,
        value: Any,
        domain: Optional[List[Any]],
        scenario: Scenario,
        builder: SchemaGeneratorBuilder,
    ) -> Any:
        """Make the item at *index* differ, as a JSON value, from the
        earlier items of the array at ``ctx.prop_path`` (``uniqueItems``).

        A closed domain (``const``, ``enum``, boolean) is sampled without
        replacement; an open domain is redrawn up to
        :data:`UNIQUE_ITEMS_ATTEMPTS` times. An item an override chose is
        left alone. A slot that was never written (scenario_only, a cut)
        is not compared; a written ``null`` is a value like any other.
        Raises :class:`_NoDistinctItem` when no distinct value exists.
        """
        stored = builder.get_value_at_path(ctx.prop_path)
        if not isinstance(stored, list) or index >= len(stored):
            return value

        earlier = {_json_key(v) for v in stored[:index]}
        item = stored[index]
        if _json_key(item) not in earlier:
            return item
        if self._scenario_defined(child_ctx.prop_path, scenario):
            return item

        if domain is not None:
            unused = [v for v in domain if _json_key(v) not in earlier]
            if not unused:
                raise _NoDistinctItem(
                    f"the item schema allows only {len(domain)} distinct "
                    f"value(s) and index {index} needs another one"
                )
            chosen = copy.deepcopy(random.choice(unused))
            builder.set_value_at_path(child_ctx.prop_path, chosen)
            return chosen

        for _ in range(UNIQUE_ITEMS_ATTEMPTS):
            # Reset the slot so containers are rebuilt from scratch rather
            # than merged with the previous draw.
            builder.set_value_at_path(child_ctx.prop_path, None)
            value = self._generate_node(child_ctx, scenario, builder)
            if index >= len(stored):
                return value
            item = stored[index]
            if _json_key(item) not in earlier:
                return item
        raise _NoDistinctItem(
            f"no value distinct from the earlier items could be drawn for "
            f"index {index} in {UNIQUE_ITEMS_ATTEMPTS} attempts"
        )

    def _handle_object(
        self, ctx: Context, scenario: Scenario, builder: SchemaGeneratorBuilder
    ) -> Dict[str, Any]:
        """
        Handle object type schema by generating properties.

        Args:
            ctx: The current generation context
            scenario: The scenario to use for generation
            builder: The builder instance for this generation

        Returns:
            Dictionary of generated property values
        """
        if self._reentry_cut(ctx, schema_marker(ctx.schema_data), builder):
            return None

        # The root container is never an uncovered optional field, so it
        # is written even in scenario_only mode.
        if not scenario.scenario_only or not ctx.prop_path:
            builder.ensure_container(ctx.prop_path, {})

        props = ctx.schema_data.get("properties", {})
        if props is None:
            return {}

        # The cycle rule: a schema node that recurs on the path is generated
        # with its required properties only, so that recursion ends where
        # the schema makes it optional.
        recursive = (
            ctx.recursive or schema_marker(ctx.schema_data) in ctx.ref_path
        )
        child_markers = ctx.ref_path + (schema_marker(ctx.schema_data),)
        required_set = set(ctx.schema_data.get("required") or [])
        # Properties that only the other oneOf branches require are left
        # out unless this object requires them or the scenario names them.
        excluded = set(ctx.excluded_properties)
        result: Dict[str, Any] = {}

        for k, v in props.items():
            child_path = join_path(ctx.prop_path, k)
            if scenario.scenario_only:
                if not self._subtree_has_coverage(
                    child_path, scenario, builder
                ):
                    continue
            elif k not in required_set and (
                scenario.minimal_mode or recursive or k in excluded
            ):
                if not self._should_include_optional(
                    child_path, scenario, builder
                ):
                    continue
            # Inherit schema_path by default; override if this property is
            # a $ref (or an allOf of refs)
            child_schema_path = self._property_schema_path(v, ctx.schema_path)

            child_ctx = ctx.copy(
                prop_path=child_path,
                schema_data=v,
                parent_schema=ctx.schema_data,
                schema_path=child_schema_path,
                data=builder.generated,
                ref_path=child_markers,
                recursive=recursive,
                excluded_properties=(),
            )
            result[k] = self._generate_node(child_ctx, scenario, builder)

        return result

    def _default_value(self, schema: Dict[str, Any]) -> Any:
        """
        Generate a default value for a schema.

        Args:
            schema: The schema to generate a value for

        Returns:
            A generated default value
        """
        return self.default_value_generator(schema)()

    def _resolve_pending_fields(
        self, scenario: Scenario, builder: SchemaGeneratorBuilder
    ) -> None:
        """
        Attempt to resolve fields that were pending during initial generation.
        This happens when a field depends on other fields that weren't yet generated.

        Args:
            scenario: The scenario to use for resolution
            builder: The builder instance for this generation
        """
        max_retries = 5
        for _ in range(max_retries):
            if not builder.pending_fields:
                break

            still_pending = []
            for ctx in builder.pending_fields:
                try:
                    # Use a copy of the context with the latest generated data
                    updated_ctx = ctx.copy(data=builder.get_result())
                    override = self._apply_scenario(updated_ctx, scenario)
                    builder.set_value_at_path(ctx.prop_path, override)
                except (KeyError, IndexError):
                    still_pending.append(ctx)

            builder.pending_fields = still_pending
