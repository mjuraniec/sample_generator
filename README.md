# json_sample_generator

Generate sample data from JSON Schema or OpenAPI (OAS) schemas. Create realistic samples for tests, examples, and fixtures.

[![CI](https://github.com/bartoszm/sample_generator/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/bartoszm/sample_generator/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/sample-generator.svg)](https://pypi.org/project/sample-generator/)
[![PyPI downloads/month](https://img.shields.io/pypi/dm/sample-generator.svg)](https://pypi.org/project/sample-generator/)
[![License](https://img.shields.io/github/license/bartoszm/sample_generator.svg)](https://github.com/bartoszm/sample_generator/blob/main/LICENSE)
[![Python versions](https://img.shields.io/pypi/pyversions/sample-generator.svg)](https://pypi.org/project/sample-generator/)

## Installation

From PyPI:

```bash
pip install sample-generator
# importable module name remains `json_sample_generator`
```

Or with uv:

```bash
uv add sample-generator
```

## Quickstart

Prerequisites:
- Python 3.12+
- uv installed

Install uv (Linux/macOS):
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Set up the project:
```bash
# Clone the repo
git clone https://github.com/<your-username>/json_sample_generator.git
cd json_sample_generator

# (Optional) create a virtualenv managed by uv
uv venv  # creates .venv/

# Install runtime deps
uv sync

# For development (tests, tools, etc.)
uv sync --group dev
```

Run tests:
```bash
uv run pytest -q
```

Run examples:
```bash
uv run python examples/simple_value_example.py
```

## Developer guide

Code quality tools used by the project

- uv (astral.sh/uv) — virtualenv + task runner used in CI
- ruff — linting (CI runs `uvx ruff check .`)
- black — formatting (CI runs `uvx black --check .`)
- isort — import sorting (CI runs `uvx isort --check-only .`)
- pytest — test runner (CI runs `uv run pytest -q`)

Automated-fix hints

- Run ruff's auto-fixer to apply quick lint fixes:
	```uvx ruff check . --fix```
- Reformat code with black:
	```uvx black .```
- Sort imports with isort:
	```uvx isort .```
- Run the full CI steps locally (create venv first):
	```bash
    uv venv
	uv sync --group dev
	uvx ruff check .
	uvx black --check . && uvx isort --check-only .
	uv run pytest -q
    ```

These commands mirror what GitHub Actions runs so you can reproduce and fix CI failures locally.

Build and publish reminders

The repository's publish workflow builds with `uv build` and uses the pypa/gh-action-pypi-publish action; for first-time releases you may need a PyPI API token (or configure OIDC/trusted publishing on PyPI).

Pre-commit (recommended):
```bash
uvx pre-commit install
uvx pre-commit run --all-files
```

## User guide: Scenarios

Scenarios let you override generated values per field path with simple values or callables, and optionally with pattern-based rules. They accept a Context so overrides can depend on other fields.

Key `Scenario` flags:

- `minimal_mode=True` — emit only `required` fields plus any explicitly referenced by the scenario.
- `scenario_only=True` — stricter: emit **only** fields covered by the scenario (overrides, pattern_overrides, default_data, oneof_selectors), regardless of `required`. Produces focused fixtures that may be schema-invalid by design.

A `oneOf`/`anyOf` branch is generated together with the keywords next to it (`type`, `properties`, `required`, `format`, ...), so branches that only add `required` or `format` work; see "How the chosen branch is generated" in the guide.

See the full guide (including `default_data`) in [`docs/SCENARIOS.md`](docs/SCENARIOS.md).

## User guide: Loading from OpenAPI (OAS)

To load a component schema from an OpenAPI Specification:

```python
import yaml
from src.json_sample_generator import JSONSchemaGenerator
from src.json_sample_generator.models import Schema

with open("api.yaml") as f:
    oas = yaml.safe_load(f)

schema = Schema.from_oas(oas, name="Pet")
gen = JSONSchemaGenerator(schema)
sample = gen.generate()
```

This correctly resolves cross-component `$ref` pointers. See the full guide in [`docs/OPENAPI.md`](docs/OPENAPI.md) for advanced usage, the `jsonref` caching details, and when to use `from_raw_data` vs `from_oas`.

## User guide: Scalar constraints

When no scenario override applies, `DefaultValueGenerator` draws a value
that honours the keywords below. When a schema cannot be satisfied it
raises `ValueError` naming the schema, rather than returning a value that
would fail validation.

- `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum` — both the
  numeric form and the draft-4 / OpenAPI 3.0 boolean form are understood;
  when a plain and an exclusive bound are both given, the tighter one
  wins. With a single bound the default span is kept: `{"minimum": 576}`
  draws 576..676, `{"maximum": -5}` draws -105..-5 (integers), and numbers
  use a span of 1.0. Exclusive bounds are exact (`floor(x) + 1`,
  `ceil(x) - 1` for integers, the open interval for numbers). An empty or
  inverted range raises.
- `multipleOf` — a random multiple inside the bounds. A one-sided bound is
  widened to reach the first multiple (`minimum: 99, multipleOf: 7` can
  give 105). Float steps are checked the way validators do, so
  `0 < x < 0.4, multipleOf: 0.1` raises when the only candidate, `0.3`,
  fails `0.3 / 0.1` in floating point.
- `minLength`, `maxLength` — applied on their own and together with
  `pattern` or `format`. `email`, `hostname`, `uri`, `url`, `phone` and
  `ipv4` are drawn from Faker first and then built to a fitting length
  (`a@xmi.vw` for `maxLength: 12`, `1.2.3.4` for `maxLength: 7`).
  Fixed-shape formats (`uuid`, `ipv6`, `date`, `time`, `date-time`) must
  fit as they are, so `uuid` with `maxLength: 10` raises. A `pattern` is
  drawn with its open-ended repeats capped at `maxLength` and floored at
  `minLength`, so `^[a-z]{200,}$` and `^[a-z]+$` with `minLength: 150`
  both work; nested open-ended repeats such as `(a+)+` draw each level
  independently, so a tight `maxLength` may be missed and raise.
  `minLength > maxLength` raises. Unknown formats are annotations and take
  the plain string path.
- Integer bounds stay exact above 2**53 (`format: int64` with
  `maximum: 9223372036854775807` works).
- `format` — `date-time` and `time` values carry a UTC offset
  (`2025-09-28T21:09:04.826286+00:00`, `19:47:52Z`), as RFC 3339 requires.

## User guide: Break Scenarios

Break scenarios take a valid generated sample and intentionally corrupt it so that it fails JSON Schema validation — useful for negative-path tests, validator error-message testing, and schema-evolution checks.

See the full guide in [`docs/BREAK_SCENARIOS.md`](docs/BREAK_SCENARIOS.md).

## User guide: Capping array size with `generator_max_items`

When a schema declares a large `maxItems` (e.g. `10000`) the generator will, by default, pick a random length up to that bound and produce that many child elements. For deeply nested schemas this can be very slow and produce huge payloads that are not useful for tests or fixtures.

Pass `generator_max_items` to the `JSONSchemaGenerator` constructor to apply a *generator-wide* upper bound on array length. It does **not** replace the schema's `maxItems`; it only caps it from above.

```python
from json_sample_generator import JSONSchemaGenerator
from json_sample_generator.models import Schema

schema = Schema(data={
    "type": "object",
    "properties": {
        "tags": {
            "type": "array",
            "maxItems": 10000,           # schema permits very large arrays
            "items": {"type": "string"},
        }
    },
    "required": ["tags"],
})

# Cap every array generated by this instance at 5 elements.
gen = JSONSchemaGenerator(schema, generator_max_items=5)
sample = gen.generate()
assert len(sample["tags"]) <= 5
```

### Algorithm

For each array node, the effective upper bound is computed as:

1. If the schema **does not** declare `maxItems`:
   - use `generator_max_items` when it is set,
   - otherwise fall back to `max(minItems, 2)` (the legacy default).
2. If the schema **does** declare `maxItems`:
   - use `min(maxItems, generator_max_items)` when the cap is set,
   - otherwise use `maxItems` as-is.
3. The final element count is `random.randint(minItems, max_items)`.

Consequences:

- `generator_max_items=None` (the default) preserves existing behavior — no global cap is applied.
- When a schema omits `maxItems`, setting `generator_max_items=N` lets arrays grow up to `N` (instead of being silently capped at the `2` default).
- The schema's `minItems` is always respected. If a schema demands `minItems: 10` but you set `generator_max_items=5`, `random.randint(10, 5)` will raise `ValueError`. Choose a cap that is not lower than any `minItems` you expect to encounter.
- The schema's `maxItems` still wins when it is *smaller* than the global cap (`min(...)` semantics).
- Applies to every array node in the schema for that generator instance — there is no per-path override. Use `scenario.overrides` if you need to control a specific array's contents.

### When to use it

- Generating fixtures from third-party OpenAPI specs that declare unrealistically large `maxItems`.
- Speeding up property-based tests where the array size is incidental.
- Producing compact sample payloads for documentation or examples.

## User guide: Property paths

Scenario override keys, `oneof_selectors` keys and `ctx.prop_path` share one path syntax:

| Path | Meaning |
|---|---|
| `address.city` | property `city` of property `address` |
| `phones[0].number` | property `number` of the first element of `phones` |
| `points[0][1]` | second element of the first element of `points` (arrays of arrays) |
| `[0]` | first element, when the root schema is an array |
| `["a.b"].c` | property `c` of a property named `a.b` |

A property name that contains `.`, `[`, `]`, `"` or `\` is written as a JSON string in brackets; any other name is written bare. `json_sample_generator.helpers.utils.join_path` renders a path the way the generator does and `parse_path` splits it back into keys and indices.

Objects and arrays are written to the result as soon as the generator enters them, so a required `{"type": "object"}` without properties yields `{}` and an array that draws zero items yields `[]`. In `scenario_only` mode a container still appears only when the scenario covers something inside it.

## User guide: Recursion, nesting depth and `uniqueItems`

Recursive schemas (`"$ref": "#"`, or components that reference each other) terminate through a cycle rule rather than a depth limit: when a schema node recurs on the current path, it is generated with only its required properties and its `minItems` array items, so the recursion stops wherever the schema makes it optional. While on a cycle, a `oneOf`/`anyOf` without a selector prefers a branch that does not re-enter a node already on the path, so a `Node | Leaf` choice escapes to the leaf. Plain deep nesting is generated in full.

A node may occur on a path twice; its third occurrence is cut, so a recursion that the schema requires all the way down, such as `{"properties": {"child": {"$ref": "#"}}, "required": ["child"]}` or a node with two required children of its own type, ends after a few levels instead of branching without bound. Such a schema has no finite valid sample: a `UserWarning` names the cut path, once per `generate()` call. `JSONSchemaGenerator(max_depth=50)` remains as a hard safety cap on property levels (array indices do not count) and warns the same way. The scenario and break enumerators apply the same rule, cap and limit, so they enumerate exactly the sites the generator can produce.

`Schema.from_oas` and `Schema.from_raw_data` resolve every `$ref` up front, so a recursive schema loaded through them arrives as a structure that contains itself. `JSONSchemaGenerator` writes such a structure back as `$ref`s, the way the document wrote them, before resolving it: `Schema.from_oas` records which schemas are its components, and `Schema.from_raw_data` keeps its references. A component from `Schema.from_oas` therefore generates the same samples as the unresolved document with that component as its root (`{**component, "components": ...}`), `ctx.schema_path` included; a schema from `Schema.from_raw_data` the same samples as the schema it was loaded from, with `ctx.schema_path` naming `#/x-sample-generator-shared/N`. Any other schema is resolved as it is.

`uniqueItems` is honoured by comparing items as JSON values (`1` and `true` differ, `null` is a value). An item schema with a closed domain (`enum`, `const`, boolean, or an integer with both bounds) is sampled without replacement and the array length is capped at the number of distinct values; any other domain is redrawn, and when no distinct value can be drawn the array simply ends there. Only when `minItems` cannot be met, for example an `enum` of two values with `minItems: 3`, does generation raise `ValueError`.

## Contributing

See `CONTRIBUTING.md`.

## License

MIT. See `LICENSE`.