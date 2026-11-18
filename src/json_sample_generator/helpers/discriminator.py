"""Names of ``oneOf``/``anyOf`` branches, as an OpenAPI ``discriminator``
gives them.

A branch is named by its key in ``discriminator.mapping`` or, when the
mapping does not list it, by the name of the schema it refers to (OpenAPI's
implicit mapping). Two things identify the schema a branch stands for:

* the ``$ref`` it was written with, while the branch is still a
  :class:`jsonref.JsonRef`;
* the :data:`SCHEMA_NAME_KEY` annotation that :func:`tag_schema_names`
  puts on every named schema of a document, which survives a loader that
  inlines the references (``Schema.from_oas``) or a deep copy.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urljoin

from jsonref import JsonRef

# A vendor extension, so the generators leave it out of merged schemas.
SCHEMA_NAME_KEY = "x-schema-name"


def schema_name(ref: Any) -> Optional[str]:
    """The name of the schema the ``$ref`` *ref* points at when it is a
    named one (``#/components/schemas/Dog``, ``#/definitions/Dog``,
    ``#/$defs/Dog``), else ``None``."""
    if not isinstance(ref, str) or "#" not in ref:
        return None
    segments = ref.rsplit("#", 1)[-1].strip("/").split("/")
    named = segments[-3:-1] == ["components", "schemas"] or (
        len(segments) >= 2 and segments[-2] in ("definitions", "$defs")
    )
    if not named or not segments[-1]:
        return None
    return segments[-1].replace("~1", "/").replace("~0", "~")


def tag_schema_names(document: Any) -> Any:
    """A copy of *document* in which every named schema (``components/
    schemas``, ``definitions``, ``$defs`` at the top level) carries its
    name as :data:`SCHEMA_NAME_KEY`. An existing annotation is kept."""
    if not isinstance(document, dict):
        return document
    tagged = copy.deepcopy(document)
    components = tagged.get("components")
    holders = [
        components.get("schemas") if isinstance(components, dict) else None,
        tagged.get("definitions"),
        tagged.get("$defs"),
    ]
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        for name, schema in holder.items():
            if isinstance(schema, dict):
                schema.setdefault(SCHEMA_NAME_KEY, name)
    return tagged


def _is_bare_name(target: str) -> bool:
    return "/" not in target and "#" not in target


def _tag(branch: Any) -> Optional[str]:
    tag = branch.get(SCHEMA_NAME_KEY) if isinstance(branch, dict) else None
    return tag if isinstance(tag, str) and tag else None


def _ref(branch: Any) -> Optional[str]:
    """The ``$ref`` the branch was written with: that of a resolved
    :class:`JsonRef`, or of a plain ``{"$ref": ...}`` dict (a schema that
    was not resolved, as the enumerators walk it)."""
    if isinstance(branch, JsonRef):
        ref = branch.__reference__.get("$ref")
    elif isinstance(branch, dict):
        ref = branch.get("$ref")
    else:
        return None
    return ref if isinstance(ref, str) and ref else None


def branch_schema_name(branch: Any) -> Optional[str]:
    """The name of the named schema *branch* stands for, else ``None``."""
    name = schema_name(_ref(branch))
    return name if name is not None else _tag(branch)


def refers_to(target: Any, branch: Any) -> bool:
    """True when *target*, a ``discriminator.mapping`` value (a reference,
    a file or a bare schema name), names the schema *branch* stands for.

    A reference is resolved against the branch's base URI, so
    ``#/definitions/Pet`` of two documents are two schemas. A bare name is
    compared with the schema's name."""
    if not isinstance(target, str) or not target:
        return False
    ref = _ref(branch)
    if ref is None:
        tag = _tag(branch)
        if tag is None:
            return False
        return tag == (
            target if _is_bare_name(target) else schema_name(target)
        )
    if target.startswith("#") and ref.startswith("#"):
        return target == ref
    try:
        # the proxy forwards attribute lookups to the schema it stands for
        base = object.__getattribute__(branch, "base_uri") or ""
    except AttributeError:
        base = ""
    if urljoin(base, target) == urljoin(base, ref):
        return True
    return _is_bare_name(target) and target == schema_name(ref)


def discriminator_name(
    parent: Any, selected: Any, preferred: Optional[str] = None
) -> Optional[Tuple[str, str]]:
    """The ``discriminator.propertyName`` of *parent* and the value that
    names the *selected* branch: its key in ``discriminator.mapping`` (the
    *preferred* key, the one a selector named it by, when several refer to
    it), else the name of the schema it stands for. A branch that is
    neither is named only by a mapping that refers to its ``title``.
    ``None`` when there is no discriminator or no name."""
    disc = parent.get("discriminator") if isinstance(parent, dict) else None
    if not isinstance(disc, dict):
        return None
    prop = disc.get("propertyName")
    if not isinstance(prop, str) or not prop:
        return None
    mapping: Dict[str, Any] = disc.get("mapping")
    if not isinstance(mapping, dict):
        mapping = {}
    keys = list(mapping)
    if preferred in mapping:
        keys.insert(0, preferred)
    for key in keys:
        if refers_to(mapping[key], selected):
            return prop, key
    name = branch_schema_name(selected)
    if name is not None:
        return prop, name
    title = selected.get("title") if isinstance(selected, dict) else None
    if isinstance(title, str):
        for key in keys:
            if str(mapping[key]).rsplit("/", 1)[-1] == title:
                return prop, key
    return None
