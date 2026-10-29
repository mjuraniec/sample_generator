# Changelog

All notable changes to this project are documented in this file.

The format is based on Keep a Changelog.

## [Unreleased]

### Fixed

- With an OpenAPI `discriminator` next to `oneOf`/`anyOf`, the property it
  names gets the value that names the generated branch: its key in
  `discriminator.mapping`, or, for a `$ref` branch the mapping does not
  list, the name of the schema it refers to. It was a random string that
  matched neither unless the branches pinned it with `const` or `enum`,
  which still wins, as does a polymorphic branch's own level. Only the
  value the branch was generated with is replaced: no property is added
  and every other value stays the same. This needs the `$ref`s, so it
  does not apply to a schema loaded with `Schema.from_oas` or
  `Schema.from_raw_data` (issue #15).
- A `str` selector finds a `$ref` branch through `discriminator.mapping`,
  comparing the reference the branch was written with (resolved, so two
  documents' `#/definitions/Pet` differ), and with a discriminator also by
  the name of the schema it refers to; it raised "no candidate matched"
  unless the schema had a `title`. The same limit applies (issue #15).

## [0.6.0] - 2026-10-21

Addresses the nine issues reported against 0.5.0, referenced below as
`issue #N`. The dependency and CI entries come from pull request #10, the
rest from pull requests #11, #12 and #13.

### Added

- `multipleOf` is honoured for integers and numbers. A one-sided bound is
  widened to reach the first multiple; `ValueError` is raised when no
  multiple fits (issue #7).
- `uniqueItems` is honoured: items are compared as JSON values (`1` and
  `true` differ), and an item schema with a closed domain (`enum`,
  `const`, boolean, or an integer with both bounds) is sampled without
  replacement with the array length capped at the number of distinct
  values; other domains end the array when no distinct value can be drawn
  (`null` is a value). `ValueError` is raised only when `minItems` cannot
  be met (issue #7).
- Property paths support arrays of arrays (`points[0][1]`), root arrays
  (`[0]`) and property names containing characters such as `.` or `[`,
  written as a quoted segment (`["a.b"].c`), both in the output and in
  scenario override keys.
  `helpers.utils.join_path` renders a path the way the generator does;
  the scenario and break enumerators use it too (issue #2).
- The draft-4 / OpenAPI 3.0 boolean form of `exclusiveMinimum` and
  `exclusiveMaximum` is understood (issue #5).
- CI runs the test suite on Python 3.12, 3.13 and 3.14 (issue #9).
- README sections "Scalar constraints", "Property paths" and "Recursion,
  nesting depth and `uniqueItems`"; `docs/SCENARIOS.md` section "How the
  chosen branch is generated".

### Changed

- **Breaking:** `helpers.utils.parse_path` returns a flat list of keys and
  indices (`["items", 0, "name"]`) instead of `(key, index)` pairs.
- Objects and arrays are written to the result as soon as the generator
  enters them, so required empty objects, arrays that draw zero items and
  minimal-mode objects with only optional properties are present, and a
  root array schema produces a list. `scenario_only` mode is unchanged
  (issue #3).
- `Context.data` is a live view of the result being built, not a snapshot:
  override callables can observe partly generated data, and reading a
  value that does not exist yet (`KeyError` or `IndexError`) defers the
  override to the second pass. `Context.data` is typed `Any` and `Context`
  uses `ConfigDict` instead of the deprecated class-based config.
- Recursive schemas end through a cycle rule (a schema node that recurs on
  the current path is generated with only its required members, and a
  `oneOf`/`anyOf` on a cycle prefers a branch that does not re-enter the
  path) and a re-entry limit (the third occurrence of a node on a path is
  cut, with a `UserWarning` when the schema requires it) instead of a
  depth limit;
  plain deep nesting is generated in full. `max_depth` is a hard cap, now
  50 by default, that warns the same way. The scenario and break
  enumerators mirror the rule, the limit and the cap (issue #8).
- `oneOf`/`anyOf` branches are generated together with the keywords next
  to them (`type`, `properties`, `required`, `format`, ...), so branches
  that only add `required` or `format` work. For `oneOf`, properties that
  only the other branches require are left out, whether or not the parent
  carries keywords of its own; a property is kept when the merged schema
  requires it or an override, `default_data` or a selector names it.
  Exclusivity is guaranteed only for branches that differ in `required`;
  branches that differ otherwise (`number` next to `integer`, overlapping
  ranges) are not checked, since the generator does not validate
  (issue #1).
- `allOf`: parts that define the same property are merged as a nested
  `allOf`, so a part can refine another's property (a `pattern` or
  `maximum` on top of a `type`) instead of replacing it; this changes
  plain `allOf` output too, not only `oneOf`/`anyOf`. A property
  re-declared with the same `$ref` stays a single reference, which keeps
  its `schema_path` and the recursion rule intact.
- Numeric bounds: with a single bound the default span is kept
  (`minimum: 500` draws 500..600) instead of the pair being swapped onto
  the wrong side; when a plain and a numeric exclusive bound are both
  given the tighter wins; exclusive bounds are exact (`floor(x) + 1`,
  `ceil(x) - 1` for integers, the open interval for numbers); integer
  bounds stay exact above 2**53; an empty or inverted range raises
  `ValueError` instead of being swapped (issue #5).
- `minLength`/`maxLength` apply together with `pattern` and `format`:
  `email`, `hostname`, `uri`, `url`, `phone` and `ipv4` are built to a
  fitting length, fixed-shape formats (`uuid`, `ipv6`, `date`, `time`,
  `date-time`) must fit as they are, and pattern repeats are capped at
  `maxLength` and floored at `minLength` (so `{200,}` works). An
  unsatisfiable combination, including `minLength > maxLength`, raises
  `ValueError`; unknown formats are annotations and take the plain string
  path; `maxLength` below 5 without `minLength` no longer crashes (issue #4).
- `format: date-time` and `format: time` values carry a UTC offset, as
  RFC 3339 requires (issue #6).
- Dependency pins relaxed to `pydantic>=2.11,<3`, `Faker>=37.1` and
  `rstr>=3.2,<4`, so the package installs on Python 3.14 (issue #9). The ruff
  rule set is pinned in `pyproject.toml` (`E4`, `E7`, `E9`, `F`).
- Break scenarios resolve `items[0].name` to the item's schema instead of
  stopping at the array.
- `oneOf`/`anyOf` branches are generated together with the keywords next
  to them (`type`, `properties`, `required`, `format`, ...), so branches
  that only add `required` or `format` work. For `oneOf`, properties that
  only the other branches require are left out, whether or not the parent
  carries keywords of its own. `allOf` (and the parent/branch merge) keeps
  both definitions of a property defined in several parts, so a part can
  refine another's property; a property re-declared with the same `$ref`
  stays a single reference, which keeps its `schema_path` and the
  recursion rule intact.

## [0.5.0] - 2026-05-07

### Added

- Added `generator_max_items` to `JSONSchemaGenerator` to cap generated
  array sizes globally.
- Added `Schema.from_oas` factory support for OpenAPI schema loading.
- Added dedicated OpenAPI and generator cap documentation in `README.md`
  and `docs/OPENAPI.md`.
- Added tests covering global array size caps and OpenAPI loading behavior.

### Fixed

- Added warning/handling around `jsonref` caching edge cases in OpenAPI
  schema resolution.

## [0.4.0] - 2026-04-26

### Added

- Added break scenario APIs for generating invalid samples for negative tests.

## [0.3.0] - 2026-04-23

### Added

- Added `minimal_mode` to `Scenario` for required-only sample generation.

## [0.2.0] - 2026-04-21

### Added

- Added scenario enumeration and selection strategies.

## [0.1.0] - 2026-04-20

### Added

- Initial alpha release.
