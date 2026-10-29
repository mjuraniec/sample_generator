# Break Scenarios

Break scenarios let you take a valid generated sample and intentionally
corrupt it so that it fails JSON Schema validation. This is useful for:

- Testing that your API or validator correctly rejects bad input.
- Generating negative test fixtures alongside your positive ones.
- Checking that error messages surface the right field paths.

Break scenarios operate on a generated sample **after** generation.
They do not modify your `Scenario` and they never touch the original
sample — every `apply` call returns a new deep copy.

---

## Concepts

A **`BreakRule`** names a field path and a `BreakKind` (e.g. "set this
field to the wrong type" or "make this string too short").

A **`BreakScenario`** is a named list of `BreakRule` objects. A scenario
with one rule produces a sample with exactly one violation; a scenario
with three rules produces a sample with (at least) three violations.

The **`SampleBreaker`** (or the functional shortcut
`apply_break_scenario`) applies a scenario to a sample and returns the
mutated copy.

**`validate_breaks`** uses `jsonschema` to verify that the mutations
really do trigger validation errors and maps each error back to the rule
that caused it.

---

## BreakKind reference

| `BreakKind` value         | What it does                                                         |
| ------------------------- | -------------------------------------------------------------------- |
| `wrong_type`              | Replace the value with a value of a type the schema does not accept there (no `oneOf`/`anyOf` branch, `type` list or `nullable` does); a value whose schema does not restrict its type gets a string, as before |
| `remove_required`         | Delete the key (triggers a `required` error on the parent)           |
| `null_value`              | Replace the value with `null`                                        |
| `enum_violation`          | Replace with a value not in `enum`                                   |
| `const_violation`         | Replace with a value that differs from `const`                       |
| `pattern_violation`       | Prepend `"!!!"` so the string no longer matches `pattern`            |
| `min_length_violation`    | Truncate the string to `minLength - 1` characters                    |
| `max_length_violation`    | Pad the string to `maxLength + 1` characters                         |
| `min_violation`           | Set the number to `minimum - 1`                                      |
| `max_violation`           | Set the number to `maximum + 1`                                      |
| `additional_property`     | Add an extra key `"__break_extra__"` to an object with `additionalProperties: false` |
| `min_items_violation`     | Slice the array to `minItems - 1` items                              |
| `max_items_violation`     | Pad the array to `maxItems + 1` items                                |
| `format_violation`        | Replace the string with a known-invalid value for its `format`       |

---

## Defining a break scenario

```python
from json_sample_generator import (
    BreakKind, BreakRule, BreakScenario, SampleBreaker,
)
from json_sample_generator.models.models import Schema

schema = Schema(data={
    "type": "object",
    "properties": {
        "age": {"type": "integer", "minimum": 0, "maximum": 150},
        "status": {"type": "string", "enum": ["active", "inactive"]},
    },
    "required": ["age", "status"],
})

sample = {"age": 30, "status": "active"}

scenario = BreakScenario(
    name="bad_age",
    rules=[
        BreakRule(path="age", kind=BreakKind.MAX_VIOLATION),
    ],
)

broken = SampleBreaker(schema).apply(sample, scenario)
# broken["age"] == 151 — above maximum
```

Use `apply_break_scenario` for one-off calls without creating a breaker:

```python
from json_sample_generator import apply_break_scenario

broken = apply_break_scenario(schema, sample, scenario)
```

### Explicit values

Set `value` on a `BreakRule` to use a specific invalid value instead of
the auto-derived one:

```python
BreakRule(path="status", kind=BreakKind.ENUM_VIOLATION, value="unknown")
```

---

## Enumerating break scenarios

`enumerate_break_scenarios` walks the schema and emits **one scenario
per (break site, kind) pair**, giving full single-failure coverage.
Each scenario has exactly one rule.

```python
from json_sample_generator import enumerate_break_scenarios

scenarios = enumerate_break_scenarios(schema)
# Each scenarios[i] has one rule and expected_failure_count=1.
```

To inspect the raw sites first:

```python
from json_sample_generator import collect_break_sites

sites = collect_break_sites(schema)
for site in sites:
    print(site.path, site.applicable)
```

`enumerate_break_scenarios` raises `ValueError` when the site count
exceeds `max_scenarios` (default `10_000`). Raise the cap explicitly
if needed:

```python
scenarios = enumerate_break_scenarios(schema, max_scenarios=50_000)
```

---

## Random break scenarios

`random_break_scenario` selects `num_failures` distinct break sites at
random and picks one applicable `BreakKind` per site.

```python
from json_sample_generator import random_break_scenario, apply_break_scenario

scenario = random_break_scenario(schema, num_failures=3, seed=42)
broken = apply_break_scenario(schema, sample, scenario)
```

Pass `seed` for reproducibility. Omit it for a different scenario on
each call.

`random_break_scenario` raises `ValueError` if `num_failures` exceeds
the number of available break sites.

---

## Composing multi-break scenarios

`enumerate_break_scenarios` and `random_break_scenario` each produce
scenarios with one or more rules. Use `merge_break_scenarios` to combine
them into a single scenario that applies all rules at once:

```python
from json_sample_generator import (
    enumerate_break_scenarios, merge_break_scenarios, apply_break_scenario,
)

all_scenarios = enumerate_break_scenarios(schema)
# Pick the first two and combine them.
merged = merge_break_scenarios(
    all_scenarios[0],
    all_scenarios[1],
    name="two_breaks",
)
broken = apply_break_scenario(schema, sample, merged)
```

`merge_break_scenarios` also accepts manually constructed
`BreakScenario` objects. Rules from the first scenario appear first in
the merged list. `expected_failure_count` is summed when all inputs
carry a value; otherwise it is `None`.

---

## Statically checking a scenario

`check_break_scenario` validates a scenario against the **schema alone**
— no sample is needed. It reports whether each rule's path exists in the
schema and whether the requested `BreakKind` is compatible with the
constraints at that path.

This is useful for catching mistakes early when hand-authoring rules:

```python
from json_sample_generator import (
    BreakKind, BreakRule, BreakScenario, check_break_scenario,
)
from json_sample_generator.models.models import Schema

schema = Schema(data={
    "type": "object",
    "properties": {
        "age": {"type": "integer", "minimum": 0},
        "status": {"type": "string", "enum": ["active", "inactive"]},
    },
    "required": ["age"],
})

# A scenario with two mistakes: a typo and an incompatible kind.
scenario = BreakScenario(
    name="my_scenario",
    rules=[
        BreakRule(path="agee", kind=BreakKind.MIN_VIOLATION),   # typo
        BreakRule(path="status", kind=BreakKind.MIN_VIOLATION), # status is a string
    ],
)

report = check_break_scenario(schema, scenario)
print(report.all_applicable)  # False

for check in report.checks:
    status = "ok" if check.applicable else "FAIL"
    print(f"[{status}] {check.rule.path!r} ({check.rule.kind.value})")
    if not check.applicable:
        print(f"  reason: {check.reason}")
```

Output:

```
False
[FAIL] 'agee' (min_violation)
  reason: path not found in schema
[FAIL] 'status' (min_violation)
  reason: min_violation: schema has no `minimum`/`exclusiveMinimum` at this path (available: wrong_type, null_value, enum_violation)
```

Each `RuleCheck` in `report.checks` carries:

| Field              | Type                     | Meaning                                                |
| ------------------ | ------------------------ | ------------------------------------------------------ |
| `rule`             | `BreakRule`              | The rule that was checked                              |
| `applicable`       | `bool`                   | Whether the rule is valid for this schema              |
| `reason`           | `str`                    | `"ok"` or a human-readable explanation of the problem  |
| `available_kinds`  | `tuple[BreakKind, ...]`  | Kinds that *could* apply at this path                  |
| `matched_path`     | `str \| None`            | Canonical schema path (with `[*]`), `None` if missing  |
| `schema_fragment`  | `dict \| None`           | The schema at the matched path, `None` if missing      |

**Note:** Concrete array indices in rule paths (`items[0]`) are
automatically normalized to `items[*]` for matching, so you can
target any element of an array.

**Schema-only vs. sample-based:** `check_break_scenario` only looks at
the schema — it cannot know which `oneOf`/`anyOf` branch a sample will
take. Use `validate_breaks` (below) after applying a scenario to confirm
the breaks actually triggered `jsonschema` errors at runtime.

---

## Validating breaks

`validate_breaks` runs `jsonschema.Draft202012Validator` on the broken
sample and maps each validation error to the `BreakRule` whose path
matches. It uses a `FormatChecker` so `format_violation` breaks are
also detected.

```python
from json_sample_generator import validate_breaks

failures = validate_breaks(schema, broken, scenario)

for f in failures:
    if f.matched:
        print(f"Rule {f.rule.path!r} ({f.rule.kind.value}) triggered:")
        for err in f.matched_errors:
            print(f"  {err.message}")
    else:
        print(f"Rule {f.rule.path!r} had no effect (path unreachable?)")
```

A rule with `matched=False` usually means the break's path did not
exist in the sample — for example, a `oneOf` branch that was not
selected. This is a signal, not an error.

---

## oneOf / anyOf interaction

Break sites are collected from **all** variants of a `oneOf`/`anyOf`,
so you will see sites for properties that live only in branch B even
when branch A was selected during generation.

When `apply_break_scenario` encounters a rule whose path does not exist
in the sample (the branch was not taken), it silently **no-ops** that
rule. Use `validate_breaks` to detect no-ops: rules that triggered no
validation errors indicate a path mismatch.

Where the path does exist, the breaker reads the schema of the branch
the sample took. At every level of the path it merges `allOf`, picks the
`oneOf`/`anyOf` branch whose types, `const` and `enum` values agree with
the sample (then, for an object, the one whose required keys and
properties it has most of; for an array, the one whose `items` its
elements fit), and merges that branch with the keywords next to the
`oneOf`, as the generator merges them. Every kind that reads the schema
reads that fragment; `wrong_type` looks at every branch. A branch that is itself an `allOf`, as strict
OpenAPI variants write a polymorphic reference, is resolved the same
way. A rule taken from another branch can still have no effect on a
sample whose path exists: a `minItems` that only the other branch sets,
or a `const` of one `anyOf` branch whose break another branch accepts,
for example.

---

## Public API summary

| Symbol                     | Kind      | Description                                                     |
| -------------------------- | --------- | --------------------------------------------------------------- |
| `BreakKind`                | enum      | All available break kinds                                       |
| `BreakRule`                | model     | A single mutation (path + kind + optional value)                |
| `BreakScenario`            | model     | Named list of break rules                                       |
| `BreakSite`                | dataclass | A schema location with applicable kinds (from enumeration)      |
| `collect_break_sites`      | function  | Walk a schema and return all `BreakSite` objects                |
| `enumerate_break_scenarios`| function  | One `BreakScenario` per (site, kind) — full coverage            |
| `random_break_scenario`    | function  | N random breaks, reproducible with seed                         |
| `merge_break_scenarios`    | function  | Combine multiple scenarios into one                             |
| `SampleBreaker`            | class     | Stateful breaker bound to a schema                              |
| `apply_break_scenario`     | function  | Functional shortcut for `SampleBreaker(...).apply(...)`         |
| `ValidationFailure`        | dataclass | A rule together with the jsonschema errors it triggered         |
| `validate_breaks`          | function  | Verify a broken sample actually fails its schema                |
| `RuleCheck`                | dataclass | Result of statically checking one rule (path + kind validity)   |
| `BreakScenarioReport`      | dataclass | Static validation report for a whole scenario                   |
| `check_break_scenario`     | function  | Schema-only check: path exists + kind compatible with constraints |
