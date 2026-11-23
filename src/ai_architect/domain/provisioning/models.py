"""Provisioning domain contract: the deterministic InfraSpec (Slice 4A).

The InfraSpec is the provisioning *intent* derived from an approved
ArchitectureResult. It is deliberately independent of Terraform/HCL
syntax: the next slice renders Terraform from it, but this model never
carries provider-specific code, and it never carries a value that was
not either derived deterministically from the architecture or explicitly
marked as still-required human input.

Design rules enforced here:

- Deterministic. No timestamps, no random identifiers, no unordered
  collections. The same ArchitectureResult always yields the same spec.
- Honest about unknowns. A parameter that cannot be derived is a
  typed pending input, never a guessed default.
- Provider/cloud agnostic in shape; Slice 4 provisioning is AWS-only.
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "infra-spec/v1"

# Providers this slice can provision for. The model shape stays
# provider-agnostic; the registry and gate are AWS-only for now.
SUPPORTED_PROVIDERS: tuple = ("aws",)


class ResourceKind(str, Enum):
    """Bounded set of provisionable resource kinds."""

    S3 = "s3"
    LAMBDA = "lambda"
    DYNAMODB = "dynamodb"
    APIGATEWAY_HTTP = "apigateway_http"
    CLOUDFRONT_DISTRIBUTION = "cloudfront_distribution"
    COGNITO_USER_POOL = "cognito_user_pool"
    SQS_QUEUE = "sqs_queue"
    SNS_TOPIC = "sns_topic"
    CLOUDWATCH_LOG_GROUP = "cloudwatch_log_group"
    CLOUDWATCH_ALARM = "cloudwatch_alarm"


class ParameterSource(str, Enum):
    """Where a resolved parameter value came from.

    DERIVED: computed deterministically from the ArchitectureResult.
    REQUIRED_INPUT: supplied explicitly as a provisioning input.
    """

    DERIVED = "derived"
    REQUIRED_INPUT = "required_input"


class ProvisioningErrorCode(str, Enum):
    """Stable codes for deterministic failure reporting."""

    UNSUPPORTED_RESOURCE = "unsupported_resource"
    PROVIDER_MISMATCH = "provider_mismatch"
    INVALID_REGION = "invalid_region"
    MISSING_REGION = "missing_region"
    MISSING_INPUT = "missing_required_input"
    DUPLICATE_IDENTITY = "duplicate_resource_identity"
    INVALID_IDENTITY = "invalid_resource_identity"
    INCONSISTENT_REFERENCE = "inconsistent_resource_reference"
    MALFORMED_PARAMETER = "malformed_parameter"
    SECRET_DETECTED = "secret_detected"


class DiagnosticCategory(str, Enum):
    """Classifies why a spec is not yet provisionable.

    STRUCTURAL: the spec itself is malformed or internally inconsistent
    (bad identity, provider/region mismatch, malformed parameter,
    credential detected, dangling reference). These indicate a bug or a
    corrupted artifact.

    MISSING_INPUT: the spec is structurally sound, but a human must still
    decide a required infrastructure parameter. This is never a defect
    and is never defaulted away.
    """

    STRUCTURAL = "structural"
    MISSING_INPUT = "missing_input"


class SpecDiagnostic(BaseModel):
    """One deterministic finding about an InfraSpec.

    Carries a machine-readable ``code`` and an explicit ``category`` so
    callers never have to pattern-match message text to tell a real
    defect apart from a legitimately outstanding human decision.
    """

    category: DiagnosticCategory
    code: ProvisioningErrorCode
    message: str
    resource_id: Optional[str] = None
    input_name: Optional[str] = None

    model_config = ConfigDict(extra="forbid")

    def __str__(self) -> str:
        return self.message


class ResolvedParameter(BaseModel):
    """One resolved provisioning parameter.

    Values are string-typed so the model stays independent of any
    provider's configuration language.
    """

    name: str
    value: str
    source: ParameterSource

    model_config = ConfigDict(extra="forbid")


class PendingInput(BaseModel):
    """A required provisioning input that is not yet supplied.

    Present rather than defaulted: the spec states exactly what a
    human must decide before this resource can be rendered or applied.
    """

    name: str
    reason: str

    model_config = ConfigDict(extra="forbid")


class SupportingResource(BaseModel):
    """An implicit supporting resource required by this instance.

    Supporting resources are never user-selectable; they exist only
    because a supported resource's contract requires them.
    """

    kind: str
    reference: str
    reason: str

    model_config = ConfigDict(extra="forbid")


class ResourceInstance(BaseModel):
    """One provisionable resource derived from one architecture node."""

    resource_id: str
    node_id: str
    kind: ResourceKind
    component_name: str
    is_entry_point: bool = False
    is_data_store: bool = False
    parameters: list[ResolvedParameter] = Field(default_factory=list)
    pending_inputs: list[PendingInput] = Field(default_factory=list)
    supporting: list[SupportingResource] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class SourceArchitectureRef(BaseModel):
    """Provenance of the architecture this spec was derived from.

    Lets later stages verify exactly which approved design is being
    provisioned, and detect any drift between approval and apply.
    """

    artifact_id: str
    version: int
    session_id: str
    upstream_inputs_hash: str
    architecture_hash: str = Field(
        description="Deterministic hash of the source ArchitectureResult"
    )

    model_config = ConfigDict(extra="forbid")


class InfraSpec(BaseModel):
    """Deterministic provisioning intent for one architecture artifact."""

    schema_version: str = SCHEMA_VERSION
    spec_id: str
    provider: str
    region_code: str
    source_architecture: SourceArchitectureRef
    resources: list[ResourceInstance] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    def unresolved_inputs(self) -> list[tuple]:
        """(resource_id, input_name) pairs still awaiting a human."""
        return [
            (resource.resource_id, pending.name)
            for resource in self.resources
            for pending in resource.pending_inputs
        ]
