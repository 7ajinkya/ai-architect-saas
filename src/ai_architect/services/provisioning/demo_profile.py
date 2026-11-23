"""Deterministic defaults for bounded reference profiles.

Two exact profiles are recognised, never an open-ended shape:

- the URL-shortener demo: one HTTP API, one Python Lambda, one DynamoDB
  table and one CloudWatch log group;
- the serverless full-stack reference: the demo plus one S3 bucket, one
  CloudFront distribution, one Cognito user pool, one SQS queue, one SNS
  topic and one CloudWatch alarm resource.

Those components need a small set of decisions before verified modules
can render. This module supplies only those decisions, and only when the
spec is exactly one of the bounded profiles; it never invents values for
other resources or widens a profile to additional services.

Every default is explicit, deterministic, and visible in the plan as
provenance. Human-supplied values always win.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from ai_architect.domain.provisioning.models import InfraSpec, ResourceKind

_SOURCE = "deterministic_default"
_REQUIRES_CONFIRMATION = True

_DEMO_KINDS = Counter(
    {
        ResourceKind.APIGATEWAY_HTTP: 1,
        ResourceKind.DYNAMODB: 1,
        ResourceKind.LAMBDA: 1,
        ResourceKind.CLOUDWATCH_LOG_GROUP: 1,
    }
)

_FULL_STACK_KINDS = Counter(
    {
        ResourceKind.APIGATEWAY_HTTP: 1,
        ResourceKind.CLOUDFRONT_DISTRIBUTION: 1,
        ResourceKind.COGNITO_USER_POOL: 1,
        ResourceKind.DYNAMODB: 1,
        ResourceKind.LAMBDA: 1,
        ResourceKind.S3: 1,
        ResourceKind.SNS_TOPIC: 1,
        ResourceKind.SQS_QUEUE: 1,
        ResourceKind.CLOUDWATCH_LOG_GROUP: 1,
        ResourceKind.CLOUDWATCH_ALARM: 1,
    }
)

_BY_KIND = {
    ResourceKind.S3: {
        "versioning": (
            "enabled",
            "Full-stack profile: the static frontend bucket keeps "
            "object versions.",
        ),
        "encryption": (
            "aes256",
            "Full-stack profile: server-side encryption with S3-managed "
            "keys.",
        ),
        "public_access_block": (
            "all_blocked",
            "Full-stack profile: the bucket is private; CloudFront reads "
            "it through an origin access control.",
        ),
    },
    ResourceKind.CLOUDFRONT_DISTRIBUTION: {
        "default_root_object": (
            "index.html",
            "Full-stack profile: the root path serves the SPA entry point.",
        ),
        "price_class": (
            "PriceClass_100",
            "Full-stack profile: the lowest-cost edge footprint.",
        ),
    },
    ResourceKind.COGNITO_USER_POOL: {
        "policy_minimum_length": (
            "12",
            "Full-stack profile: passwords are at least 12 characters.",
        ),
        "mfa_configuration": (
            "OFF",
            "Full-stack profile: MFA stays optional-off until the "
            "application defines enrollment.",
        ),
        "self_sign_up_enabled": (
            "true",
            "Full-stack profile: the public frontend allows self sign-up.",
        ),
    },
    ResourceKind.SQS_QUEUE: {
        "visibility_timeout_seconds": (
            "30",
            "Full-stack profile: a consumed message is hidden for 30 "
            "seconds.",
        ),
        "message_retention_seconds": (
            "86400",
            "Full-stack profile: messages are retained for one day.",
        ),
    },
    ResourceKind.DYNAMODB: {
        "billing_mode": (
            "PAY_PER_REQUEST",
            "Demo profile: on-demand capacity avoids idle-capacity "
            "guesses for a shortener workload.",
        ),
        "attribute_definitions": (
            "pk:S",
            "Demo profile: one string partition key named pk.",
        ),
        "key_schema": (
            "HASH:pk",
            "Demo profile: the table has a single HASH key named pk.",
        ),
        "point_in_time_recovery": (
            "true",
            "Demo profile: point-in-time recovery is enabled.",
        ),
    },
    ResourceKind.LAMBDA: {
        "runtime": (
            "python3.12",
            "Demo profile: the packaged demo handler targets Python 3.12.",
        ),
        "handler": (
            "handler.handler",
            "Demo profile: the packaged demo artifact contains "
            "handler.py, whose module-level handler() entry point is "
            "the configured handler.handler.",
        ),
        "memory_mb": (
            "512",
            "Demo profile: 512 MB is the demo function's fixed allocation.",
        ),
        "timeout_seconds": (
            "30",
            "Demo profile: the demo function has a 30-second timeout.",
        ),
    },
    ResourceKind.CLOUDWATCH_LOG_GROUP: {
        "retention_days": (
            "30",
            "Demo profile: retain demo logs for 30 days.",
        ),
    },
    ResourceKind.CLOUDWATCH_ALARM: {
        "api_5xx_error_threshold": (
            "5",
            "Full-stack profile: alert after five API 5XX errors in a period.",
        ),
        "api_latency_p99_threshold_ms": (
            "2000",
            "Full-stack profile: alert when API p99 latency exceeds 2 s.",
        ),
        "lambda_error_threshold": (
            "1",
            "Full-stack profile: alert on any Lambda error in a period.",
        ),
        "lambda_duration_p99_threshold_ms": (
            "5000",
            "Full-stack profile: alert when Lambda p99 duration exceeds 5 s.",
        ),
        "period_seconds": (
            "300",
            "Full-stack profile: evaluate five-minute periods.",
        ),
        "evaluation_periods": (
            "1",
            "Full-stack profile: one period before alarm state.",
        ),
        "comparison_operator": (
            "GreaterThanOrEqualToThreshold",
            "Full-stack profile: upper-bound comparison.",
        ),
        "missing_data_treatment": (
            "notBreaching",
            "Full-stack profile: missing data does not page.",
        ),
        "notification_target": (
            "none",
            "Full-stack profile: alarms remain visible without an external "
            "notification destination.",
        ),
    },
}


@dataclass(frozen=True)
class DemoProfileDefault:
    resource_id: str
    name: str
    value: str
    rationale: str
    source: str = _SOURCE
    requires_confirmation: bool = _REQUIRES_CONFIRMATION


def demo_profile_defaults(spec: InfraSpec) -> tuple[DemoProfileDefault, ...]:
    """Return defaults only for one of the exact bounded profiles."""
    if spec.provider != "aws":
        return ()
    shape = Counter(resource.kind for resource in spec.resources)
    if shape not in (_DEMO_KINDS, _FULL_STACK_KINDS):
        return ()

    defaults: list[DemoProfileDefault] = []
    for resource in spec.resources:
        pending = {item.name for item in resource.pending_inputs}
        for name, (value, rationale) in _BY_KIND.get(
            resource.kind, {}
        ).items():
            if name not in pending:
                continue
            defaults.append(
                DemoProfileDefault(
                    resource_id=resource.resource_id,
                    name=name,
                    value=value,
                    rationale=rationale,
                )
            )
    return tuple(defaults)
