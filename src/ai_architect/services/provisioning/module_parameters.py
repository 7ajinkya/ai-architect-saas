"""Contract-driven typed parameter decoding for Slice 4C.

The pipeline is deliberately one-directional and fully determined by the
module contract:

    InfraSpec ResolvedParameter (frozen 4A, unchanged)
        -> ModuleInputDefinition (declares type + constraints)
        -> typed, validated TypedValue
        -> ModuleInputBinding

The **contract drives the decoding**. A decoder never inspects a parameter's
name to decide what it means, and there is no general HCL, JSON or YAML
escape hatch: a value is either representable under its declared type and
constraints, or it fails closed.

This is the same anti-reinterpretation discipline the frozen 4A spec builder
used, promoted from renderer-local code into declared, versioned catalog
content. The ``ATTRIBUTE_LIST`` / ``KEY_SCHEMA`` grammars exist only as a
migration bridge for the two frozen 4A parameters that encode structure in
strings; the ``nosql_table`` contract immediately projects them into scalar
module inputs so no caller ever expresses Terraform structure.

4B's ``terraform_renderer._CODECS`` and its DynamoDB token grammar remain in
place untouched. They become obsolete when the module invocation renderer
lands in a later slice; this module does not modify or delete them.
"""

import re
from typing import Optional

from ai_architect.domain.provisioning.models import (
    ResourceInstance,
    ResourceKind,
)
from ai_architect.domain.provisioning.module import (
    AttributeSpec,
    BindingOrigin,
    DerivationKind,
    KeySchemaSpec,
    ModuleContract,
    ModuleDiagnostic,
    ModuleDiagnosticCategory,
    ModuleErrorCode,
    ModuleInputBinding,
    ModuleInputDefinition,
    ParameterBindingSpec,
    ParameterConstraints,
    ParameterType,
    TypedArtifactRef,
    TypedAttributeList,
    TypedBool,
    TypedEnum,
    TypedInt,
    TypedKeySchema,
    TypedSecretRef,
    TypedString,
    TypedStringList,
    TypedValue,
)
from ai_architect.services.provisioning.deployment_naming import (
    namespaced_name,
)

#: Fixed literal sets for boolean parameters. Deliberately excludes "1",
#: "yes" and "on": truthiness coercion is exactly the silent reinterpretation
#: this boundary exists to prevent.
_TRUE_LITERALS = frozenset({"true", "enabled"})
_FALSE_LITERALS = frozenset({"false", "disabled"})

#: Scalars that may appear in a structured token. Excludes quote, brace,
#: dollar, backslash and newline, so no HCL syntax can survive the grammar.
_IDENTIFIER = r"[A-Za-z][A-Za-z0-9_-]{0,62}"
_ATTRIBUTE_TOKEN = re.compile(rf"^({_IDENTIFIER}):([SNB])$")
_KEY_TOKEN = re.compile(rf"^(HASH|RANGE):({_IDENTIFIER})$")
_INT_TOKEN = re.compile(r"^[0-9]{1,9}$")


class ParameterDecodeError(Exception):
    """A value cannot be represented under its declared contract type."""

    def __init__(self, message, code, input_name=None, optional_absent=False):
        super().__init__(message)
        self.message = message
        self.code = code
        self.input_name = input_name
        # True when a *non-required* input simply has no value for this
        # resource. That is not an error; the input is left unbound.
        self.optional_absent = optional_absent


def _supplied(resource: ResourceInstance) -> dict:
    return {parameter.name: parameter.value for parameter in resource.parameters}


def _check_length(definition: ModuleInputDefinition, value: str) -> None:
    minimum = definition.constraints.min_length
    maximum = definition.constraints.max_length
    if minimum is not None and len(value) < minimum:
        raise ParameterDecodeError(
            f"Input {definition.name!r} is shorter than the declared minimum "
            f"length of {minimum}.",
            ModuleErrorCode.MODULE_INPUT_TYPE_INVALID,
            definition.name,
        )
    if maximum is not None and len(value) > maximum:
        raise ParameterDecodeError(
            f"Input {definition.name!r} exceeds the declared maximum length of "
            f"{maximum}.",
            ModuleErrorCode.MODULE_INPUT_TYPE_INVALID,
            definition.name,
        )


def _check_pattern(definition: ModuleInputDefinition, value: str) -> None:
    pattern = definition.constraints.pattern
    if pattern and not re.fullmatch(pattern, value):
        raise ParameterDecodeError(
            f"Input {definition.name!r} does not match the pattern its contract "
            f"declares.",
            ModuleErrorCode.MODULE_INPUT_TYPE_INVALID,
            definition.name,
        )


def _check_enum(definition: ModuleInputDefinition, value: str) -> None:
    allowed = definition.constraints.allowed_values
    if value not in allowed:
        raise ParameterDecodeError(
            f"Input {definition.name!r} value {value!r} is outside the contract's "
            f"closed allowlist; accepted: {', '.join(sorted(allowed))}.",
            ModuleErrorCode.MODULE_INPUT_OUT_OF_ALLOWLIST,
            definition.name,
        )


def _reject_injection(definition: ModuleInputDefinition, value: str) -> None:
    """Belt-and-braces structural check.

    Patterns already forbid these characters, so this should be
    unreachable; it exists so that a future loosening of a pattern cannot
    silently open an injection path.
    """
    for character in ('"', "$", "{", "}", "<", ">", "\\", "\n", "\r", "`"):
        if character in value:
            raise ParameterDecodeError(
                f"Input {definition.name!r} contains a character that could carry "
                f"HCL structure or interpolation.",
                ModuleErrorCode.MODULE_PARAMETER_UNREPRESENTABLE,
                definition.name,
            )


def _decode_scalar(definition: ModuleInputDefinition, raw: str) -> TypedValue:
    kind = definition.type
    value = (raw or "").strip()

    if kind is ParameterType.STRING:
        _reject_injection(definition, value)
        _check_length(definition, value)
        _check_pattern(definition, value)
        return TypedString(value=value)

    if kind is ParameterType.ARTIFACT_REF:
        _reject_injection(definition, value)
        _check_pattern(definition, value)
        return TypedArtifactRef(value=value)

    if kind is ParameterType.SECRET_REF:
        _reject_injection(definition, value)
        _check_pattern(definition, value)
        return TypedSecretRef(value=value)

    if kind is ParameterType.ENUM:
        _check_enum(definition, value)
        return TypedEnum(value=value)

    if kind is ParameterType.BOOL:
        lowered = value.lower()
        if lowered in _TRUE_LITERALS:
            return TypedBool(value=True)
        if lowered in _FALSE_LITERALS:
            return TypedBool(value=False)
        raise ParameterDecodeError(
            f"Input {definition.name!r} value {raw!r} is not a recognized boolean; "
            f"accepted: {', '.join(sorted(_TRUE_LITERALS | _FALSE_LITERALS))}.",
            ModuleErrorCode.MODULE_INPUT_OUT_OF_ALLOWLIST,
            definition.name,
        )

    if kind is ParameterType.INT:
        if not _INT_TOKEN.match(value):
            raise ParameterDecodeError(
                f"Input {definition.name!r} value {raw!r} is not a plain integer.",
                ModuleErrorCode.MODULE_INPUT_TYPE_INVALID,
                definition.name,
            )
        number = int(value)
        minimum = definition.constraints.minimum
        maximum = definition.constraints.maximum
        if minimum is not None and number < minimum:
            raise ParameterDecodeError(
                f"Input {definition.name!r} value {number} is below the contract "
                f"minimum of {minimum}.",
                ModuleErrorCode.MODULE_INPUT_OUT_OF_ALLOWLIST,
                definition.name,
            )
        if maximum is not None and number > maximum:
            raise ParameterDecodeError(
                f"Input {definition.name!r} value {number} is above the contract "
                f"maximum of {maximum}.",
                ModuleErrorCode.MODULE_INPUT_OUT_OF_ALLOWLIST,
                definition.name,
            )
        return TypedInt(value=number)

    if kind is ParameterType.STRING_LIST:
        separator = definition.constraints.separator
        items = tuple(
            item.strip() for item in value.split(separator) if item.strip()
        )
        if not items:
            raise ParameterDecodeError(
                f"Input {definition.name!r} produced an empty list.",
                ModuleErrorCode.MODULE_INPUT_TYPE_INVALID,
                definition.name,
            )
        for item in items:
            _reject_injection(definition, item)
            _check_pattern(definition, item)
        return TypedStringList(value=items)

    if kind is ParameterType.ATTRIBUTE_LIST:
        return TypedAttributeList(value=_decode_attribute_list(definition, value))

    if kind is ParameterType.KEY_SCHEMA:
        return TypedKeySchema(value=_decode_key_schema(definition, value))

    raise ParameterDecodeError(
        f"Input {definition.name!r} declares unsupported type {kind.value!r}.",
        ModuleErrorCode.MODULE_PARAMETER_UNREPRESENTABLE,
        definition.name,
    )


def _tokens(definition: ModuleInputDefinition, value: str) -> list:
    separator = definition.constraints.separator
    tokens = [token.strip() for token in value.split(separator)]
    if not tokens or any(not token for token in tokens):
        raise ParameterDecodeError(
            f"Input {definition.name!r} must be a separator-delimited list of "
            f"tokens.",
            ModuleErrorCode.MODULE_INPUT_TYPE_INVALID,
            definition.name,
        )
    return tokens


def _decode_attribute_list(
    definition: ModuleInputDefinition, value: str
) -> tuple:
    parsed = []
    seen = set()
    for token in _tokens(definition, value):
        match = _ATTRIBUTE_TOKEN.match(token)
        if not match:
            raise ParameterDecodeError(
                f"Input {definition.name!r} token {token!r} is malformed; expected "
                f"an attribute name and one of the types S, N, B.",
                ModuleErrorCode.MODULE_INPUT_TYPE_INVALID,
                definition.name,
            )
        name = match.group(1)
        if name in seen:
            raise ParameterDecodeError(
                f"Input {definition.name!r} declares attribute {name!r} more than "
                f"once.",
                ModuleErrorCode.MODULE_INPUT_INCONSISTENT,
                definition.name,
            )
        seen.add(name)
        parsed.append(AttributeSpec(name=name, type=match.group(2)))
    # Deterministic ordering regardless of input order.
    return tuple(sorted(parsed, key=lambda item: item.name))


def _decode_key_schema(
    definition: ModuleInputDefinition, value: str
) -> KeySchemaSpec:
    parsed = {}
    for token in _tokens(definition, value):
        match = _KEY_TOKEN.match(token)
        if not match:
            raise ParameterDecodeError(
                f"Input {definition.name!r} token {token!r} is malformed; expected "
                f"HASH:<attribute> and optionally RANGE:<attribute>.",
                ModuleErrorCode.MODULE_INPUT_TYPE_INVALID,
                definition.name,
            )
        if match.group(1) in parsed:
            raise ParameterDecodeError(
                f"Input {definition.name!r} declares {match.group(1)} more than once.",
                ModuleErrorCode.MODULE_INPUT_INCONSISTENT,
                definition.name,
            )
        parsed[match.group(1)] = match.group(2)
    if "HASH" not in parsed:
        raise ParameterDecodeError(
            f"Input {definition.name!r} must declare a HASH key.",
            ModuleErrorCode.MODULE_INPUT_TYPE_INVALID,
            definition.name,
        )
    return KeySchemaSpec(
        hash_key=parsed["HASH"], range_key=parsed.get("RANGE")
    )


# ----------------------------------------------------------- derivations

#: Platform naming policy for a managed log group, anchored on the stable
#: resource identity plus the optional deployment namespace. With an empty
#: namespace this mirrors the raw renderer exactly; the parity tests assert
#: that legacy form stays in lockstep.
_LOG_GROUP_NAME_TEMPLATE = "/aws/ai-architect/{resource_id}"


def _validated_string(
    definition: ModuleInputDefinition, value: str
) -> TypedString:
    _reject_injection(definition, value)
    _check_length(definition, value)
    _check_pattern(definition, value)
    return TypedString(value=value)


def _sibling_ids(siblings, kind: ResourceKind) -> list:
    """Resource identities of the sibling resources of one kind.

    ``siblings`` preserves InfraSpec order, which is exactly the order the
    raw renderer's context wiring uses, so selection and rendering cannot
    disagree about which sibling is which.
    """
    return [s.resource_id for s in siblings if s.kind is kind]


def _derived_single_sibling(
    definition: ModuleInputDefinition, siblings, kind: ResourceKind
) -> TypedString:
    """Exactly-one sibling wiring; zero or many is a diagnostic."""
    matches = _sibling_ids(siblings, kind)
    if len(matches) != 1:
        raise ParameterDecodeError(
            f"Input {definition.name!r} requires exactly one "
            f"{kind.value} resource in the same spec; found "
            f"{len(matches)}. Ambiguous topology is refused, never guessed.",
            ModuleErrorCode.MODULE_INPUT_INCONSISTENT,
            definition.name,
        )
    return _validated_string(definition, matches[0])


def _derived_table_arns(definition: ModuleInputDefinition, siblings) -> TypedValue:
    matches = _sibling_ids(siblings, ResourceKind.DYNAMODB)
    if not matches:
        raise _absent(definition)
    for item in matches:
        _reject_injection(definition, item)
        _check_pattern(definition, item)
    return TypedStringList(value=tuple(matches))


def _provider_name(
    definition: ModuleInputDefinition, resource_id: str, namespace: str
) -> str:
    try:
        return namespaced_name(resource_id, namespace)
    except ValueError as error:
        raise ParameterDecodeError(
            str(error),
            ModuleErrorCode.MODULE_PARAMETER_UNREPRESENTABLE,
            definition.name,
        ) from error


def _derived_table_name(
    definition: ModuleInputDefinition, siblings, namespace: str
) -> TypedValue:
    matches = _sibling_ids(siblings, ResourceKind.DYNAMODB)
    if not matches:
        raise _absent(definition)
    return _validated_string(
        definition, _provider_name(definition, matches[0], namespace)
    )


def _derived_log_group_name(
    resource: ResourceInstance,
    definition: ModuleInputDefinition,
    namespace: str,
) -> TypedString:
    resource_name = _provider_name(
        definition, resource.resource_id, namespace
    )
    value = _LOG_GROUP_NAME_TEMPLATE.format(resource_id=resource_name)
    return _validated_string(definition, value)


def _derived_access_log_format(definition: ModuleInputDefinition) -> TypedEnum:
    """Bind the single named access-log format policy the contract declares.

    The policy name is data, never free-form format text: the invocation
    renderer resolves it to the exact static format string. With more than
    one declared policy the derivation cannot choose and fails closed.
    """
    allowed = sorted(definition.constraints.allowed_values)
    if len(allowed) != 1:
        raise ParameterDecodeError(
            f"Input {definition.name!r} declares {len(allowed)} access-log "
            "format policies; the derivation binds exactly one named "
            "policy and cannot choose.",
            ModuleErrorCode.MODULE_PARAMETER_UNREPRESENTABLE,
            definition.name,
        )
    return TypedEnum(value=allowed[0])


def _synthetic(name: str, kind: ParameterType) -> ModuleInputDefinition:
    """A decoder shim for the two structured 4A parameters.

    They are decoded once per resource and then projected into scalar module
    inputs, so they never need a real catalog entry.
    """
    return ModuleInputDefinition(
        name=name,
        type=kind,
        required=True,
        binding=ParameterBindingSpec(parameter_name=name),
        constraints=ParameterConstraints(),
        description="structural projection input",
    )


#: The two frozen 4A parameters that encode structure in strings. They have
#: no catalog entry of their own; the contract projects them into scalar
#: module inputs instead, and this table is how a caller finds the decoder
#: that validates them.
STRUCTURAL_PARAMETER_TYPES: dict = {
    "attribute_definitions": ParameterType.ATTRIBUTE_LIST,
    "key_schema": ParameterType.KEY_SCHEMA,
}


def definition_for_parameter(
    contract: ModuleContract, parameter_name: str
) -> Optional[ModuleInputDefinition]:
    """The module input a human-supplied parameter binds to.

    Resolves the contract input whose binding names this provisioning
    parameter. The two structural parameters (attribute definitions and
    key schema) have no catalog entry of their own, so they resolve to
    the declared structural decoder. Returns ``None`` when the contract
    does not accept the parameter at all, which means the value can
    never be validated under this contract.
    """
    for definition in contract.inputs:
        if definition.binding.parameter_name == parameter_name:
            return definition
    kind = STRUCTURAL_PARAMETER_TYPES.get(parameter_name)
    if kind is not None:
        return _synthetic(parameter_name, kind)
    return None


def decode_parameter_value(
    definition: ModuleInputDefinition, raw: str
) -> TypedValue:
    """Decode one human-supplied parameter value under a contract input.

    The same decoder the plan path uses, exposed so a proposed value is
    validated by exactly the code that will later accept or refuse it.
    Raises ``ParameterDecodeError`` when the value is not representable
    under the declared type and constraints.
    """
    return _decode_scalar(definition, raw)


def _structural(
    resource: ResourceInstance, definition: ModuleInputDefinition
) -> dict:
    supplied = _supplied(resource)
    for name in ("attribute_definitions", "key_schema"):
        if name not in supplied:
            raise ParameterDecodeError(
                f"Resource {resource.resource_id!r} does not supply {name!r}, "
                f"which the contract derives module inputs from.",
                ModuleErrorCode.MODULE_INPUT_REQUIRED,
                definition.name,
            )
    attributes = _decode_attribute_list(
        _synthetic("attribute_definitions", ParameterType.ATTRIBUTE_LIST),
        supplied["attribute_definitions"],
    )
    schema = _decode_key_schema(
        _synthetic("key_schema", ParameterType.KEY_SCHEMA),
        supplied["key_schema"],
    )
    return {"attributes": {item.name: item.type for item in attributes},
            "schema": schema}


def derive(
    resource: ResourceInstance,
    definition: ModuleInputDefinition,
    siblings: tuple = (),
    namespace: str = "",
) -> TypedValue:
    """Produce one module input from a declared, deterministic projection.

    ``siblings`` carries the other resources of the same InfraSpec for the
    cross-resource wiring derivations and is ignored by the per-resource
    projections. Sibling values are resource identities, never HCL: the
    invocation renderer resolves them to Terraform references.

    ``namespace`` affects provider-visible names only. Sibling identity
    bindings stay on the stable resource id and continue to resolve to
    module outputs at render time.
    """
    if definition.binding.resource_id:
        value = _provider_name(
            definition, resource.resource_id, namespace
        )
        _reject_injection(definition, value)
        _check_length(definition, value)
        _check_pattern(definition, value)
        return TypedString(value=value)

    kind = definition.binding.derivation
    if kind is None:
        raise ParameterDecodeError(
            f"Input {definition.name!r} declares no binding source.",
            ModuleErrorCode.MODULE_PARAMETER_UNREPRESENTABLE,
            definition.name,
        )

    if kind is DerivationKind.LOG_GROUP_NAME:
        return _derived_log_group_name(resource, definition, namespace)
    if kind is DerivationKind.API_GATEWAY_API_ID:
        return _derived_single_sibling(
            definition, siblings, ResourceKind.APIGATEWAY_HTTP
        )
    if kind is DerivationKind.LAMBDA_FUNCTION_NAME:
        return _derived_single_sibling(
            definition, siblings, ResourceKind.LAMBDA
        )
    if kind is DerivationKind.LOG_GROUP_ARN:
        return _derived_single_sibling(
            definition, siblings, ResourceKind.CLOUDWATCH_LOG_GROUP
        )
    if kind is DerivationKind.LAMBDA_ARN:
        return _derived_single_sibling(
            definition, siblings, ResourceKind.LAMBDA
        )
    if kind is DerivationKind.LAMBDA_INVOKE_ARN:
        return _derived_single_sibling(
            definition, siblings, ResourceKind.LAMBDA
        )
    if kind is DerivationKind.S3_BUCKET_ID:
        return _derived_single_sibling(
            definition, siblings, ResourceKind.S3
        )
    if kind is DerivationKind.S3_BUCKET_ARN:
        return _derived_single_sibling(
            definition, siblings, ResourceKind.S3
        )
    if kind is DerivationKind.S3_BUCKET_DOMAIN_NAME:
        return _derived_single_sibling(
            definition, siblings, ResourceKind.S3
        )
    if kind is DerivationKind.SQS_QUEUE_ARN:
        return _derived_single_sibling(
            definition, siblings, ResourceKind.SQS_QUEUE
        )
    if kind is DerivationKind.SQS_QUEUE_URL:
        return _derived_single_sibling(
            definition, siblings, ResourceKind.SQS_QUEUE
        )
    if kind is DerivationKind.TABLE_ARNS:
        return _derived_table_arns(definition, siblings)
    if kind is DerivationKind.TABLE_NAME:
        return _derived_table_name(definition, siblings, namespace)
    if kind is DerivationKind.ACCESS_LOG_FORMAT:
        return _derived_access_log_format(definition)

    context = _structural(resource, definition)
    schema = context["schema"]
    attributes = context["attributes"]

    if kind is DerivationKind.HASH_KEY_NAME:
        return TypedString(value=schema.hash_key)
    if kind is DerivationKind.RANGE_KEY_NAME:
        if schema.range_key is None:
            raise _absent(definition)
        return TypedString(value=schema.range_key)
    if kind is DerivationKind.HASH_KEY_TYPE:
        return TypedEnum(value=_attribute_type(definition, schema.hash_key, attributes))
    if kind is DerivationKind.RANGE_KEY_TYPE:
        if schema.range_key is None:
            raise _absent(definition)
        return TypedEnum(
            value=_attribute_type(definition, schema.range_key, attributes)
        )

    raise ParameterDecodeError(
        f"Input {definition.name!r} declares unknown derivation {kind!r}.",
        ModuleErrorCode.MODULE_PARAMETER_UNREPRESENTABLE,
        definition.name,
    )


def _absent(definition: ModuleInputDefinition) -> ParameterDecodeError:
    return ParameterDecodeError(
        f"Optional input {definition.name!r} has no value for this resource.",
        ModuleErrorCode.MODULE_INPUT_REQUIRED,
        definition.name,
        optional_absent=True,
    )


def _attribute_type(
    definition: ModuleInputDefinition, name: str, attributes: dict
) -> str:
    if name not in attributes:
        raise ParameterDecodeError(
            f"Input {definition.name!r}: key attribute {name!r} is not declared in "
            f"attribute_definitions.",
            ModuleErrorCode.MODULE_INPUT_INCONSISTENT,
            definition.name,
        )
    return attributes[name]


# ------------------------------------------------------------- projection

def bind_resource_inputs(
    resource: ResourceInstance,
    contract: ModuleContract,
    siblings: tuple = (),
    namespace: str = "",
) -> tuple:
    """Bind every declared module input for one InfraSpec resource.

    Returns ``(bindings, diagnostics)``. An absent optional input is simply
    not bound; it is never defaulted by 4C, because a default is a property
    of the module and is applied at invocation time. ``namespace`` applies
    only to provider-visible names.
    """
    bindings = []
    diagnostics = []
    supplied = _supplied(resource)

    for definition in sorted(contract.inputs, key=lambda item: item.name):
        try:
            if definition.binding.parameter_name:
                name = definition.binding.parameter_name
                if name not in supplied:
                    if definition.required:
                        raise ParameterDecodeError(
                            f"Input {definition.name!r} requires provisioning "
                            f"parameter {name!r}, which is unresolved for resource "
                            f"{resource.resource_id!r}.",
                            ModuleErrorCode.MODULE_INPUT_REQUIRED,
                            definition.name,
                        )
                    continue
                value = _decode_scalar(definition, supplied[name])
                origin = BindingOrigin.REQUIRED_INPUT
                source_parameter = name
            else:
                value = derive(
                    resource, definition, siblings, namespace=namespace
                )
                origin = BindingOrigin.DERIVED
                source_parameter = None
        except ParameterDecodeError as error:
            if error.optional_absent:
                # An optional input with no value is left unbound, not an
                # error and not a default. Defaults belong to the module.
                continue
            category = (
                ModuleDiagnosticCategory.UNMAPPED
                if error.code is ModuleErrorCode.MODULE_PARAMETER_UNMAPPED
                else ModuleDiagnosticCategory.INVALID_PARAMETER
            )
            diagnostics.append(
                ModuleDiagnostic(
                    category=category,
                    code=error.code,
                    message=error.message,
                    resource_id=resource.resource_id,
                    input_name=error.input_name or definition.name,
                )
            )
            continue

        bindings.append(
            ModuleInputBinding(
                input_name=definition.name,
                value=value,
                origin=origin,
                from_parameter=source_parameter,
            )
        )

    return tuple(bindings), tuple(diagnostics)


def unmapped_parameters(
    resource: ResourceInstance, contract: ModuleContract
) -> tuple:
    """4A parameters the contract does not consume.

    Reported explicitly. A silently dropped parameter is a design error
    nobody would notice.
    """
    consumed = set(contract.bound_parameter_names)
    # Structural parameters are consumed indirectly, through projections.
    consumed.update({"attribute_definitions", "key_schema"})
    return tuple(
        sorted(name for name in _supplied(resource) if name not in consumed)
    )