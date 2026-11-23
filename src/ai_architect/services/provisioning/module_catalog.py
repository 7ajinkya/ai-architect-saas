"""The project-owned verified module catalog: Slice 4C's trust boundary.

This module is the authoritative statement of which Terraform modules this
platform is willing to invoke, at which exact version, with which inputs.

Two properties are load-bearing:

1. **``source`` and ``version`` are written here and nowhere else.** No
   selection path, agent proposal, InfraSpec parameter or user input can
   influence them. They are constructor arguments to a frozen
   ``ModuleContract``.
2. **MVP modules are project-owned, not public community modules.** Public
   availability is discovery, not trust: it says nothing about testing,
   licence obligations, transitive constraints or an upstream release
   cadence nobody here controls. Each module ships under ``infra/modules``
   and is pinned by a content digest computed over its ``.tf`` files.

``serverless_function`` graduated to VERIFIED for the bounded demo
path: the code artifact arrives as two human-supplied, contract-
validated inputs (basename + sha256) produced by the reproducible
builder in ``infra/lambda``. The platform still never authors
application code; ``infra/modules/aws-serverless-function/main.tf``
is the reviewed implementation the renderer mirrors.
"""

from datetime import date

from ai_architect.domain.provisioning.module import (
    MODULE_ROOT,
    DerivationKind,
    ModuleContract,
    ModuleId,
    ModuleInputDefinition,
    ModuleOutputReference,
    ParameterBindingSpec,
    ParameterConstraints,
    ParameterType,
    SourceKind,
    VerificationMetadata,
    VerificationMethod,
    VerificationStatus,
)

# Bumping this invalidates every content hash that includes it and is the
# single place a catalog-wide trust change is recorded.
CATALOG_VERSION = "module-catalog/1.3.0"

# Fixed review dates. These are recorded facts about when verification was
# performed, not runtime timestamps, so the catalog stays deterministic.
_VERIFIED_ON = date(2026, 10, 9)
# Slice 2 reconciled the catalog with the real module variables and
# re-proved the interface with the parity tests below. The date records
# that review; it is a fact, not a timestamp.
_REVERIFIED_ON = date(2026, 10, 18)
_REVIEWER = "ai-architect-platform-review"
_EVIDENCE = "tests/test_provisioning_module_contract.py"
_ALARM_VERIFIED_ON = date(2026, 10, 20)
_SERVERLESS_VERIFIED_ON = date(2026, 10, 20)

_S3_PARAMETERS = ParameterConstraints(
    # Bucket naming rule, anchored. Rejects anything that could carry HCL.
    pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$"
)

_VERSIONING_CONSTRAINTS = ParameterConstraints(
    allowed_values=frozenset({"enabled", "suspended"})
)

_ENCRYPTION_CONSTRAINTS = ParameterConstraints(
    allowed_values=frozenset({"aes256"})
)

_PUBLIC_ACCESS_CONSTRAINTS = ParameterConstraints(
    allowed_values=frozenset({"all_blocked", "none_blocked"})
)

_KEY_TYPE_CONSTRAINTS = ParameterConstraints(
    allowed_values=frozenset({"S", "N", "B"})
)

_BILLING_CONSTRAINTS = ParameterConstraints(
    # Only on-demand is exposed. Provisioned capacity would require
    # read/write capacity values the frozen InfraSpec does not carry, and
    # inventing them is forbidden. The single allowed value is the exact
    # provider value the module variable receives: no case folding or
    # lossy normalization is applied at the contract boundary.
    allowed_values=frozenset({"PAY_PER_REQUEST"})
)

_IDENTIFIER_CONSTRAINTS = ParameterConstraints(
    pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,62}$"
)

# DynamoDB table names follow the service's own documented limits
# (3-255 characters; letters, digits, underscore, hyphen, dot), not
# the generic 63-character identifier cap. The leading-letter rule is
# kept as a conservative, HCL-safe floor.
_TABLE_NAME_CONSTRAINTS = ParameterConstraints(
    pattern=r"^[A-Za-z][A-Za-z0-9_.-]{2,254}$"
)

_RETENTION_CONSTRAINTS = ParameterConstraints(
    allowed_values=frozenset({"7", "30", "90", "365"})
)

# Sibling resource identity for cross-resource wiring inputs. Mirrors the
# invocation renderer's safe identifier set (a leading alphanumeric, then
# up to 200 characters of letters, digits, underscore, hyphen or dot):
# the renderer maps the identity to the actual Terraform reference and
# fails closed on anything else.
_SIBLING_ID_CONSTRAINTS = ParameterConstraints(
    pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,200}$"
)

# Managed log-group name under the platform naming policy, anchored on
# the stable resource identity plus the optional deployment namespace
# (the raw renderer emits the same policy when the namespace is empty).
_LOG_GROUP_NAME_CONSTRAINTS = ParameterConstraints(
    pattern=r"^/aws/ai-architect/[A-Za-z0-9][A-Za-z0-9_.-]{0,200}$"
)

# Named static access-log format policy. Exactly one policy is exposed;
# the invocation renderer resolves the name to the exact format string,
# so no free-form format text can enter the selection document.
_ACCESS_LOG_FORMAT_CONSTRAINTS = ParameterConstraints(
    allowed_values=frozenset({"json_request_context_v1"})
)

_CODE_ARTIFACT_PATH_CONSTRAINTS = ParameterConstraints(
    # Basename only: the deployment bundle carries the file next to the
    # generated tree, so directory traversal has nowhere to hide.
    pattern=r"^[A-Za-z0-9._-]{1,64}\.zip$"
)

_CODE_ARTIFACT_SHA_CONSTRAINTS = ParameterConstraints(
    pattern=r"^[0-9a-f]{64}$"
)

_BOOL_CONSTRAINTS = ParameterConstraints(
    allowed_values=frozenset({"true", "enabled", "false", "disabled"})
)

_ALARM_NAME_PREFIX_CONSTRAINTS = ParameterConstraints(
    pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,200}$"
)

_ALARM_COUNT_THRESHOLD_CONSTRAINTS = ParameterConstraints(
    minimum=0,
    maximum=1_000_000,
)

_ALARM_API_LATENCY_THRESHOLD_CONSTRAINTS = ParameterConstraints(
    minimum=1,
    maximum=3_600_000,
)

_ALARM_LAMBDA_DURATION_THRESHOLD_CONSTRAINTS = ParameterConstraints(
    minimum=1,
    maximum=900_000,
)

_ALARM_PERIOD_CONSTRAINTS = ParameterConstraints(
    allowed_values=frozenset({"60", "300", "900"})
)

_ALARM_EVALUATION_PERIODS_CONSTRAINTS = ParameterConstraints(
    minimum=1,
    maximum=100,
)

_ALARM_COMPARISON_CONSTRAINTS = ParameterConstraints(
    allowed_values=frozenset(
        {"GreaterThanThreshold", "GreaterThanOrEqualToThreshold"}
    )
)

_ALARM_MISSING_DATA_CONSTRAINTS = ParameterConstraints(
    allowed_values=frozenset(
        {"breaching", "notBreaching", "ignore", "missing"}
    )
)

_DEFAULT_ROOT_OBJECT_CONSTRAINTS = ParameterConstraints(
    # Object key only: no scheme, query, fragment or traversal segment.
    pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
)

_PRICE_CLASS_CONSTRAINTS = ParameterConstraints(
    allowed_values=frozenset(
        {"PriceClass_100", "PriceClass_200", "PriceClass_All"}
    )
)

_PASSWORD_LENGTH_CONSTRAINTS = ParameterConstraints(
    minimum=8,
    maximum=99,
)

_MFA_CONSTRAINTS = ParameterConstraints(
    allowed_values=frozenset({"OFF", "OPTIONAL"})
)

_VISIBILITY_TIMEOUT_CONSTRAINTS = ParameterConstraints(
    allowed_values=frozenset({"30", "60", "300"})
)

_MESSAGE_RETENTION_CONSTRAINTS = ParameterConstraints(
    allowed_values=frozenset({"86400", "345600", "1209600"})
)

_ALARM_NOTIFICATION_CONSTRAINTS = ParameterConstraints(
    # "none" is the explicit no-notification choice. Any other value must
    # be an existing SNS topic ARN in the same partition/region/account;
    # the platform never creates an SNS topic or invents a destination.
    pattern=(
        r"^none$|^arn:aws[a-z-]*:sns:[a-z0-9-]+:[0-9]{12}:"
        r"[A-Za-z0-9_-]{1,256}$"
    )
)


OBJECT_STORAGE = ModuleContract(
    module_id=ModuleId.OBJECT_STORAGE,
    provider="aws",
    source=f"../../{MODULE_ROOT}/aws-object-storage",
    source_kind=SourceKind.LOCAL_PATH,
    # Placeholder replaced by catalog_digest_of() in the registry at load
    # time; the literal below is only a fallback for direct use.
    source_digest="unverified",
    version="1.0.0",
    description=(
        "Durable object storage with versioning, encryption at rest and "
        "public access blocking applied as properties of the capability."
    ),
    capability_tags=frozenset(
        {"durable", "versioned", "encrypted", "private"}
    ),
    inputs=(
        ModuleInputDefinition(
            name="bucket_name",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(parameter_name="bucket_name"),
            constraints=_S3_PARAMETERS,
            description="Globally unique bucket name.",
        ),
        ModuleInputDefinition(
            name="versioning_status",
            type=ParameterType.ENUM,
            required=True,
            binding=ParameterBindingSpec(parameter_name="versioning"),
            constraints=_VERSIONING_CONSTRAINTS,
            description="Object versioning posture.",
        ),
        ModuleInputDefinition(
            name="sse_algorithm",
            type=ParameterType.ENUM,
            required=True,
            binding=ParameterBindingSpec(parameter_name="encryption"),
            constraints=_ENCRYPTION_CONSTRAINTS,
            description="Server-side encryption algorithm.",
        ),
        ModuleInputDefinition(
            name="access_posture",
            type=ParameterType.ENUM,
            required=True,
            binding=ParameterBindingSpec(
                parameter_name="public_access_block"
            ),
            constraints=_PUBLIC_ACCESS_CONSTRAINTS,
            description=(
                "Named public-access posture. A named posture rather than a "
                "boolean, because the module is what decides which flags that "
                "implies; the caller never has to remember four of them."
            ),
        ),
    ),
    outputs=(
        ModuleOutputReference(
            name="bucket_id", type=ParameterType.STRING,
            description="Identifier of the bucket.",
        ),
        ModuleOutputReference(
            name="bucket_arn", type=ParameterType.STRING,
            description="ARN of the bucket.",
        ),
        ModuleOutputReference(
            name="regional_domain_name", type=ParameterType.STRING,
            description="Regional domain name of the bucket, for CloudFront origins.",
        ),
    ),
    verification=VerificationMetadata(
        status=VerificationStatus.VERIFIED,
        verified_by=_REVIEWER,
        verified_on=_VERIFIED_ON,
        methods=frozenset(
            {VerificationMethod.PINNED_REVISION, VerificationMethod.TESTED_CONTRACT}
        ),
        catalog_revision=CATALOG_VERSION,
        evidence_ref=_EVIDENCE,
        limitations=(
            "Versioning posture is enabled or suspended only; no lifecycle or archival policy.",
        ),
    ),
    contract_version="1.1.0",
)


NOSQL_TABLE = ModuleContract(
    module_id=ModuleId.NOSQL_TABLE,
    provider="aws",
    source=f"../../{MODULE_ROOT}/aws-nosql-table",
    source_kind=SourceKind.LOCAL_PATH,
    source_digest="unverified",
    version="1.0.0",
    description=(
        "Key-value table capability. Takes scalar key names and types and "
        "derives attribute declarations internally, so no caller ever "
        "expresses Terraform attribute blocks."
    ),
    capability_tags=frozenset({"key_value", "serverless", "on_demand"}),
    inputs=(
        ModuleInputDefinition(
            name="table_name",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(resource_id=True),
            constraints=_TABLE_NAME_CONSTRAINTS,
            description="Name of the table, derived from the resource identity.",
        ),
        ModuleInputDefinition(
            name="hash_key_name",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(
                derivation=DerivationKind.HASH_KEY_NAME
            ),
            constraints=_IDENTIFIER_CONSTRAINTS,
            description="Attribute name of the partition key.",
        ),
        ModuleInputDefinition(
            name="hash_key_type",
            type=ParameterType.ENUM,
            required=True,
            binding=ParameterBindingSpec(
                derivation=DerivationKind.HASH_KEY_TYPE
            ),
            constraints=_KEY_TYPE_CONSTRAINTS,
            description="Scalar type of the partition key.",
        ),
        ModuleInputDefinition(
            name="range_key_name",
            type=ParameterType.STRING,
            required=False,
            binding=ParameterBindingSpec(
                derivation=DerivationKind.RANGE_KEY_NAME
            ),
            constraints=_IDENTIFIER_CONSTRAINTS,
            description="Attribute name of the sort key, when present.",
        ),
        ModuleInputDefinition(
            name="range_key_type",
            type=ParameterType.ENUM,
            required=False,
            binding=ParameterBindingSpec(
                derivation=DerivationKind.RANGE_KEY_TYPE
            ),
            constraints=_KEY_TYPE_CONSTRAINTS,
            description="Scalar type of the sort key, when present.",
        ),
        ModuleInputDefinition(
            name="billing_mode",
            type=ParameterType.ENUM,
            required=True,
            binding=ParameterBindingSpec(parameter_name="billing_mode"),
            constraints=_BILLING_CONSTRAINTS,
            description="Capacity mode, in provider spelling (on-demand only).",
        ),
        ModuleInputDefinition(
            name="point_in_time_recovery",
            type=ParameterType.BOOL,
            required=True,
            binding=ParameterBindingSpec(
                parameter_name="point_in_time_recovery"
            ),
            constraints=_BOOL_CONSTRAINTS,
            description="Whether point-in-time recovery is enabled.",
        ),
    ),
    outputs=(
        ModuleOutputReference(
            name="table_id", type=ParameterType.STRING,
            description="Identifier of the table.",
        ),
        ModuleOutputReference(
            name="table_arn", type=ParameterType.STRING,
            description="ARN of the table.",
        ),
    ),
    verification=VerificationMetadata(
        status=VerificationStatus.VERIFIED,
        verified_by=_REVIEWER,
        verified_on=_REVERIFIED_ON,
        methods=frozenset(
            {VerificationMethod.PINNED_REVISION, VerificationMethod.TESTED_CONTRACT}
        ),
        catalog_revision=CATALOG_VERSION,
        evidence_ref=_EVIDENCE,
        limitations=(
            "On-demand billing only (PAY_PER_REQUEST); provisioned capacity "
            "would require read/write capacity values the frozen InfraSpec "
            "does not carry.",
            "No TTL, global secondary indexes, or streams are exposed.",
        ),
    ),
    contract_version="1.1.0",
)


SERVERLESS_FUNCTION = ModuleContract(
    module_id=ModuleId.SERVERLESS_FUNCTION,
    provider="aws",
    source=f"../../{MODULE_ROOT}/aws-serverless-function",
    source_kind=SourceKind.LOCAL_PATH,
    source_digest="unverified",
    version="1.0.0",
    description=(
        "Event-driven compute capability. The platform does not author "
        "application code: the deployment package is externally "
        "produced (see infra/lambda/build_demo_artifact.py) and "
        "referenced by basename plus sha256, both human-supplied and "
        "contract-validated. The renderer verifies the hash shape "
        "without reading the operator's filesystem."
    ),
    capability_tags=frozenset({"compute", "event_driven"}),
    inputs=(
        ModuleInputDefinition(
            name="function_name",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(resource_id=True),
            constraints=_IDENTIFIER_CONSTRAINTS,
            description="Name of the function.",
        ),
        ModuleInputDefinition(
            name="code_artifact_path",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(parameter_name="code_artifact_path"),
            constraints=_CODE_ARTIFACT_PATH_CONSTRAINTS,
            description=(
                "Basename of the deployment zip carried next to the "
                "generated tree. Never a directory, never inline code."
            ),
        ),
        ModuleInputDefinition(
            name="code_artifact_sha256",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(parameter_name="code_artifact_sha256"),
            constraints=_CODE_ARTIFACT_SHA_CONSTRAINTS,
            description=(
                "Lowercase hex sha256 of the deployment zip. Rendered "
                "as source_code_hash (base64); verified by the "
                "executor against the actual file before apply."
            ),
        ),
        ModuleInputDefinition(
            name="runtime",
            type=ParameterType.ENUM,
            required=True,
            binding=ParameterBindingSpec(parameter_name="runtime"),
            constraints=ParameterConstraints(
                allowed_values=frozenset({"python3.12", "nodejs20.x"})
            ),
            description="Lambda runtime identifier.",
        ),
        ModuleInputDefinition(
            name="handler",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(parameter_name="handler"),
            constraints=ParameterConstraints(max_length=128),
            description="Entry point within the artifact.",
        ),
        ModuleInputDefinition(
            name="memory_mb",
            type=ParameterType.INT,
            required=True,
            binding=ParameterBindingSpec(parameter_name="memory_mb"),
            constraints=ParameterConstraints(minimum=128, maximum=10240),
            description="Memory allocated to the function, in megabytes.",
        ),
        ModuleInputDefinition(
            name="timeout_seconds",
            type=ParameterType.INT,
            required=True,
            binding=ParameterBindingSpec(parameter_name="timeout_seconds"),
            constraints=ParameterConstraints(minimum=1, maximum=900),
            description="Maximum execution duration, in seconds.",
        ),
        ModuleInputDefinition(
            name="log_group_arn",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(
                derivation=DerivationKind.LOG_GROUP_ARN
            ),
            constraints=_SIBLING_ID_CONSTRAINTS,
            description=(
                "Resource identity of the single CloudWatch log group in "
                "the same spec; the invocation renderer resolves it to the "
                "group ARN reference used by the least-privilege policy."
            ),
        ),
        ModuleInputDefinition(
            name="table_arns",
            type=ParameterType.STRING_LIST,
            required=False,
            binding=ParameterBindingSpec(
                derivation=DerivationKind.TABLE_ARNS
            ),
            constraints=_SIBLING_ID_CONSTRAINTS,
            description=(
                "Resource identities of the DynamoDB tables in the same "
                "spec, resolved to ARN references at invocation. Absent "
                "when the function uses no table; the module default "
                "applies."
            ),
        ),
        ModuleInputDefinition(
            name="table_name",
            type=ParameterType.STRING,
            required=False,
            binding=ParameterBindingSpec(
                derivation=DerivationKind.TABLE_NAME
            ),
            constraints=_SIBLING_ID_CONSTRAINTS,
            description=(
                "Resource identity of the first table in the spec, exposed "
                "as the function TABLE_NAME environment value. Absent when "
                "the function uses no table."
            ),
        ),
    ),
    outputs=(
        ModuleOutputReference(
            name="function_arn", type=ParameterType.STRING,
            description="ARN of the function.",
        ),
        ModuleOutputReference(
            name="function_name", type=ParameterType.STRING,
            description="Name of the function.",
        ),
        ModuleOutputReference(
            name="function_invoke_arn", type=ParameterType.STRING,
            description="Invoke ARN of the function, for API Gateway integration.",
        ),
    ),
    verification=VerificationMetadata(
        status=VerificationStatus.VERIFIED,
        verified_by=_REVIEWER,
        verified_on=_REVERIFIED_ON,
        methods=frozenset(
            {VerificationMethod.PINNED_REVISION, VerificationMethod.TESTED_CONTRACT}
        ),
        catalog_revision=CATALOG_VERSION,
        evidence_ref=_EVIDENCE,
        limitations=(
            "Zip packages only; no container images.",
            "No VPC attachment, no provisioned concurrency.",
            "Execution role is least-privilege for CloudWatch Logs and "
            "the DynamoDB tables in the same spec only.",
            "The code artifact itself is operator-supplied; the platform "
            "verifies its hash shape at render and its bytes at apply.",
            "log_group_arn, table_arns and table_name bind to sibling "
            "resources of the same spec and are resolved to Terraform "
            "references at invocation; ambiguous topologies are refused.",
        ),
    ),
    contract_version="1.1.1",
)


HTTP_API = ModuleContract(
    module_id=ModuleId.HTTP_API,
    provider="aws",
    source=f"../../{MODULE_ROOT}/aws-http-api",
    source_kind=SourceKind.LOCAL_PATH,
    source_digest="unverified",
    version="1.0.0",
    description=(
        "Managed HTTP entry point (API Gateway HTTP API) with a default "
        "stage, access logging, one Lambda proxy integration and one "
        "$default route. Names derive from stable resource identities; "
        "the integration target is the single Lambda in the same spec."
    ),
    capability_tags=frozenset({"entry_point", "managed", "http"}),
    inputs=(
        ModuleInputDefinition(
            name="api_name",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(resource_id=True),
            constraints=_IDENTIFIER_CONSTRAINTS,
            description="Name of the API, derived from the resource identity.",
        ),
        ModuleInputDefinition(
            name="lambda_invoke_arn",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(
                derivation=DerivationKind.LAMBDA_INVOKE_ARN
            ),
            constraints=_SIBLING_ID_CONSTRAINTS,
            description=(
                "Resource identity of the single Lambda in the same spec; "
                "resolved to its invoke ARN reference at invocation."
            ),
        ),
        ModuleInputDefinition(
            name="lambda_arn",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(
                derivation=DerivationKind.LAMBDA_ARN
            ),
            constraints=_SIBLING_ID_CONSTRAINTS,
            description=(
                "Resource identity of the single Lambda in the same spec; "
                "resolved to its ARN reference for the invoke permission."
            ),
        ),
        ModuleInputDefinition(
            name="log_group_arn",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(
                derivation=DerivationKind.LOG_GROUP_ARN
            ),
            constraints=_SIBLING_ID_CONSTRAINTS,
            description=(
                "Resource identity of the single log group in the same "
                "spec; resolved to its ARN reference for access logging."
            ),
        ),
        ModuleInputDefinition(
            name="access_log_format",
            type=ParameterType.ENUM,
            required=True,
            binding=ParameterBindingSpec(
                derivation=DerivationKind.ACCESS_LOG_FORMAT
            ),
            constraints=_ACCESS_LOG_FORMAT_CONSTRAINTS,
            description=(
                "Named static access-log format policy; the invocation "
                "renderer resolves the name to the exact format string. "
                "Free-form format text never enters the selection."
            ),
        ),
    ),
    outputs=(
        ModuleOutputReference(
            name="api_id", type=ParameterType.STRING,
            description="API identifier used by metric-alarm dimensions.",
        ),
        ModuleOutputReference(
            name="api_endpoint", type=ParameterType.STRING,
            description="Invoke URL of the default stage.",
        ),
        ModuleOutputReference(
            name="api_execution_arn", type=ParameterType.STRING,
            description="Execution ARN constraining invoke permission.",
        ),
    ),
    verification=VerificationMetadata(
        status=VerificationStatus.VERIFIED,
        verified_by=_REVIEWER,
        verified_on=_REVERIFIED_ON,
        methods=frozenset(
            {VerificationMethod.PINNED_REVISION, VerificationMethod.TESTED_CONTRACT}
        ),
        catalog_revision=CATALOG_VERSION,
        evidence_ref=_EVIDENCE,
        limitations=(
            "Exactly one Lambda integration per spec; multi-function "
            "routing needs an explicit routing decision.",
            "Exactly one log group in the spec receives access logs.",
            "No custom domain, authorizers, or usage plans exposed.",
            "The access-log format is a named platform policy resolved at "
            "invocation; no user-supplied format text is accepted.",
        ),
    ),
    contract_version="1.1.0",
)


CLOUDFRONT_DISTRIBUTION = ModuleContract(
    module_id=ModuleId.CLOUDFRONT_DISTRIBUTION,
    provider="aws",
    source=f"../../{MODULE_ROOT}/aws-cloudfront-distribution",
    source_kind=SourceKind.LOCAL_PATH,
    source_digest="unverified",
    version="1.0.0",
    description=(
        "Managed edge distribution for one private S3 origin. The module "
        "creates an origin access control, the distribution and the "
        "bucket policy scoped to this distribution's ARN; the bucket "
        "itself stays private."
    ),
    capability_tags=frozenset({"edge", "cdn", "entry_point", "https"}),
    inputs=(
        ModuleInputDefinition(
            name="distribution_name",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(resource_id=True),
            constraints=_IDENTIFIER_CONSTRAINTS,
            description=(
                "Name of the distribution and its origin access control, "
                "derived from the resource identity."
            ),
        ),
        ModuleInputDefinition(
            name="origin_bucket_id",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(
                derivation=DerivationKind.S3_BUCKET_ID
            ),
            constraints=_SIBLING_ID_CONSTRAINTS,
            description=(
                "Resource identity of the single S3 bucket in the same "
                "spec; resolved to its bucket-id output for the policy."
            ),
        ),
        ModuleInputDefinition(
            name="origin_bucket_arn",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(
                derivation=DerivationKind.S3_BUCKET_ARN
            ),
            constraints=_SIBLING_ID_CONSTRAINTS,
            description=(
                "Resource identity of the single S3 bucket in the same "
                "spec; resolved to its ARN output for the policy."
            ),
        ),
        ModuleInputDefinition(
            name="origin_bucket_domain_name",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(
                derivation=DerivationKind.S3_BUCKET_DOMAIN_NAME
            ),
            constraints=_SIBLING_ID_CONSTRAINTS,
            description=(
                "Resource identity of the single S3 bucket in the same "
                "spec; resolved to its regional domain name for the origin."
            ),
        ),
        ModuleInputDefinition(
            name="default_root_object",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(
                parameter_name="default_root_object"
            ),
            constraints=_DEFAULT_ROOT_OBJECT_CONSTRAINTS,
            description="Object returned for the root path.",
        ),
        ModuleInputDefinition(
            name="price_class",
            type=ParameterType.ENUM,
            required=True,
            binding=ParameterBindingSpec(parameter_name="price_class"),
            constraints=_PRICE_CLASS_CONSTRAINTS,
            description="CloudFront price class, a cost decision.",
        ),
    ),
    outputs=(
        ModuleOutputReference(
            name="distribution_id", type=ParameterType.STRING,
            description="Identifier of the distribution.",
        ),
        ModuleOutputReference(
            name="distribution_domain_name", type=ParameterType.STRING,
            description="Domain name assigned to the distribution.",
        ),
        ModuleOutputReference(
            name="distribution_arn", type=ParameterType.STRING,
            description="ARN of the distribution.",
        ),
    ),
    verification=VerificationMetadata(
        status=VerificationStatus.VERIFIED,
        verified_by=_REVIEWER,
        verified_on=_SERVERLESS_VERIFIED_ON,
        methods=frozenset(
            {VerificationMethod.PINNED_REVISION, VerificationMethod.TESTED_CONTRACT}
        ),
        catalog_revision=CATALOG_VERSION,
        evidence_ref="tests/test_provisioning_serverless_modules.py",
        limitations=(
            "Exactly one S3 origin per spec; the bucket policy created by "
            "the module grants read only to this distribution's ARN.",
            "Fixed HTTPS redirect, GET/HEAD methods, managed "
            "CachingOptimized cache policy and the CloudFront default "
            "certificate; no custom domain, WAF, functions or Lambda@Edge.",
            "No invalidation or cache-behavior customization is exposed.",
        ),
    ),
    contract_version="1.0.0",
)


COGNITO_USER_POOL = ModuleContract(
    module_id=ModuleId.COGNITO_USER_POOL,
    provider="aws",
    source=f"../../{MODULE_ROOT}/aws-cognito-user-pool",
    source_kind=SourceKind.LOCAL_PATH,
    source_digest="unverified",
    version="1.0.0",
    description=(
        "Cognito user pool with email-based sign-in and one public "
        "application client. Password policy, MFA posture and self-sign-up "
        "are human security decisions; the module applies them as a "
        "capability rather than exposing raw resource blocks."
    ),
    capability_tags=frozenset({"identity", "authentication", "security"}),
    inputs=(
        ModuleInputDefinition(
            name="user_pool_name",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(resource_id=True),
            constraints=_IDENTIFIER_CONSTRAINTS,
            description="Name of the user pool, derived from the resource identity.",
        ),
        ModuleInputDefinition(
            name="policy_minimum_length",
            type=ParameterType.INT,
            required=True,
            binding=ParameterBindingSpec(
                parameter_name="policy_minimum_length"
            ),
            constraints=_PASSWORD_LENGTH_CONSTRAINTS,
            description="Minimum password length enforced by the pool.",
        ),
        ModuleInputDefinition(
            name="mfa_configuration",
            type=ParameterType.ENUM,
            required=True,
            binding=ParameterBindingSpec(
                parameter_name="mfa_configuration"
            ),
            constraints=_MFA_CONSTRAINTS,
            description=(
                "MFA posture. ON is not exposed because it requires an "
                "enrolled second factor the platform cannot provide."
            ),
        ),
        ModuleInputDefinition(
            name="self_sign_up_enabled",
            type=ParameterType.BOOL,
            required=True,
            binding=ParameterBindingSpec(
                parameter_name="self_sign_up_enabled"
            ),
            constraints=_BOOL_CONSTRAINTS,
            description="Whether users may create their own accounts.",
        ),
    ),
    outputs=(
        ModuleOutputReference(
            name="user_pool_id", type=ParameterType.STRING,
            description="Identifier of the user pool.",
        ),
        ModuleOutputReference(
            name="user_pool_arn", type=ParameterType.STRING,
            description="ARN of the user pool.",
        ),
        ModuleOutputReference(
            name="user_pool_endpoint", type=ParameterType.STRING,
            description="Endpoint of the user pool.",
        ),
        ModuleOutputReference(
            name="client_id", type=ParameterType.STRING,
            description="Identifier of the public application client.",
        ),
        ModuleOutputReference(
            name="issuer_url", type=ParameterType.STRING,
            description="OIDC issuer URL of the user pool.",
        ),
    ),
    verification=VerificationMetadata(
        status=VerificationStatus.VERIFIED,
        verified_by=_REVIEWER,
        verified_on=_SERVERLESS_VERIFIED_ON,
        methods=frozenset(
            {VerificationMethod.PINNED_REVISION, VerificationMethod.TESTED_CONTRACT}
        ),
        catalog_revision=CATALOG_VERSION,
        evidence_ref="tests/test_provisioning_serverless_modules.py",
        limitations=(
            "Standalone pool: it is not wired as an authorizer on the HTTP "
            "API in this pass, so application routes are not protected by "
            "this module's outputs.",
            "Email username attribute and auto-verification are fixed; no "
            "phone, social/SAML federation, hosted UI domain or callback "
            "URLs are exposed.",
            "Only OFF and OPTIONAL MFA postures are exposed; no MFA "
            "methods are configured.",
            "The app client is public (no secret) with SRP and refresh "
            "flows only.",
        ),
    ),
    contract_version="1.0.0",
)


SQS_QUEUE = ModuleContract(
    module_id=ModuleId.SQS_QUEUE,
    provider="aws",
    source=f"../../{MODULE_ROOT}/aws-sqs-queue",
    source_kind=SourceKind.LOCAL_PATH,
    source_digest="unverified",
    version="1.0.0",
    description=(
        "Standard SQS queue with server-side encryption managed by SQS. "
        "Retention and visibility timeout are human decisions from closed "
        "allowlists."
    ),
    capability_tags=frozenset({"queue", "decoupling", "buffer"}),
    inputs=(
        ModuleInputDefinition(
            name="queue_name",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(resource_id=True),
            constraints=_IDENTIFIER_CONSTRAINTS,
            description="Name of the queue, derived from the resource identity.",
        ),
        ModuleInputDefinition(
            name="visibility_timeout_seconds",
            type=ParameterType.ENUM,
            required=True,
            binding=ParameterBindingSpec(
                parameter_name="visibility_timeout_seconds"
            ),
            constraints=_VISIBILITY_TIMEOUT_CONSTRAINTS,
            description="Visibility timeout applied to received messages.",
        ),
        ModuleInputDefinition(
            name="message_retention_seconds",
            type=ParameterType.ENUM,
            required=True,
            binding=ParameterBindingSpec(
                parameter_name="message_retention_seconds"
            ),
            constraints=_MESSAGE_RETENTION_CONSTRAINTS,
            description="How long the queue retains a message.",
        ),
    ),
    outputs=(
        ModuleOutputReference(
            name="queue_arn", type=ParameterType.STRING,
            description="ARN of the queue.",
        ),
        ModuleOutputReference(
            name="queue_url", type=ParameterType.STRING,
            description="URL of the queue.",
        ),
        ModuleOutputReference(
            name="queue_name", type=ParameterType.STRING,
            description="Name of the queue.",
        ),
    ),
    verification=VerificationMetadata(
        status=VerificationStatus.VERIFIED,
        verified_by=_REVIEWER,
        verified_on=_SERVERLESS_VERIFIED_ON,
        methods=frozenset(
            {VerificationMethod.PINNED_REVISION, VerificationMethod.TESTED_CONTRACT}
        ),
        catalog_revision=CATALOG_VERSION,
        evidence_ref="tests/test_provisioning_serverless_modules.py",
        limitations=(
            "Standard queues only; FIFO is refused at name resolution.",
            "No dead-letter queue, delay queue, or redrive policy is exposed.",
            "No Lambda event-source mapping is created; consumers are an "
            "application decision outside this module.",
            "SSE-SQS is fixed; no KMS key is accepted.",
        ),
    ),
    contract_version="1.0.0",
)


SNS_TOPIC = ModuleContract(
    module_id=ModuleId.SNS_TOPIC,
    provider="aws",
    source=f"../../{MODULE_ROOT}/aws-sns-topic",
    source_kind=SourceKind.LOCAL_PATH,
    source_digest="unverified",
    version="1.0.0",
    description=(
        "SNS topic with exactly one SQS subscriber. The module also "
        "creates the queue policy that lets this topic deliver messages "
        "to the subscriber, scoped by the topic ARN."
    ),
    capability_tags=frozenset({"pub_sub", "fanout", "notification"}),
    inputs=(
        ModuleInputDefinition(
            name="topic_name",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(resource_id=True),
            constraints=_IDENTIFIER_CONSTRAINTS,
            description="Name of the topic, derived from the resource identity.",
        ),
        ModuleInputDefinition(
            name="subscriber_queue_arn",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(
                derivation=DerivationKind.SQS_QUEUE_ARN
            ),
            constraints=_SIBLING_ID_CONSTRAINTS,
            description=(
                "Resource identity of the single SQS queue in the same "
                "spec; resolved to its ARN for the subscription."
            ),
        ),
        ModuleInputDefinition(
            name="subscriber_queue_url",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(
                derivation=DerivationKind.SQS_QUEUE_URL
            ),
            constraints=_SIBLING_ID_CONSTRAINTS,
            description=(
                "Resource identity of the single SQS queue in the same "
                "spec; resolved to its URL for the queue policy."
            ),
        ),
    ),
    outputs=(
        ModuleOutputReference(
            name="topic_arn", type=ParameterType.STRING,
            description="ARN of the topic.",
        ),
        ModuleOutputReference(
            name="topic_name", type=ParameterType.STRING,
            description="Name of the topic.",
        ),
    ),
    verification=VerificationMetadata(
        status=VerificationStatus.VERIFIED,
        verified_by=_REVIEWER,
        verified_on=_SERVERLESS_VERIFIED_ON,
        methods=frozenset(
            {VerificationMethod.PINNED_REVISION, VerificationMethod.TESTED_CONTRACT}
        ),
        catalog_revision=CATALOG_VERSION,
        evidence_ref="tests/test_provisioning_serverless_modules.py",
        limitations=(
            "Exactly one SQS subscriber per spec; no email, SMS, mobile "
            "push, HTTP or Lambda subscriptions.",
            "Standard topics only; FIFO topics are refused at name resolution.",
            "Raw message delivery is fixed to true.",
            "No topic policy or KMS encryption is exposed; the only queue "
            "policy created is the scoped SNS-to-SQS delivery grant.",
        ),
    ),
    contract_version="1.0.0",
)


LOG_GROUP = ModuleContract(
    module_id=ModuleId.LOG_GROUP,
    provider="aws",
    source=f"../../{MODULE_ROOT}/aws-log-group",
    source_kind=SourceKind.LOCAL_PATH,
    source_digest="unverified",
    version="1.0.0",
    description=(
        "Centralized log retention for one group. The group name "
        "derives from the stable resource identity; retention is a "
        "human cost decision from a closed allowlist."
    ),
    capability_tags=frozenset({"observability", "logging", "retention"}),
    inputs=(
        ModuleInputDefinition(
            name="log_group_name",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(
                derivation=DerivationKind.LOG_GROUP_NAME
            ),
            constraints=_LOG_GROUP_NAME_CONSTRAINTS,
            description=(
                "Name of the log group, derived by the platform naming "
                "policy from the stable resource identity."
            ),
        ),
        ModuleInputDefinition(
            name="retention_days",
            type=ParameterType.ENUM,
            required=True,
            binding=ParameterBindingSpec(parameter_name="retention_days"),
            constraints=_RETENTION_CONSTRAINTS,
            description="Log retention in days.",
        ),
    ),
    outputs=(
        ModuleOutputReference(
            name="log_group_arn", type=ParameterType.STRING,
            description="ARN of the log group.",
        ),
    ),
    verification=VerificationMetadata(
        status=VerificationStatus.VERIFIED,
        verified_by=_REVIEWER,
        verified_on=_REVERIFIED_ON,
        methods=frozenset(
            {VerificationMethod.PINNED_REVISION, VerificationMethod.TESTED_CONTRACT}
        ),
        catalog_revision=CATALOG_VERSION,
        evidence_ref=_EVIDENCE,
        limitations=(
            "Retention only from the closed allowlist.",
            "No metric filters, alarms, or subscription filters exposed.",
        ),
    ),
    contract_version="1.1.0",
)


CLOUDWATCH_ALARMS = ModuleContract(
    module_id=ModuleId.CLOUDWATCH_ALARMS,
    provider="aws",
    source=f"../../{MODULE_ROOT}/aws-cloudwatch-alarm",
    source_kind=SourceKind.LOCAL_PATH,
    source_digest="unverified",
    version="1.0.0",
    description=(
        "Four CloudWatch metric alarms for the single HTTP API and single "
        "Lambda in the same spec: API Gateway 5XX errors and p99 latency, "
        "Lambda errors and p99 duration. Thresholds, evaluation policy, "
        "comparison and notification are explicit human decisions; the "
        "platform supplies only deterministic dimensions and names."
    ),
    capability_tags=frozenset(
        {"observability", "monitoring", "alarm", "metrics"}
    ),
    inputs=(
        ModuleInputDefinition(
            name="alarm_name_prefix",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(resource_id=True),
            constraints=_ALARM_NAME_PREFIX_CONSTRAINTS,
            description=(
                "Stable prefix for all four alarm names, derived from the "
                "resource identity."
            ),
        ),
        ModuleInputDefinition(
            name="api_id",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(
                derivation=DerivationKind.API_GATEWAY_API_ID
            ),
            constraints=_SIBLING_ID_CONSTRAINTS,
            description=(
                "Resource identity of the single HTTP API in the same spec; "
                "resolved to its API identifier output at invocation."
            ),
        ),
        ModuleInputDefinition(
            name="function_name",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(
                derivation=DerivationKind.LAMBDA_FUNCTION_NAME
            ),
            constraints=_SIBLING_ID_CONSTRAINTS,
            description=(
                "Resource identity of the single Lambda in the same spec; "
                "resolved to its function-name output at invocation."
            ),
        ),
        ModuleInputDefinition(
            name="api_5xx_error_threshold",
            type=ParameterType.INT,
            required=True,
            binding=ParameterBindingSpec(
                parameter_name="api_5xx_error_threshold"
            ),
            constraints=_ALARM_COUNT_THRESHOLD_CONSTRAINTS,
            description=(
                "Maximum API Gateway 5XXError count in one period before "
                "the alarm fires. A count, not a rate."
            ),
        ),
        ModuleInputDefinition(
            name="api_latency_p99_threshold_ms",
            type=ParameterType.INT,
            required=True,
            binding=ParameterBindingSpec(
                parameter_name="api_latency_p99_threshold_ms"
            ),
            constraints=_ALARM_API_LATENCY_THRESHOLD_CONSTRAINTS,
            description=(
                "API Gateway p99 Latency threshold in milliseconds."
            ),
        ),
        ModuleInputDefinition(
            name="lambda_error_threshold",
            type=ParameterType.INT,
            required=True,
            binding=ParameterBindingSpec(
                parameter_name="lambda_error_threshold"
            ),
            constraints=_ALARM_COUNT_THRESHOLD_CONSTRAINTS,
            description=(
                "Maximum Lambda Errors count in one period before the "
                "alarm fires. A count, not a rate."
            ),
        ),
        ModuleInputDefinition(
            name="lambda_duration_p99_threshold_ms",
            type=ParameterType.INT,
            required=True,
            binding=ParameterBindingSpec(
                parameter_name="lambda_duration_p99_threshold_ms"
            ),
            constraints=_ALARM_LAMBDA_DURATION_THRESHOLD_CONSTRAINTS,
            description=(
                "Lambda p99 Duration threshold in milliseconds."
            ),
        ),
        ModuleInputDefinition(
            name="period_seconds",
            type=ParameterType.ENUM,
            required=True,
            binding=ParameterBindingSpec(parameter_name="period_seconds"),
            constraints=_ALARM_PERIOD_CONSTRAINTS,
            description="Alarm metric period in seconds.",
        ),
        ModuleInputDefinition(
            name="evaluation_periods",
            type=ParameterType.INT,
            required=True,
            binding=ParameterBindingSpec(
                parameter_name="evaluation_periods"
            ),
            constraints=_ALARM_EVALUATION_PERIODS_CONSTRAINTS,
            description="Number of periods evaluated before alarm state.",
        ),
        ModuleInputDefinition(
            name="comparison_operator",
            type=ParameterType.ENUM,
            required=True,
            binding=ParameterBindingSpec(
                parameter_name="comparison_operator"
            ),
            constraints=_ALARM_COMPARISON_CONSTRAINTS,
            description=(
                "Threshold comparison. Only greater-than comparisons are "
                "exposed because the capability monitors upper bounds."
            ),
        ),
        ModuleInputDefinition(
            name="missing_data_treatment",
            type=ParameterType.ENUM,
            required=True,
            binding=ParameterBindingSpec(
                parameter_name="missing_data_treatment"
            ),
            constraints=_ALARM_MISSING_DATA_CONSTRAINTS,
            description="CloudWatch treatment of missing datapoints.",
        ),
        ModuleInputDefinition(
            name="notification_target",
            type=ParameterType.STRING,
            required=True,
            binding=ParameterBindingSpec(
                parameter_name="notification_target"
            ),
            constraints=_ALARM_NOTIFICATION_CONSTRAINTS,
            description=(
                "Either 'none' or an existing same-region SNS topic ARN. "
                "The platform never creates a topic."
            ),
        ),
    ),
    outputs=(
        ModuleOutputReference(
            name="alarm_arns", type=ParameterType.STRING_LIST,
            description="ARNs of the four created alarms.",
        ),
    ),
    verification=VerificationMetadata(
        status=VerificationStatus.VERIFIED,
        verified_by=_REVIEWER,
        verified_on=_ALARM_VERIFIED_ON,
        methods=frozenset(
            {VerificationMethod.PINNED_REVISION, VerificationMethod.TESTED_CONTRACT}
        ),
        catalog_revision=CATALOG_VERSION,
        evidence_ref="tests/test_provisioning_cloudwatch_alarm.py",
        limitations=(
            "Exactly one HTTP API and one Lambda per spec; ambiguous "
            "topologies are refused.",
            "API Gateway alarms use the fixed AWS/ApiGateway 5XXError and "
            "Latency metrics for the HTTP API's $default stage.",
            "Lambda alarms use the fixed AWS/Lambda Errors and Duration "
            "metrics.",
            "No error-rate, throttling, composite, metric-math, anomaly-"
            "detection or additional-metric alarms are exposed.",
            "Thresholds, period, evaluation periods, comparison operator, "
            "missing-data treatment and notification target are human "
            "inputs with no defaults.",
            "Notification supports 'none' or an existing same-region SNS "
            "topic ARN; no topic is created and no OK/insufficient-data "
            "actions are configured.",
        ),
    ),
    contract_version="1.0.0",
)


CATALOG: tuple = (
    NOSQL_TABLE,
    OBJECT_STORAGE,
    SERVERLESS_FUNCTION,
    HTTP_API,
    CLOUDFRONT_DISTRIBUTION,
    COGNITO_USER_POOL,
    SQS_QUEUE,
    SNS_TOPIC,
    LOG_GROUP,
    CLOUDWATCH_ALARMS,
)