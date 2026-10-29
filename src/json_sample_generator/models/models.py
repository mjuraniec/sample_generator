# scenario.py
from __future__ import annotations

import warnings
from typing import Any, Callable, Dict, List, Optional, Tuple, Union, cast

from jsonref import jsonloader, replace_refs
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

# Define a type for scenario override functions
ScenarioOverrideFn = Callable[["Context"], Any]

# Define a type for scenario override values (can be a function or a direct value)
ScenarioOverrideValue = Union[ScenarioOverrideFn, Any]


class Scenario(BaseModel):
    name: str
    description: Optional[str] = None
    overrides: Dict[str, Any] = Field(
        default_factory=dict
    )  # Allow any value type
    pattern_overrides: List[Tuple[str, Any]] = Field(
        default_factory=list
    )  # Allow any value type
    # Optional selectors for oneOf arrays. Keys are property paths, values are
    # callables that receive a Context and the list of candidate schemas and
    # return either an index (int) or a selected schema object.
    oneof_selectors: Dict[str, Any] = Field(default_factory=dict)
    # Default data used to initialize the generated result
    default_data: Dict[str, Any] = Field(default_factory=dict)
    # When True, omit optional properties that are not explicitly referenced
    # by overrides, default_data, or oneof_selectors. Only required fields
    # (and explicitly-mentioned ones) are emitted.
    minimal_mode: bool = False
    # When True, emit ONLY fields covered by the scenario
    # (overrides, pattern_overrides, default_data, oneof_selectors).
    # Stricter than minimal_mode: even required fields are omitted if
    # the scenario does not touch them. The produced sample may be
    # schema-invalid by design.
    scenario_only: bool = False

    @property
    def variant_selectors(self) -> Dict[str, Any]:
        """Read-only alias for :attr:`oneof_selectors`.

        Selectors now apply to both ``oneOf`` and ``anyOf`` branches, so
        ``variant_selectors`` reads more accurately. The underlying dict is
        the same object, so mutations through either name are visible.
        """
        return self.oneof_selectors

    def normalize(self) -> "Scenario":
        """
        Convert any non-function overrides to lambda functions automatically.
        This allows users to specify simple values directly without writing lambdas.
        """
        # Process regular overrides
        converted_overrides = {}
        for key, value in self.overrides.items():
            if callable(value):
                # Keep function as is
                converted_overrides[key] = value
            else:
                # Convert direct value to a lambda function that returns the value
                direct_value = value  # Capture the value in a closure
                converted_overrides[key] = lambda ctx, val=direct_value: val

        # Process pattern overrides
        converted_pattern_overrides = []
        for pattern, value in self.pattern_overrides:
            if callable(value):
                # Keep function as is
                converted_pattern_overrides.append((pattern, value))
            else:
                # Convert direct value to a lambda function that returns the value
                direct_value = value  # Capture the value in a closure
                converted_pattern_overrides.append(
                    (pattern, lambda ctx, val=direct_value: val)
                )

        # Update the scenario with converted values
        self.overrides = converted_overrides
        self.pattern_overrides = converted_pattern_overrides

        # Process oneOf selectors: convert non-callables to callables that
        # return the provided value (index or schema) so users can provide a
        # simple integer index or schema directly.
        converted_oneof = {}
        for key, value in self.oneof_selectors.items():
            if callable(value):
                converted_oneof[key] = value
            else:
                direct_value = value
                converted_oneof[key] = (
                    lambda ctx, schemas, val=direct_value: val
                )

        self.oneof_selectors = converted_oneof

        return self


class Schema(BaseModel):
    data: Dict[Any, Any]
    base_uri: Optional[str] = None
    # Set by from_oas: each resolved component schema in data, with its
    # name, so that JSONSchemaGenerator can write a recursive schema back
    # as the document wrote it (see _as_references there).
    _components: List[Tuple[Any, str]] = PrivateAttr(default_factory=list)

    @staticmethod
    def from_raw_data(raw: Dict[str, Any], base_uri: str) -> Schema:
        """
        Create a Schema instance from raw data.
        This is useful for creating schemas without needing to load from a file.
        """
        if "#" not in base_uri and "components" in raw:
            warnings.warn(
                "base_uri has no fragment but the schema data contains a "
                "'components' key — this looks like a full OpenAPI document. "
                "Use Schema.from_oas(oas_dict, name=...) to correctly resolve "
                "$refs within component schemas.",
                UserWarning,
                stacklevel=2,
            )

        data = cast(
            Dict[Any, Any],
            replace_refs(raw, base_uri=base_uri, loader=jsonloader),
        )

        if "#" in base_uri:
            # If base_uri contains a fragment, remove it for consistency
            fragment = base_uri.split("#")[1]

            # Navigate to the fragment path in the data
            fragment_parts = fragment.split("/")
            current_data = data
            for part in fragment_parts:
                if part and part in current_data:
                    current_data = current_data[part]
                elif part:
                    raise ValueError(
                        f"Fragment '{fragment}' not found in schema data."
                    )
            data = current_data

        return Schema(data=data, base_uri=base_uri)

    @staticmethod
    def from_oas(
        oas_dict: Dict[str, Any],
        name: str,
        base_uri: str = "file:///oas.yaml",
    ) -> "Schema":
        """
        Create a Schema from a named component in an OpenAPI Specification.

        This method resolves all $refs in the context of the full OAS
        document before extracting the target component, avoiding the
        jsonref caching bug that occurs when replace_refs is called on a
        schema fragment directly.

        Args:
            oas_dict: The full OpenAPI Specification dictionary.
            name: The name of the component schema to extract
                (from components/schemas/{name}).
            base_uri: The base URI for ref resolution (default
                "file:///oas.yaml").

        Returns:
            A Schema instance with the extracted component and a
            base_uri pointing to the component fragment.

        Raises:
            ValueError: If the named schema is not found in
                components/schemas.
        """
        resolved = replace_refs(
            oas_dict, base_uri=base_uri, proxies=False, loader=jsonloader
        )
        try:
            schema_data = resolved["components"]["schemas"][name]
        except KeyError:
            raise ValueError(
                f"Schema '{name}' not found in components/schemas "
                "of the OAS document."
            )
        schema = Schema(
            data=dict(schema_data),
            base_uri=f"{base_uri}#/components/schemas/{name}",
        )
        schema._components = [
            (component, component_name)
            for component_name, component in resolved["components"][
                "schemas"
            ].items()
            if isinstance(component, dict)
        ]
        return schema


class Context(BaseModel):
    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    prop_path: str
    # The result built so far: a dict, or a list for a root array schema.
    data: Any
    schema_data: Dict[str, Any]
    schema_path: Optional[str] = None
    parent_schema: Optional[Dict[str, Any]] = None
    parent_ctx: Optional["Context"] = None
    # Markers of the schema nodes on the path from the root ("$ref"
    # strings and ids of inline schema dicts). A marker that repeats means
    # the schema recursed into itself.
    ref_path: Tuple[Any, ...] = ()
    # True once a marker repeated on the path: from here down optional
    # properties and array items beyond minItems are left out, which is
    # what makes recursive schemas terminate.
    recursive: bool = False
    # Properties that only the non-selected oneOf branches require. The
    # object handler leaves them out unless the object itself requires
    # them or the scenario names them. Applies to this node only.
    excluded_properties: Tuple[str, ...] = ()

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.data[key] = value

    def copy(self, **updates) -> "Context":
        """
        Shallow clone with optional field overrides:
            new_rec = rec.copy(path="new.json", data={**rec.data, "x": 1})
        """
        return self.model_copy(update=updates)
