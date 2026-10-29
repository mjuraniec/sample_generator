"""Validate break scenarios — both statically and against broken samples.

Two complementary validators:

* :func:`check_break_scenario` — **schema-only**, no sample needed.
  Checks that every rule's path exists in the schema and that the
  requested :class:`~.BreakKind` is compatible with the constraints
  at that path.  Returns a :class:`BreakScenarioReport`.

* :func:`validate_breaks` — **sample-based**.  Applies the scenario
  to a broken sample and checks that each rule triggered at least one
  real ``jsonschema`` validation error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import jsonschema

from .break_enum import BreakSite, collect_break_sites
from .breaker import SampleBreaker
from .helpers.utils import path_startswith
from .models.break_models import BreakKind, BreakRule, BreakScenario
from .models.models import Schema


@dataclass
class ValidationFailure:
    """A break rule together with the jsonschema errors it triggered.

    Attributes:
        rule: The rule that was checked.
        matched_errors: The errors at or around the rule's path.
        unchecked_format: For a ``format_violation`` rule that matched no
            error, the format at its path when the validator cannot check
            it: ``url``, which JSON Schema does not define, or any other
            format it does not know, such as one whose checker package is
            missing. ``matched`` is then ``False`` because the break
            cannot be seen, not because it had no effect. ``None``
            otherwise.
    """

    rule: BreakRule
    matched_errors: List[jsonschema.ValidationError] = field(
        default_factory=list
    )
    unchecked_format: Optional[str] = None

    @property
    def matched(self) -> bool:
        """``True`` when at least one validation error was matched."""
        return bool(self.matched_errors)

    @property
    def checked(self) -> bool:
        """``False`` when the validator could not check the rule's break
        (see :attr:`unchecked_format`)."""
        return self.unchecked_format is None


@dataclass
class RuleCheck:
    """Result of statically checking one :class:`~.BreakRule` against a schema.

    Attributes:
        rule: The rule that was checked.
        applicable: ``True`` when the path exists in the schema and the
            kind is compatible with the constraints there.
        reason: Human-readable explanation (``"ok"`` when applicable).
        available_kinds: The :class:`~.BreakKind` values that *could*
            apply at this path.  Empty when the path is not found.
        matched_path: The schema site's canonical path (uses ``[*]``
            for array items).  ``None`` when the path is not found.
        schema_fragment: The schema dict at the matched site.
            ``None`` when the path is not found.
    """

    rule: BreakRule
    applicable: bool
    reason: str
    available_kinds: Tuple[BreakKind, ...] = field(default_factory=tuple)
    matched_path: Optional[str] = None
    schema_fragment: Optional[dict] = None


@dataclass
class BreakScenarioReport:
    """Static validation report for a :class:`~.BreakScenario`.

    Attributes:
        scenario_name: The name of the checked scenario.
        checks: One :class:`RuleCheck` per rule in the scenario.
    """

    scenario_name: str
    checks: List[RuleCheck]

    @property
    def all_applicable(self) -> bool:
        """``True`` when every rule in the scenario is applicable."""
        return all(c.applicable for c in self.checks)


def check_break_scenario(
    schema: Schema,
    scenario: BreakScenario,
) -> BreakScenarioReport:
    """Statically validate *scenario* against *schema*.

    Checks each rule without needing a sample — purely schema-driven.
    Complements :func:`validate_breaks`, which confirms a break actually
    triggered a ``jsonschema`` error at runtime.

    Parameters
    ----------
    schema:
        The JSON Schema the scenario will be applied to.
    scenario:
        The break scenario to validate.

    Returns
    -------
    BreakScenarioReport
        One :class:`RuleCheck` per rule.  Inspect
        ``report.all_applicable`` for a quick pass/fail, or iterate
        ``report.checks`` for per-rule details.
    """
    # Build lookup: normalized wildcard path → list of sites.
    # Multiple sites can share the same path (oneOf branches with
    # overlapping properties); we union their applicable kinds.
    site_map: Dict[str, List[BreakSite]] = {}
    for site in collect_break_sites(schema):
        site_map.setdefault(site.path, []).append(site)

    checks: List[RuleCheck] = []
    for rule in scenario.rules:
        norm_path = _normalize_to_wildcard(rule.path)
        matching = site_map.get(norm_path)

        if not matching:
            checks.append(
                RuleCheck(
                    rule=rule,
                    applicable=False,
                    reason="path not found in schema",
                    available_kinds=(),
                    matched_path=None,
                    schema_fragment=None,
                )
            )
            continue

        # Union applicable kinds across all matching sites.
        union: Tuple[BreakKind, ...] = ()
        for site in matching:
            for k in site.applicable:
                if k not in union:
                    union = union + (k,)

        # Use the first matching site's fragment for the report.
        primary = matching[0]

        if rule.kind in union:
            checks.append(
                RuleCheck(
                    rule=rule,
                    applicable=True,
                    reason="ok",
                    available_kinds=union,
                    matched_path=primary.path,
                    schema_fragment=primary.schema_fragment,
                )
            )
        else:
            reason = _explain_mismatch(rule.kind, union)
            checks.append(
                RuleCheck(
                    rule=rule,
                    applicable=False,
                    reason=reason,
                    available_kinds=union,
                    matched_path=primary.path,
                    schema_fragment=primary.schema_fragment,
                )
            )

    return BreakScenarioReport(
        scenario_name=scenario.name,
        checks=checks,
    )


def validate_breaks(
    schema: Schema,
    broken_sample: dict,
    scenario: BreakScenario,
) -> List[ValidationFailure]:
    """Check that *broken_sample* fails validation in the expected places.

    Parameters
    ----------
    schema:
        The JSON Schema that the original (unbroken) sample conforms to.
    broken_sample:
        The output of :func:`~.apply_break_scenario`.
    scenario:
        The break scenario that was applied.

    Returns
    -------
    list[ValidationFailure]
        One entry per rule in *scenario*. ``entry.matched`` is ``True``
        when at least one ``jsonschema`` error path aligns with the
        rule's path.  An unmatched entry means the break had no visible
        effect — likely because the rule's path was unreachable in the
        sample's shape (e.g., a ``oneOf`` branch that was not selected) —
        unless ``entry.checked`` is ``False``: a ``format_violation`` of a
        format the validator cannot check (``entry.unchecked_format``).

    Notes
    -----
    Uses ``jsonschema.Draft202012Validator``.  The schema is used as-is
    (the caller is responsible for ref resolution when needed).
    """
    format_checker = _format_checker()
    validator = jsonschema.Draft202012Validator(
        schema.data,
        format_checker=format_checker,
    )
    all_errors: List[jsonschema.ValidationError] = list(
        validator.iter_errors(broken_sample)
    )

    breaker: Optional[SampleBreaker] = None
    results: List[ValidationFailure] = []
    for rule in scenario.rules:
        matched = _match_errors(rule, all_errors)
        unchecked: Optional[str] = None
        if not matched and rule.kind == BreakKind.FORMAT_VIOLATION:
            # The format the breaker broke there, read as it reads it.
            breaker = breaker or SampleBreaker(schema)
            try:
                fmt = breaker._resolve_schema(broken_sample, rule.path).get(
                    "format"
                )
            except ValueError:
                fmt = None  # a path parse_path cannot read
            if isinstance(fmt, str) and fmt not in format_checker.checkers:
                unchecked = fmt
        results.append(
            ValidationFailure(
                rule=rule, matched_errors=matched, unchecked_format=unchecked
            )
        )
    return results


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _format_checker() -> jsonschema.FormatChecker:
    """Draft 2020-12's format checks, with any other format
    ``jsonschema.FormatChecker()`` knows (one registered with
    ``FormatChecker.cls_checks``, say) kept. ``FormatChecker()`` alone
    holds every draft's formats, and its ``time`` is draft 3's, which
    rejects an RFC 3339 time such as ``19:47:52Z``."""
    checker = jsonschema.FormatChecker()
    checker.checkers.update(
        jsonschema.Draft202012Validator.FORMAT_CHECKER.checkers
    )
    return checker


def _normalize_to_wildcard(path: str) -> str:
    """Replace concrete array indices (``[0]``, ``[42]``) with ``[*]``."""
    return re.sub(r"\[\d+\]", "[*]", path)


_KIND_REASONS: Dict[BreakKind, str] = {
    BreakKind.ENUM_VIOLATION: "schema has no `enum` at this path",
    BreakKind.CONST_VIOLATION: "schema has no `const` at this path",
    BreakKind.PATTERN_VIOLATION: "schema has no `pattern` at this path",
    BreakKind.MIN_LENGTH_VIOLATION: "schema has no `minLength` at this path",
    BreakKind.MAX_LENGTH_VIOLATION: "schema has no `maxLength` at this path",
    BreakKind.MIN_VIOLATION: (
        "schema has no `minimum`/`exclusiveMinimum` at this path"
    ),
    BreakKind.MAX_VIOLATION: (
        "schema has no `maximum`/`exclusiveMaximum` at this path"
    ),
    BreakKind.MIN_ITEMS_VIOLATION: "schema has no `minItems` at this path",
    BreakKind.MAX_ITEMS_VIOLATION: "schema has no `maxItems` at this path",
    BreakKind.FORMAT_VIOLATION: (
        "schema has no recognized `format` at this path"
    ),
    BreakKind.REMOVE_REQUIRED: (
        "property is not listed in its parent's `required` array"
    ),
    BreakKind.ADDITIONAL_PROPERTY: (
        "parent object does not set `additionalProperties: false`"
    ),
    BreakKind.NULL_VALUE: (
        "schema already accepts `null` (nullable or type list includes null)"
    ),
    BreakKind.WRONG_TYPE: "schema has insufficient type information",
}


def _explain_mismatch(
    kind: BreakKind, available: Tuple[BreakKind, ...]
) -> str:
    base = _KIND_REASONS.get(kind, f"{kind.value} not applicable here")
    available_str = ", ".join(k.value for k in available)
    return f"{kind.value}: {base} (available: {available_str})"


def _error_path_str(error: jsonschema.ValidationError) -> str:
    """Convert an error's absolute_path deque to a dot/bracket string."""
    parts: List[str] = []
    for segment in error.absolute_path:
        if isinstance(segment, int):
            if parts:
                parts[-1] = f"{parts[-1]}[{segment}]"
            else:
                parts.append(f"[{segment}]")
        else:
            parts.append(str(segment))
    return ".".join(parts)


def _match_errors(
    rule: BreakRule,
    errors: List[jsonschema.ValidationError],
) -> List[jsonschema.ValidationError]:
    """Return errors whose path starts with or equals *rule.path*."""
    matched = []
    for err in errors:
        err_path = _error_path_str(err)
        # An empty rule path (root-level break) matches everything.
        if not rule.path:
            matched.append(err)
        # Exact match or error fired on an ancestor of the broken field.
        elif err_path and path_startswith(rule.path, err_path):
            matched.append(err)
        # Error fired below the broken field.
        elif err_path and path_startswith(err_path, rule.path):
            matched.append(err)
        elif not err_path:
            # Root-level error (e.g. "required") — match when the error
            # message references the terminal segment of the rule path.
            prop = rule.path.split(".")[0].split("[")[0]
            if prop in err.message:
                matched.append(err)
    return matched
