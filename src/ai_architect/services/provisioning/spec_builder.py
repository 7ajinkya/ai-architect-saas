"""ArchitectureResult -> InfraSpec, and the deterministic spec validator.

Pure deterministic transformation: no LLM, no network, no Terraform, no
timestamps, no random identifiers. Given the same ArchitectureResult and
provider it produces the same spec, byte for byte after canonicalization.

Two distinct, deliberate failure modes:

- **Unsupported component**: ``build_infra_spec`` raises immediately.
  A mixed architecture therefore fails closed rather than silently
  dropping or dropping-and-warning nodes.
- **Missing required inputs**: the builder records typed pending inputs
  and ``validate_spec`` reports them. Unresolved inputs are represented,
  never defaulted.

One narrow, documented exception to the never-drop rule: a node whose
vendor-stripped normalized name is exactly ``iam`` is not provisionable
directly (IAM is realized only as implicit supporting resources, e.g.
the Lambda execution role) and is excluded with an explicit entry in
``spec.warnings`` naming the node and the reason. Anything more
specific (``iam user``, ``iam policy`` and the like) still fails
closed: only the bare capability name takes this path, so no concrete
access-control requirement can be silently absorbed.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ai_architect.domain.architecture.models import ArchitectureResult

from ai_architect.domain.provisioning.models import (
    SUPPORTED_PROVIDERS,
    DiagnosticCategory,
    InfraSpec,
    ParameterSource,
    PendingInput,
    ProvisioningErrorCode,
    ResourceInstance,
    ResourceKind,
    ResolvedParameter,
    SourceArchitectureRef,
    SpecDiagnostic,
    SupportingResource,
)
from ai_architect.domain.requirements.models import (
    RequirementsExtraction,
    RequirementsSpec,
)
from ai_architect.services.cost.pricing_service import (
    normalize_component_name,
)
from ai_architect.services.provisioning.registry import (
    RESOURCE_CONTRACTS,
    VENDOR_PREFIXES,
    UnsupportedResourceError,
    contract_for_kind,
    match_contract,
)
from ai_architect.services.provisioning.structured_inputs import (
    StructuredInputError,
    normalize_resource_inputs,
)
from ai_architect.services.region.normalizer import RegionNormalizationError
from ai_architect.services.region.normalizer import normalize_region

# Credential-shaped parameter names/values must never enter a spec.
_SECRET_NAME = re.compile(
    r"(password|passwd|secret|token|private[_-]?key|access[_-]?key"
    r"|credential|authorization)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(AKIA[0-9A-Z]{16}|-----BEGIN[A-Z ]*PRIVATE KEY-----|"
    r"aws_secret_access_key\s*[=:]|aws_session_token\s*[=:])",
    re.IGNORECASE,
)


class ProvisioningError(ValueError):
    """A deterministic refusal to derive a provisioning spec."""

    def __init__(
        self,
        detail: str,
        code: str = ProvisioningErrorCode.PROVIDER_MISMATCH.value,
    ):
        super().__init__(detail)
        self.detail = detail
        self.code = code


def _canonical(payload: object) -> str:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        default=str,
    )


def architecture_hash(architecture: ArchitectureResult) -> str:
    """Deterministic hash of the source architecture artifact."""
    return hashlib.sha256(
        _canonical(architecture).encode("utf-8")
    ).hexdigest()


def _resolve_region(region_code: str, provider: str) -> str:
    """Canonicalize the architecture's region using the shared resolver."""
    text = (region_code or "").strip()
    if not text:
        raise ProvisioningError(
            "Source architecture has no region code; region is required "
            "for provisioning.",
            code=ProvisioningErrorCode.MISSING_REGION.value,
        )
    try:
        return normalize_region(text, provider=provider)
    except RegionNormalizationError as exc:
        raise ProvisioningError(
            f"Source architecture region {text!r} cannot be normalized: {exc}",
            code=ProvisioningErrorCode.INVALID_REGION.value,
        ) from exc


_IMPLICIT_LAMBDA_EXECUTION_ROLE_NAMES = frozenset({
    "iam execution role for lambda",
    "iam role for lambda",
    "iam role for lambda execution",
    "lambda execution role",
})


def is_bare_iam_node(component_name: str) -> bool:
    """Whether a node names the implicit IAM capability and nothing else.

    Vendor prefixes and parenthetical aliases are normalized away with
    the same function the registry uses, so ``AWS IAM (service role)``
    and ``AWS IAM`` both qualify while ``AWS IAM User`` does not.

    The Lambda execution role is the one specific IAM shape the verified
    Lambda module realizes itself as a least-privilege supporting
    resource, so that exact capability is excluded loudly too. Every
    other IAM name still fails closed in ``match_contract``.
    """
    normalized = normalize_component_name(component_name or "")
    for prefix in VENDOR_PREFIXES:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):].strip()
            break
    return (
        normalized == "iam"
        or normalized in _IMPLICIT_LAMBDA_EXECUTION_ROLE_NAMES
    )


def iam_exclusion_warning(node) -> str:
    """The recorded reason a bare-IAM node leaves the spec.

    Single source for the message so the direct spec path and the
    fallback-partition path cannot drift apart.
    """
    return (
        f"Architecture node {node.node_id!r} "
        f"({node.component_name!r}) is an IAM capability, which is "
        f"realized only as implicit supporting resources (for "
        f"example the Lambda execution role) and is never "
        f"provisioned directly; it is excluded from the spec."
    )


def build_infra_spec(
    architecture: ArchitectureResult,
    *,
    provider: str,
    requirements=None,
) -> InfraSpec:
    """Derive the deterministic InfraSpec for an architecture.

    ``provider`` is supplied by the caller because ArchitectureResult
    carries region and upstream provenance but no cloud provider; the
    frozen architecture contract is left untouched.

    ``requirements`` is the caller's requirements contract
    (RequirementsExtraction / RequirementsSpec, or their dict forms). It
    is the only source for values ArchitectureNode cannot carry: today an
    explicit CloudWatch log-retention day count. When omitted, no
    requirement is assumed and every required input stays pending.
    """
    provider_norm = (provider or "").strip().lower()
    if provider_norm not in SUPPORTED_PROVIDERS:
        raise ProvisioningError(
            f"Provisioning is not implemented for provider {provider_norm!r}; "
            f"supported: {', '.join(SUPPORTED_PROVIDERS)}.",
            code=ProvisioningErrorCode.PROVIDER_MISMATCH.value,
        )

    region_code = _resolve_region(architecture.inputs_ref.region_code, provider_norm)

    entry_points = set(architecture.entry_points or [])
    data_stores = set(architecture.data_store_node_ids or [])

    resources: list[ResourceInstance] = []
    warnings: list[str] = []

    for node in architecture.nodes:
        # Bare-IAM exception (see module docstring): the capability is
        # realized only as implicit supporting resources, never as a
        # directly provisioned resource. Anything else unregistered
        # fails closed below; names are never rewritten.
        if is_bare_iam_node(node.component_name):
            warnings.append(iam_exclusion_warning(node))
            continue
        # Unregistered component: fail closed. Nodes are never dropped and
        # names are never rewritten.
        contract = match_contract(node.component_name)
        node_id = (node.node_id or "").strip()
        component_name = node.component_name

        if not node_id:
            raise UnsupportedResourceError(
                f"Architecture node for {component_name!r} has an empty "
                f"node_id, so no deterministic resource identity can be "
                f"derived.",
                component_name=component_name,
                code=ProvisioningErrorCode.INVALID_IDENTITY.value,
            )

        parameters = _derived_parameters(
            contract.kind, node, region_code, requirements
        )
        pending = [
            PendingInput(
                name=name,
                reason=(
                    f"{name} is required to provision "
                    f"{contract.kind.value} and is not derivable from the "
                    f"architecture artifact."
                ),
            )
            for name in contract.required_inputs
            if name not in {p.name for p in parameters}
        ]
        supporting = [
            SupportingResource(kind=kind, reference=reference, reason=reason)
            for kind, reference, reason in contract.supporting
        ]

        if not node_id:
            raise UnsupportedResourceError(
                f"Architecture node for {component_name!r} has an empty "
                f"node_id, so no deterministic resource identity can be "
                f"derived.",
                component_name=component_name,
                code=ProvisioningErrorCode.INVALID_IDENTITY.value,
            )

        resources.append(
            ResourceInstance(
                resource_id=f"{contract.kind.value}-{node_id}",
                node_id=node_id,
                kind=contract.kind,
                component_name=component_name,
                is_entry_point=node_id in entry_points,
                is_data_store=node_id in data_stores,
                parameters=parameters,
                pending_inputs=pending,
                supporting=supporting,
            )
        )

    if not resources:
        warnings.append(
            "Architecture contains no nodes; the provisioning spec is empty."
        )

    source = SourceArchitectureRef(
        artifact_id=architecture.artifact_id,
        version=architecture.version,
        session_id=architecture.session_id,
        upstream_inputs_hash=architecture.inputs_ref.upstream_inputs_hash,
        architecture_hash=architecture_hash(architecture),
    )

    spec = InfraSpec(
        spec_id="",
        provider=provider_norm,
        region_code=region_code,
        source_architecture=source,
        resources=resources,
        warnings=warnings,
    )

    # spec_id is derived from the content, so it is stable and needs no
    # random identifier or timestamp.
    return spec.model_copy(update={"spec_id": _compute_spec_id(spec)})


# Closed allowlist for CloudWatch log-group retention. Kept identical to
# services.provisioning.registry (RESOURCE_CONTRACTS[CLOUDWATCH_LOG_GROUP])
# and the log_group module contract; the log-group render and module
# binding re-validate it a second time. An explicit requirement outside
# this set is refused, never coerced onto the nearest allowed value.
LOG_RETENTION_DAYS = ("7", "30", "90", "365")

# additional_requirements categories that mark a retention entry as a
# log-retention statement rather than (e.g.) data/storage retention.
_LOG_RETENTION_CATEGORY_MARKERS = ("log", "monitoring", "observability")

# Explicit numeric retention, e.g. "30" in "retain logs for 30 days".
# A range, fraction, or non-day unit is deliberately not matched.
_RETENTION_NUMBER_RE = re.compile(r"(\d+)\s*(days?)?", re.IGNORECASE)


@dataclass(frozen=True)
class ResolvedRetention:
    """Outcome of resolving an explicit log-retention requirement.

    Three states, never collapsed into a fabricated value:

    - ``explicit``: a clean numeric in-day requirement was found; ``value``
      is the exact integer days string.
    - ``ambiguous``: retention was mentioned but not as an unambiguous
      numeric day count (missing, a range, or a non-day unit). No value is
      invented.
    - ``missing``: no retention requirement is present at all.
    """

    status: str
    value: Optional[str] = None
    source: Optional[str] = None

    @property
    def is_explicit(self) -> bool:
        return self.status == "explicit"


def _retention_from_text(text: object) -> Optional[str]:
    """Extract an unambiguous integer day count from a requirement string.

    Returns the canonical integer string only for a single, in-day,
    whole-number value. Returns None for missing, empty, ranged,
    fractional, or non-day text so callers never fabricate a number.
    """
    if not isinstance(text, str):
        return None
    match = _RETENTION_NUMBER_RE.search(text)
    if match is None:
        return None
    number = match.group(1)
    if not number.isdigit():
        return None
    unit = (match.group(2) or "").strip().lower()
    if unit and unit not in ("day", "days"):
        return None
    # Reject ranges/fractions the leading-number regex would otherwise
    # half-match (e.g. "30-90 days", "1.5 days").
    tail = text[match.end():].strip()
    if tail and not tail.startswith(("days", "day")):
        return None
    return str(int(number))


def _is_log_retention_entry(name: str, category: str) -> bool:
    """Whether an additional_requirements entry is a log-retention statement.

    The name must mention retention; when a category is present it must
    mark a log/monitoring scope, so a storage/data retention entry is
    never mistaken for the log-group retention.
    """
    name_l = name.lower()
    if "retention" not in name_l:
        return False
    if "log" in name_l:
        return True
    if not category:
        return False
    category_l = category.lower()
    return any(marker in category_l for marker in _LOG_RETENTION_CATEGORY_MARKERS)


def _coerce_requirements(requirements):
    """Normalize a requirements input to a RequirementsSpec or Extraction.

    Accepts the domain objects or their plain-dict forms. Anything
    unrecognized becomes None so the caller treats retention as missing
    rather than guessing.
    """
    if requirements is None:
        return None
    if isinstance(requirements, (RequirementsExtraction, RequirementsSpec)):
        return requirements
    if isinstance(requirements, dict):
        if "additional_requirements" in requirements or "application_description" in requirements:
            try:
                return RequirementsExtraction.model_validate(requirements)
            except Exception:  # noqa: BLE001 - fall through to spec
                pass
        try:
            return RequirementsSpec.model_validate(requirements)
        except Exception:  # noqa: BLE001 - unrecognized shape
            return None
    return None


def _resolve_retention_days(requirements) -> ResolvedRetention:
    """Resolve the explicit CloudWatch log-group retention requirement.

    Reads only the existing requirements contract: the structured
    additional_requirements list first, then the dedicated retention
    field. Never parses free-form user text and never infers a number
    from an ambiguous phrase. Returns a ResolvedRetention that records
    whether the value was explicit, ambiguous, or missing.
    """
    spec = _coerce_requirements(requirements)
    if spec is None:
        return ResolvedRetention(status="missing")

    additional = getattr(spec, "additional_requirements", None)
    if additional:
        for entry in additional:
            name = getattr(entry, "name", "") or ""
            category = getattr(entry, "category", "") or ""
            if not _is_log_retention_entry(name, category):
                continue
            value = _retention_from_text(getattr(entry, "value", ""))
            if value is not None and value in LOG_RETENTION_DAYS:
                return ResolvedRetention(
                    status="explicit", value=value, source=name
                )
            return ResolvedRetention(status="ambiguous", source=name)

    retention = getattr(spec, "retention", "")
    if isinstance(retention, str) and retention.strip():
        value = _retention_from_text(retention)
        if value is not None and value in LOG_RETENTION_DAYS:
            return ResolvedRetention(
                status="explicit", value=value, source="retention"
            )
        return ResolvedRetention(status="ambiguous", source="retention")

    return ResolvedRetention(status="missing")


def _retention_parameter(requirements) -> Optional[ResolvedParameter]:
    """A retention parameter only when the requirement is explicit.

    Explicit requirement -> a resolved parameter carrying the exact value.
    Missing/ambiguous requirement -> None, so the registry's pending-input
    machinery states the outstanding human decision instead of a default.
    """
    resolved = _resolve_retention_days(requirements)
    if not resolved.is_explicit:
        return None
    return ResolvedParameter(
        name="retention_days",
        value=resolved.value,
        source=ParameterSource.DERIVED,
    )


def _derived_parameters(kind: ResourceKind, node, region_code: str,
                        requirements=None) -> list:
    """Parameters provably derivable from the architecture node.

    ArchitectureNode carries no resource parameters, so derivation is
    limited to values the requirements contract states explicitly and
    that map safely onto a provisioning value. Today that is CloudWatch
    log-group retention: an explicit in-allowlist day count becomes a
    resolved parameter; anything missing or ambiguous is left for the
    registry's pending-input machinery rather than invented here.

    Future derivations are added here, in one deterministic place,
    rather than being guessed at during rendering.
    """
    del node, region_code
    if kind is ResourceKind.CLOUDWATCH_LOG_GROUP:
        parameter = _retention_parameter(requirements)
        if parameter is not None:
            return [parameter]
    return []


def _compute_spec_id(spec: InfraSpec) -> str:
    document = spec.model_dump(mode="json")
    document.pop("spec_id", None)
    return hashlib.sha256(
        _canonical(document).encode("utf-8")
    ).hexdigest()


def apply_supplied_inputs(
    spec: InfraSpec, inputs: dict[str, dict[str, str]]
) -> InfraSpec:
    """Attach caller-supplied required inputs, returning a re-identified spec.

    ``inputs`` maps resource_id -> {parameter_name: value}. Values are
    recorded as REQUIRED_INPUT parameters and validated downstream by
    the existing contract machinery (patterns, enums, secret
    detection); nothing here trusts them. The returned spec carries a
    recomputed content-derived spec_id, so identity always matches
    content. Unknown resource ids fail loudly rather than being
    silently dropped.
    """
    from ai_architect.domain.provisioning.models import (
        ParameterSource,
        ResolvedParameter,
    )

    known_ids = {resource.resource_id for resource in spec.resources}
    for resource_id in inputs:
        if resource_id not in known_ids:
            raise ProvisioningError(
                f"Supplied inputs name unknown resource "
                f"{resource_id!r}; expected one of "
                f"{sorted(known_ids)}.",
                code=ProvisioningErrorCode.INCONSISTENT_REFERENCE.value,
            )
    resources = []
    for resource in spec.resources:
        supplied = inputs.get(resource.resource_id) or {}
        if not supplied:
            resources.append(resource)
            continue
        try:
            supplied = normalize_resource_inputs(resource, supplied)
        except StructuredInputError as exc:
            raise ProvisioningError(
                str(exc),
                code=ProvisioningErrorCode.MALFORMED_PARAMETER.value,
            ) from exc
        existing = {param.name for param in resource.parameters}
        parameters = list(resource.parameters)
        for name in sorted(supplied):
            value = supplied[name]
            if not isinstance(value, str):
                raise ProvisioningError(
                    f"Supplied input {name!r} for resource "
                    f"{resource.resource_id!r} must be a string.",
                    code=ProvisioningErrorCode.MALFORMED_PARAMETER.value,
                )
            if name in existing:
                raise ProvisioningError(
                    f"Supplied input {name!r} for resource "
                    f"{resource.resource_id!r} collides with a "
                    "derived parameter.",
                    code=ProvisioningErrorCode.MALFORMED_PARAMETER.value,
                )
            parameters.append(
                ResolvedParameter(
                    name=name,
                    value=value,
                    source=ParameterSource.REQUIRED_INPUT,
                )
            )
        # A supplied requirement is resolved: it leaves pending_inputs so
        # the plan display and unresolved_inputs() reflect what the human
        # actually still owes, not what was owed at derivation time.
        pending = [
            item for item in resource.pending_inputs
            if item.name not in supplied
        ]
        resources.append(
            resource.model_copy(
                update={"parameters": parameters, "pending_inputs": pending}
            )
        )
    updated = spec.model_copy(update={"resources": resources})
    return updated.model_copy(
        update={"spec_id": _compute_spec_id(updated)}
    )


def demo_lambda_artifact() -> Optional[tuple[str, str]]:
    """Return (basename, sha256) of the project's demo Lambda artifact.

    Uses the existing deterministic builder in infra/lambda/ (the same
    code referenced by the module catalog). Returns None when the source
    or builder is unavailable, so callers preserve the explicit
    missing-input state rather than fabricating values.

    The platform never reads the operator's filesystem for a hash: the
    hash comes from build_artifact's return value, computed from the
    source it builds.
    """
    try:
        import importlib.util
        import pathlib
        import sys

        repo_root = pathlib.Path(__file__).resolve().parents[4]
        lambda_dir = repo_root / "infra" / "lambda"
        source = lambda_dir / "demo_handler.py"
        out_dir = lambda_dir / "dist"
        if not source.exists():
            return None
        builder_path = lambda_dir / "build_demo_artifact.py"
        if not builder_path.exists():
            return None
        spec = importlib.util.spec_from_file_location(
            "_build_demo_artifact", builder_path
        )
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        target, digest = module.build_artifact(source, out_dir)
        if not target or not digest:
            return None
        return (target.name, digest)
    except Exception:  # noqa: BLE001 - unavailable means missing-input
        return None


def merge_demo_lambda_artifact_inputs(
    supplied: dict[str, dict[str, str]],
    spec: InfraSpec,
) -> dict[str, dict[str, str]]:
    """Auto-supply demo Lambda artifact inputs when not explicitly given.

    For each Lambda resource in the spec, if the user has not supplied
    code_artifact_path or code_artifact_sha256, supply them from the
    demo artifact (if available). User-supplied values always take
    precedence. Returns a new dict; never mutates the caller's.
    """
    artifact = demo_lambda_artifact()
    if artifact is None:
        return dict(supplied)
    basename, sha256 = artifact
    merged = {key: dict(value) for key, value in (supplied or {}).items()}
    for resource in spec.resources:
        if resource.kind is not ResourceKind.LAMBDA:
            continue
        existing = dict(merged.get(resource.resource_id, {}))
        if "code_artifact_path" in existing or "code_artifact_sha256" in existing:
            # User provided at least one; do not auto-supply.
            continue
        existing["code_artifact_path"] = basename
        existing["code_artifact_sha256"] = sha256
        merged[resource.resource_id] = existing
    return merged


def _looks_like_secret(name: str, value: str) -> bool:
    return bool(
        _SECRET_NAME.search(name or "")
        or _SECRET_VALUE.search(value or "")
    )


def diagnose_spec(spec: InfraSpec) -> list[SpecDiagnostic]:
    """Deterministic, categorised InfraSpec diagnostics.

    Every finding is either STRUCTURAL (the artifact is malformed or
    inconsistent) or MISSING_INPUT (the artifact is sound, but a human
    must still decide a required infrastructure parameter). The two are
    never conflated: a registry match alone does not make a resource
    provisionable.

    Ordering is deterministic - resources in spec order, then each
    resource's checks in a fixed sequence, then required inputs in
    contract order.

    Raises nothing.
    """
    diagnostics: list[SpecDiagnostic] = []

    def structural(code, message, resource_id=None):
        diagnostics.append(
            SpecDiagnostic(
                category=DiagnosticCategory.STRUCTURAL,
                code=ProvisioningErrorCode(code),
                message=message,
                resource_id=resource_id,
            )
        )

    def missing_input(message, resource_id, input_name):
        diagnostics.append(
            SpecDiagnostic(
                category=DiagnosticCategory.MISSING_INPUT,
                code=ProvisioningErrorCode.MISSING_INPUT,
                message=message,
                resource_id=resource_id,
                input_name=input_name,
            )
        )

    provider = (spec.provider or "").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        structural(
            ProvisioningErrorCode.PROVIDER_MISMATCH,
            f"Provider {provider!r} is not supported for provisioning; "
            f"supported: {', '.join(SUPPORTED_PROVIDERS)}.",
        )

    region = (spec.region_code or "").strip()
    if not region:
        structural(
            ProvisioningErrorCode.MISSING_REGION,
            "Spec has no region_code; region is required.",
        )
    else:
        try:
            canonical = normalize_region(region, provider=provider)
            if canonical != region:
                structural(
                    ProvisioningErrorCode.INVALID_REGION,
                    f"region_code {region!r} is not canonical; expected "
                    f"{canonical!r}.",
                )
        except RegionNormalizationError as exc:
            structural(
                ProvisioningErrorCode.INVALID_REGION,
                f"region_code {region!r} is invalid: {exc}",
            )

    if not (spec.spec_id or "").strip():
        structural(
            ProvisioningErrorCode.INVALID_IDENTITY,
            "Spec has no spec_id.",
        )

    seen: dict = {}
    for resource in spec.resources:
        identity = (resource.resource_id or "").strip()
        node_id = resource.node_id

        if not identity:
            structural(
                ProvisioningErrorCode.INVALID_IDENTITY,
                f"Resource for node {node_id!r} has an empty resource_id.",
            )
        elif identity in seen:
            structural(
                ProvisioningErrorCode.DUPLICATE_IDENTITY,
                f"Duplicate resource identity {identity!r} used by nodes "
                f"{seen[identity]!r} and {node_id!r}.",
                resource_id=identity,
            )
        else:
            seen[identity] = node_id

        if not (node_id or "").strip():
            structural(
                ProvisioningErrorCode.INVALID_IDENTITY,
                f"Resource {identity!r} has an empty node_id.",
                resource_id=identity,
            )

        if identity and resource.kind is not None:
            expected_prefix = f"{resource.kind.value}-"
            if not identity.startswith(expected_prefix):
                structural(
                    ProvisioningErrorCode.INVALID_IDENTITY,
                    f"Resource identity {identity!r} does not start with its "
                    f"kind prefix {expected_prefix!r}.",
                    resource_id=identity,
                )

        contract = (
            contract_for_kind(resource.kind, provider)
            if resource.kind is not None
            else None
        )
        if contract is None:
            structural(
                ProvisioningErrorCode.UNSUPPORTED_RESOURCE,
                f"Resource {identity!r} has kind "
                f"{getattr(resource.kind, 'value', resource.kind)!r} with no "
                f"registered contract for provider {provider!r}.",
                resource_id=identity,
            )
        elif contract.provider != provider:
            structural(
                ProvisioningErrorCode.PROVIDER_MISMATCH,
                f"Resource {identity!r} requires provider "
                f"{contract.provider!r} but the spec declares {provider!r}.",
                resource_id=identity,
            )

        if not (resource.component_name or "").strip():
            structural(
                ProvisioningErrorCode.INVALID_IDENTITY,
                f"Resource {identity!r} has an empty component_name.",
                resource_id=identity,
            )

        if contract is not None:
            supplied = {p.name for p in resource.parameters}
            for name in contract.required_inputs:
                if name in supplied:
                    continue
                if any(p.name == name for p in resource.pending_inputs):
                    missing_input(
                        f"Resource {identity!r} is missing required "
                        f"provisioning input {name!r}.",
                        identity,
                        name,
                    )
                else:
                    # Declared nowhere: an inconsistent spec, not merely
                    # an outstanding decision.
                    structural(
                        ProvisioningErrorCode.MISSING_INPUT,
                        f"Resource {identity!r} neither supplies nor "
                        f"declares required provisioning input {name!r}.",
                        resource_id=identity,
                    )

        for parameter in resource.parameters:
            if not (parameter.name or "").strip():
                structural(
                    ProvisioningErrorCode.MALFORMED_PARAMETER,
                    f"Resource {identity!r} has a nameless parameter.",
                    resource_id=identity,
                )
            if not isinstance(parameter.value, str):
                structural(
                    ProvisioningErrorCode.MALFORMED_PARAMETER,
                    f"Parameter {parameter.name!r} on {identity!r} has a "
                    f"non-string value; parameters must be provider-agnostic "
                    f"strings.",
                    resource_id=identity,
                )
            if _looks_like_secret(parameter.name, parameter.value):
                structural(
                    ProvisioningErrorCode.SECRET_DETECTED,
                    f"Parameter {parameter.name!r} on {identity!r} looks like "
                    f"a credential; credentials must never enter an InfraSpec.",
                    resource_id=identity,
                )

        for supporting in resource.supporting:
            if not (supporting.reference or "").strip():
                structural(
                    ProvisioningErrorCode.INCONSISTENT_REFERENCE,
                    f"Resource {identity!r} declares a supporting resource "
                    f"with an empty reference.",
                    resource_id=identity,
                )

    return diagnostics


def validate_spec(spec: InfraSpec) -> list:
    """Deterministic InfraSpec validation.

    Same convention as the existing architecture validator: returns a
    list of error strings; empty means valid. Raises nothing.

    Reports BOTH structural errors and unresolved required human inputs -
    a registry match alone does not make a resource provisionable. Call
    :func:`diagnose_spec` when the two need to be told apart.
    """
    return [diagnostic.message for diagnostic in diagnose_spec(spec)]
