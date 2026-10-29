# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`json_sample_generator` (distributed on PyPI as `sample-generator`) generates sample data from JSON Schema or OpenAPI (OAS) schemas for tests, examples, and fixtures. Requires Python 3.12+. Dependency/task management uses `uv`.

## Common commands

Install dev deps: `uv sync --group dev`

Run the full test suite: `uv run pytest -q`

Run a single test file / function:
```bash
uv run pytest tests/test_json_schema_generator.py -q
uv run pytest tests/test_oneof_selector.py::test_name -q
```

Lint / format (CI mirrors these):
```bash
uvx ruff check .          # uvx ruff check . --fix  to auto-fix
uvx black --check .       # uvx black .             to reformat
uvx isort --check-only .  # uvx isort .             to sort
```

Run an example: `uv run python examples/simple_value_example.py`

Build wheel/sdist: `uv build` (hatchling backend; version is pulled dynamically from `src/json_sample_generator/__init__.py:__version__`).

## Style constraints

- Line length is **79** (configured in `[tool.black]`). Ruff/black/isort must all pass for CI.
- `from __future__ import annotations` is used throughout — prefer it in new modules.

## Architecture

The public surface re-exported from `json_sample_generator/__init__.py` is small: `JSONSchemaGenerator`, `DefaultValueGenerator`, `SchemaGeneratorBuilder`, and `duuid`. Understanding the generator requires reading several files together:

- **`JSONSchemaGenerator`** (`JSONSchemaGenerator.py`) — entrypoint. Constructor deep-copies the input `Schema`, runs `jsonref.replace_refs` to inline `$ref`s (a schema that contains itself, the recursive `$ref` that `Schema.from_oas`/`from_raw_data` resolve up front, is first written back as `$ref` by `_as_references`, from the components `from_oas` records in `Schema._components` or the `JsonRef`s `from_raw_data` keeps), and owns a `Scenario` (defaults to an empty one). `generate(scenario)` walks the schema and produces a sample. Also monkey-patches `proxytypes.LazyProxy.__subject__` to work around a jsonref/LazyProxy interaction — do not remove this without understanding why.
- **`Scenario`** (`models/models.py`) — pydantic model driving overrides. Four knobs: `overrides` (exact property-path → value/callable), `pattern_overrides` (regex-path → value/callable, ordered list), `oneof_selectors` (property-path → callable `(ctx, candidates) -> index|schema` for deterministic `oneOf` choice), and `default_data` (seed values written into the result before generation). `normalize()` wraps bare values in `lambda ctx: value` closures so the generator can treat everything uniformly — call it (or rely on the generator to) before use. See `docs/SCENARIOS.md` for full semantics.
- **`Context`** (`models/models.py`) — pydantic model with `extra="forbid"`, passed to every override callable. Carries `prop_path`, the in-progress `data`, the current `schema_data`/`schema_path`, and parent pointers (`parent_schema`, `parent_ctx`). Overrides read sibling fields from `ctx.data` via `__getitem__`.
- **`SchemaGeneratorBuilder`** (`SchemaGeneratorBuilder.py`) — mutable state during a single `generate()` call. Owns `generated` (the partial result), `pending_fields` (deferred overrides that depend on not-yet-generated siblings — resolved in a second pass), and the current `Context`. Provides path helpers (`set_value_at_path`, `get_value_at_path`, `has_value_at_path`) that use `helpers.utils.parse_path` for dot/bracket notation (e.g. `address.city`, `items[0].name`, `points[0][1]`, root array `[0]`, quoted key `["a.b"]`). `parse_path` returns a flat list of `str | int` segments; `join_path` renders one. `ensure_container` writes `{}`/`[]` as soon as an object/array is entered (skipped in `scenario_only` mode), so empty containers appear in the result. Recursion ends through the cycle rule (`Context.ref_path`/`recursive`: a schema node that recurs on the path gets only its required members; on a cycle, `oneOf`/`anyOf` prefer a branch that does not re-enter the path) and the re-entry limit (`helpers.utils.MAX_REENTRIES`: the third occurrence of a node on a path is cut, with a warning when required); `max_depth` (default 50, `helpers.utils.DEFAULT_MAX_DEPTH`) is only a hard cap that warns. The enumerators in `scenario_enum.py`/`break_enum.py` mirror the same rule, limit and cap.
- **`DefaultValueGenerator`** — per-type fallback sample values (backed by `Faker` + `rstr`) used when no override matches.
- **`helpers/allof_handler.py`** (`allof_merge`) — merges `allOf` compositions into a single schema before generation. Injected via the `allof_merger` constructor arg so it can be swapped in tests.

### Two-pass generation

Overrides can reference sibling data that hasn't been generated yet (the callable raises `KeyError` or `IndexError` against the live `ctx.data`). When that happens the override is parked on `builder.pending_fields` and retried after the first traversal completes. Tests under `tests/test_simple_overrides.py` and `tests/test_scenario_default_data.py` exercise this; preserve the deferred-resolution ordering when refactoring.

### Ref handling

Schemas may carry a `base_uri`. `Schema.from_raw_data` supports URIs with fragments (`…#/components/schemas/Foo`) — it resolves refs against the whole document, then navigates into the fragment. OpenAPI-style tests in `tests/test_oas_components_allof.py` and `tests/test_ctx_schema_path_for_refs.py` are the reference cases for this behavior.

### Generator-level array cap (`generator_max_items`)

`JSONSchemaGenerator(..., generator_max_items=N)` applies a global upper bound on array length inside `_handle_array`. When `maxItems` is **absent** from the schema, the cap *replaces* the legacy `max(minItems, 2)` default; when `maxItems` is **present**, it becomes `min(schema.maxItems, generator_max_items)`. `None` (default) preserves legacy behavior in both cases. The schema's `minItems` is **not** clamped — picking `generator_max_items` lower than any encountered `minItems` will make `random.randint(min, max)` raise. Useful for taming third-party schemas with unrealistically large `maxItems`. User-facing docs live in `README.md` under "User guide: Capping array size".

## Testing notes

- `pytest.ini` sets `testpaths = tests` and `-v` by default.
- `tests/conftest.py` contains shared fixtures; check it before adding new ones.
- `tests/test_schema_with_fragment.json` is the canonical fixture for fragment/ref tests — reuse it instead of inlining new schemas when possible.
