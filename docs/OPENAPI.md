# User Guide: Loading from OpenAPI Specifications

This guide covers how to load and generate samples from OpenAPI Specification (OAS) documents using `json_sample_generator`.

## Overview

OpenAPI documents define APIs using a standard JSON/YAML format. The schemas for request/response bodies are defined in the `components/schemas` section. This library provides a factory method `Schema.from_oas()` that correctly handles the complexity of resolving cross-component `$ref` pointers.

## The Problem: Reference Resolution in OpenAPI

OpenAPI documents often contain cross-component references like:

```yaml
components:
  schemas:
    Pet:
      type: object
      properties:
        name: { type: string }
        category: { $ref: '#/components/schemas/Category' }
    Category:
      type: object
      properties:
        id: { type: integer }
        name: { type: string }
```

When you extract just the `Pet` schema and try to resolve it with:

```python
Schema.from_raw_data(pet_schema, base_uri="file:///oas.yaml")
```

the reference `$ref: '#/components/schemas/Category'` fails because the extracted fragment doesn't contain a `components` key — it's only a sub-object of the larger document.

### The `jsonref` Caching Issue

The root cause is how the `jsonref` library caches resolved documents. When you call `replace_refs(fragment, base_uri="file:///oas.yaml")` on a schema fragment:

1. `jsonref` caches the fragment under `"file:///oas.yaml"`
2. Later, a `$ref: '#/components/schemas/...'` tries to resolve against the cached document
3. The lookup fails with `KeyError: 'components'` because the cache has the fragment, not the full OAS

## The Solution: `Schema.from_oas()`

Use `Schema.from_oas()` to load from a full OpenAPI document:

```python
import yaml
from src.json_sample_generator import JSONSchemaGenerator
from src.json_sample_generator.models import Schema

# Load the full OAS document
with open("api.yaml") as f:
    oas = yaml.safe_load(f)

# Extract the Pet schema correctly
schema = Schema.from_oas(oas, name="Pet")

# Generate sample data
gen = JSONSchemaGenerator(schema)
sample = gen.generate()

print(sample)
# {
#   "name": "...",
#   "category": {
#     "id": 123,
#     "name": "..."
#   }
# }
```

### Method Signature

```python
@staticmethod
Schema.from_oas(
    oas_dict: Dict[str, Any],
    name: str,
    base_uri: str = "file:///oas.yaml",
) -> Schema
```

**Parameters:**

- **`oas_dict`** — The full OpenAPI Specification as a dictionary. Can be loaded from YAML/JSON.
- **`name`** — The name of the component schema to extract from `components/schemas/{name}`.
- **`base_uri`** — (Optional) The URI for reference resolution. Defaults to `"file:///oas.yaml"`. Use a custom value if your OAS references external files with specific URIs.

**Returns:** A `Schema` instance ready to pass to `JSONSchemaGenerator`.

**Raises:** `ValueError` if the named schema is not found in `components/schemas`.

## How It Works

`Schema.from_oas()`:

1. Calls `replace_refs()` on the **full OAS document** (not a fragment)
2. This ensures all cross-component `$ref` pointers are resolved in the correct context
3. Extracts the target component schema from the resolved document
4. Returns a `Schema` with the correct `base_uri` fragment pointing to that component

The key insight: resolve first in full context, then extract. This is the opposite of extracting first and then trying to resolve (which fails).

### Recursive components

A component that refers to itself, directly or through others (a `QuoteItem` holding a list of `QuoteItem`), resolves to a structure that contains itself. `JSONSchemaGenerator` accepts it: `Schema.from_oas` records which schemas are the document's components, and the generator writes the structure back with a `$ref` to each, as the document has them, before resolving it. The component then generates the same samples as the unresolved document with that component as its root, and the recursion ends through the cycle rule described in the README ("Recursion, nesting depth and `uniqueItems`"). The break enumerators and `SampleBreaker` read the resolved structure as it is.

## Complete Example

```python
import yaml
from src.json_sample_generator import JSONSchemaGenerator
from src.json_sample_generator.models import Schema, Scenario

# Load OpenAPI spec
with open("petstore.yaml") as f:
    oas = yaml.safe_load(f)

# Extract and customize the Pet schema
schema = Schema.from_oas(oas, name="Pet")

scenario = Scenario(
    name="valid-dog",
    overrides={
        "name": "Fluffy",
        "category.name": "Dogs",
    }
).normalize()

gen = JSONSchemaGenerator(schema, scenario=scenario)
sample = gen.generate()

print(sample)
# {
#   "name": "Fluffy",
#   "category": {
#     "id": 123,
#     "name": "Dogs"
#   }
# }
```

## When to Use `from_raw_data` vs `from_oas`

### Use `Schema.from_oas()` when:

- Loading a component from a full OpenAPI document
- The component contains `$ref` pointers to other components
- You want automatic, correct resolution without manual setup

### Use `Schema.from_raw_data()` when:

- Working with a pure JSON Schema (no OpenAPI structure)
- The schema doesn't have a `components` key at the top level
- You're loading a single, self-contained schema with its own `$ref` handling

## Warning: Accidental Misuse

If you call `Schema.from_raw_data()` with a full OAS document and no fragment, you'll see a warning:

```
UserWarning: base_uri has no fragment but the schema data contains a 'components' 
key — this looks like a full OpenAPI document. Use Schema.from_oas(oas_dict, 
name=...) to correctly resolve $refs within component schemas.
```

This warning alerts you to the broken pattern and suggests the correct approach. You can safely ignore it if you deliberately want to treat the whole OAS as a JSON Schema (unusual but sometimes valid).

## Advanced: Custom Base URI

For OpenAPI documents loaded from or containing references to external files, provide a custom `base_uri`:

```python
# If your OAS references https://api.example.com/schemas.json#/components/schemas/Foo
schema = Schema.from_oas(
    oas, 
    name="Pet",
    base_uri="https://api.example.com/schemas.json"
)
```

The returned `Schema` will have `base_uri="https://api.example.com/schemas.json#/components/schemas/Pet"`, and the `JSONSchemaGenerator` will use this for any further ref resolution.

## See Also

- [`docs/SCENARIOS.md`](SCENARIOS.md) — Scenario overrides and the Context object
- [`examples/usage_example.py`](../examples/usage_example.py) — Full working example
