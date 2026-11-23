"""Slice 4C domain contracts: verified Terraform module composition.

Slice 4A's ``InfraSpec`` states *what infrastructure capability* an approved
architecture needs. This module states *which verified module* realises each
capability and *with what typed parameters*.

Design boundary (Slice 4C ends here):

    ArchitectureResult -> InfraSpec [4A, frozen]
                       -> VerifiedModuleRegistry [4C]
                       -> ModuleSelection [4C, terminal artifact]

Nothing here renders Terraform. The module invocation renderer, the guard's
``module`` policy, policy-as-code, plan, approval and apply are all later
slices. 4C's deliverable is a deterministic, fully-provenanced selection
document plus a machine-readable record of what was rejected and why.

Key properties:

- **Capability identity, not resource identity.** ``ModuleId`` is
  ``object_storage`` / ``nosql_table``, never ``aws_s3_bucket``. A single
  capability is often several raw resources (a durable, versioned,
  encrypted, private bucket is four), and collapsing that into one module is
  the entire point of composing from a verified library.
- **Trust is data.** ``source`` and ``version`` exist only on
  ``ModuleContract``, which only the project-owned catalog constructs. No
  proposal, prompt or parameter can influence them.
- **"Verified" is a recorded claim, not registry presence.** A module being
  publicly available is discovery, never trust.
- **Fail closed, all or nothing.** A partial ``ModuleSelection`` is never
  emitted; an unsupported capability fails the whole selection.
- **Deterministic.** Canonical JSON + SHA-256, sorted collections, no
  timestamps, no UUIDs, no network, no LLM.
"""

from datetime import date
from enum import Enum
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "module-selection/v1"

# Relative path, from the future Terraform working directory, to the
# project-owned module tree that forms the MVP trust boundary.
MODULE_ROOT = "infra/modules"


class ModuleId(str, Enum):
    """Stable capability-level module identity.

    Deliberately not Terraform resource types: the requirements compose
    from a curated library of verified modules, so the abstraction has to
    sit at the level of a capability rather than a provider argument list.
    """

    OBJECT_STORAGE = "object_storage"
    NOSQL_TABLE = "nosql_table"
    SERVERLESS_FUNCTION = "serverless_function"
    HTTP_API = "http_api"
    CLOUDFRONT_DISTRIBUTION = "cloudfront_distribution"
    COGNITO_USER_POOL = "cognito_user_pool"
    SQS_QUEUE = "sqs_queue"
    SNS_TOPIC = "sns_topic"
    LOG_GROUP = "log_group"
    CLOUDWATCH_ALARMS = "cloudwatch_alarms"


class VerificationStatus(str, Enum):
    """Whether a contract may be selected. Only ``VERIFIED`` may."""

    VERIFIED = "verified"
    PENDING = "pending"
    DEPRECATED = "deprecated"
    REVOKED = "revoked"


class VerificationMethod(str, Enum):
    """How a verification claim was established."""

    PINNED_REVISION = "pinned_revision"
    TESTED_CONTRACT = "tested_contract"
    MANUAL_REVIEW = "manual_review"


class SourceKind(str, Enum):
    """How a module's ``source`` resolves.

    ``LOCAL_PATH`` is MVP: the module ships in this repository under
    ``infra/modules`` and is pinned by content digest rather than by
    published version. ``REGISTRY`` is the post-publication state.
    """

    LOCAL_PATH = "local_path"
    REGISTRY = "registry"


class ParameterType(str, Enum):
    """Closed set of module parameter types.

    ``ATTRIBUTE_LIST`` and ``KEY_SCHEMA`` exist only as a migration bridge
    for the frozen 4A parameters that encode structure in strings. A
    curated module should take scalars and derive structure internally; the
    MVP ``nosql_table`` contract projects these two into scalar inputs
    instead of exposing them.
    """

    STRING = "string"
    BOOL = "bool"
    INT = "int"
    ENUM = "enum"
    STRING_LIST = "string_list"
    ARTIFACT_REF = "artifact_ref"
    SECRET_REF = "secret_ref"
    ATTRIBUTE_LIST = "attribute_list"
    KEY_SCHEMA = "key_schema"


class BindingOrigin(str, Enum):
    """Where a bound module input's value came from.

    ``DERIVED`` and ``REQUIRED_INPUT`` mirror the frozen 4A
    ``ParameterSource`` values exactly so provenance stays comparable
    across the 4A -> 4C boundary without modifying 4A.
    """

    DERIVED = "derived"
    REQUIRED_INPUT = "required_input"
    MODULE_DEFAULT = "module_default"


class DerivationKind(str, Enum):
    """Closed set of deterministic projections from frozen 4A data.

    ``RESOURCE_ID`` through ``RANGE_KEY_TYPE`` project one resource's own
    frozen parameters. The remaining kinds are cross-resource wiring
    derivations introduced when the catalog was reconciled with the real
    module variables: their value is the *sibling resource identity* from
    the same InfraSpec (never HCL), and the invocation renderer resolves
    that identity to the Terraform reference, refusing ambiguous
    topologies exactly as the raw renderer does today.
    """

    RESOURCE_ID = "resource_id"
    HASH_KEY_NAME = "hash_key_name"
    RANGE_KEY_NAME = "range_key_name"
    HASH_KEY_TYPE = "hash_key_type"
    RANGE_KEY_TYPE = "range_key_type"
    # Platform naming policy for a managed log group, anchored on the
    # stable resource identity (the same policy the raw renderer emits).
    LOG_GROUP_NAME = "log_group_name"
    # Exactly-one sibling wiring: the single log group / single Lambda
    # the spec carries. Zero or multiple siblings is a diagnostic, never
    # a guess.
    LOG_GROUP_ARN = "log_group_arn"
    LAMBDA_ARN = "lambda_arn"
    LAMBDA_INVOKE_ARN = "lambda_invoke_arn"
    # Object-storage wiring: the single S3 bucket identity the spec
    # carries. Exactly-one sibling; resolved to the bucket's declared
    # module outputs at invocation time.
    S3_BUCKET_ID = "s3_bucket_id"
    S3_BUCKET_ARN = "s3_bucket_arn"
    S3_BUCKET_DOMAIN_NAME = "s3_bucket_domain_name"
    # Queue wiring: the single SQS queue identity the spec carries.
    # Exactly-one sibling; resolved to the queue's declared outputs.
    SQS_QUEUE_ARN = "sqs_queue_arn"
    SQS_QUEUE_URL = "sqs_queue_url"
    # Alarm wiring: the single API Gateway / single Lambda identity the
    # alarms monitor. Zero or multiple siblings is a diagnostic, never a
    # guess. The value is the sibling resource identity; the invocation
    # renderer resolves it to the module output reference.
    API_GATEWAY_API_ID = "api_gateway_api_id"
    LAMBDA_FUNCTION_NAME = "lambda_function_name"
    # Table wiring: every table sibling (spec order), or absent so the
    # module default applies when the function uses no table.
    TABLE_ARNS = "table_arns"
    TABLE_NAME = "table_name"
    # Named static access-log format policy. The catalog allowlist holds
    # exactly one policy name; the invocation renderer resolves the name
    # to the exact format string. Free-form format text never enters the
    # selection document.
    ACCESS_LOG_FORMAT = "access_log_format"


class ParameterConstraints(BaseModel):
    """Closed validation constraints for one parameter.

    Anything not expressible here is not representable at all: there is no
    general HCL/JSON/YAML escape hatch.
    """

    allowed_values: frozenset[str] = frozenset()
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    pattern: Optional[str] = None
    # Structured types: fixed token grammar, declared not guessed.
    token_pattern: Optional[str] = None
    separator: str = ","
    prefix_pattern: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class AttributeSpec(BaseModel):
    """One attribute declaration: name plus scalar type."""

    name: str
    type: str

    model_config = ConfigDict(extra="forbid")


class KeySchemaSpec(BaseModel):
    """Hash key with an optional range key."""

    hash_key: str
    range_key: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class TypedValue(BaseModel):
    """Base of the discriminated typed-value union."""

    type: ParameterType

    model_config = ConfigDict(extra="forbid", frozen=True)


class TypedString(TypedValue):
    type: Literal[ParameterType.STRING] = ParameterType.STRING
    value: str


class TypedBool(TypedValue):
    type: Literal[ParameterType.BOOL] = ParameterType.BOOL
    value: bool


class TypedInt(TypedValue):
    type: Literal[ParameterType.INT] = ParameterType.INT
    value: int


class TypedEnum(TypedValue):
    type: Literal[ParameterType.ENUM] = ParameterType.ENUM
    value: str


class TypedStringList(TypedValue):
    type: Literal[ParameterType.STRING_LIST] = ParameterType.STRING_LIST
    value: tuple[str, ...]


class TypedArtifactRef(TypedValue):
    """Opaque reference to an externally produced artifact.

    Never inline content. Required so compute can be represented without
    the platform authoring application code.
    """

    type: Literal[ParameterType.ARTIFACT_REF] = ParameterType.ARTIFACT_REF
    value: str


class TypedSecretRef(TypedValue):
    """Reference to secret material held outside the configuration."""

    type: Literal[ParameterType.SECRET_REF] = ParameterType.SECRET_REF
    value: str


class TypedAttributeList(TypedValue):
    type: Literal[ParameterType.ATTRIBUTE_LIST] = ParameterType.ATTRIBUTE_LIST
    value: tuple[AttributeSpec, ...]


class TypedKeySchema(TypedValue):
    type: Literal[ParameterType.KEY_SCHEMA] = ParameterType.KEY_SCHEMA
    value: KeySchemaSpec


TypedValueUnion = Annotated[
    Union[
        TypedString,
        TypedBool,
        TypedInt,
        TypedEnum,
        TypedStringList,
        TypedArtifactRef,
        TypedSecretRef,
        TypedAttributeList,
        TypedKeySchema,
    ],
    Field(discriminator="type"),
]


class ParameterBindingSpec(BaseModel):
    """Declares where a module input's value comes from.

    This is what makes selection a pure function of ``InfraSpec`` rather
    than of a model. There is no "ask the LLM what this variable should be"
    path: every input is bound by one of these three declarations.
    """

    parameter_name: Optional[str] = None
    resource_id: bool = False
    derivation: Optional[DerivationKind] = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "ParameterBindingSpec":
        declared = sum(
            [
                self.parameter_name is not None,
                bool(self.resource_id),
                self.derivation is not None,
            ]
        )
        if declared != 1:
            raise ValueError(
                "a module input must declare exactly one binding source "
                "(parameter_name, resource_id or derivation)"
            )
        return self


class ModuleInputDefinition(BaseModel):
    """One declared module variable.

    ``name`` is the exact Terraform variable name inside the module, so the
    future renderer never has to guess a name and the guard can verify that
    emitted arguments are a subset of this declaration.
    """

    name: str
    type: ParameterType
    required: bool
    binding: ParameterBindingSpec
    constraints: ParameterConstraints = Field(
        default_factory=ParameterConstraints
    )
    default: Optional[TypedValueUnion] = None
    description: str
    is_secret: bool = False

    model_config = ConfigDict(extra="forbid")


class ModuleOutputReference(BaseModel):
    """One declared module output."""

    name: str
    type: ParameterType
    description: str

    model_config = ConfigDict(extra="forbid")


class VerificationMetadata(BaseModel):
    """The evidence behind a "verified" claim.

    Presence in a public registry is explicitly not sufficient: this record
    is what makes the claim auditable and revocable.
    """

    status: VerificationStatus
    verified_by: str
    verified_on: date
    methods: frozenset[VerificationMethod] = frozenset()
    catalog_revision: str
    evidence_ref: Optional[str] = None
    expires_on: Optional[date] = None
    limitations: tuple[str, ...] = ()

    model_config = ConfigDict(extra="forbid", frozen=True)


class ModuleContract(BaseModel):
    """A verified, parameterized module in the trusted catalog."""

    module_id: ModuleId
    provider: str
    source: str
    source_kind: SourceKind = SourceKind.LOCAL_PATH
    # sha256 over the module's .tf files; the MVP pinning mechanism.
    source_digest: str
    version: str
    description: str
    capability_tags: frozenset[str] = frozenset()
    inputs: tuple[ModuleInputDefinition, ...]
    outputs: tuple[ModuleOutputReference, ...]
    verification: VerificationMetadata
    contract_version: str

    model_config = ConfigDict(extra="forbid", frozen=True)

    @property
    def required_input_names(self) -> tuple:
        return tuple(
            sorted(i.name for i in self.inputs if i.required)
        )

    @property
    def defaultable_input_names(self) -> tuple:
        return tuple(
            sorted(i.name for i in self.inputs if not i.required)
        )

    @property
    def bound_parameter_names(self) -> tuple:
        """4A parameter names this contract consumes."""
        return tuple(
            sorted(
                i.binding.parameter_name
                for i in self.inputs
                if i.binding.parameter_name
            )
        )

    @property
    def selectable(self) -> bool:
        return self.verification.status is VerificationStatus.VERIFIED


class ModuleInputBinding(BaseModel):
    """A validated, typed module input ready for invocation."""

    input_name: str
    value: TypedValueUnion
    origin: BindingOrigin
    from_parameter: Optional[str] = None

    model_config = ConfigDict(extra="forbid", frozen=True)


class ModuleInstance(BaseModel):
    """One verified module realising one InfraSpec resource."""

    instance_id: str
    resource_id: str
    module_id: ModuleId
    # Always copied from the catalog; never from any proposal.
    source: str
    source_kind: SourceKind
    source_digest: str
    version: str
    bindings: tuple[ModuleInputBinding, ...]

    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceSpecRef(BaseModel):
    """Full provenance chain back to the approved architecture."""

    spec_id: str
    spec_schema_version: str
    artifact_id: str
    architecture_hash: str
    upstream_inputs_hash: str

    model_config = ConfigDict(extra="forbid", frozen=True)


class ModuleDiagnosticCategory(str, Enum):
    UNAVAILABLE = "unavailable"
    NOT_VERIFIED = "not_verified"
    INVALID_PARAMETER = "invalid_parameter"
    UNMAPPED = "unmapped"
    CATALOG = "catalog"
    STRUCTURAL = "structural"


class ModuleErrorCode(str, Enum):
    MODULE_NOT_AVAILABLE = "module_not_available"
    MODULE_NOT_VERIFIED = "module_not_verified"
    MODULE_PROVIDER_INCOMPATIBLE = "module_provider_incompatible"
    MODULE_INPUT_UNKNOWN = "module_input_unknown"
    MODULE_INPUT_REQUIRED = "module_input_required"
    MODULE_INPUT_TYPE_INVALID = "module_input_type_invalid"
    MODULE_INPUT_OUT_OF_ALLOWLIST = "module_input_out_of_allowlist"
    MODULE_INPUT_INCONSISTENT = "module_input_inconsistent"
    MODULE_PARAMETER_UNMAPPED = "module_parameter_unmapped"
    MODULE_PARAMETER_UNREPRESENTABLE = "module_parameter_unrepresentable"
    MODULE_SOURCE_NOT_ALLOWED = "module_source_not_allowed"
    MODULE_VERSION_NOT_ALLOWED = "module_version_not_allowed"
    CATALOG_INVALID = "catalog_invalid"


class ModuleDiagnostic(BaseModel):
    """One deterministic finding from the registry or selection."""

    category: ModuleDiagnosticCategory
    code: ModuleErrorCode
    message: str
    resource_id: Optional[str] = None
    input_name: Optional[str] = None

    model_config = ConfigDict(extra="forbid", frozen=True)

    def __str__(self) -> str:
        return self.message


class ModuleSelection(BaseModel):
    """Deterministic verified-module composition for one InfraSpec.

    The terminal artifact of Slice 4C. A future renderer consumes exactly
    this plus the registry allowlist; it receives no Terraform text and no
    free-form input from anywhere.
    """

    schema_version: str = SCHEMA_VERSION
    selection_id: str
    provider: str
    region_code: str
    catalog_version: str
    catalog_digest: str
    source_spec: SourceSpecRef
    instances: tuple[ModuleInstance, ...] = ()
    diagnostics: tuple[ModuleDiagnostic, ...] = ()

    model_config = ConfigDict(extra="forbid", frozen=True)

    @property
    def selected(self) -> bool:
        """All-or-nothing: never partially populated."""
        return not self.diagnostics

    def instance_for(self, resource_id: str) -> Optional[ModuleInstance]:
        for instance in self.instances:
            if instance.resource_id == resource_id:
                return instance
        return None