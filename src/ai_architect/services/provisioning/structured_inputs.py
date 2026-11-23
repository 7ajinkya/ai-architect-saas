"""Boundary normalization for structured human provisioning inputs.

The InfraSpec contract encodes DynamoDB attribute definitions and key
schema as comma-delimited tokens. The module contract decoders accept
exactly that grammar and deliberately have no JSON/YAML escape hatch.
Operators, however, commonly paste the JSON arrays shown by AWS and
Terraform documentation. Normalizing those documented shapes here,
before the strict contract decoder sees them, keeps the contract
grammar unchanged while making the human boundary usable.

Only exact, documented shapes are accepted. Nothing is inferred from
arbitrary JSON, and canonical token strings are passed through for the
contract decoder to validate.
"""
from __future__ import annotations

import json
import re

from ai_architect.domain.provisioning.models import (
    InfraSpec,
    ResourceInstance,
    ResourceKind,
)

_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,62}\Z")
_ATTRIBUTE_DEFINITION_KEYS = frozenset({"attribute_name", "attribute_type"})
_KEY_SCHEMA_KEYS = frozenset({"attribute_name", "key_type"})
_ATTRIBUTE_TYPES = frozenset({"S", "N", "B"})
_KEY_TYPES = frozenset({"HASH", "RANGE"})


class StructuredInputError(ValueError):
    """A structured input is not in an accepted shape."""


def _reject_extra_keys(
    item: dict, expected: frozenset[str], *, input_name: str,
    resource_id: str, position: int,
) -> None:
    unexpected = sorted(set(item) - expected)
    missing = sorted(expected - set(item))
    if unexpected or missing:
        detail = []
        if missing:
            detail.append(f"missing {missing}")
        if unexpected:
            detail.append(f"unexpected {unexpected}")
        raise StructuredInputError(
            f"Input {input_name!r} for resource {resource_id!r} entry "
            f"{position} must contain exactly {sorted(expected)} "
            f"({'; '.join(detail)})."
        )


def _string_field(
    item: dict, key: str, *, input_name: str, resource_id: str,
    position: int,
) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise StructuredInputError(
            f"Input {input_name!r} for resource {resource_id!r} entry "
            f"{position} field {key!r} must be a non-empty string."
        )
    return value.strip()


def _load_array(text: str, *, input_name: str, resource_id: str) -> list:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StructuredInputError(
            f"Input {input_name!r} for resource {resource_id!r} looks like "
            f"JSON but cannot be parsed: {exc.msg} at line {exc.lineno} "
            f"column {exc.colno}."
        ) from exc
    if not isinstance(payload, list):
        raise StructuredInputError(
            f"Input {input_name!r} for resource {resource_id!r} must be a "
            "JSON array."
        )
    if not payload:
        raise StructuredInputError(
            f"Input {input_name!r} for resource {resource_id!r} must contain "
            "at least one entry."
        )
    return payload


def _normalize_attribute_definitions(
    text: str, *, resource_id: str
) -> str:
    input_name = "attribute_definitions"
    tokens: list[str] = []
    seen: set[str] = set()
    for position, item in enumerate(
        _load_array(text, input_name=input_name, resource_id=resource_id)
    ):
        if not isinstance(item, dict):
            raise StructuredInputError(
                f"Input {input_name!r} for resource {resource_id!r} entry "
                f"{position} must be an object."
            )
        _reject_extra_keys(
            item, _ATTRIBUTE_DEFINITION_KEYS, input_name=input_name,
            resource_id=resource_id, position=position,
        )
        name = _string_field(
            item, "attribute_name", input_name=input_name,
            resource_id=resource_id, position=position,
        )
        attribute_type = _string_field(
            item, "attribute_type", input_name=input_name,
            resource_id=resource_id, position=position,
        ).upper()
        if not _IDENTIFIER.fullmatch(name):
            raise StructuredInputError(
                f"Input {input_name!r} for resource {resource_id!r} entry "
                f"{position} attribute_name {name!r} must start with a "
                "letter and contain only letters, digits, '_' or '-'."
            )
        if attribute_type not in _ATTRIBUTE_TYPES:
            raise StructuredInputError(
                f"Input {input_name!r} for resource {resource_id!r} entry "
                f"{position} attribute_type {attribute_type!r} must be one "
                "of B, N, S."
            )
        if name in seen:
            raise StructuredInputError(
                f"Input {input_name!r} for resource {resource_id!r} names "
                f"attribute {name!r} more than once."
            )
        seen.add(name)
        tokens.append(f"{name}:{attribute_type}")
    return ",".join(sorted(tokens))


def _normalize_key_schema(text: str, *, resource_id: str) -> str:
    input_name = "key_schema"
    by_key_type: dict[str, str] = {}
    for position, item in enumerate(
        _load_array(text, input_name=input_name, resource_id=resource_id)
    ):
        if not isinstance(item, dict):
            raise StructuredInputError(
                f"Input {input_name!r} for resource {resource_id!r} entry "
                f"{position} must be an object."
            )
        _reject_extra_keys(
            item, _KEY_SCHEMA_KEYS, input_name=input_name,
            resource_id=resource_id, position=position,
        )
        name = _string_field(
            item, "attribute_name", input_name=input_name,
            resource_id=resource_id, position=position,
        )
        key_type = _string_field(
            item, "key_type", input_name=input_name,
            resource_id=resource_id, position=position,
        ).upper()
        if not _IDENTIFIER.fullmatch(name):
            raise StructuredInputError(
                f"Input {input_name!r} for resource {resource_id!r} entry "
                f"{position} attribute_name {name!r} must start with a "
                "letter and contain only letters, digits, '_' or '-'."
            )
        if key_type not in _KEY_TYPES:
            raise StructuredInputError(
                f"Input {input_name!r} for resource {resource_id!r} entry "
                f"{position} key_type {key_type!r} must be HASH or RANGE."
            )
        if key_type in by_key_type:
            raise StructuredInputError(
                f"Input {input_name!r} for resource {resource_id!r} names "
                f"key_type {key_type!r} more than once."
            )
        by_key_type[key_type] = name
    if "HASH" not in by_key_type:
        raise StructuredInputError(
            f"Input {input_name!r} for resource {resource_id!r} must "
            "contain exactly one HASH key."
        )
    tokens = [f"HASH:{by_key_type['HASH']}"]
    if "RANGE" in by_key_type:
        tokens.append(f"RANGE:{by_key_type['RANGE']}")
    return ",".join(tokens)


def normalize_resource_inputs(
    resource: ResourceInstance, values: dict[str, str]
) -> dict[str, str]:
    """Return a copy of ``values`` with structured JSON normalized.

    Only DynamoDB ``attribute_definitions`` and ``key_schema`` are in
    scope. Values that do not start with ``[`` or ``{`` are left alone
    so the strict contract decoder remains the authority for canonical
    strings and malformed tokens.
    """
    normalized = dict(values)
    if resource.kind is not ResourceKind.DYNAMODB:
        return normalized
    parsers = {
        "attribute_definitions": _normalize_attribute_definitions,
        "key_schema": _normalize_key_schema,
    }
    for name, parser in parsers.items():
        if name not in normalized:
            continue
        value = normalized[name]
        if not isinstance(value, str):
            raise StructuredInputError(
                f"Input {name!r} for resource {resource.resource_id!r} must "
                "be a string."
            )
        text = value.strip()
        if not text.startswith(("[", "{")):
            continue
        if text.startswith("{"):
            raise StructuredInputError(
                f"Input {name!r} for resource {resource.resource_id!r} must "
                "be a JSON array, not an object."
            )
        normalized[name] = parser(text, resource_id=resource.resource_id)
    return normalized


def normalize_supplied_inputs(
    spec: InfraSpec, inputs: dict[str, dict[str, str]]
) -> dict[str, dict[str, str]]:
    """Normalize every known resource's supplied inputs without mutation."""
    resources = {resource.resource_id: resource for resource in spec.resources}
    normalized: dict[str, dict[str, str]] = {}
    for resource_id, values in inputs.items():
        resource = resources.get(resource_id)
        if resource is None:
            normalized[resource_id] = dict(values)
        else:
            normalized[resource_id] = normalize_resource_inputs(resource, values)
    return normalized
