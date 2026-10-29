"""``validate_breaks`` sees a ``format_violation`` for every format the
breaker breaks and that JSON Schema defines, and says when it cannot
check one (issue #18)."""

from __future__ import annotations

import jsonschema
import pytest

from json_sample_generator import SampleBreaker, validate_breaks
from json_sample_generator.breaker import _FORMAT_INVALID
from json_sample_generator.models import Schema
from json_sample_generator.models.break_models import (
    BreakKind,
    BreakRule,
    BreakScenario,
)

VALID = {
    "email": "quote@example.com",
    "date-time": "2026-10-26T12:00:00Z",
    "date": "2026-10-26",
    "time": "12:00:00Z",
    "uri": "https://example.com/quote/1",
    "url": "https://example.com/quote/1",
    "uuid": "0192a3b4-c5d6-7e8f-9a0b-1c2d3e4f5a6b",
    "hostname": "example.com",
    "ipv4": "10.0.0.1",
    "ipv6": "2001:db8::1",
    "x-unknown": "anything",
}
CHECKED = sorted(f for f in _FORMAT_INVALID if f != "url")


def _schema(fmt):
    return Schema(
        data={
            "type": "object",
            "required": ["v"],
            "properties": {
                "v": {"type": "string", "format": fmt},
                "n": {"type": "integer"},
            },
        }
    )


def _validate(fmt, kind=BreakKind.FORMAT_VIOLATION, path="v"):
    schema = _schema(fmt)
    sample = {"v": VALID[fmt], "n": 1}
    checker = jsonschema.Draft202012Validator.FORMAT_CHECKER
    validator = jsonschema.Draft202012Validator(
        schema.data, format_checker=checker
    )
    assert validator.is_valid(sample), list(validator.iter_errors(sample))
    scenario = BreakScenario(name=fmt, rules=[BreakRule(path=path, kind=kind)])
    broken = SampleBreaker(schema).apply(sample, scenario)
    return validate_breaks(schema, broken, scenario)[0]


def test_the_formats_the_breaker_breaks_are_checked():
    # rfc3339-validator, rfc3986-validator and fqdn are dependencies.
    checkers = jsonschema.Draft202012Validator.FORMAT_CHECKER.checkers
    assert set(CHECKED) <= set(checkers)


@pytest.mark.parametrize("fmt", CHECKED)
def test_format_violation_is_seen(fmt):
    result = _validate(fmt)
    assert result.matched
    assert result.checked and result.unchecked_format is None
    assert any(e.validator == "format" for e in result.matched_errors)


@pytest.mark.parametrize("fmt", ["url", "x-unknown"])
def test_a_format_the_validator_cannot_check_is_named(fmt):
    result = _validate(fmt)
    assert not result.matched
    assert not result.checked
    assert result.unchecked_format == fmt


def test_a_missing_checker_package_is_named(monkeypatch):
    # what an environment without rfc3339-validator looks like
    monkeypatch.delitem(
        jsonschema.Draft202012Validator.FORMAT_CHECKER.checkers, "date-time"
    )
    monkeypatch.delitem(jsonschema.FormatChecker.checkers, "date-time")
    result = _validate("date-time")
    assert not result.matched
    assert result.unchecked_format == "date-time"


def test_a_format_registered_with_jsonschema_is_still_checked(monkeypatch):
    # what FormatChecker.cls_checks("url") registers, without its
    # deprecation warning
    monkeypatch.setitem(
        jsonschema.FormatChecker.checkers,
        "url",
        (lambda value: str(value).startswith("https://"), ()),
    )
    result = _validate("url")
    assert result.matched and result.checked


def test_only_an_unmatched_format_violation_is_unchecked():
    # a wrong_type at a url property is seen by its type
    wrong = _validate("url", kind=BreakKind.WRONG_TYPE)
    assert wrong.matched and wrong.checked
    # a rule that had no effect for another reason stays checked
    absent = _validate("url", kind=BreakKind.ENUM_VIOLATION, path="missing")
    assert not absent.matched and absent.checked


def _format_violation(schema, sample, path):
    scenario = BreakScenario(
        name=path,
        rules=[BreakRule(path=path, kind=BreakKind.FORMAT_VIOLATION)],
    )
    broken = SampleBreaker(schema).apply(sample, scenario)
    return validate_breaks(schema, broken, scenario)[0]


def test_a_format_violation_the_breaker_skipped_stays_checked():
    # the path is absent from the sample, here a oneOf branch not taken
    schema = Schema(
        data={
            "type": "object",
            "properties": {
                "p": {
                    "oneOf": [
                        {
                            "type": "object",
                            "required": ["a"],
                            "properties": {"a": {"type": "integer"}},
                        },
                        {
                            "type": "object",
                            "required": ["u"],
                            "properties": {
                                "u": {"type": "string", "format": "url"}
                            },
                        },
                    ]
                }
            },
        }
    )
    result = _format_violation(schema, {"p": {"a": 1}}, "p.u")
    assert not result.matched and result.checked


def test_a_path_that_cannot_be_parsed_is_no_error():
    schema = _schema("url")
    scenario = BreakScenario(
        name="bad",
        rules=[BreakRule(path="v[", kind=BreakKind.FORMAT_VIOLATION)],
    )
    (result,) = validate_breaks(schema, {"v": "x", "n": 1}, scenario)
    assert not result.matched and result.checked


def test_a_valid_rfc3339_time_is_no_error():
    # jsonschema.FormatChecker() checks "time" the draft 3 way, which
    # rejects 12:00:00Z: a break next to a valid time matched that error.
    # additional_property breaks nothing here (additionalProperties is not
    # false), so the rule must match no error at all.
    schema = Schema(
        data={
            "type": "object",
            "properties": {
                "obj": {
                    "type": "object",
                    "properties": {
                        "t": {"type": "string", "format": "time"},
                        "n": {"type": "integer"},
                    },
                }
            },
        }
    )
    scenario = BreakScenario(
        name="extra",
        rules=[BreakRule(path="obj", kind=BreakKind.ADDITIONAL_PROPERTY)],
    )
    sample = {"obj": {"t": "12:00:00Z", "n": 1}}
    broken = SampleBreaker(schema).apply(sample, scenario)
    (result,) = validate_breaks(schema, broken, scenario)
    assert not result.matched_errors
