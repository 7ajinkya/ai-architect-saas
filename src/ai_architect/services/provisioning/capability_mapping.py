"""Deterministic capability mapping: Slice 4A ``ResourceKind`` -> ``ModuleId``.

Selection has to be a pure function of the frozen InfraSpec rather than of
a model, so the mapping from a resource kind to the module that realises the
capability is an explicit, closed, auditable table.

Two properties matter:

- **Explicit mapping, not inference.** Every entry is written down. There is
  no category fallback, no substring match and no fuzzy resolution, matching
  the fail-closed discipline already established in the frozen 4A resource
  registry.
- **Capability level, not resource level.** One module may satisfy a
  capability that several raw resources would otherwise express separately.
  ``ResourceKind.S3`` maps to ``object_storage`` even though that capability
  is four AWS resources: composing from a verified library is precisely what
  collapses them.

Adding a capability is a deliberate, reviewed table change plus a catalog
entry, never a code path that infers from a name.
"""

from ai_architect.domain.provisioning.models import InfraSpec, ResourceKind
from ai_architect.domain.provisioning.module import ModuleId

#: Closed, explicit capability mapping. Deliberately not derived from
#: ``component_name``, ``category`` or any other free-form field.
CAPABILITY_MAPPING: dict = {
    ResourceKind.S3: ModuleId.OBJECT_STORAGE,
    ResourceKind.DYNAMODB: ModuleId.NOSQL_TABLE,
    ResourceKind.LAMBDA: ModuleId.SERVERLESS_FUNCTION,
    ResourceKind.APIGATEWAY_HTTP: ModuleId.HTTP_API,
    ResourceKind.CLOUDFRONT_DISTRIBUTION: ModuleId.CLOUDFRONT_DISTRIBUTION,
    ResourceKind.COGNITO_USER_POOL: ModuleId.COGNITO_USER_POOL,
    ResourceKind.SQS_QUEUE: ModuleId.SQS_QUEUE,
    ResourceKind.SNS_TOPIC: ModuleId.SNS_TOPIC,
    ResourceKind.CLOUDWATCH_LOG_GROUP: ModuleId.LOG_GROUP,
    ResourceKind.CLOUDWATCH_ALARM: ModuleId.CLOUDWATCH_ALARMS,
}

#: Workload-neutral, for reporting and for capability coverage queries.
CAPABILITY_DESCRIPTIONS: dict = {
    ModuleId.OBJECT_STORAGE: "durable object storage",
    ModuleId.NOSQL_TABLE: "key-value table",
    ModuleId.SERVERLESS_FUNCTION: "event-driven compute",
    ModuleId.HTTP_API: "managed HTTP entry point",
    ModuleId.CLOUDFRONT_DISTRIBUTION: "managed edge distribution",
    ModuleId.COGNITO_USER_POOL: "user-pool authentication",
    ModuleId.SQS_QUEUE: "managed message queue",
    ModuleId.SNS_TOPIC: "publish/subscribe topic",
    ModuleId.LOG_GROUP: "centralized log retention",
    ModuleId.CLOUDWATCH_ALARMS: "metric alarms for API and function health",
}


def module_for_kind(kind: ResourceKind) -> ModuleId:
    """Exact capability mapping, or ``None`` when unmapped.

    ``None`` is a real, expected outcome: it means the catalog has no
    verified module for this capability, which is reported to the user
    explicitly rather than approximated.
    """
    return CAPABILITY_MAPPING.get(kind)


def mapped_capabilities() -> tuple:
    """Deterministically ordered list of mapped capabilities."""
    return tuple(
        sorted({module_id.value for module_id in CAPABILITY_MAPPING.values()})
    )


def describe_capability(module_id: ModuleId) -> str:
    return CAPABILITY_DESCRIPTIONS.get(module_id, module_id.value)