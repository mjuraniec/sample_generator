"""Break scenarios inside ``oneOf``/``anyOf``: the breaker resolves the
branch the sample took, ``allOf`` merged at every level, and a
``wrong_type`` value is of a type no branch accepts (issue #17)."""

from __future__ import annotations

import random
import time

import pytest
from faker import Faker
from jsonschema import Draft202012Validator

from json_sample_generator import validate_breaks
from json_sample_generator.breaker import (
    SampleBreaker,
    _accepted_types,
    _json_type,
    _same_value,
)
from json_sample_generator.models import Schema
from json_sample_generator.models.break_models import (
    BreakKind,
    BreakRule,
    BreakScenario,
)

REF = "#/components/schemas/"


def _oas(**schemas):
    return {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": "1"},
        "paths": {},
        "components": {"schemas": schemas},
    }


def _validator(oas, name):
    return Draft202012Validator(
        {"$ref": REF + name, "components": oas["components"]}
    )


def _breaks(schema, validator, sample, path, kind, seeds=20):
    """How many of *seeds* breaks make *sample* invalid."""
    assert validator.is_valid(sample), list(validator.iter_errors(sample))
    breaker = SampleBreaker(schema)
    scenario = BreakScenario(
        name=path, rules=[BreakRule(path=path, kind=kind)]
    )
    broke = 0
    for seed in range(seeds):
        random.seed(seed)
        Faker.seed(seed)
        broke += not validator.is_valid(breaker.apply(sample, scenario))
    return broke


# ---------------------------------------------------------------------------
# The report of issue #17: a oneOf whose branch is an allOf
# ---------------------------------------------------------------------------

TAIL = {
    "type": "object",
    "properties": {
        "@type": {"const": "OfferingRef"},
        "name": {"type": "string", "maxLength": 20},
    },
}


def _offering_oas(offering_ref):
    return _oas(
        Ref={
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "string"}},
        },
        OfferingRef=offering_ref,
        Item={
            "type": "object",
            "required": ["offering", "note"],
            "properties": {
                "offering": {"$ref": REF + "OfferingRef"},
                "note": {"type": "string"},
            },
        },
    )


SHAPES = {
    "allOf": {"allOf": [{"$ref": REF + "Ref"}, TAIL]},
    # a polymorphic reference as strict OpenAPI variants write it
    "oneOf-of-allOf": {"oneOf": [{"allOf": [{"$ref": REF + "Ref"}, TAIL]}]},
}
OFFERING = {
    "offering": {"@type": "OfferingRef", "id": "o-1", "name": "Ka capacity"},
    "note": "n",
}


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("path", ["note", "offering.id", "offering.name"])
def test_wrong_type_breaks_a_property_of_an_allof_branch(shape, path):
    oas = _offering_oas(SHAPES[shape])
    schema = Schema.from_oas(oas, name="Item")
    assert (
        _breaks(
            schema,
            _validator(oas, "Item"),
            OFFERING,
            path,
            BreakKind.WRONG_TYPE,
        )
        == 20
    )


@pytest.mark.parametrize("shape", SHAPES)
def test_resolve_schema_reaches_into_an_allof_branch(shape):
    oas = _offering_oas(SHAPES[shape])
    breaker = SampleBreaker(Schema.from_oas(oas, name="Item"))
    assert breaker._resolve_schema(OFFERING, "offering.id") == {
        "type": "string"
    }
    assert breaker._resolve_schema(OFFERING, "offering.name") == {
        "type": "string",
        "maxLength": 20,
    }


def test_validate_breaks_confirms_the_wrong_type():
    oas = _offering_oas(SHAPES["oneOf-of-allOf"])
    schema = Schema.from_oas(oas, name="Item")
    scenario = BreakScenario(
        name="id",
        rules=[BreakRule(path="offering.id", kind=BreakKind.WRONG_TYPE)],
    )
    broken = SampleBreaker(schema).apply(OFFERING, scenario)
    assert not isinstance(broken["offering"]["id"], str)
    assert validate_breaks(schema, broken, scenario)[0].matched


def test_constraint_of_an_allof_branch_is_broken():
    # Every kind reads the resolved fragment: maxLength sits in the branch.
    oas = _offering_oas(SHAPES["oneOf-of-allOf"])
    schema = Schema.from_oas(oas, name="Item")
    assert (
        _breaks(
            schema,
            _validator(oas, "Item"),
            OFFERING,
            "offering.name",
            BreakKind.MAX_LENGTH_VIOLATION,
        )
        == 20
    )


# ---------------------------------------------------------------------------
# The branch the sample took
# ---------------------------------------------------------------------------


def _characteristic_oas():
    # TMF's polymorphic characteristic in a strict variant: each branch
    # pins @type through an allOf, and value has another type in each.
    def branch(name, value):
        return {
            "allOf": [
                {"$ref": REF + "Characteristic"},
                {
                    "type": "object",
                    "properties": {
                        "@type": {"const": name},
                        "value": value,
                    },
                },
            ]
        }

    return _oas(
        Characteristic={
            "type": "object",
            "required": ["@type", "name"],
            "properties": {
                "@type": {"type": "string"},
                "name": {"type": "string"},
            },
        },
        StringCharacteristic=branch(
            "StringCharacteristic", {"type": "string"}
        ),
        IntegerCharacteristic=branch(
            "IntegerCharacteristic", {"type": "integer"}
        ),
        Product={
            "type": "object",
            "properties": {
                "characteristic": {
                    "type": "array",
                    "items": {
                        "oneOf": [
                            {"$ref": REF + "StringCharacteristic"},
                            {"$ref": REF + "IntegerCharacteristic"},
                        ],
                        "discriminator": {"propertyName": "@type"},
                    },
                }
            },
        },
    )


def test_branch_is_chosen_by_its_const_inside_allof():
    oas = _characteristic_oas()
    sample = {
        "characteristic": [
            {"@type": "IntegerCharacteristic", "name": "n", "value": 34}
        ]
    }
    schema = Schema.from_oas(oas, name="Product")
    breaker = SampleBreaker(schema)
    assert breaker._resolve_schema(sample, "characteristic[0].value") == {
        "type": "integer"
    }
    assert (
        _breaks(
            schema,
            _validator(oas, "Product"),
            sample,
            "characteristic[0].value",
            BreakKind.WRONG_TYPE,
        )
        == 20
    )


def test_parent_properties_next_to_oneof_are_resolved():
    # The OpenAPI Pet shape: the parent's own properties next to the
    # branches, merged with the chosen branch as the generator merges them.
    oas = _oas(
        Dog={
            "type": "object",
            "required": ["bark"],
            "properties": {"bark": {"type": "boolean"}},
        },
        Cat={
            "type": "object",
            "required": ["meow"],
            "properties": {"meow": {"type": "integer"}},
        },
        Pet={
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string", "maxLength": 8}},
            "oneOf": [{"$ref": REF + "Dog"}, {"$ref": REF + "Cat"}],
        },
    )
    schema = Schema.from_oas(oas, name="Pet")
    sample = {"name": "Rex", "bark": True}
    assert SampleBreaker(schema)._resolve_schema(sample, "name") == {
        "type": "string",
        "maxLength": 8,
    }
    for path, kind in [
        ("name", BreakKind.MAX_LENGTH_VIOLATION),
        ("name", BreakKind.WRONG_TYPE),
        ("bark", BreakKind.WRONG_TYPE),
    ]:
        assert _breaks(schema, _validator(oas, "Pet"), sample, path, kind)


def test_scalar_oneof_branch_is_chosen_by_the_value_type():
    raw = {
        "type": "object",
        "properties": {
            "x": {
                "oneOf": [
                    {"type": "string", "maxLength": 3},
                    {"type": "integer", "maximum": 5},
                ]
            }
        },
    }
    schema = Schema(data=raw)
    breaker = SampleBreaker(schema)
    assert breaker._resolve_schema({"x": 4}, "x") == {
        "type": "integer",
        "maximum": 5,
    }
    assert breaker._resolve_schema({"x": "ab"}, "x") == {
        "type": "string",
        "maxLength": 3,
    }
    validator = Draft202012Validator(raw)
    assert _breaks(schema, validator, {"x": 4}, "x", BreakKind.MAX_VIOLATION)


# ---------------------------------------------------------------------------
# A wrong type is one no branch accepts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prop, value",
    [
        ({"oneOf": [{"type": "string"}, {"type": "integer"}]}, "a"),
        ({"anyOf": [{"type": "integer"}, {"type": "string"}]}, 3),
        ({"type": ["integer", "string"]}, 3),
        ({"type": "number"}, 1.5),
        ({"type": "string", "nullable": True}, "a"),
        ({"enum": ["a", 1]}, 1),
        (
            {"allOf": [{"type": ["string", "integer"]}, {"type": "string"}]},
            "a",
        ),
    ],
    ids=[
        "oneOf",
        "anyOf",
        "type-list",
        "number-takes-integers",
        "nullable",
        "enum",
        "allOf",
    ],
)
def test_wrong_type_is_a_type_the_property_does_not_accept(prop, value):
    raw = {"type": "object", "properties": {"x": prop}}
    validator = Draft202012Validator(raw)
    assert (
        _breaks(
            Schema(data=raw),
            validator,
            {"x": value},
            "x",
            BreakKind.WRONG_TYPE,
        )
        == 20
    )


@pytest.mark.parametrize(
    "prop, value",
    [
        ({"type": ["string", "number"]}, "a"),
        # every scalar type is accepted: an array or an object is wrong
        ({"type": ["string", "number", "boolean", "null"]}, "a"),
        (
            {
                "anyOf": [
                    {"enum": ["x", "y"]},
                    {"type": "number"},
                    {"type": "boolean"},
                    {"type": "null"},
                ]
            },
            "x",
        ),
    ],
    ids=["number-takes-integers", "all-scalars", "all-scalars-anyOf"],
)
def test_wrong_type_avoids_every_accepted_type(prop, value):
    raw = {"type": "object", "properties": {"x": prop}}
    validator = Draft202012Validator(raw)
    assert (
        _breaks(
            Schema(data=raw),
            validator,
            {"x": value},
            "x",
            BreakKind.WRONG_TYPE,
        )
        == 20
    )


@pytest.mark.parametrize(
    "prop, value",
    [({"pattern": "^a"}, "abc"), ({"format": "date"}, "2026-10-29")],
    ids=["pattern", "format"],
)
def test_an_untyped_property_gets_what_it_got_before(prop, value):
    # No type is wrong for it; the string written as before still breaks
    # its pattern or format.
    raw = {"type": "object", "properties": {"x": prop}}
    validator = Draft202012Validator(
        raw, format_checker=Draft202012Validator.FORMAT_CHECKER
    )
    assert (
        _breaks(
            Schema(data=raw),
            validator,
            {"x": value},
            "x",
            BreakKind.WRONG_TYPE,
        )
        == 20
    )


@pytest.mark.parametrize(
    "sample", [{"x": [1, 2]}, {"x": ["a", "b"]}], ids=["integers", "strings"]
)
def test_an_array_branch_is_chosen_by_its_items(sample):
    raw = {
        "type": "object",
        "properties": {
            "x": {
                "oneOf": [
                    {"type": "array", "items": {"type": "string"}},
                    {"type": "array", "items": {"type": "integer"}},
                ]
            }
        },
    }
    validator = Draft202012Validator(raw)
    assert (
        _breaks(
            Schema(data=raw), validator, sample, "x[0]", BreakKind.WRONG_TYPE
        )
        == 20
    )


def test_branches_alike_but_for_their_const_are_told_apart():
    # Both branches have a string v; only the const tells which maxLength
    # applies.
    def branch(name, max_length):
        return {
            "type": "object",
            "required": ["@type", "v"],
            "properties": {
                "@type": {"const": name},
                "v": {"type": "string", "maxLength": max_length},
            },
        }

    raw = {
        "type": "object",
        "properties": {"x": {"oneOf": [branch("A", 3), branch("B", 10)]}},
    }
    sample = {"x": {"@type": "B", "v": "abcdefgh"}}
    assert (
        _breaks(
            Schema(data=raw),
            Draft202012Validator(raw),
            sample,
            "x.v",
            BreakKind.MAX_LENGTH_VIOLATION,
        )
        == 20
    )


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def test_accepted_types():
    assert _accepted_types({"type": "string", "nullable": True}) == {
        "string",
        "null",
    }
    assert _accepted_types({"type": "number"}) == {"number", "integer"}
    assert _accepted_types({"type": "string", "enum": ["a", 1]}) == {"string"}
    assert _accepted_types({"const": None}) == {"null"}
    assert _accepted_types({"oneOf": [{"type": "string"}, {}]}) is None
    assert _accepted_types({"description": "anything"}) is None


def test_accepted_types_of_a_composition_that_contains_itself():
    # three allOf parts that lead back to the node, level after level
    node = {"allOf": []}
    node["allOf"].extend([node, {"allOf": [node]}, {"type": "string"}])
    started = time.perf_counter()
    assert _accepted_types(node) == {"string"}
    assert time.perf_counter() - started < 1


def test_json_values():
    assert _json_type(1.0) == "integer" and _json_type(1.5) == "number"
    assert not _same_value(1, True) and _same_value(1, 1.0)
