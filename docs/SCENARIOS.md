# User Guide: Scenarios

This library generates sample JSON from JSON Schema/OpenAPI. Scenarios let you override generated values deterministically or dynamically.

## What is a Scenario?
A `Scenario` captures how particular fields should be set during generation.

- Use simple values for fixed fields.
- Use callables for dynamic values that can depend on the current `Context`.
- Optional `pattern_overrides` apply by substring matching on the property path.

Normalization: `Scenario.normalize()` converts any simple values into callables under the hood so the generator can treat all overrides uniformly. It mutates the Scenario instance; call it only once at the end or recreate your scenario if you need raw values again.

## The Context object
Overrides receive a `Context` with helpful info:
- `prop_path`: path of the field, e.g. `address.city`, `phones[0].number` or `points[0][1]`; a property name containing `.` or `[` is quoted, as in `["a.b"].c` (see "Property paths" in the README). Override keys use the same syntax.
- `data`: the result built so far. It is a live view, not a snapshot: a value a sibling has already produced is visible, and `ctx.data.get("x", default)` returns the real value once it exists. Reading something not generated yet raises `KeyError` or `IndexError`, which defers the override to a second pass after the first traversal (see "Callable overrides").
- `schema_data`: the schema node for the current property.
- `schema_path`: when the current node comes from a `$ref`, its reference path.

## Basic usage
```python
from src.json_sample_generator import JSONSchemaGenerator
from src.json_sample_generator.models import Schema, Scenario

schema = Schema(data={
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "email": {"type": "string", "format": "email"},
        "age": {"type": "integer"},
        "address": {
            "type": "object",
            "properties": {
                "city": {"type": "string"}
            }
        }
    }
})

# Simple values get normalized to callables automatically
scenario = Scenario(
    name="demo",
    overrides={
        "id": "user-1",
        "age": 30,
        "address.city": "Dublin",
    }
).normalize()

gen = JSONSchemaGenerator(schema)
result = gen.generate(scenario)
```

## Callable overrides

```python
def derived_email(ctx):
    # You can use ctx.data to reference other fields already set
    name = ctx.data.get("name", "user").lower()
    return f"{name}@example.com"

scenario = Scenario(
    name="callables",
    overrides={
        "name": "Alice",
        "email": derived_email,  # receives Context
    }
).normalize()
```

## Pattern overrides
`pattern_overrides` apply a rule when the substring is present in the property path.

```python
scenario = Scenario(
    name="pattern",
    pattern_overrides=[
        ("phones[", lambda ctx: "+100000000"),  # applies to phones[0].number, phones[1]..., etc.
        ("address.", "N/A"),  # sets all address.* fields to "N/A" unless more specific overrides exist
    ],
).normalize()
```

Notes:
- Matching is substring, not regex. Use specific substrings to limit scope.
- Direct overrides for exact paths win over pattern overrides.

## Variant selectors (`oneof_selectors` / `variant_selectors`)

Provide `oneof_selectors` on `Scenario` to choose a `oneOf` **or** `anyOf`
branch deterministically instead of letting the generator pick randomly.

### Key lookup

1. **Exact key** — the key matches `ctx.prop_path` literally.
2. **Regex fallback** — keys are treated as `re.fullmatch` patterns; the
   first matching key (in insertion order) wins.

Use the regex form to target every item in an array at once:

```python
scenario = Scenario(
    name="variants",
    oneof_selectors={
        # exact path: pick index 1 from the oneOf on "pet"
        "pet": 1,

        # regex: apply to items[0], items[1], … — any array index
        r"order\.items\[\d+\]": lambda ctx, schemas: 0,
    },
).normalize()
```

### Selector return values

| Return type | Behaviour |
|---|---|
| `int` | Selects the candidate at that index (bounds-checked, `bool` rejected). |
| `str` | Matched against each candidate's `title`, then the OpenAPI `discriminator` mapping (a `$ref` branch by the reference it was written with), then `properties[propertyName].const/enum/default`, then, with a discriminator, the name of the schema a `$ref` branch refers to (OpenAPI's implicit mapping). A schema loaded with `Schema.from_oas` or `Schema.from_raw_data` has no `$ref` branches left. Raises `ValueError` on no match. |
| `dict` | Taken as the selected schema fragment directly. |

```python
# Select by schema title (works for both oneOf and anyOf)
Scenario(
    name="by_title",
    oneof_selectors={"pet": "Cat"},
).normalize()

# Select by OpenAPI discriminator value
Scenario(
    name="by_disc",
    oneof_selectors={"pet": "cat"},  # matches properties.kind.const == "cat"
).normalize()
```

### Notes
- `normalize()` wraps bare `int`, `str`, and `dict` values into callables.
- The field alias `variant_selectors` is a read-only synonym for
  `oneof_selectors` — both names refer to the same dict.
- `bool` returns from a selector raise `TypeError` (Python's `isinstance(True, int)` gotcha).
- If no selector matches a path, a random candidate is chosen (default behaviour).

### How the chosen branch is generated

JSON Schema evaluates the keywords next to `oneOf`/`anyOf` together with
the chosen branch, and so does the generator: when the parent carries
constraints of its own (`type`, `properties`, `required`, `format`, ...)
they are merged with the branch the way `allOf` is before generating.
These common idioms therefore work:

```python
# an exclusive choice of properties
{
    "type": "object",
    "properties": {"name": {"type": "string"}, "code": {"type": "integer"}},
    "oneOf": [{"required": ["name"]}, {"required": ["code"]}],
}

# a choice of format
{"type": "string", "oneOf": [{"format": "ipv4"}, {"format": "ipv6"}]}
```

When the parent and the branch both define a property, the two
definitions are combined (`allOf`), so a branch may refine a parent
property with a `pattern`, a `maximum`, a `const`, and so on. A property
re-declared with the same `$ref` keeps that single reference, and a
property defined as an `allOf` of references takes its `schema_path` from
the last reference.

For `oneOf` the instance must match exactly one branch. The generator
guarantees that for branches that differ in `required` (including
`required` nested in their `allOf` or behind `$ref`): a property that only
the *other* branches require is left out of the generated object, unless
the merged schema requires it or an override, `default_data` or a selector
names it. Branches that differ in other ways are **not** checked for
exclusivity, since the generator does not validate: with `{"type":
"number"}` next to `{"type": "integer"}`, overlapping numeric ranges, or
`{"type": "object"}` next to `{"required": ["x"]}`, the sample may satisfy
more than one branch.

A parent that holds nothing but the branch list and annotations
(`discriminator`, `title`, `description`, ...) is generated from the
branch alone, so `$ref` branches keep their `schema_path`; the properties
that only the other `oneOf` branches require are left out there too.

With an OpenAPI `discriminator`, the property it names is set to the
value that names the chosen branch: the branch's key in
`discriminator.mapping` (the key a `str` selector gave, when several refer
to the branch), or, for a branch the mapping does not list, the name of
the schema its `$ref` points at (`#/components/schemas/Dog` gives `Dog`,
as OpenAPI's implicit mapping does). The branch is generated as without
a discriminator, and only then the value it got for the property is
replaced, so no property appears that would not have otherwise (an
optional one in `minimal_mode` or on a cycle, one that only the other
`oneOf` branches require, one `scenario_only` does not cover) and every
other value is the same. The value is left as generated when the branch
pins the property with its own `const` or `enum`, when the branch is
polymorphic in turn (its own `oneOf`/`anyOf`, whose level names the
concrete type), when the value would not validate against the
property's schema, and when an override or `default_data` sets it. An
inline branch is named only through a mapping that refers to its
`title`. A schema loaded with `Schema.from_oas` or `Schema.from_raw_data`
has its `$ref`s resolved, so its branches have no name to give, for the
value nor for a `str` selector: pin the property with `const` in each
branch (strict OpenAPI variants do), or load the document with
`Schema(data=..., base_uri=...)`.


## Reusing and composing scenarios
There are two convenient approaches:

1) Keep raw values until the end, merge dicts, then call `normalize()` once.
```python
base = Scenario(name="base", overrides={
    "is_active": True,
})

feature = Scenario(name="feature", overrides={
    "role": "admin",
})

# Compose raw values, then normalize
combined = Scenario(
    name="combined",
    overrides={**base.overrides, **feature.overrides},
    pattern_overrides=[*base.pattern_overrides, *feature.pattern_overrides],
).normalize()
```

2) If you already called `normalize()` on bases, you’re working with callables.
```python
base = Scenario(name="base", overrides={"is_active": True}).normalize()
extra = Scenario(name="extra", overrides={"tier": "gold"}).normalize()

# Merge callable maps (later keys override earlier ones)
reused = Scenario(
    name="reused",
    overrides={**base.overrides, **extra.overrides},
    pattern_overrides=[*base.pattern_overrides, *extra.pattern_overrides],
)
```

Tip: prefer approach (1) for simple authoring. Normalize only at the end just before `generate()`.

## Using scenarios across runs
- Pass a `Scenario` each time: `gen.generate(scenario)`
- Or set a default when creating the generator: `JSONSchemaGenerator(schema, scenario=default_scenario)`

## Arrays and indices in paths
Use bracket notation to target array items when overriding.

```python
Scenario(
    name="array",
    overrides={
        "phones[0].number": "+200000000",
    }
)
```

## Scenario default_data (initialize the result)
You can pre-populate the generated result with default data using the `default_data` field on `Scenario`. These defaults are deep-merged into the result before generation starts, and primitive values from `default_data` are preserved (not overwritten by the generator).

```python
from src.json_sample_generator import JSONSchemaGenerator
from src.json_sample_generator.models import Schema, Scenario

schema = Schema(data={
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "age": {"type": "integer"},
        "nested": {
            "type": "object",
            "properties": {
                "flag": {"type": "boolean"},
                "count": {"type": "integer"},
            },
        },
    },
})

scenario = Scenario(
    name="with-defaults",
    default_data={
        "name": "Alice",
        "nested": {"flag": True},
    },
)

gen = JSONSchemaGenerator(schema)
result = gen.generate(scenario)

assert result["name"] == "Alice"          # preserved from defaults
assert result["nested"]["flag"] is True   # preserved from defaults
assert isinstance(result["age"], int)       # generator fills in the rest
assert "count" in result["nested"]
```

Behavior notes:
- `default_data` is deep-merged into the result at the start.
- Primitive values present in `default_data` are kept and not overwritten by the generator.
- Object defaults are merged; missing properties are added by the generator.
- Overrides and pattern overrides still apply as usual and can compute values based on the evolving Context.

## Enumerating scenarios for oneOf/anyOf coverage

Use `collect_variant_sites`, `cartesian_scenarios`, and `minimal_scenarios`
from `json_sample_generator` to auto-build a scenario set that covers every
branch in your schema.

```python
from json_sample_generator import (
    JSONSchemaGenerator,
    cartesian_scenarios,
    minimal_scenarios,
)
from json_sample_generator.models import Schema

schema = Schema.from_raw_data(
    {
        "type": "object",
        "properties": {
            "animal": {
                "oneOf": [
                    {"title": "Dog", "type": "object",
                     "properties": {"kind": {"const": "dog"}}},
                    {"title": "Cat", "type": "object",
                     "properties": {"kind": {"const": "cat"}}},
                ]
            },
            "color": {"oneOf": [{"const": "red"}, {"const": "blue"}]},
        },
    },
    base_uri="file://example.json",
)

gen = JSONSchemaGenerator(schema=schema)

# Full cartesian product: 2 × 2 = 4 scenarios
for scenario in cartesian_scenarios(schema):
    print(scenario.name, scenario.description)
    result = gen.generate(scenario)
    print(result)

# Minimal 1-wise cover: max(2, 2) = 2 scenarios
# — every variant of every site appears at least once
for scenario in minimal_scenarios(schema):
    print(scenario.name, scenario.description)
    result = gen.generate(scenario)
    print(result)
```

### API

| Function | Returns | Count |
|---|---|---|
| `collect_variant_sites(schema)` | `list[VariantSite]` | — |
| `cartesian_scenarios(schema, *, name_prefix, base, max_scenarios)` | `list[Scenario]` | `∏ site.count` |
| `minimal_scenarios(schema, *, name_prefix, base)` | `list[Scenario]` | `max(site.count)` |

**`VariantSite` fields:** `path` (e.g. `"animal"` or `"items[*]"`), `kind`
(`"oneOf"` or `"anyOf"`), `count`, `names` (titles/discriminator values, or
`"variant_0"`, `"variant_1"`, …).

**`base` parameter:** pass an existing `Scenario` to inherit its `overrides`,
`pattern_overrides`, and `default_data` on every generated scenario.

**Cartesian cap:** if the product would exceed `max_scenarios` (default
10 000), `cartesian_scenarios` raises `ValueError`. Pass `max_scenarios=N`
to override.

**Nested sites:** sites nested inside oneOf variants are collected
independently. The coverage claim ("every variant appears at least once")
holds per site; whether a nested branch is reachable depends on which outer
variant was selected.

## Minimal mode

Set `minimal_mode=True` on a `Scenario` to generate the smallest valid
sample: only **required** fields (recursively through nested objects,
`allOf`, `oneOf`, `anyOf`) plus any fields you have **explicitly
referenced** in the scenario.

```python
scenario = Scenario(
    name="smoke",
    minimal_mode=True,
    overrides={"desc": "forced"},   # pulls in optional "desc"
)
```

### Inclusion rules (applied per object node)

An optional property `k` at path `P` is kept when any of the following
is true:

| Condition | Detail |
|---|---|
| `k in required` | The parent schema's `required` list contains `k`. |
| Exact override | `scenario.overrides` has a key equal to `P`. |
| Descendant override | Any `overrides` key starts with `P.` or `P[`. |
| `default_data` pre-seeded | The builder already holds a value at `P` (via `default_data`). |
| Selector targets subtree | Any `oneof_selectors` key equals `P` or starts with `P.` / `P[`. |

`pattern_overrides` are **not** forcing — they apply to fields that
survive filtering for other reasons but do not cause optional fields to
appear. Because patterns are substring matches they can be broad; treating
them as forcing would often pull in unintended fields.

### Interaction with other features

- **`allOf`** — `allof_merge` already unions `required` across all
  branches, so the merged schema's `required` list is the authoritative
  source for the minimal filter.
- **`oneOf` / `anyOf`** — variant selection runs first (respecting
  `oneof_selectors`), then the chosen branch's own `required` governs
  which fields appear.
- **Arrays** — the array field itself is subject to the object-level
  filter. Once kept, every item is generated with `minItems`/`maxItems`
  bounds unchanged, and the minimal filter recurses into each item's
  object schema.
- **`max_depth`** and ref resolution — unchanged.
- **`default_data`** — pre-seeded values at a path force that path to be
  kept, and seeded values are preserved in the output.

## Scenario-only mode

Set `scenario_only=True` on a `Scenario` to emit **only** what the
scenario explicitly covers. This is stricter than `minimal_mode`:
even `required` fields are dropped if the scenario does not touch
them. The resulting sample may be schema-invalid by design — useful
for focused fixtures and contract-test payloads where the test only
cares about a specific subset of fields.

```python
scenario = Scenario(
    name="contract",
    scenario_only=True,
    overrides={"address.city": "NYC"},
)
# result: {"address": {"city": "NYC"}}
# "id", "name", "address.street" etc. are all absent
```

### Coverage rules

A field at path `P` is **covered** (and therefore emitted) when any
of the following is true:

| Condition | Detail |
|---|---|
| Exact override | `scenario.overrides` has a key equal to `P`. |
| Descendant override | Any `overrides` key starts with `P.` or `P[`. |
| Pattern override | Any pattern string in `pattern_overrides` is a substring of `P`, **or** `P` is a substring of the pattern (permissive check to avoid false negatives on deep paths). |
| `default_data` pre-seeded | The builder already holds a value at `P` (from `default_data` merge). |
| Selector targets subtree | Any `oneof_selectors` key equals `P` or starts with `P.` / `P[`. |

If none of these conditions hold for a given path, no value is written
and the field does not appear in the output. Ancestor objects and arrays
with no covered descendants are also absent because they are only
materialised by writing to leaf paths.

### Interaction with other features

- **`minimal_mode`** — if both are `True`, `scenario_only` wins (it is
  strictly stricter). Required fields are still omitted if uncovered.
- **`oneOf` / `anyOf`** — the branch is picked as normal (random or via
  `oneof_selectors`); inside the chosen branch only covered fields appear.
- **Arrays** — if no covered path descends into the array the whole array
  is omitted. When covered (e.g. via `pattern_overrides` or
  `default_data`), items are generated with the normal bounds.
- **`default_data`** — pre-seeded values are preserved exactly as provided.

## Best practices
- Prefer simple values; use callables only when you need context-aware logic.
- Keep scenarios small and composable; merge at the edge.
- Use `pattern_overrides` sparingly and with specific substrings.
- Call `.normalize()` once at the end to avoid accidental mutations.
