"""A recursive ``$ref`` resolved up front: ``Schema.from_oas`` and
``Schema.from_raw_data`` hand it over as a structure that contains itself.
The generator writes it back as ``$ref`` and generates it the way the same
schema written with ``$ref`` is generated (issue #16)."""

from __future__ import annotations

import importlib
import json
import random
import time
import warnings

import pytest
from faker import Faker
from jsonschema import Draft202012Validator

from json_sample_generator import JSONSchemaGenerator
from json_sample_generator.JSONSchemaGenerator import (
    _SHARED_KEY,
    _as_references,
    _contains_itself,
    _holds_ref,
)
from json_sample_generator.models import Scenario, Schema

REF = "#/components/schemas/"


def _oas(**schemas):
    return {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": "1"},
        "paths": {},
        "components": {"schemas": schemas},
    }


def _oas_validator(oas, name):
    """Validates against the unresolved document, which jsonschema
    resolves lazily."""
    return Draft202012Validator(
        {"$ref": REF + name, "components": oas["components"]},
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def _generate_many(schema, validator=None, n=30):
    gen = JSONSchemaGenerator(schema)
    outputs = []
    for seed in range(n):
        random.seed(seed)
        Faker.seed(seed)
        out = gen.generate()
        if validator is not None:
            errors = [e.message for e in validator.iter_errors(out)]
            assert not errors, (seed, out, errors)
        outputs.append(out)
    return outputs


def _same_samples(a, b, n=30):
    for seed in range(n):
        samples = []
        for schema in (a, b):
            random.seed(seed)
            Faker.seed(seed)
            samples.append(JSONSchemaGenerator(schema).generate())
        assert samples[0] == samples[1], seed


ITEM_OAS = _oas(
    Item={
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "item": {"type": "array", "items": {"$ref": REF + "Item"}},
        },
    },
    Quote={
        "type": "object",
        "required": ["item"],
        "properties": {
            "item": {"type": "array", "items": {"$ref": REF + "Item"}}
        },
    },
)

SELF = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "item": {"type": "array", "items": {"$ref": "#"}},
    },
}

# Recursive shapes of tests/test_containers_depth_unique.py and
# tests/test_variant_merge.py.
BRANCHING_REQUIRED = {
    "type": "object",
    "required": ["root"],
    "properties": {"root": {"$ref": "#/$defs/T"}},
    "$defs": {
        "T": {
            "type": "object",
            "required": ["c0", "c1"],
            "properties": {
                "c0": {"$ref": "#/$defs/T"},
                "c1": {"$ref": "#/$defs/T"},
            },
        }
    },
}

RECURSIVE_ONEOF = {
    "type": "object",
    "required": ["root"],
    "properties": {"root": {"$ref": "#/$defs/T"}},
    "$defs": {
        "T": {
            "oneOf": [
                {
                    "type": "object",
                    "required": ["v"],
                    "properties": {"v": {"type": "integer"}},
                },
                {
                    "type": "object",
                    "required": ["c0", "c1", "c2"],
                    "properties": {
                        "c0": {"$ref": "#/$defs/T"},
                        "c1": {"$ref": "#/$defs/T"},
                        "c2": {"$ref": "#/$defs/T"},
                    },
                },
            ]
        }
    },
}

RECURSIVE_ONEOF_ALLOF = {
    "type": "object",
    "required": ["root"],
    "properties": {"root": {"$ref": "#/$defs/T"}},
    "$defs": {
        "T": {
            "oneOf": [
                {
                    "type": "object",
                    "required": ["v"],
                    "properties": {"v": {"type": "integer"}},
                },
                {
                    "allOf": [
                        {
                            "type": "object",
                            "required": ["a"],
                            "properties": {"a": {"type": "string"}},
                        },
                        {
                            "required": ["c"],
                            "properties": {"c": {"$ref": "#/$defs/T"}},
                        },
                    ]
                },
            ]
        }
    },
}

REFINED_NODE = {
    "definitions": {
        "Base": {
            "type": "object",
            "required": ["v"],
            "properties": {
                "v": {"type": "string"},
                "next": {"$ref": "#/definitions/Node"},
            },
        },
        "Node": {
            "allOf": [
                {"$ref": "#/definitions/Base"},
                {
                    "properties": {
                        "next": {"$ref": "#/definitions/Node"},
                        "kind": {"oneOf": [{"const": "x"}, {"const": "y"}]},
                    }
                },
            ]
        },
    },
    "type": "object",
    "required": ["root"],
    "properties": {"root": {"$ref": "#/definitions/Node"}},
}


# ---------------------------------------------------------------------------
# The reports of issue #16
# ---------------------------------------------------------------------------


def test_from_oas_component_holding_a_list_of_itself_generates():
    schema = Schema.from_oas(ITEM_OAS, name="Quote")
    outputs = _generate_many(schema, _oas_validator(ITEM_OAS, "Quote"))
    # the recursion is really generated: an Item holding Items
    assert any(
        item.get("item") for out in outputs for item in out["item"]
    ), outputs


def test_from_raw_data_self_reference_generates():
    schema = Schema.from_raw_data(SELF, base_uri="file:///item.json")
    outputs = _generate_many(schema, Draft202012Validator(SELF))
    assert any(out.get("item") for out in outputs)


# ---------------------------------------------------------------------------
# Generated the way the same schema written with $ref is
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        SELF,
        {
            "type": "object",
            "required": ["child"],
            "properties": {"child": {"$ref": "#"}},
        },
        REFINED_NODE,
        RECURSIVE_ONEOF,
        RECURSIVE_ONEOF_ALLOF,
        BRANCHING_REQUIRED,
    ],
    ids=[
        "self",
        "required-self",
        "refined-node",
        "recursive-oneof",
        "recursive-oneof-allof",
        "branching-required",
    ],
)
def test_from_raw_data_generates_what_the_unresolved_schema_does(raw):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        _same_samples(
            Schema(data=raw, base_uri="file://rec.json"),
            Schema.from_raw_data(raw, "file://rec.json"),
        )


def _unresolved(oas, name):
    """The component *name* as the root of its document, refs unresolved."""
    return Schema(
        data={
            **oas["components"]["schemas"][name],
            "components": oas["components"],
        },
        base_uri="file:///oas.json",
    )


def _same_without_components(a, b, n=30):
    for seed in range(n):
        samples = []
        for schema in (a, b):
            random.seed(seed)
            Faker.seed(seed)
            out = JSONSchemaGenerator(schema).generate()
            out.pop("components", None)
            samples.append(out)
        assert samples[0] == samples[1], seed


R = "#/components/schemas/"
BASE = {"type": "object", "properties": {"id": {"type": "string"}}}

# OpenAPI inheritance: each type is an allOf of a shared Base, every one of
# them used once, and a Root that holds a list of itself.
INHERITANCE_OAS = _oas(
    Base=BASE,
    A={
        "allOf": [
            {"$ref": R + "Base"},
            {
                "type": "object",
                "required": ["b"],
                "properties": {"b": {"$ref": R + "B"}},
            },
        ]
    },
    B={
        "allOf": [
            {"$ref": R + "Base"},
            {
                "type": "object",
                "required": ["c"],
                "properties": {"c": {"$ref": R + "C"}},
            },
        ]
    },
    C={
        "allOf": [
            {"$ref": R + "Base"},
            {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "opt": {"type": "string"},
                },
            },
        ]
    },
    Root={
        "type": "object",
        "required": ["a"],
        "properties": {
            "a": {"$ref": R + "A"},
            "children": {"type": "array", "items": {"$ref": R + "Root"}},
        },
    },
)

# Optional mutual recursion through a component used once.
MUTUAL_OAS = _oas(
    A={
        "type": "object",
        "properties": {"v": {"type": "integer"}, "b": {"$ref": R + "B"}},
    },
    B={
        "type": "object",
        "properties": {"w": {"type": "string"}, "a": {"$ref": R + "A"}},
    },
)


@pytest.mark.parametrize(
    "oas, name",
    [
        (ITEM_OAS, "Quote"),
        (ITEM_OAS, "Item"),
        (INHERITANCE_OAS, "Root"),
        (MUTUAL_OAS, "A"),
    ],
    ids=["no-cycle-at-root", "recursive-root", "inheritance", "mutual"],
)
def test_from_oas_generates_what_the_unresolved_document_does(oas, name):
    # The component as the root of the unresolved document: from_oas
    # records its components, so the generator writes them back as such.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _same_without_components(
            _unresolved(oas, name), Schema.from_oas(oas, name=name)
        )


def test_inheritance_is_not_taken_for_recursion():
    # A, B and C share Base: once inlined, their allOf would make Base look
    # like a node that recurs, and cut c. Generated in full, and valid.
    schema = Schema.from_oas(INHERITANCE_OAS, name="Root")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        outputs = _generate_many(
            schema, _oas_validator(INHERITANCE_OAS, "Root")
        )
    assert all("opt" in out["a"]["b"]["c"] for out in outputs)


def test_from_raw_data_with_a_fragment_generates_what_the_document_does(
    tmp_path,
):
    path = tmp_path / "doc.json"
    path.write_text(json.dumps(INHERITANCE_OAS))
    resolved = Schema.from_raw_data(
        INHERITANCE_OAS, f"{path.as_uri()}#/components/schemas/Root"
    )
    _same_without_components(_unresolved(INHERITANCE_OAS, "Root"), resolved)


def test_schema_path_names_the_reference_the_document_has():
    def schema_paths(schema):
        seen = set()

        def record(ctx):
            seen.add(ctx.schema_path)
            return "n"

        scenario = Scenario(
            name="s", pattern_overrides=[("item[0].id", record)]
        )
        for seed in range(10):
            random.seed(seed)
            JSONSchemaGenerator(schema).generate(scenario)
        return seen

    resolved = schema_paths(Schema.from_oas(ITEM_OAS, name="Item"))
    assert resolved == schema_paths(_unresolved(ITEM_OAS, "Item"))
    assert resolved == {R + "Item"}


# ---------------------------------------------------------------------------
# The cycle rule ends recursion through allOf, the OpenAPI inheritance shape
# ---------------------------------------------------------------------------

BASE = {"type": "object", "properties": {"id": {"type": "string"}}}


def _node_oas(**node_props):
    required = [k for k in node_props if not k.startswith("opt")]
    return _oas(
        Base=BASE,
        Node={
            "allOf": [
                {"$ref": REF + "Base"},
                {
                    "type": "object",
                    "required": required,
                    "properties": node_props,
                },
            ]
        },
    )


def test_required_recursion_through_allof_is_cut_and_warns():
    oas = _node_oas(child={"$ref": REF + "Node"})
    gen = JSONSchemaGenerator(Schema.from_oas(oas, name="Node"))
    random.seed(0)
    with pytest.warns(UserWarning, match="recursion limit"):
        out = gen.generate()
    # child: first occurrence of Node (full); child.child: second (required
    # members only); child.child.child: third, cut
    assert set(out["child"]) == {"id", "child"}
    assert out["child"]["child"] == {}


def test_branching_required_recursion_through_allof_warns_quickly():
    oas = _node_oas(a={"$ref": REF + "Node"}, b={"$ref": REF + "Node"})
    gen = JSONSchemaGenerator(Schema.from_oas(oas, name="Node"))
    started = time.perf_counter()
    with pytest.warns(UserWarning, match="recursion limit"):
        out = gen.generate()
    assert time.perf_counter() - started < 1
    assert {"a", "b"} <= set(out)


def test_optional_recursion_through_allof_terminates_silently():
    oas = _node_oas(optA={"$ref": REF + "Node"}, optB={"$ref": REF + "Node"})
    started = time.perf_counter()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _generate_many(
            Schema.from_oas(oas, name="Node"), _oas_validator(oas, "Node")
        )
    assert time.perf_counter() - started < 2


def test_recursive_property_defined_twice_stays_one_reference():
    # Base and Node both define `next`, Node through an inline allOf: the
    # merge wraps the two definitions in a fresh allOf, which the cycle
    # rule sees through only by the reference both of them hold.
    oas = _oas(
        Base={
            "type": "object",
            "required": ["v"],
            "properties": {
                "v": {"type": "string"},
                "next": {"$ref": REF + "Node"},
                "other": {"$ref": REF + "Node"},
            },
        },
        Node={
            "allOf": [
                {"$ref": REF + "Base"},
                {
                    "properties": {
                        "next": {
                            "allOf": [{"$ref": REF + "Node"}],
                            "description": "refined",
                        },
                        "other": {
                            "allOf": [{"$ref": REF + "Node"}],
                            "description": "refined",
                        },
                    }
                },
            ]
        },
    )
    started = time.perf_counter()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _generate_many(
            Schema.from_oas(oas, name="Node"), _oas_validator(oas, "Node")
        )
    assert time.perf_counter() - started < 2


def test_polymorphic_item_holding_items_validates():
    # The shape of TMF's QuoteItem in a strict OpenAPI variant: a
    # discriminated allOf whose items hold a list of the same type.
    oas = _oas(
        Entity={
            "type": "object",
            "properties": {
                "@type": {"type": "string"},
                "href": {"type": "string", "format": "uri"},
            },
        },
        QuoteItem={
            "discriminator": {
                "propertyName": "@type",
                "mapping": {"QuoteItem": REF + "QuoteItem"},
            },
            "allOf": [
                {"$ref": REF + "Entity"},
                {
                    "type": "object",
                    "required": ["id", "action"],
                    "properties": {
                        "id": {"type": "string"},
                        "action": {"enum": ["add", "modify", "delete"]},
                        "quoteItem": {
                            "type": "array",
                            "items": {"$ref": REF + "QuoteItem"},
                        },
                    },
                },
            ],
        },
        Quote={
            "type": "object",
            "required": ["quoteItem"],
            "properties": {
                "quoteItem": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"$ref": REF + "QuoteItem"},
                }
            },
        },
    )
    outputs = _generate_many(
        Schema.from_oas(oas, name="Quote"), _oas_validator(oas, "Quote")
    )
    assert any(
        item.get("quoteItem") for out in outputs for item in out["quoteItem"]
    )


# ---------------------------------------------------------------------------
# Only a schema that contains itself takes this path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "make",
    [
        lambda: Schema.from_oas(_oas(Base=BASE), name="Base"),
        lambda: Schema.from_raw_data(
            {
                "definitions": {"Base": BASE},
                "type": "object",
                "properties": {"b": {"$ref": "#/definitions/Base"}},
            },
            "file://b.json",
        ),
        lambda: Schema(data=SELF, base_uri="file://rec.json"),
    ],
    ids=["from-oas", "from-raw-data", "unresolved-recursive"],
)
def test_other_schemas_are_not_rewritten(make, monkeypatch):
    # the package's JSONSchemaGenerator attribute is the class
    module = importlib.import_module(
        "json_sample_generator.JSONSchemaGenerator"
    )
    calls = []
    monkeypatch.setattr(
        module,
        "_as_references",
        lambda data: calls.append(data) or _as_references(data),
    )
    JSONSchemaGenerator(make()).generate()
    assert calls == []


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def test_contains_itself():
    looped = {"type": "object", "properties": {}}
    looped["properties"]["child"] = looped
    listed = []
    listed.append(listed)
    shared = {"type": "string"}
    assert _contains_itself(looped)
    assert _contains_itself({"allOf": listed})
    # the same dict reached twice is not a cycle
    assert not _contains_itself({"properties": {"a": shared, "b": shared}})
    assert not _contains_itself(SELF)


def test_holds_ref():
    assert _holds_ref(SELF)
    # a property named "$ref" is not a reference
    assert not _holds_ref(
        {"type": "object", "properties": {"$ref": {"type": "string"}}}
    )
    assert not _holds_ref(Schema.from_raw_data(SELF, "file://x.json").data)


def test_as_references_writes_shared_dicts_once():
    item = {"type": "object", "properties": {"id": {"type": "string"}}}
    item["properties"]["item"] = {"type": "array", "items": item}
    root = {"type": "object", "properties": {"a": item, "b": item}}
    tree = _as_references(root)
    assert tree["properties"]["a"] == {"$ref": f"#/{_SHARED_KEY}/0"}
    assert tree["properties"]["b"] == {"$ref": f"#/{_SHARED_KEY}/0"}
    shared = tree[_SHARED_KEY]["0"]
    assert shared["properties"]["item"]["items"] == {
        "$ref": f"#/{_SHARED_KEY}/0"
    }
    # a tree again: plain JSON
    json.dumps(tree)


def test_as_references_writes_a_copied_root_in_place():
    # Schema.from_oas hands over a shallow copy of the component: the root
    # is written in place, the component it refers to once beside it.
    node = {"type": "object", "properties": {}}
    node["properties"]["child"] = node
    tree = _as_references(dict(node))
    ref = {"$ref": f"#/{_SHARED_KEY}/0"}
    assert tree == {
        "type": "object",
        "properties": {"child": ref},
        _SHARED_KEY: {"0": {"type": "object", "properties": {"child": ref}}},
    }


def test_as_references_keeps_a_hash_reference():
    resolved = Schema.from_raw_data(SELF, "file:///item.json").data
    tree = _as_references(resolved)
    assert tree["properties"]["item"]["items"] == {"$ref": "#"}
    assert _SHARED_KEY not in tree


def test_as_references_keeps_a_key_of_the_same_name():
    node = {"type": "object", _SHARED_KEY: "mine", "properties": {}}
    node["properties"]["child"] = node
    tree = _as_references(dict(node))
    assert tree[_SHARED_KEY] == "mine"
    assert tree["properties"]["child"] == {"$ref": f"#/{_SHARED_KEY}_/0"}


def test_as_references_takes_allof_for_a_component_when_nothing_says():
    # a self-containing Python structure: no JsonRef, no from_oas record
    base = {"type": "object", "properties": {"id": {"type": "string"}}}
    root = {"type": "object", "properties": {}}
    leaf = {
        "allOf": [
            base,
            {"type": "object", "properties": {"n": {"type": "string"}}},
        ]
    }
    root["properties"]["leaf"] = leaf
    root["properties"]["again"] = {"type": "array", "items": root}
    tree = _as_references(root)
    assert tree["properties"]["leaf"] == {"$ref": f"#/{_SHARED_KEY}/1"}


def test_as_references_gives_up_on_a_cycle_through_lists_only():
    listed = []
    listed.append(listed)
    assert _as_references({"allOf": listed}) is None
