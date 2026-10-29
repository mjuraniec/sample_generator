"""A ``oneOf``/``anyOf`` with an OpenAPI ``discriminator``: the
discriminator property names the branch that was generated, and a ``str``
selector finds a ``$ref`` branch through ``discriminator.mapping``
(issue #15)."""

from __future__ import annotations

import copy
import random

import pytest
from faker import Faker
from jsonschema import Draft202012Validator

from json_sample_generator import JSONSchemaGenerator
from json_sample_generator.JSONSchemaGenerator import _schema_name
from json_sample_generator.models import Scenario, Schema

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


def _pet(discriminator, pet_type=None, kind="oneOf", required=True):
    """The OpenAPI Pet shape: petType on the parent, neither branch pins
    it."""
    return {
        "definitions": {"Dog": DOG, "Cat": CAT},
        "type": "object",
        "properties": {
            "petType": pet_type or {"type": "string"},
            "name": {"type": "string"},
        },
        "required": ["name"] + (["petType"] if required else []),
        "discriminator": discriminator,
        kind: [{"$ref": "#/definitions/Dog"}, {"$ref": "#/definitions/Cat"}],
    }


MAPPING = {
    "propertyName": "petType",
    "mapping": {"dog": "#/definitions/Dog", "cat": "#/definitions/Cat"},
}


def _generate_many(schema, n=30, scenario=None, validate=True):
    gen = JSONSchemaGenerator(Schema(data=schema, base_uri="file://pets.json"))
    validator = Draft202012Validator(schema)
    outputs = []
    for seed in range(n):
        random.seed(seed)
        Faker.seed(seed)
        out = gen.generate(scenario)
        if validate:
            assert not list(validator.iter_errors(out)), (seed, out)
        outputs.append(out)
    return outputs


def _branch(out):
    return "dog" if "bark" in out else "cat"


# ---------------------------------------------------------------------------
# The discriminator property names the generated branch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["oneOf", "anyOf"])
def test_discriminator_is_the_mapping_key_of_the_branch(kind):
    outputs = _generate_many(_pet(MAPPING, kind=kind))
    assert {out["petType"] for out in outputs} == {"dog", "cat"}
    for out in outputs:
        assert out["petType"] == _branch(out), out


def test_mapping_to_a_bare_schema_name():
    disc = {"propertyName": "petType", "mapping": {"dog": "Dog", "cat": "Cat"}}
    for out in _generate_many(_pet(disc)):
        assert out["petType"] == _branch(out)


def test_without_mapping_the_discriminator_is_the_schema_name():
    # OpenAPI's implicit mapping
    for out in _generate_many(_pet({"propertyName": "petType"})):
        assert out["petType"] == {"dog": "Dog", "cat": "Cat"}[_branch(out)]


def test_branches_that_define_the_property_themselves():
    # nothing next to the oneOf but the discriminator: the branch is
    # generated directly, and still named
    def with_pet_type(branch):
        return {
            **branch,
            "properties": {
                **branch["properties"],
                "petType": {"type": "string"},
            },
            "required": branch["required"] + ["petType"],
        }

    schema = {
        "definitions": {"Dog": with_pet_type(DOG), "Cat": with_pet_type(CAT)},
        "discriminator": MAPPING,
        "oneOf": [
            {"$ref": "#/definitions/Dog"},
            {"$ref": "#/definitions/Cat"},
        ],
    }
    for out in _generate_many(schema):
        assert out["petType"] == _branch(out)


def test_each_array_item_names_its_own_branch():
    pet = _pet(MAPPING)
    schema = {
        "definitions": pet["definitions"],
        "type": "object",
        "required": ["pets"],
        "properties": {
            "pets": {
                "type": "array",
                "minItems": 3,
                "maxItems": 3,
                "items": {k: v for k, v in pet.items() if k != "definitions"},
            }
        },
    }
    for out in _generate_many(schema, n=10):
        for item in out["pets"]:
            assert item["petType"] == _branch(item)


# ---------------------------------------------------------------------------
# When the discriminator is left alone
# ---------------------------------------------------------------------------


def test_a_branch_that_pins_the_property_keeps_its_value():
    pinned = {
        **DOG,
        "properties": {**DOG["properties"], "petType": {"const": "canine"}},
    }
    schema = _pet(MAPPING)
    schema["definitions"] = {"Dog": pinned, "Cat": CAT}
    outputs = _generate_many(schema)
    assert {o["petType"] for o in outputs if "bark" in o} == {"canine"}


def test_a_value_the_property_rejects_is_not_written():
    # The implicit name "Dog" does not match the pattern: the property is
    # generated as before, and the sample stays valid.
    schema = _pet(
        {"propertyName": "petType"}, {"type": "string", "pattern": "^[a-z]+$"}
    )
    outputs = _generate_many(schema)
    assert not {"Dog", "Cat"} & {out["petType"] for out in outputs}


def test_override_and_default_data_win():
    scenario = Scenario(name="o", overrides={"petType": "set"})
    for out in _generate_many(_pet(MAPPING), n=10, scenario=scenario):
        assert out["petType"] == "set"
    seeded = Scenario(name="d", default_data={"petType": "seeded"})
    for out in _generate_many(_pet(MAPPING), n=10, scenario=seeded):
        assert out["petType"] == "seeded"


def test_minimal_mode_writes_only_a_required_discriminator():
    minimal = Scenario(name="m", minimal_mode=True)
    for out in _generate_many(_pet(MAPPING), n=10, scenario=minimal):
        assert out["petType"] == _branch(out)
    optional = _pet(MAPPING, required=False)
    for out in _generate_many(optional, n=10, scenario=minimal):
        assert "petType" not in out


def test_scenario_only_writes_nothing():
    only = Scenario(name="s", scenario_only=True)
    for out in _generate_many(
        _pet(MAPPING), n=5, scenario=only, validate=False
    ):
        assert "petType" not in out


def test_an_enum_on_the_parent_does_not_pin_the_branch():
    # a generated spec lists every discriminator value on the parent
    schema = _pet(MAPPING, {"type": "string", "enum": ["dog", "cat"]})
    for out in _generate_many(schema):
        assert out["petType"] == _branch(out)


HUSKY = {
    "type": "object",
    "required": ["petType", "pull"],
    "properties": {
        "petType": {"type": "string", "enum": ["husky"]},
        "pull": {"type": "integer"},
    },
}


def _nested(dog_discriminator=None, husky=HUSKY):
    """Dog is polymorphic in turn: a oneOf of Husky."""
    dog = {
        "type": "object",
        "properties": {"petType": {"type": "string"}},
        "oneOf": [{"$ref": "#/definitions/Husky"}],
    }
    if dog_discriminator:
        dog["discriminator"] = dog_discriminator
    return {
        "definitions": {"Husky": husky, "Dog": dog, "Cat": CAT},
        "type": "object",
        "required": ["petType"],
        "properties": {"petType": {"type": "string"}},
        "discriminator": {
            "propertyName": "petType",
            "mapping": {
                "husky": "#/definitions/Husky",
                "cat": "#/definitions/Cat",
            },
        },
        "oneOf": [
            {"$ref": "#/definitions/Dog"},
            {"$ref": "#/definitions/Cat"},
        ],
    }


def test_a_polymorphic_branch_is_named_by_its_own_level():
    # The outer level does not name Dog: its leaves pin the value...
    for out in _generate_many(_nested(), n=50):
        assert out["petType"] == ("husky" if "pull" in out else "cat")
    # ...or its own discriminator names the concrete type.
    unpinned = {
        **HUSKY,
        "properties": {**HUSKY["properties"], "petType": {"type": "string"}},
    }
    schema = _nested({"propertyName": "petType"}, unpinned)
    for out in _generate_many(schema, n=50):
        assert out["petType"] == ("Husky" if "pull" in out else "cat")


def test_oneof_exclusivity_still_leaves_the_property_out():
    # petType is required by Cat only: a Dog sample leaves it out, or it
    # would match both branches.
    schema = {
        "definitions": {
            "Dog": DOG,
            "Cat": {"type": "object", "required": ["petType"]},
        },
        "type": "object",
        "properties": {"petType": {"type": "string"}},
        "discriminator": MAPPING,
        "oneOf": [
            {"$ref": "#/definitions/Dog"},
            {"$ref": "#/definitions/Cat"},
        ],
    }
    outputs = _generate_many(schema, n=50)
    assert all("petType" not in out for out in outputs if "bark" in out)


def _without_discriminator(node):
    if isinstance(node, dict):
        return {
            k: _without_discriminator(v)
            for k, v in node.items()
            if k != "discriminator"
        }
    if isinstance(node, list):
        return [_without_discriminator(v) for v in node]
    return node


def _blank(node, prop):
    if isinstance(node, dict):
        return {
            k: "?" if k == prop else _blank(v, prop) for k, v in node.items()
        }
    if isinstance(node, list):
        return [_blank(v, prop) for v in node]
    return node


TREE = {
    "definitions": {
        "Leaf": {"type": "object", "properties": {"v": {"type": "integer"}}},
        "Tree": {
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "children": {
                    "type": "array",
                    "items": {"$ref": "#/definitions/Node"},
                },
            },
        },
        "Node": {
            "type": "object",
            "properties": {"kind": {"type": "string"}},
            "discriminator": {"propertyName": "kind"},
            "oneOf": [
                {"$ref": "#/definitions/Leaf"},
                {"$ref": "#/definitions/Tree"},
            ],
        },
    },
    "$ref": "#/definitions/Node",
}


@pytest.mark.parametrize(
    "schema, prop",
    [
        (_pet(MAPPING), "petType"),
        (_pet({"propertyName": "petType"}, required=False), "petType"),
        (_nested({"propertyName": "petType"}), "petType"),
        # an optional discriminator, and a cycle that drops optional
        # members from the second occurrence of Node on
        ({"allOf": [{"$ref": "#/definitions/Node"}], **TREE}, "kind"),
    ],
    ids=["mapping", "optional", "nested", "recursive"],
)
def test_only_the_discriminator_value_changes(schema, prop):
    # The same seeds without the discriminator give the same samples, the
    # discriminator's values aside: no member is added or left out.
    schema = {k: v for k, v in schema.items() if k != "$ref"}
    plain = _without_discriminator(schema)
    named = _generate_many(schema, validate=False)
    unnamed = _generate_many(plain, validate=False)
    for a, b in zip(named, unnamed):
        assert _blank(a, prop) == _blank(b, prop)


# ---------------------------------------------------------------------------
# A str selector through discriminator.mapping and $ref branches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, branch", [("cat", "cat"), ("dog", "dog")], ids=["cat", "dog"]
)
def test_str_selector_resolves_a_ref_branch_through_the_mapping(name, branch):
    scenario = Scenario(name="s", oneof_selectors={"": name})
    for out in _generate_many(_pet(MAPPING), n=10, scenario=scenario):
        assert _branch(out) == branch
        assert out["petType"] == name


def test_str_selector_resolves_the_schema_name():
    scenario = Scenario(name="s", oneof_selectors={"": "Cat"})
    for out in _generate_many(
        _pet({"propertyName": "petType"}), n=10, scenario=scenario
    ):
        assert _branch(out) == "cat"


def test_a_selector_key_names_the_branch_among_aliases():
    disc = {
        "propertyName": "petType",
        "mapping": {
            "dog": "#/definitions/Dog",
            "puppy": "#/definitions/Dog",
            "cat": "#/definitions/Cat",
        },
    }
    scenario = Scenario(name="s", oneof_selectors={"": "puppy"})
    for out in _generate_many(_pet(disc), n=10, scenario=scenario):
        assert (_branch(out), out["petType"]) == ("dog", "puppy")
    # without a selector the first key is used
    for out in _generate_many(_pet(disc), n=10):
        assert out["petType"] == _branch(out)


def test_references_to_other_documents_are_told_apart():
    # both documents define Pet: the mapping names them by document
    documents = {
        "file:///dog.json": {"definitions": {"Pet": DOG}},
        "file:///cat.json": {"definitions": {"Pet": CAT}},
    }

    def loader(uri, **kwargs):
        return copy.deepcopy(documents[uri.split("#")[0]])

    schema = {
        "type": "object",
        "required": ["petType"],
        "properties": {"petType": {"type": "string"}},
        "discriminator": {
            "propertyName": "petType",
            "mapping": {
                "dog": "dog.json#/definitions/Pet",
                "cat": "cat.json#/definitions/Pet",
            },
        },
        "oneOf": [
            {"$ref": "dog.json#/definitions/Pet"},
            {"$ref": "cat.json#/definitions/Pet"},
        ],
    }
    gen = JSONSchemaGenerator(
        Schema(data=schema, base_uri="file:///main.json"), loader=loader
    )
    for seed in range(20):
        random.seed(seed)
        out = gen.generate()
        assert out["petType"] == _branch(out)
    for name in ("cat", "dog"):
        out = gen.generate(Scenario(name="s", oneof_selectors={"": name}))
        assert (_branch(out), out["petType"]) == (name, name)


def test_str_selector_with_an_unknown_name_still_raises():
    gen = JSONSchemaGenerator(
        Schema(data=_pet(MAPPING), base_uri="file://p.json")
    )
    with pytest.raises(ValueError, match="no candidate matched"):
        gen.generate(Scenario(name="s", oneof_selectors={"": "snake"}))


# ---------------------------------------------------------------------------
# Only a named schema has a name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ref, name",
    [
        ("#/components/schemas/Dog", "Dog"),
        ("pets.yaml#/components/schemas/Dog", "Dog"),
        ("#/definitions/Dog", "Dog"),
        ("#/$defs/Dog", "Dog"),
        ("#/definitions/a~1b", "a/b"),
        ("#/components/schemas/Dog/properties/bark", None),
        ("#/x-sample-generator-shared/0", None),
        ("#/properties/pet", None),
        ("#", None),
        ("Dog", None),
    ],
)
def test_schema_name(ref, name):
    assert _schema_name(ref) == name
