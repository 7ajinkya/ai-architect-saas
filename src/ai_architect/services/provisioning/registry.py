"""Bounded, deterministic resource registry for provisioning.

This mirrors the bounded-registry philosophy already used for pricing
(``SERVICE_SPECS`` in services.cost.pricing_service): a small curated
table keyed by normalized component name, resolved deterministically,
and **failing closed** with an explicit reason when a component is not
registered.

Deliberate properties:

- No LLM participates in resource resolution.
- Resolution is exact normalized name, then the same safe vendor-prefix
  normalization used elsewhere in the repository, then an explicit
  unsupported error. Never a category guess: "storage" does not imply S3.
- Component names are never rewritten.
- IAM appears only as an implicit supporting resource required by the
  Lambda contract; it is not user-selectable here.
"""

from dataclasses import dataclass, field
from typing import Optional

from ai_architect.domain.provisioning.models import (
    SUPPORTED_PROVIDERS,
    ProvisioningErrorCode,
    ResourceKind,
)
from ai_architect.services.cost.pricing_service import canonical_component_name

# Reused normalization: lowercase, strip parenthetical aliases, collapse
# whitespace. Kept in one place so provisioning and pricing cannot drift.
VENDOR_PREFIXES = ("amazon ", "aws ")

# Capability qualifiers that name a DIFFERENT resource than the
# registered contract. Parenthetical aliases are deliberately stripped
# during normalization (so "Amazon S3 (Standard)" still matches), which
# means these must be rejected from the raw name first: silently mapping
# "S3 static website hosting" to an object bucket (or a REST/WebSocket
# API to an HTTP API) would provision the wrong capability.
_QUALIFIER_REJECTIONS: dict = {
    ResourceKind.S3: (
        ("static website", "S3 static website hosting"),
        ("website hosting", "S3 website hosting"),
        ("static site", "S3 static site hosting"),
        ("website", "S3 website hosting"),
    ),
    ResourceKind.APIGATEWAY_HTTP: (
        ("rest api", "API Gateway REST API"),
        ("rest)", "API Gateway REST API"),
        ("(rest", "API Gateway REST API"),
        ("websocket", "API Gateway WebSocket API"),
        ("web socket", "API Gateway WebSocket API"),
    ),
    ResourceKind.CLOUDWATCH_LOG_GROUP: (
        ("alarm", "CloudWatch alarms"),
        ("metric", "CloudWatch metrics"),
    ),
    ResourceKind.CLOUDWATCH_ALARM: (
        ("metric alarm", "single CloudWatch metric alarms"),
        ("error rate", "CloudWatch error-rate alarms"),
        ("throttl", "CloudWatch throttling alarms"),
        ("composite", "CloudWatch composite alarms"),
        ("anomaly", "CloudWatch anomaly-detection alarms"),
        ("metric math", "CloudWatch metric-math alarms"),
    ),
    ResourceKind.CLOUDFRONT_DISTRIBUTION: (
        ("function", "CloudFront Functions"),
        ("lambda@edge", "Lambda@Edge"),
        ("edge function", "CloudFront edge functions"),
        ("streaming", "CloudFront media streaming"),
    ),
    ResourceKind.COGNITO_USER_POOL: (
        ("identity pool", "Cognito identity pools"),
        ("identity pools", "Cognito identity pools"),
        ("federated identit", "Cognito federated identities"),
        ("sso", "Cognito SSO"),
    ),
    ResourceKind.SQS_QUEUE: (
        ("fifo", "SQS FIFO queues"),
        ("first-in", "SQS FIFO queues"),
        ("dead letter", "SQS dead-letter queues"),
        ("dead-letter", "SQS dead-letter queues"),
        ("dlq", "SQS dead-letter queues"),
    ),
    ResourceKind.SNS_TOPIC: (
        ("email", "SNS email subscriptions"),
        ("sms", "SNS SMS delivery"),
        ("mobile push", "SNS mobile push"),
        ("push notification", "SNS mobile push"),
        ("platform application", "SNS platform applications"),
        ("fifo", "SNS FIFO topics"),
        ("http", "SNS HTTP/HTTPS subscriptions"),
    ),
}


@dataclass(frozen=True)
class ResourceContract:
    """What one supported resource kind requires to be provisioned."""

    kind: ResourceKind
    provider: str
    normalized_names: frozenset
    required_inputs: tuple
    supporting: tuple = field(default_factory=tuple)
    #: Canonical human-facing name for this capability. It is the exact
    #: string the platform provisions, and it resolves back to this
    #: contract through ``match_contract``. Written down explicitly, never
    #: derived from a name pattern.
    display_name: str = ""

    def supports_name(self, normalized: str) -> bool:
        if normalized in self.normalized_names:
            return True
        for prefix in VENDOR_PREFIXES:
            if normalized.startswith(prefix):
                short = normalized[len(prefix):].strip()
                if short in self.normalized_names:
                    return True
        return False


RESOURCE_CONTRACTS: dict = {
    ResourceKind.S3: ResourceContract(
        kind=ResourceKind.S3,
        provider="aws",
        normalized_names=frozenset({"amazon s3", "simple storage service"}),
        # Nothing about versioning, encryption or exposure can be derived
        # from an ArchitectureResult, so each is an explicit input.
        required_inputs=(
            "bucket_name",
            "versioning",
            "encryption",
            "public_access_block",
        ),
        supporting=(),
        display_name="Amazon S3",
    ),
    ResourceKind.LAMBDA: ResourceContract(
        kind=ResourceKind.LAMBDA,
        provider="aws",
        normalized_names=frozenset({"aws lambda", "amazon lambda", "lambda"}),
        # Runtime, handler, memory and timeout are not carried by
        # ArchitectureNode. None are guessed. The code artifact is a
        # human-supplied deployment package reference (path inside the
        # deployment bundle plus its sha256); the platform never authors
        # application code, and the renderer verifies the hash shape
        # without reading the operator's filesystem. Packaging type is
        # not a decision at all: the verified module is zip-packaged by
        # construction and takes no such variable.
        required_inputs=(
            "runtime",
            "handler",
            "memory_mb",
            "timeout_seconds",
            "code_artifact_path",
            "code_artifact_sha256",
        ),
        # A function cannot execute without an execution role.
        supporting=(
            ("iam_role", "lambda_execution_role", "execution role required"),
        ),
        display_name="AWS Lambda",
    ),
    ResourceKind.APIGATEWAY_HTTP: ResourceContract(
        kind=ResourceKind.APIGATEWAY_HTTP,
        provider="aws",
        normalized_names=frozenset({"api gateway", "api gateway http api"}),
        # Everything is derived deterministically (names from the stable
        # resource identity; integration from the single Lambda in the
        # same spec; access-log destination from the single log group).
        # Ambiguous topologies are refused at render time, never
        # guessed, so there are no required human inputs.
        required_inputs=(),
        supporting=(),
        display_name="Amazon API Gateway (HTTP API)",
    ),
    ResourceKind.CLOUDWATCH_LOG_GROUP: ResourceContract(
        kind=ResourceKind.CLOUDWATCH_LOG_GROUP,
        provider="aws",
        normalized_names=frozenset(
            {"cloudwatch", "amazon cloudwatch", "cloudwatch logs",
             "amazon cloudwatch logs"}
        ),
        # The group name derives from the stable resource identity;
        # retention is a cost decision stated by the human.
        required_inputs=("retention_days",),
        supporting=(),
        display_name="Amazon CloudWatch Logs",
    ),
    ResourceKind.DYNAMODB: ResourceContract(
        kind=ResourceKind.DYNAMODB,
        provider="aws",
        normalized_names=frozenset({"amazon dynamodb", "dynamodb"}),
        # Billing mode and key schema change cost and capacity, so they
        # are stated rather than inferred from read/write characteristics.
        required_inputs=(
            "billing_mode",
            "attribute_definitions",
            "key_schema",
            "point_in_time_recovery",
        ),
        supporting=(),
        display_name="Amazon DynamoDB",
    ),
    ResourceKind.CLOUDFRONT_DISTRIBUTION: ResourceContract(
        kind=ResourceKind.CLOUDFRONT_DISTRIBUTION,
        provider="aws",
        normalized_names=frozenset(
            {"amazon cloudfront", "cloudfront"}
        ),
        # A distribution is bound to exactly one object-storage origin in
        # the same spec (OAC + bucket policy included). The root object
        # and price class are cost/routing decisions stated by the human.
        required_inputs=("default_root_object", "price_class"),
        supporting=(),
        display_name="Amazon CloudFront",
    ),
    ResourceKind.COGNITO_USER_POOL: ResourceContract(
        kind=ResourceKind.COGNITO_USER_POOL,
        provider="aws",
        normalized_names=frozenset(
            {
                "amazon cognito",
                "cognito",
                "amazon cognito user pool",
                "amazon cognito user pools",
                "cognito user pool",
                "cognito user pools",
            }
        ),
        # Password policy, MFA posture and self-sign-up are human
        # security decisions; the platform never invents them.
        required_inputs=(
            "policy_minimum_length",
            "mfa_configuration",
            "self_sign_up_enabled",
        ),
        supporting=(),
        display_name="Amazon Cognito",
    ),
    ResourceKind.SQS_QUEUE: ResourceContract(
        kind=ResourceKind.SQS_QUEUE,
        provider="aws",
        normalized_names=frozenset(
            {
                "amazon sqs",
                "sqs",
                "amazon simple queue service",
                "simple queue service",
                "amazon simple queue service standard",
            }
        ),
        # Retention and visibility timeout change delivery semantics and
        # cost; both are stated by the human from closed allowlists.
        required_inputs=(
            "visibility_timeout_seconds",
            "message_retention_seconds",
        ),
        supporting=(),
        display_name="Amazon SQS",
    ),
    ResourceKind.SNS_TOPIC: ResourceContract(
        kind=ResourceKind.SNS_TOPIC,
        provider="aws",
        normalized_names=frozenset(
            {
                "amazon sns",
                "sns",
                "amazon sns topics",
                "sns topics",
                "amazon simple notification service",
                "simple notification service",
                "amazon simple notification service topics",
            }
        ),
        # Topic name derives from the stable resource identity; the
        # subscriber is the single SQS queue in the same spec.
        required_inputs=(),
        supporting=(),
        display_name="Amazon SNS",
    ),
    ResourceKind.CLOUDWATCH_ALARM: ResourceContract(
        kind=ResourceKind.CLOUDWATCH_ALARM,
        provider="aws",
        normalized_names=frozenset(
            {"cloudwatch alarm", "cloudwatch alarms", "amazon cloudwatch alarm", "amazon cloudwatch alarms"}
        ),
        # Every threshold and the notification destination is a human
        # decision. The platform derives only names and dimensions.
        required_inputs=(
            "api_5xx_error_threshold",
            "api_latency_p99_threshold_ms",
            "lambda_error_threshold",
            "lambda_duration_p99_threshold_ms",
            "period_seconds",
            "evaluation_periods",
            "comparison_operator",
            "missing_data_treatment",
            "notification_target",
        ),
        supporting=(),
        display_name="Amazon CloudWatch Alarms",
    ),
}


class UnsupportedResourceError(ValueError):
    """A component has no registered provisioning resource."""

    def __init__(
        self,
        detail: str,
        *,
        component_name: str,
        node_id: str = "",
        code: str = ProvisioningErrorCode.UNSUPPORTED_RESOURCE.value,
    ):
        super().__init__(detail)
        self.detail = detail
        self.component_name = component_name
        self.node_id = node_id
        self.code = code


def supported_component_names() -> list:
    """Human-readable list of currently registered components."""
    return sorted(
        name for contract in RESOURCE_CONTRACTS.values()
        for name in contract.normalized_names
    )


def provisionable_component_names() -> list:
    """Canonical names of every capability the platform can provision.

    This is the closed world of provisionable components for the AWS
    provisioning path: one canonical name per registered resource kind,
    in deterministic order. It is written down explicitly in the
    contracts (never inferred from a name pattern) and every entry
    resolves back to its contract through ``match_contract``.

    Callers that steer a model toward provisionable components (for
    example the component-selection prompt) use this list so the model
    cannot name a capability the platform has no module for. A
    requirement that needs something outside this list is a real gap and
    must be stated, never approximated by a near-miss component.
    """
    names = [
        contract.display_name
        for contract in RESOURCE_CONTRACTS.values()
        if contract.display_name
    ]
    return sorted(set(names))


def match_contract(component_name: str) -> ResourceContract:
    """Resolve one component name to its resource contract, or fail closed.
    Resolution order: explicit descriptive aliases, exact normalized
    name, then vendor-prefix normalization. There is no category
    fallback and no fuzzy matching: an unregistered component is an
    explicit error, never a guess.
    """
    normalized = canonical_component_name(component_name)
    if not normalized:
        raise UnsupportedResourceError(
            "Component has no usable name and cannot be matched to a "
            "provisioning resource.",
            component_name=component_name,
        )
    raw_lowered = " ".join((component_name or "").lower().split())
    for contract in RESOURCE_CONTRACTS.values():
        if not contract.supports_name(normalized):
            continue
        for marker, label in _QUALIFIER_REJECTIONS.get(contract.kind, ()):
            if marker in raw_lowered:
                raise UnsupportedResourceError(
                    f"Component {component_name!r} names {label}, which "
                    f"is not the registered {contract.kind.value} "
                    "resource; refusing rather than silently mapping it "
                    "to a different capability.",
                    component_name=component_name,
                )
        return contract
    raise UnsupportedResourceError(
        f"Component {component_name!r} (normalized {normalized!r}) has no "
        f"registered provisioning resource. Supported: "
        f"{', '.join(supported_component_names())}.",
        component_name=component_name,
    )


def contract_for_kind(
    kind: ResourceKind, provider: str
) -> Optional[ResourceContract]:
    contract = RESOURCE_CONTRACTS.get(kind)
    if contract is None:
        return None
    if contract.provider not in SUPPORTED_PROVIDERS:
        return None
    return contract
