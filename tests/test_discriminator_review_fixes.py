"""Regressions found while reviewing the discriminator naming (PR #22).

Each test pins one review finding; see the test names for the behaviour.
"""

from __future__ import annotations

import copy
import json
import random

import pytest
from faker import Faker
from jsonschema import Draft202012Validator, FormatChecker

from json_sample_generator import JSONSchemaGenerator
from json_sample_generator.models import Scenario, Schema
from json_sample_generator.scenario_enum import collect_variant_sites

DOG = {
    "type": "object",
    "properties": {"bark": {"type": "string"}},
    "required": ["bark"],
}
CAT = {
    "type": "object",
    "properties": {"meow": {"type": "string"}},
    "required": ["meow"],
}
MAPPING = {
    "propertyName": "petType",
    "mapping": {"dog": "#/definitions/Dog", "cat": "#/definitions/Cat"},
}


def _pet(
    disc=MAPPING,
    props=None,
    defs=None,
    branches=None,
    required=("name", "petType"),
    extra=None,
):
    schema = {
        "definitions": defs or {"Dog": DOG, "Cat": CAT},
        "type": "object",
        "properties": props
        or {"petType": {"type": "string"}, "name": {"type": "string"}},
        "required": list(required),
        "oneOf": branches
        or [{"$ref": "#/definitions/Dog"}, {"$ref": "#/definitions/Cat"}],
    }
    if disc is not None:
        schema["discriminator"] = disc
    schema.update(extra or {})
    return schema


def _generate(
    schema, scenario=None, n=30, base_uri="file://pets.json", **kwargs
):
    gen = JSONSchemaGenerator(Schema(data=schema, base_uri=base_uri), **kwargs)
    outputs = []
    for seed in range(n):
        random.seed(seed)
        Faker.seed(seed)
        outputs.append(gen.generate(scenario))
    return outputs


def _scenario(**kwargs):
    return Scenario(name="s", **kwargs)


# -- selection -----------------------------------------------------------


def test_exact_mapping_match_beats_an_earlier_branchs_title():
    legacy = {
        "title": "Cat",
        "type": "object",
        "properties": {"legacy": {"type": "string"}},
        "required": ["legacy"],
    }
    schema = _pet(
        disc={
            "propertyName": "petType",
            "mapping": {"cat": "#/definitions/Cat"},
        },
        defs={"LegacyCat": legacy, "Cat": CAT},
        branches=[
            {"$ref": "#/definitions/LegacyCat"},
            {"$ref": "#/definitions/Cat"},
        ],
    )
    for out in _generate(schema, _scenario(oneof_selectors={"": "cat"}), n=5):
        assert "meow" in out and "legacy" not in out
        assert out["petType"] == "cat"


def test_relative_file_mapping_values_name_and_select(tmp_path):
    (tmp_path / "Dog.json").write_text(json.dumps(DOG))
    (tmp_path / "Cat.json").write_text(json.dumps(CAT))
    schema = _pet(
        disc={
            "propertyName": "petType",
            "mapping": {"dog": "Dog.json", "cat": "Cat.json"},
        },
        defs={},
        branches=[{"$ref": "Dog.json"}, {"$ref": "Cat.json"}],
    )
    base = (tmp_path / "pet.json").as_uri()
    for out in _generate(schema, base_uri=base, n=20):
        assert out["petType"] == ("dog" if "bark" in out else "cat")
    out = _generate(
        schema, _scenario(oneof_selectors={"": "dog"}), base_uri=base, n=1
    )[0]
    assert out["petType"] == "dog" and "bark" in out


# -- what is left alone ---------------------------------------------------


def test_branch_that_declares_the_value_with_default_is_left_alone():
    dog = {
        "type": "object",
        "required": ["bark", "petType"],
        "properties": {
            "bark": {"type": "string"},
            "petType": {"type": "string", "default": "doggo"},
        },
    }
    schema = _pet(
        disc={"propertyName": "petType"}, defs={"Dog": dog, "Cat": CAT}
    )
    dogs = [o for o in _generate(schema) if "bark" in o]
    assert dogs
    assert {o["petType"] for o in dogs} != {"Dog"}


def test_property_format_is_respected():
    schema = _pet(
        props={
            "petType": {"type": "string", "format": "email"},
            "name": {"type": "string"},
        }
    )
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    for out in _generate(schema, n=20):
        assert not list(validator.iter_errors(out)), out


# -- branches that are polymorphic in turn ---------------------------------


def test_inner_anyof_without_discriminator_is_named_by_the_outer_level():
    dog = {
        "type": "object",
        "properties": {
            "bark": {"type": "string"},
            "wag": {"type": "string"},
        },
        "anyOf": [{"required": ["bark"]}, {"required": ["wag"]}],
    }
    schema = _pet(defs={"Dog": dog, "Cat": CAT})
    dogs = [o for o in _generate(schema, n=40) if "meow" not in o]
    assert dogs
    for out in dogs:
        assert out["petType"] == "dog", out


def test_anyof_and_oneof_on_one_node_still_name_the_branch():
    schema = _pet(
        props={
            "petType": {"type": "string"},
            "name": {"type": "string"},
            "tag": {"type": "string"},
            "id": {"type": "string"},
        },
        extra={"anyOf": [{"required": ["tag"]}, {"required": ["id"]}]},
    )
    for out in _generate(schema):
        assert out["petType"] == ("dog" if "bark" in out else "cat"), out


# -- the written value --------------------------------------------------------


@pytest.mark.parametrize("order", ["type-first", "name-first"])
def test_siblings_see_the_name_not_the_random_value(order):
    props = {"petType": {"type": "string"}, "name": {"type": "string"}}
    if order == "name-first":
        props = dict(reversed(list(props.items())))
    scenario = _scenario(
        overrides={"name": lambda ctx: f"{ctx.data['petType']}-1"}
    )
    for out in _generate(_pet(props=props), scenario):
        assert out["petType"] in ("dog", "cat")
        assert out["name"] == f"{out['petType']}-1", out


def test_a_selector_below_the_branch_sees_the_name():
    seen = []

    def pick(ctx, candidates):
        seen.append(ctx.data["petType"])
        return 0

    pet = _pet()
    pet["properties"]["toy"] = {
        "oneOf": [
            {"type": "object", "title": "Ball"},
            {"type": "object", "title": "Bone"},
        ]
    }
    pet["required"].append("toy")
    _generate(pet, _scenario(oneof_selectors={"toy": pick}), n=10)
    assert seen and set(seen) <= {"dog", "cat"}


def test_nullable_discriminator_generated_as_null_is_named():
    schema = _pet(
        props={
            "petType": {"type": ["null", "string"]},
            "name": {"type": "string"},
        }
    )
    for out in _generate(schema, n=10):
        assert out["petType"] == ("dog" if "bark" in out else "cat"), out


def test_unique_items_of_discriminated_branches_still_generate():
    shapes = {
        "definitions": {
            "Circle": {
                "type": "object",
                "properties": {"r": {"type": "number"}},
            },
            "Square": {
                "type": "object",
                "properties": {"s": {"type": "number"}},
            },
        },
        "type": "object",
        "required": ["shapes"],
        "properties": {
            "shapes": {
                "type": "array",
                "uniqueItems": True,
                "minItems": 3,
                "items": {
                    "type": "object",
                    "required": ["kind"],
                    "properties": {"kind": {"type": "string"}},
                    "discriminator": {
                        "propertyName": "kind",
                        "mapping": {
                            "circle": "#/definitions/Circle",
                            "square": "#/definitions/Square",
                        },
                    },
                    "oneOf": [
                        {"$ref": "#/definitions/Circle"},
                        {"$ref": "#/definitions/Square"},
                    ],
                },
            }
        },
    }
    for out in _generate(
        shapes, _scenario(minimal_mode=True), base_uri="file://s.json", n=10
    ):
        items = out["shapes"]
        assert len(items) >= 3
        assert len({json.dumps(i, sort_keys=True) for i in items}) == len(
            items
        )


# -- loaders -------------------------------------------------------------------


def _oas_doc():
    return {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {},
        "components": {
            "schemas": {
                "Dog": copy.deepcopy(DOG),
                "Cat": copy.deepcopy(CAT),
                "Pet": {
                    "type": "object",
                    "required": ["name", "petType"],
                    "properties": {
                        "petType": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    "discriminator": {
                        "propertyName": "petType",
                        "mapping": {
                            "dog": "#/components/schemas/Dog",
                            "cat": "#/components/schemas/Cat",
                        },
                    },
                    "oneOf": [
                        {"$ref": "#/components/schemas/Dog"},
                        {"$ref": "#/components/schemas/Cat"},
                    ],
                },
            }
        },
    }


@pytest.fixture
def oas_file(tmp_path):
    path = tmp_path / "oas.json"
    path.write_text(json.dumps(_oas_doc()))
    return path


@pytest.mark.parametrize("loader", ["from_oas", "from_raw_data"])
def test_loaders_keep_the_branch_names(loader, oas_file):
    uri = oas_file.as_uri()
    if loader == "from_oas":
        schema = Schema.from_oas(_oas_doc(), "Pet", base_uri=uri)
    else:
        schema = Schema.from_raw_data(
            json.loads(oas_file.read_text()), uri + "#/components/schemas/Pet"
        )
    gen = JSONSchemaGenerator(schema)
    for seed in range(20):
        random.seed(seed)
        Faker.seed(seed)
        out = gen.generate()
        assert out["petType"] == ("dog" if "bark" in out else "cat"), out
    random.seed(0)
    out = gen.generate(_scenario(oneof_selectors={"": "cat"}))
    assert out["petType"] == "cat" and "meow" in out


# -- enumerator labels -----------------------------------------------------------


def test_variant_sites_are_labelled_with_the_names_the_generator_writes():
    sites = collect_variant_sites(
        Schema(data=_pet(), base_uri="file://pets.json")
    )
    assert [s.names for s in sites] == [("dog", "cat")]
