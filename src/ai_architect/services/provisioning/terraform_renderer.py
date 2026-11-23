"""Deterministic Terraform renderer for Slice 4A InfraSpec.

    InfraSpec -> deterministic renderer -> guard -> Terraform source tree

Properties enforced here:

- **Deterministic.** Block order, attribute order and file order are
  produced from explicit tuples and sorts, never from dict insertion
  order. No timestamps, UUIDs, machine paths, environment reads or LLM
  calls. ``render(spec) == render(spec)`` byte for byte.
- **Closed world.** The renderable Terraform vocabulary is keyed by the
  Slice 4A :class:`ResourceKind`. There is no path from a user or LLM
  string to a Terraform type; the registry is reused, never duplicated.
- **No invention.** Only parameters explicitly present in the spec are
  rendered. Slice 4A parameters are provider-agnostic *strings*, so each
  one is decoded through a narrow closed codec: a fixed enum allowlist, a
  fixed boolean representation, a strict pattern, or a narrow token
  grammar. Arbitrary HCL, JSON or YAML is never interpreted, and any
  value without exactly one safe interpretation fails closed.
- **Honest limits.** Lambda cannot be rendered in this slice: Slice 4A
  carries no code-artifact location, so ``aws_lambda_function`` would
  require inventing a filename, S3 URI or image URI. It fails closed with
  ``missing_code_artifact`` and is *not* removed from the registry.

A second, verified-first path exists alongside the raw renderer:
``render_module_composition`` consumes the Slice 4C ``ModuleSelection``
from the verified catalog and emits one pinned ``module`` block per
selected resource, copying each module's pinned ``.tf`` files into the
tree so it is self-contained wherever it is materialized. Sibling
resource identities resolve to module output references there and only
there; the guard enforces the pinned-module policy on both paths.
"""

import hashlib
import json
import re
from typing import Callable, Optional

from ai_architect.domain.provisioning.models import (
    InfraSpec,
    ResourceInstance,
    ResourceKind,
)
from ai_architect.domain.provisioning.module import (
    DerivationKind,
    ModuleErrorCode,
    ModuleSelection,
    TypedArtifactRef,
    TypedBool,
    TypedEnum,
    TypedInt,
    TypedSecretRef,
    TypedString,
    TypedStringList,
)
from ai_architect.domain.provisioning.render import (
    RenderDiagnostic,
    RenderDiagnosticCategory,
    RenderErrorCode,
    RenderedFile,
    RenderResult,
    RenderStatus,
)
from ai_architect.services.provisioning.module_registry import (
    CatalogError,
    VerifiedModuleRegistry,
    local_source_tree_path,
)
from ai_architect.services.provisioning.module_selection import (
    validate_module_selection,
)
from ai_architect.services.provisioning.registry import RESOURCE_CONTRACTS
from ai_architect.services.provisioning.terraform_guard import (
    guard_terraform_source,
)

# Terraform resource types this slice may emit, keyed by the Slice 4A
# provisioning kind they represent. This is the render vocabulary, not a
# second registry: kinds and required inputs come from RESOURCE_CONTRACTS.
REGISTERED_TF_RESOURCE_TYPES: dict = {
    ResourceKind.S3: frozenset(
        {
            "aws_s3_bucket",
            "aws_s3_bucket_versioning",
            "aws_s3_bucket_server_side_encryption_configuration",
            "aws_s3_bucket_public_access_block",
        }
    ),
    ResourceKind.DYNAMODB: frozenset({"aws_dynamodb_table"}),
    ResourceKind.LAMBDA: frozenset(
        {
            "aws_iam_role",
            "aws_iam_role_policy",
            "aws_lambda_function",
            "aws_lambda_permission",
        }
    ),
    ResourceKind.APIGATEWAY_HTTP: frozenset(
        {
            "aws_apigatewayv2_api",
            "aws_apigatewayv2_stage",
            "aws_apigatewayv2_integration",
            "aws_apigatewayv2_route",
        }
    ),
    ResourceKind.CLOUDWATCH_LOG_GROUP: frozenset(
        {"aws_cloudwatch_log_group"}
    ),
}

ALLOWED_TF_RESOURCE_TYPES: frozenset = frozenset(
    tf_type
    for types in REGISTERED_TF_RESOURCE_TYPES.values()
    for tf_type in types
)

# Kinds that cannot be rendered until a code artifact is representable.
# Lambda stays in this set: a function WITHOUT human-supplied code
# artifact inputs still fails closed below. A function WITH a valid
# code_artifact_path + code_artifact_sha256 renders via _render_lambda.
UNRENDERABLE_KINDS: frozenset = frozenset({ResourceKind.LAMBDA})

_NAME = r"[A-Za-z][A-Za-z0-9_]{0,63}"
_BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_DDB_ATTRIBUTE = re.compile(rf"^({_NAME}):([SNB])$")
_DDB_KEY = re.compile(rf"^HASH:({_NAME})$|^RANGE:({_NAME})$")
_HANDLER = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_ARTIFACT_PATH = re.compile(r"^[A-Za-z0-9._-]{1,64}\.zip$")
_ARTIFACT_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TF_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,200}$")


class ParameterError(Exception):
    """A parameter value cannot be represented safely."""

    def __init__(self, message, code=RenderErrorCode.UNREPRESENTABLE_PARAMETER):
        super().__init__(message)
        self.message = message
        self.code = code


def _enum(mapping: dict) -> Callable:
    """Closed enum codec: exact key, case/space insensitive, one meaning."""

    def decode(name, value, resource_id):
        key = (value or "").strip().lower()
        if key not in mapping:
            raise ParameterError(
                f"Parameter {name!r} value {value!r} is outside the closed "
                f"allowlist; accepted: {', '.join(sorted(mapping))}.",
                code=RenderErrorCode.PARAMETER_OUT_OF_ALLOWLIST,
            )
        return mapping[key]

    return decode


def _boolean(name, value, resource_id):
    """Fixed accepted representations, normalized to true/false."""
    key = (value or "").strip().lower()
    if key in ("true", "enabled"):
        return "true"
    if key in ("false", "disabled"):
        return "false"
    raise ParameterError(
        f"Parameter {name!r} value {value!r} is not a recognized boolean; "
        f"accepted: true, false, enabled, disabled.",
        code=RenderErrorCode.PARAMETER_OUT_OF_ALLOWLIST,
    )


def _bucket_name(name, value, resource_id):
    text = (value or "").strip()
    if not _BUCKET_NAME.match(text):
        raise ParameterError(
            f"Parameter {name!r} value {value!r} is not a valid S3 bucket "
            f"name; refusing to render it.",
            code=RenderErrorCode.MALFORMED_PARAMETER,
        )
    return text


def _ddb_attributes(name, value, resource_id):
    """Narrow token grammar: name:type pairs, comma separated.

    Deliberately *not* an HCL/JSON/YAML encoding. Each token is matched
    against a closed pattern; anything else fails closed, so no arbitrary
    text can reach the generated configuration.
    """
    tokens = [token.strip() for token in (value or "").split(",")]
    if not tokens or any(not token for token in tokens):
        raise ParameterError(
            f"Parameter {name!r} must be a comma-separated list of "
            f"name:type pairs.",
            code=RenderErrorCode.MALFORMED_PARAMETER,
        )
    parsed = []
    for token in tokens:
        match = _DDB_ATTRIBUTE.match(token)
        if not match:
            raise ParameterError(
                f"Parameter {name!r} token {token!r} is malformed; expected "
                f"an attribute name and one of the types S, N, B.",
                code=RenderErrorCode.MALFORMED_PARAMETER,
            )
        parsed.append((match.group(1), match.group(2)))
    seen = set()
    for attribute_name, _ in parsed:
        if attribute_name in seen:
            raise ParameterError(
                f"Parameter {name!r} declares attribute {attribute_name!r} "
                f"more than once.",
                code=RenderErrorCode.MALFORMED_PARAMETER,
            )
        seen.add(attribute_name)
    return tuple(sorted(parsed))


def _ddb_key_schema(name, value, resource_id):
    """Narrow token grammar: HASH:<attr> and optional RANGE:<attr>."""
    tokens = [token.strip() for token in (value or "").split(",")]
    parsed = {}
    for token in tokens:
        match = _DDB_KEY.match(token)
        if not match:
            raise ParameterError(
                f"Parameter {name!r} token {token!r} is malformed; expected "
                f"HASH:<attribute> and optionally RANGE:<attribute>.",
                code=RenderErrorCode.MALFORMED_PARAMETER,
            )
        key = "HASH" if match.group(1) else "RANGE"
        parsed[key] = match.group(1) or match.group(2)
    if "HASH" not in parsed:
        raise ParameterError(
            f"Parameter {name!r} must declare a HASH key.",
            code=RenderErrorCode.MISSING_REQUIRED_INPUT,
        )
    return parsed["HASH"], parsed.get("RANGE")


def _handler_entrypoint(name, value, resource_id):
    """Entry point within the artifact: safe chars, bounded length."""
    text = (value or "").strip()
    if not _HANDLER.match(text):
        raise ParameterError(
            f"Parameter {name!r} value {value!r} is not a safe handler "
            f"entry point; refusing to render it.",
            code=RenderErrorCode.MALFORMED_PARAMETER,
        )
    return text


def _int_range(low: int, high: int) -> Callable:
    """Closed integer range: digits only, bounded, no units smuggled."""

    def decode(name, value, resource_id):
        text = (value or "").strip()
        if not re.fullmatch(r"[0-9]+", text):
            raise ParameterError(
                f"Parameter {name!r} value {value!r} is not a plain "
                f"integer; refusing to render it.",
                code=RenderErrorCode.MALFORMED_PARAMETER,
            )
        number = int(text)
        if not low <= number <= high:
            raise ParameterError(
                f"Parameter {name!r} value {value!r} is outside "
                f"{low}..{high}; refusing to render it.",
                code=RenderErrorCode.MALFORMED_PARAMETER,
            )
        return str(number)

    return decode


def _artifact_path(name, value, resource_id):
    """Deployment zip basename: no directories, no traversal, zip only."""
    text = (value or "").strip()
    if not _ARTIFACT_PATH.match(text):
        raise ParameterError(
            f"Parameter {name!r} value {value!r} is not a zip basename; "
            f"directories and non-zip files are refused.",
            code=RenderErrorCode.MALFORMED_PARAMETER,
        )
    return text


def _artifact_sha256(name, value, resource_id):
    """Lowercase hex sha256: exactly 64 hex chars, nothing else."""
    text = (value or "").strip()
    if not _ARTIFACT_SHA256.match(text):
        raise ParameterError(
            f"Parameter {name!r} value {value!r} is not a lowercase hex "
            f"sha256; refusing to render it.",
            code=RenderErrorCode.MALFORMED_PARAMETER,
        )
    return text


# Closed codecs, per Slice 4A parameter name.
_CODECS: dict = {
    "bucket_name": _bucket_name,
    "versioning": _enum({"enabled": "Enabled", "suspended": "Suspended"}),
    "encryption": _enum({"aes256": "AES256"}),
    "public_access_block": _enum(
        {"all_blocked": True, "none_blocked": False}
    ),
    "billing_mode": _enum({"pay_per_request": "PAY_PER_REQUEST"}),
    "attribute_definitions": _ddb_attributes,
    "key_schema": _ddb_key_schema,
    "point_in_time_recovery": _boolean,
    "runtime": _enum({"python3.12": "python3.12", "nodejs20.x": "nodejs20.x"}),
    "handler": _handler_entrypoint,
    "memory_mb": _int_range(128, 10240),
    "timeout_seconds": _int_range(1, 900),
    "code_artifact_path": _artifact_path,
    "code_artifact_sha256": _artifact_sha256,
    "retention_days": _enum(
        {"7": "7", "30": "30", "90": "90", "365": "365"}
    ),
}


def _canonical(payload) -> str:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        default=str,
    )


def tree_hash(files: list) -> str:
    """sha256 over canonical JSON of the tree, sorted by path."""
    payload = sorted(
        ({"path": entry.path, "content": entry.content} for entry in files),
        key=lambda entry: entry["path"],
    )
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _decode(resource: ResourceInstance, name: str) -> object:
    codec = _CODECS.get(name)
    if codec is None:
        raise ParameterError(
            f"Parameter {name!r} has no closed codec in this slice; "
            f"refusing to render it rather than interpreting the string.",
            code=RenderErrorCode.UNREPRESENTABLE_PARAMETER,
        )
    supplied = {p.name: p.value for p in resource.parameters}
    if name not in supplied:
        raise ParameterError(
            f"Parameter {name!r} is absent from the InfraSpec.",
            code=RenderErrorCode.MISSING_REQUIRED_INPUT,
        )
    return codec(name, supplied[name], resource.resource_id)


def _require_all(resource: ResourceInstance) -> dict:
    contract = RESOURCE_CONTRACTS[resource.kind]
    supplied = {p.name for p in resource.parameters}
    for name in contract.required_inputs:
        if name not in supplied:
            raise ParameterError(
                f"Resource {resource.resource_id!r} is missing required "
                f"provisioning input {name!r}.",
                code=RenderErrorCode.MISSING_REQUIRED_INPUT,
            )
    return {name: _decode(resource, name) for name in contract.required_inputs}


def _render_s3(resource: ResourceInstance, values: dict) -> list:
    bucket = values["bucket_name"]
    identifier = resource.resource_id
    reference = f"aws_s3_bucket.{identifier}.id"
    public = "true" if values["public_access_block"] else "false"
    return [
        (
            f'resource "aws_s3_bucket" "{identifier}"',
            [("bucket", f'"{bucket}"')],
        ),
        (
            f'resource "aws_s3_bucket_versioning" "{identifier}_versioning"',
            [
                ("bucket", reference),
                (
                    "versioning_configuration",
                    None,
                    [("status", f'"{values["versioning"]}"')],
                ),
            ],
        ),
        (
            'resource "aws_s3_bucket_server_side_encryption_configuration" '
            f'"{identifier}_encryption"',
            [
                ("bucket", reference),
                (
                    "rule",
                    None,
                    [
                        (
                            "apply_server_side_encryption_by_default",
                            None,
                            [("sse_algorithm", f'"{values["encryption"]}"')],
                        )
                    ],
                ),
            ],
        ),
        (
            f'resource "aws_s3_bucket_public_access_block" '
            f'"{identifier}_public_access_block"',
            [
                ("bucket", reference),
                ("block_public_acls", public),
                ("block_public_policy", public),
                ("ignore_public_acls", public),
                ("restrict_public_buckets", public),
            ],
        ),
    ]


def _validate_decoded(kind: ResourceKind, values: dict, resource_id: str) -> None:
    """Cross-parameter consistency, run before anything is emitted."""
    if kind is not ResourceKind.DYNAMODB:
        return
    hash_key, range_key = values["key_schema"]
    declared = {name for name, _ in values["attribute_definitions"]}
    for required in (hash_key,) + ((range_key,) if range_key else ()):
        if required not in declared:
            raise ParameterError(
                f"Resource {resource_id!r} key attribute {required!r} is "
                f"not declared in attribute_definitions.",
                code=RenderErrorCode.INCONSISTENT_REFERENCE,
            )


def _render_dynamodb(resource: ResourceInstance, values: dict) -> list:
    hash_key, range_key = values["key_schema"]
    attributes = [
        ("attribute", None, [("name", f'"{name}"'), ("type", f'"{kind}"')])
        for name, kind in values["attribute_definitions"]
    ]
    body = [
        ("billing_mode", f'"{values["billing_mode"]}"'),
        ("hash_key", f'"{hash_key}"'),
        ("name", f'"{resource.resource_id}"'),
    ]
    if range_key:
        body.append(("range_key", f'"{range_key}"'))
    body.extend(attributes)
    body.append(
        (
            "point_in_time_recovery",
            None,
            [("enabled", values["point_in_time_recovery"])],
        )
    )
    return [
        (
            f'resource "aws_dynamodb_table" "{resource.resource_id}"',
            body,
        )
    ]


def _tf_safe_name(resource_id: str, context: str) -> str:
    """Terraform block labels track resource ids verbatim (existing
    convention). Anything outside the safe set fails closed rather
    than being rewritten into a different identity."""
    text = (resource_id or "").strip()
    if not _TF_SAFE_NAME.match(text):
        raise ParameterError(
            f"{context} {resource_id!r} is not a safe Terraform "
            f"identifier; refusing to render it.",
            code=RenderErrorCode.MALFORMED_PARAMETER,
        )
    return text


def _role_name(resource_id: str) -> str:
    """Execution-role name derived from the function identity.

    Dots are legal in IAM role names, so no rewriting is needed; overlong
    identities fail closed rather than being truncated.
    """
    name = _tf_safe_name(resource_id, "Resource")
    role = f"{name}-execution-role"
    if len(role) > 64:
        raise ParameterError(
            f"Resource {resource_id!r} yields an IAM role name over 64 "
            f"characters; refusing to truncate an identity.",
            code=RenderErrorCode.MALFORMED_PARAMETER,
        )
    return role


def _has_code_artifact(resource: ResourceInstance) -> bool:
    """A Lambda renders only with human-supplied artifact inputs present."""
    supplied = {param.name for param in resource.parameters}
    return (
        "code_artifact_path" in supplied
        and "code_artifact_sha256" in supplied
    )


def _check_topology(resources: list) -> None:
    """Deterministic cross-resource rules for the demo stack.

    An HTTP API integrates exactly one Lambda and logs to exactly one
    log group; a Lambda needs exactly one log group and at least one
    table for its TABLE_NAME wiring. Anything else is an explicit
    routing/observability decision the spec does not carry, so it
    fails closed instead of being guessed.
    """
    by_kind: dict = {}
    for resource in resources:
        by_kind.setdefault(resource.kind, []).append(resource.resource_id)
    apis = by_kind.get(ResourceKind.APIGATEWAY_HTTP, [])
    lambdas = by_kind.get(ResourceKind.LAMBDA, [])
    groups = by_kind.get(ResourceKind.CLOUDWATCH_LOG_GROUP, [])
    tables = by_kind.get(ResourceKind.DYNAMODB, [])
    if apis:
        if len(lambdas) != 1:
            raise ParameterError(
                "HTTP API integration is ambiguous: the spec carries "
                f"{len(lambdas)} Lambda resources, exactly one is "
                "required; refusing to choose.",
                code=RenderErrorCode.INCONSISTENT_REFERENCE,
            )
        if len(groups) != 1:
            raise ParameterError(
                "HTTP API access logging is ambiguous: the spec carries "
                f"{len(groups)} log groups, exactly one is required; "
                "refusing to choose.",
                code=RenderErrorCode.INCONSISTENT_REFERENCE,
            )
    if lambdas and len(groups) != 1:
        raise ParameterError(
            "Lambda log permissions are ambiguous: the spec carries "
            f"{len(groups)} log groups, exactly one is required; "
            "refusing to choose.",
            code=RenderErrorCode.INCONSISTENT_REFERENCE,
        )
    if lambdas and not tables:
        raise ParameterError(
            "Lambda table wiring is ambiguous: the spec carries no "
            "DynamoDB table for TABLE_NAME, and inventing one is "
            "forbidden; refusing to render.",
            code=RenderErrorCode.INCONSISTENT_REFERENCE,
        )


def _canonical_json(payload: object) -> str:
    """Deterministic JSON for hashes and diagnostics, never for HCL."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def _hcl_value(value: object) -> str:
    """Render pure data as single-line HCL inside jsonencode(...).

    Dicts sort keys and use ``=`` (HCL object syntax, never JSON
    colons); strings escape backslash and quote; only plain JSON-like
    data is accepted, so no traversal, interpolation, or heredoc can
    enter a policy document.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_hcl_value(item) for item in value) + "]"
    if isinstance(value, dict):
        parts = [
            f"{key} = {_hcl_value(value[key])}"
            for key in sorted(value, key=str)
        ]
        if not parts:
            return "{}"
        # Spaces inside the braces: this is the single-line object form
        # `terraform fmt` canonicalizes to.
        return "{ " + ", ".join(parts) + " }"
    raise ParameterError(
        f"Policy document value {value!r} has an unsupported type.",
        code=RenderErrorCode.MALFORMED_PARAMETER,
    )


def _trust_document() -> str:
    """Static Lambda trust policy. No inputs, no variables, no ARNs."""
    return "jsonencode(" + _hcl_value(
        {
            "Statement": [
                {
                    "Action": "sts:AssumeRole",
                    "Effect": "Allow",
                    "Principal": {"Service": "lambda.amazonaws.com"},
                }
            ],
            "Version": "2012-10-17",
        }
    ) + ")"


def _permissions_document(log_group_arn: str, table_arns: list) -> str:
    """Least-privilege policy: logs on one group, reads/writes on the
    spec's tables only. No wildcards, no other actions, no ARNs beyond
    resources declared in the same tree."""
    statements = [
        {
            "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
            "Effect": "Allow",
            "Resource": [log_group_arn],
        }
    ]
    if table_arns:
        statements.append(
            {
                "Action": [
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                ],
                "Effect": "Allow",
                "Resource": sorted(table_arns),
            }
        )
    return "jsonencode(" + _hcl_value(
        {"Statement": statements, "Version": "2012-10-17"}
    ) + ")"


def _source_hash_hex_to_b64(value: str) -> str:
    """Render-time encoding of the human-supplied artifact hash.

    Pure base64 of the validated hex digest: no filesystem reads, so
    rendering stays deterministic across machines.
    """
    import base64
    import binascii

    try:
        raw = binascii.unhexlify(value)
    except (binascii.Error, ValueError) as exc:
        raise ParameterError(
            f"Code artifact sha256 {value!r} is not valid hex.",
            code=RenderErrorCode.MALFORMED_PARAMETER,
        ) from exc
    return base64.b64encode(raw).decode("ascii")


def _render_lambda(resource: ResourceInstance, values: dict, ctx: dict) -> list:
    identifier = _tf_safe_name(resource.resource_id, "Resource")
    role = _role_name(resource.resource_id)
    log_group_arn = ctx["log_group_arn"]
    table_arns = ctx["table_arns"]
    source_hash = _source_hash_hex_to_b64(values["code_artifact_sha256"])
    return [
        (
            f'resource "aws_iam_role" "{identifier}_execution_role"',
            [
                ("name", f'"{role}"'),
                ("assume_role_policy", _trust_document()),
            ],
        ),
        (
            f'resource "aws_iam_role_policy" "{identifier}_policy"',
            [
                ("name", f'"{role}-policy"'),
                ("role", f"aws_iam_role.{identifier}_execution_role.id"),
                (
                    "policy",
                    _permissions_document(log_group_arn, table_arns),
                ),
            ],
        ),
        (
            f'resource "aws_lambda_function" "{identifier}"',
            [
                ("function_name", f'"{identifier}"'),
                (
                    "role",
                    f"aws_iam_role.{identifier}_execution_role.arn",
                ),
                ("handler", f'"{values["handler"]}"'),
                ("runtime", f'"{values["runtime"]}"'),
                ("filename", f'"{values["code_artifact_path"]}"'),
                ("source_code_hash", f'"{source_hash}"'),
                ("memory_size", values["memory_mb"]),
                ("timeout", values["timeout_seconds"]),
                (
                    "environment",
                    None,
                    [
                        (
                            "variables",
                            "=",
                            [
                                (
                                    "TABLE_NAME",
                                    f'"{ctx["table_name"]}"',
                                )
                            ],
                        )
                    ],
                ),
                (
                    "logging_config",
                    None,
                    [
                        ("log_format", '"Text"'),
                        ("log_group", ctx["log_group_name"]),
                    ],
                ),
            ],
        ),
    ]


def _render_apigateway(resource: ResourceInstance, values: dict, ctx: dict) -> list:
    del values
    identifier = _tf_safe_name(resource.resource_id, "Resource")
    lambda_id = ctx["lambda_id"]
    log_group_arn = ctx["log_group_arn"]
    api_name = identifier
    return [
        (
            f'resource "aws_apigatewayv2_api" "{identifier}"',
            [
                ("name", f'"{api_name}"'),
                ("protocol_type", '"HTTP"'),
            ],
        ),
        (
            f'resource "aws_apigatewayv2_integration" "{identifier}_integration"',
            [
                ("api_id", f"aws_apigatewayv2_api.{identifier}.id"),
                ("integration_type", '"AWS_PROXY"'),
                (
                    "integration_uri",
                    f"aws_lambda_function.{lambda_id}.invoke_arn",
                ),
                ("integration_method", '"POST"'),
                ("payload_format_version", '"2.0"'),
            ],
        ),
        (
            f'resource "aws_apigatewayv2_route" "{identifier}_default_route"',
            [
                ("api_id", f"aws_apigatewayv2_api.{identifier}.id"),
                ("route_key", '"$default"'),
                (
                    "target",
                    "join("
                    '"/", '
                    '["integrations", '
                    f"aws_apigatewayv2_integration.{identifier}_integration.id])",
                ),
            ],
        ),
        (
            f'resource "aws_apigatewayv2_stage" "{identifier}_default_stage"',
            [
                ("api_id", f"aws_apigatewayv2_api.{identifier}.id"),
                ("name", '"$default"'),
                ("auto_deploy", "true"),
                (
                    "access_log_settings",
                    None,
                    [
                        ("destination_arn", log_group_arn),
                        (
                            "format",
                            '"{\\"requestId\\":\\"$context.requestId\\",'
                            '\\"ip\\":\\"$context.identity.sourceIp\\",'
                            '\\"routeKey\\":\\"$context.routeKey\\",'
                            '\\"status\\":\\"$context.status\\"}"',
                        ),
                    ],
                ),
            ],
        ),
        (
            f'resource "aws_lambda_permission" "{identifier}_invoke"',
            [
                ("statement_id", f'"{identifier}-invoke"'),
                ("action", '"lambda:InvokeFunction"'),
                (
                    "function_name",
                    f"aws_lambda_function.{lambda_id}.arn",
                ),
                ("principal", '"apigateway.amazonaws.com"'),
                (
                    "source_arn",
                    "join("
                    '"/", ['
                    f"aws_apigatewayv2_api.{identifier}.execution_arn, "
                    '"$default", "$default"])',
                ),
            ],
        ),
    ]


def _render_log_group(resource: ResourceInstance, values: dict, ctx: dict) -> list:
    del ctx
    identifier = _tf_safe_name(resource.resource_id, "Resource")
    return [
        (
            f'resource "aws_cloudwatch_log_group" "{identifier}"',
            [
                ("name", f'"/aws/ai-architect/{identifier}"'),
                ("retention_in_days", values["retention_days"]),
            ],
        )
    ]


_ASSIGN_LINE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*) = (.*)$")


def _align_equals(text: str) -> str:
    """Pad contiguous ``key = value`` runs the way ``terraform fmt`` does.

    Consecutive assignment lines at the same indentation form one
    alignment group; the keys are padded to the group's widest key.
    Block boundaries, blank lines, non-assignment lines and indentation
    changes break groups. Single-line groups are unchanged, so
    single-attribute blocks are unaffected. Deterministic: the input
    order fully determines the output.
    """
    lines = text.split("\n")
    aligned: list = []
    index = 0
    while index < len(lines):
        match = _ASSIGN_LINE.match(lines[index])
        if not match:
            aligned.append(lines[index])
            index += 1
            continue
        indent = match.group(1)
        end = index
        while end < len(lines):
            nxt = _ASSIGN_LINE.match(lines[end])
            if nxt is None or nxt.group(1) != indent:
                break
            end += 1
        width = max(
            len(_ASSIGN_LINE.match(lines[k]).group(2))
            for k in range(index, end)
        )
        for k in range(index, end):
            match = _ASSIGN_LINE.match(lines[k])
            aligned.append(
                f"{match.group(1)}{match.group(2).ljust(width)} "
                f"= {match.group(3)}"
            )
        index = end
    return "\n".join(aligned)


def _emit(header: str, attributes: list) -> list:
    """Emit one block. Attribute order is the tuple order, never a dict.

    A 2-tuple is ``key = value``. A 3-tuple is a nested block ``key {``,
    except when the middle element is the ``"="`` sentinel, which emits
    the object form ``key = {`` (required for e.g. a Lambda
    ``environment { variables = { ... } }`` map, where the block form
    would be invalid HCL).
    """
    lines = [f"{header} {{"]
    for attribute in attributes:
        if len(attribute) == 2:
            key, value = attribute
            lines.append(f"  {key} = {value}")
        else:
            key, sep, nested = attribute
            if sep == "=":
                lines.append(f"  {key} = {{")
            else:
                lines.append(f"  {key} {{")
            for inner in nested:
                if len(inner) == 2:
                    inner_key, inner_value = inner
                    lines.append(f"    {inner_key} = {inner_value}")
                else:
                    inner_key, inner_sep, inner_nested = inner
                    if inner_sep == "=":
                        lines.append(f"    {inner_key} = {{")
                    else:
                        lines.append(f"    {inner_key} {{")
                    for deep in inner_nested:
                        deep_key, deep_value = deep
                        lines.append(f"      {deep_key} = {deep_value}")
                    lines.append("    }")
            lines.append("  }")
    lines.append("}")
    return lines


def _render_main(decoded: list, ctx: dict) -> str:
    """decoded is a list of (ResourceInstance, decoded values) pairs.

    ``ctx`` carries deterministic cross-resource references (single
    Lambda identity, log-group/table ARNs, table name), resolved once
    by the caller so emitters never search the spec themselves.
    """
    blocks = []
    for resource, values in decoded:
        if resource.kind is ResourceKind.S3:
            blocks.extend(_render_s3(resource, values))
        elif resource.kind is ResourceKind.DYNAMODB:
            blocks.extend(_render_dynamodb(resource, values))
        elif resource.kind is ResourceKind.LAMBDA:
            blocks.extend(_render_lambda(resource, values, ctx))
        elif resource.kind is ResourceKind.APIGATEWAY_HTTP:
            blocks.extend(_render_apigateway(resource, values, ctx))
        elif resource.kind is ResourceKind.CLOUDWATCH_LOG_GROUP:
            blocks.extend(_render_log_group(resource, values, ctx))
        else:  # pragma: no cover - guarded by UNRENDERABLE_KINDS
            raise ParameterError(
                f"No renderer for kind {resource.kind!r}.",
                code=RenderErrorCode.UNREPRESENTABLE_PARAMETER,
            )
    lines = []
    for header, attributes in blocks:
        lines.extend(_emit(header, attributes))
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _render_provider(spec: InfraSpec) -> str:
    return (
        'provider "aws" {\n'
        f"  region = \"{spec.region_code}\"\n"
        "}\n"
    )


def _render_versions() -> str:
    return (
        "terraform {\n"
        '  required_version = ">= 1.5.0"\n'
        "\n"
        "  required_providers {\n"
        "    aws = {\n"
        '      source  = "hashicorp/aws"\n'
        '      version = "~> 5.0"\n'
        "    }\n"
        "  }\n"
        "}\n"
    )


def render_infra_spec(spec: InfraSpec) -> RenderResult:
    """Render a validated InfraSpec into a guarded Terraform source tree.

    Never raises for content reasons: an unrepresentable resource, an
    unsupported kind or a guard failure is reported through
    ``RenderResult.status`` and ``RenderResult.diagnostics``. The only
    exception is a caller error such as a wrong type.
    """
    diagnostics = []

    unsupported = [
        resource
        for resource in spec.resources
        if resource.kind not in RESOURCE_CONTRACTS
        or resource.kind not in REGISTERED_TF_RESOURCE_TYPES
    ]
    if unsupported:
        for resource in unsupported:
            diagnostics.append(
                RenderDiagnostic(
                    category=RenderDiagnosticCategory.STRUCTURAL,
                    code=RenderErrorCode.UNREPRESENTABLE_PARAMETER,
                    message=(
                        f"Resource {resource.resource_id!r} has no registered "
                        f"renderer; refusing to guess a Terraform type."
                    ),
                    resource_id=resource.resource_id,
                )
            )
        return RenderResult(
            status=RenderStatus.UNREPRESENTABLE,
            provider=spec.provider,
            region_code=spec.region_code,
            diagnostics=diagnostics,
        )

    blocking = [
        resource for resource in spec.resources
        if resource.kind in UNRENDERABLE_KINDS
        and not _has_code_artifact(resource)
    ]
    if blocking:
        for resource in blocking:
            diagnostics.append(
                RenderDiagnostic(
                    category=RenderDiagnosticCategory.MISSING_INPUT,
                    code=RenderErrorCode.MISSING_CODE_ARTIFACT,
                    message=(
                        f"Resource {resource.resource_id!r} "
                        f"({resource.kind.value}) cannot be rendered: the "
                        f"Slice 4A InfraSpec carries no code-artifact "
                        f"location (filename, S3 location or image URI), and "
                        f"inventing one is not permitted. Supply a code "
                        f"artifact representation before rendering."
                    ),
                    resource_id=resource.resource_id,
                )
            )
        return RenderResult(
            status=RenderStatus.MISSING_CODE_ARTIFACT,
            provider=spec.provider,
            region_code=spec.region_code,
            diagnostics=diagnostics,
        )

    # Decode once, in a stable order, before emitting anything.
    decoded = []
    for resource in sorted(spec.resources, key=lambda item: item.resource_id):
        try:
            values = _require_all(resource)
            _validate_decoded(resource.kind, values, resource.resource_id)
        except ParameterError as error:
            unrepresentable = error.code in (
                RenderErrorCode.UNREPRESENTABLE_PARAMETER,
                RenderErrorCode.PARAMETER_OUT_OF_ALLOWLIST,
                RenderErrorCode.MALFORMED_PARAMETER,
                RenderErrorCode.INCONSISTENT_REFERENCE,
            )
            diagnostics.append(
                RenderDiagnostic(
                    category=(
                        RenderDiagnosticCategory.UNREPRESENTABLE
                        if unrepresentable
                        else RenderDiagnosticCategory.MISSING_INPUT
                    ),
                    code=error.code,
                    message=f"{error.message} (resource {resource.resource_id!r})",
                    resource_id=resource.resource_id,
                )
            )
            continue
        decoded.append((resource, values))

    if diagnostics:
        status = (
            RenderStatus.MISSING_CODE_ARTIFACT
            if any(
                d.code is RenderErrorCode.MISSING_CODE_ARTIFACT
                for d in diagnostics
            )
            else RenderStatus.UNREPRESENTABLE
        )
        return RenderResult(
            status=status,
            provider=spec.provider,
            region_code=spec.region_code,
            diagnostics=diagnostics,
        )

    # Cross-resource wiring rules, checked once every resource decoded
    # cleanly. A violation fails the whole tree: a half-wired demo
    # stack (an API with no function, a function with no logs) would
    # apply cleanly and fail only at runtime.
    try:
        _check_topology(spec.resources)
    except ParameterError as error:
        return RenderResult(
            status=RenderStatus.UNREPRESENTABLE,
            provider=spec.provider,
            region_code=spec.region_code,
            diagnostics=[
                RenderDiagnostic(
                    category=RenderDiagnosticCategory.UNREPRESENTABLE,
                    code=error.code,
                    message=error.message,
                )
            ],
        )

    by_kind: dict = {}
    for resource, _values in decoded:
        by_kind.setdefault(resource.kind, []).append(resource)
    lambdas = by_kind.get(ResourceKind.LAMBDA, [])
    groups = by_kind.get(ResourceKind.CLOUDWATCH_LOG_GROUP, [])
    tables = by_kind.get(ResourceKind.DYNAMODB, [])
    ctx = {
        "lambda_id": lambdas[0].resource_id if lambdas else None,
        "log_group_arn": (
            f"aws_cloudwatch_log_group.{groups[0].resource_id}.arn"
            if groups else None
        ),
        "log_group_name": (
            f"aws_cloudwatch_log_group.{groups[0].resource_id}.name"
            if groups else None
        ),
        "table_arns": [
            f"aws_dynamodb_table.{table.resource_id}.arn"
            for table in tables
        ],
        "table_name": tables[0].resource_id if tables else None,
    }

    files = [
        RenderedFile(
            path="main.tf", content=_align_equals(_render_main(decoded, ctx))
        ),
        RenderedFile(path="provider.tf", content=_render_provider(spec)),
        RenderedFile(path="versions.tf", content=_render_versions()),
    ]
    files.sort(key=lambda entry: entry.path)

    source_tree = {entry.path: entry.content for entry in files}
    guard_diagnostics = guard_terraform_source(
        source_tree, allowed_resource_types=ALLOWED_TF_RESOURCE_TYPES
    )
    if guard_diagnostics:
        return RenderResult(
            status=RenderStatus.GUARD_FAILED,
            provider=spec.provider,
            region_code=spec.region_code,
            diagnostics=guard_diagnostics,
        )

    return RenderResult(
        status=RenderStatus.RENDERED,
        provider=spec.provider,
        region_code=spec.region_code,
        files=files,
        tree_hash=tree_hash(files),
        diagnostics=diagnostics,
    )


# ----------------------------------------------------------------------
# Slice 3 / 4C: verified module composition.
#
# One ``module`` block per selected resource, plus each selected
# module's pinned ``.tf`` files copied in-tree so the generated tree is
# self-contained: Terraform resolves relative module sources from the
# module root, so the tree must work wherever it is materialized, never
# only beside the repository. Binding values are resolved here -- and
# only here -- from sibling resource identities to Terraform module
# output references, exactly as the raw renderer resolves resource
# references from the spec.
# ----------------------------------------------------------------------

# Named access-log format policies resolved to their exact HCL string
# literal, byte for byte identical to the raw renderer's literal
# (escaped quotes, no interpolation).
_ACCESS_LOG_FORMAT_LITERALS: dict = {
    "json_request_context_v1": (
        '"{\\"requestId\\":\\"$context.requestId\\",'
        '\\"ip\\":\\"$context.identity.sourceIp\\",'
        '\\"routeKey\\":\\"$context.routeKey\\",'
        '\\"status\\":\\"$context.status\\"}"'
    ),
}

# Cross-resource derivations and the module output that resolves each
# sibling identity to a Terraform reference.
_WIRING_OUTPUTS: dict = {
    DerivationKind.LOG_GROUP_ARN: "log_group_arn",
    DerivationKind.LAMBDA_ARN: "function_arn",
    DerivationKind.LAMBDA_INVOKE_ARN: "function_invoke_arn",
    DerivationKind.TABLE_ARNS: "table_arn",
    DerivationKind.API_GATEWAY_API_ID: "api_id",
    DerivationKind.LAMBDA_FUNCTION_NAME: "function_name",
    DerivationKind.S3_BUCKET_ID: "bucket_id",
    DerivationKind.S3_BUCKET_ARN: "bucket_arn",
    DerivationKind.S3_BUCKET_DOMAIN_NAME: "regional_domain_name",
    DerivationKind.SQS_QUEUE_ARN: "queue_arn",
    DerivationKind.SQS_QUEUE_URL: "queue_url",
}

# Terraform module labels: identifiers may not contain dots (resource
# ids may), so a label outside this set fails closed rather than being
# rewritten into a different identity.
_MODULE_LABEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def _module_selection_diagnostic(diagnostic) -> RenderDiagnostic:
    """Map one selection/registry diagnostic into the render vocabulary."""
    by_code = {
        ModuleErrorCode.MODULE_INPUT_REQUIRED: (
            RenderDiagnosticCategory.MISSING_INPUT,
            RenderErrorCode.MISSING_REQUIRED_INPUT,
        ),
        ModuleErrorCode.MODULE_INPUT_INCONSISTENT: (
            RenderDiagnosticCategory.UNREPRESENTABLE,
            RenderErrorCode.INCONSISTENT_REFERENCE,
        ),
        ModuleErrorCode.MODULE_INPUT_OUT_OF_ALLOWLIST: (
            RenderDiagnosticCategory.UNREPRESENTABLE,
            RenderErrorCode.PARAMETER_OUT_OF_ALLOWLIST,
        ),
        ModuleErrorCode.MODULE_INPUT_TYPE_INVALID: (
            RenderDiagnosticCategory.UNREPRESENTABLE,
            RenderErrorCode.UNREPRESENTABLE_PARAMETER,
        ),
        ModuleErrorCode.MODULE_PARAMETER_UNREPRESENTABLE: (
            RenderDiagnosticCategory.UNREPRESENTABLE,
            RenderErrorCode.UNREPRESENTABLE_PARAMETER,
        ),
    }
    category, code = by_code.get(
        diagnostic.code,
        (
            RenderDiagnosticCategory.STRUCTURAL,
            RenderErrorCode.UNREPRESENTABLE_PARAMETER,
        ),
    )
    return RenderDiagnostic(
        category=category,
        code=code,
        message=diagnostic.message,
        resource_id=diagnostic.resource_id,
        parameter=diagnostic.input_name,
    )


def module_guard_policy(
    selection: ModuleSelection, *, registry: VerifiedModuleRegistry
) -> dict:
    """The guard's pinned-module policy for one verified selection.

    Keys are the exact rendered ``source`` strings (the catalog's
    render-relative source with the fixed prefix translated to
    ``./infra/modules/``); values carry the repository-relative tree
    directory, the pinned content digest and the declared input/output
    names. Both the renderer's own guard call and the plan-level
    merged-tree guard use exactly this policy.
    """
    policy = {}
    module_ids = sorted(
        {instance.module_id for instance in selection.instances},
        key=lambda module_id: module_id.value,
    )
    for module_id in module_ids:
        contract = registry.get(module_id)
        tree_dir = local_source_tree_path(contract.source)
        policy[f"./{tree_dir}"] = {
            "tree_dir": tree_dir,
            "digest": contract.source_digest,
            "inputs": frozenset(
                definition.name for definition in contract.inputs
            ),
            "outputs": frozenset(
                output.name for output in contract.outputs
            ),
        }
    return policy


def _module_argument_value(
    instance,
    binding,
    definition,
    *,
    contracts: dict,
    by_resource_id: dict,
    diagnostics: list,
) -> Optional[str]:
    """One module argument as HCL text; ``None`` records a diagnostic.

    The derivation kind -- not the input name -- decides the
    projection: sibling identities become ``module.<label>.<output>``
    references or an in-file list of them, the access-log policy name
    becomes the exact literal, and every other typed value becomes a
    plain HCL literal (quoted string, number, boolean). Rendering
    validity (injection, length, allowlists) has already been enforced
    during selection; anything the renderer cannot map fails closed.
    """
    value = binding.value
    kind = definition.binding.derivation

    def unresolvable(message):
        diagnostics.append(
            RenderDiagnostic(
                category=RenderDiagnosticCategory.UNREPRESENTABLE,
                code=RenderErrorCode.INCONSISTENT_REFERENCE,
                message=message,
                resource_id=instance.resource_id,
                parameter=binding.input_name,
            )
        )
        return None

    if kind is DerivationKind.ACCESS_LOG_FORMAT:
        literal = _ACCESS_LOG_FORMAT_LITERALS.get(value.value)
        if literal is None:
            diagnostics.append(
                RenderDiagnostic(
                    category=RenderDiagnosticCategory.UNREPRESENTABLE,
                    code=RenderErrorCode.UNREPRESENTABLE_PARAMETER,
                    message=(
                        f"Access-log format {value.value!r} on "
                        f"{instance.instance_id!r} has no renderer "
                        f"mapping; refusing to guess."
                    ),
                    resource_id=instance.resource_id,
                    parameter=binding.input_name,
                )
            )
            return None
        return literal

    if kind in _WIRING_OUTPUTS:
        output_name = _WIRING_OUTPUTS[kind]
        if kind is DerivationKind.TABLE_ARNS:
            if not isinstance(value, TypedStringList):
                return unresolvable(
                    f"Module instance {instance.instance_id!r} binds "
                    f"{binding.input_name!r} to a non-list value; "
                    f"refusing to wire."
                )
            identities = value.value
        else:
            if not isinstance(value, TypedString):
                return unresolvable(
                    f"Module instance {instance.instance_id!r} binds "
                    f"{binding.input_name!r} to a non-string sibling "
                    f"identity; refusing to wire."
                )
            identities = (value.value,)
        references = []
        for identity in identities:
            target = by_resource_id.get(identity)
            if target is None:
                return unresolvable(
                    f"Module instance {instance.instance_id!r} binds "
                    f"{binding.input_name!r} to resource {identity!r}, "
                    f"which has no module instance in the selection; "
                    f"refusing to guess."
                )
            target_contract = contracts[target.module_id]
            if not any(
                declared.name == output_name
                for declared in target_contract.outputs
            ):
                return unresolvable(
                    f"Module {target.module_id.value!r} does not declare "
                    f"output {output_name!r} required by input "
                    f"{binding.input_name!r}; refusing to wire."
                )
            references.append(
                f"module.{target.instance_id}.{output_name}"
            )
        if kind is DerivationKind.TABLE_ARNS:
            return "[" + ", ".join(references) + "]"
        return references[0]

    if isinstance(value, TypedBool):
        return "true" if value.value else "false"
    if isinstance(value, TypedInt):
        return str(value.value)
    if isinstance(
        value, (TypedString, TypedEnum, TypedArtifactRef, TypedSecretRef)
    ):
        return f'"{value.value}"'
    diagnostics.append(
        RenderDiagnostic(
            category=RenderDiagnosticCategory.UNREPRESENTABLE,
            code=RenderErrorCode.UNREPRESENTABLE_PARAMETER,
            message=(
                f"Module input {binding.input_name!r} on "
                f"{instance.instance_id!r} has no renderer mapping for "
                f"type {value.type.value!r}; refusing to guess."
            ),
            resource_id=instance.resource_id,
            parameter=binding.input_name,
        )
    )
    return None


def _render_modules_main(blocks: list) -> str:
    """One ``module`` block per selected instance, in instance order."""
    lines = []
    for label, attributes in blocks:
        lines.extend(_emit(f'module "{label}"', attributes))
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _render_root_outputs(selection: ModuleSelection, contracts: dict) -> str:
    """Root outputs exposing every selected module's declared outputs.

    The executor reads these after apply (``terraform output -json``) to
    verify the deployed stack without importing AWS SDKs into the
    deterministic layer. Labels are ``<instance_id>__<output_name>``;
    names are emitted in deterministic order and a duplicate label fails
    the render rather than silently overwriting a value.
    """
    lines = []
    seen = set()
    for instance in sorted(
        selection.instances, key=lambda item: item.instance_id
    ):
        contract = contracts[instance.module_id]
        for output in sorted(contract.outputs, key=lambda item: item.name):
            label = f"{instance.instance_id}__{output.name}"
            if label in seen:
                raise ValueError(
                    f"duplicate root output label {label!r}; refusing to "
                    f"overwrite a deployed value"
                )
            seen.add(label)
            lines.append(f'output "{label}" {{')
            lines.append(f"  value = module.{instance.instance_id}.{output.name}")
            lines.append("}")
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def render_module_composition(
    spec: InfraSpec,
    selection: ModuleSelection,
    *,
    registry: VerifiedModuleRegistry,
) -> RenderResult:
    """Render a verified ``ModuleSelection`` into a guarded module tree.

    Deterministic and closed-world: every emitted line derives from the
    selection (itself a pure function of spec + registry), the catalog
    contracts and the pinned module files. Never raises for content
    reasons; every refusal is a typed ``RenderDiagnostic``. When the
    selection carries diagnostics, or its trust state fails
    re-validation, nothing is rendered: a partial or unverified
    composition must never reach Terraform.
    """
    diagnostics = []

    def refuse(status):
        return RenderResult(
            status=status,
            provider=spec.provider,
            region_code=spec.region_code,
            diagnostics=diagnostics,
        )

    if selection.diagnostics:
        for diagnostic in selection.diagnostics:
            diagnostics.append(_module_selection_diagnostic(diagnostic))
        return refuse(RenderStatus.UNREPRESENTABLE)

    try:
        validation_errors = validate_module_selection(
            selection, registry=registry
        )
    except Exception as error:  # noqa: BLE001 - fail closed
        diagnostics.append(
            RenderDiagnostic(
                category=RenderDiagnosticCategory.STRUCTURAL,
                code=RenderErrorCode.UNREPRESENTABLE_PARAMETER,
                message=(
                    f"Module selection cannot be validated against the "
                    f"catalog: {error}"
                ),
            )
        )
        return refuse(RenderStatus.UNREPRESENTABLE)
    if validation_errors:
        for message in validation_errors:
            diagnostics.append(
                RenderDiagnostic(
                    category=RenderDiagnosticCategory.GUARD,
                    code=RenderErrorCode.GUARD_VIOLATION,
                    message=(
                        f"Module selection failed pre-render "
                        f"validation: {message}"
                    ),
                )
            )
        return refuse(RenderStatus.GUARD_FAILED)

    contracts = {}
    for module_id in sorted(
        {instance.module_id for instance in selection.instances},
        key=lambda module_id: module_id.value,
    ):
        try:
            contracts[module_id] = registry.get(module_id)
        except KeyError as error:
            diagnostics.append(
                RenderDiagnostic(
                    category=RenderDiagnosticCategory.STRUCTURAL,
                    code=RenderErrorCode.UNREPRESENTABLE_PARAMETER,
                    message=(
                        f"Module selection names a capability outside "
                        f"the verified catalog: {error}"
                    ),
                )
            )
    if diagnostics:
        return refuse(RenderStatus.UNREPRESENTABLE)

    by_resource_id = {}
    for instance in selection.instances:
        if instance.resource_id in by_resource_id:
            diagnostics.append(
                RenderDiagnostic(
                    category=RenderDiagnosticCategory.STRUCTURAL,
                    code=RenderErrorCode.INCONSISTENT_REFERENCE,
                    message=(
                        f"Resource {instance.resource_id!r} is realised "
                        f"by more than one module instance; refusing."
                    ),
                    resource_id=instance.resource_id,
                )
            )
        by_resource_id[instance.resource_id] = instance
    for instance in selection.instances:
        if not _MODULE_LABEL.match(instance.instance_id):
            diagnostics.append(
                RenderDiagnostic(
                    category=RenderDiagnosticCategory.STRUCTURAL,
                    code=RenderErrorCode.MALFORMED_PARAMETER,
                    message=(
                        f"Module instance {instance.instance_id!r} is "
                        f"not a safe Terraform module identifier; "
                        f"refusing to render it."
                    ),
                    resource_id=instance.resource_id,
                )
            )
    if diagnostics:
        return refuse(RenderStatus.UNREPRESENTABLE)

    blocks = []
    for instance in sorted(
        selection.instances, key=lambda item: item.instance_id
    ):
        contract = contracts[instance.module_id]
        definitions = {
            definition.name: definition for definition in contract.inputs
        }
        attributes = [
            (
                "source",
                f'"./{local_source_tree_path(contract.source)}"',
            )
        ]
        for binding in instance.bindings:
            definition = definitions.get(binding.input_name)
            if definition is None:
                diagnostics.append(
                    RenderDiagnostic(
                        category=RenderDiagnosticCategory.STRUCTURAL,
                        code=RenderErrorCode.UNREPRESENTABLE_PARAMETER,
                        message=(
                            f"Module instance {instance.instance_id!r} "
                            f"binds undeclared input "
                            f"{binding.input_name!r}; refusing."
                        ),
                        resource_id=instance.resource_id,
                        parameter=binding.input_name,
                    )
                )
                continue
            rendered = _module_argument_value(
                instance,
                binding,
                definition,
                contracts=contracts,
                by_resource_id=by_resource_id,
                diagnostics=diagnostics,
            )
            if rendered is not None:
                attributes.append((binding.input_name, rendered))
        blocks.append((instance.instance_id, attributes))
    if diagnostics:
        return refuse(RenderStatus.UNREPRESENTABLE)

    try:
        outputs_source = _render_root_outputs(selection, contracts)
    except ValueError as error:
        diagnostics.append(
            RenderDiagnostic(
                category=RenderDiagnosticCategory.STRUCTURAL,
                code=RenderErrorCode.UNREPRESENTABLE_PARAMETER,
                message=str(error),
            )
        )
        return refuse(RenderStatus.UNREPRESENTABLE)

    policy = module_guard_policy(selection, registry=registry)

    files = [
        RenderedFile(
            path="main.tf",
            content=_align_equals(_render_modules_main(blocks)),
        ),
        RenderedFile(path="outputs.tf", content=outputs_source),
        RenderedFile(path="provider.tf", content=_render_provider(spec)),
        RenderedFile(path="versions.tf", content=_render_versions()),
    ]
    tree_dirs = {}
    for module_id in sorted(contracts, key=lambda item: item.value):
        contract = contracts[module_id]
        tree_dirs[local_source_tree_path(contract.source)] = contract
    for tree_dir in sorted(tree_dirs):
        contract = tree_dirs[tree_dir]
        try:
            module_files = registry.module_tree(contract.module_id)
        except CatalogError as error:
            diagnostics.append(
                RenderDiagnostic(
                    category=RenderDiagnosticCategory.STRUCTURAL,
                    code=RenderErrorCode.UNREPRESENTABLE_PARAMETER,
                    message=(
                        f"Module {contract.module_id.value!r} content "
                        f"is unavailable: {error}"
                    ),
                )
            )
            continue
        for name in sorted(module_files):
            files.append(
                RenderedFile(
                    path=f"{tree_dir}/{name}", content=module_files[name]
                )
            )
    if diagnostics:
        return refuse(RenderStatus.UNREPRESENTABLE)

    files.sort(key=lambda entry: entry.path)
    source_tree = {entry.path: entry.content for entry in files}
    guard_diagnostics = guard_terraform_source(
        source_tree,
        allowed_resource_types=ALLOWED_TF_RESOURCE_TYPES,
        module_policy=policy,
    )
    if guard_diagnostics:
        return RenderResult(
            status=RenderStatus.GUARD_FAILED,
            provider=spec.provider,
            region_code=spec.region_code,
            diagnostics=guard_diagnostics,
        )

    return RenderResult(
        status=RenderStatus.RENDERED,
        provider=spec.provider,
        region_code=spec.region_code,
        files=files,
        tree_hash=tree_hash(files),
        diagnostics=[],
    )
