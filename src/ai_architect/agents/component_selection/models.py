from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


class ComponentCategory(str, Enum):
    """Categories of infrastructure components.

    Architectural roles, not provisioning families: a category names
    what a component DOES in the system, independently of whether a
    verified Terraform module exists for it.
    """

    COMPUTE = "compute"
    STORAGE = "storage"
    DATABASE = "database"
    MESSAGING = "messaging"
    NETWORKING = "networking"
    CACHING = "caching"
    CDN = "cdn"
    DNS = "dns"
    LOAD_BALANCING = "load_balancing"
    SECURITY = "security"
    MONITORING = "monitoring"
    CI_CD = "ci_cd"
    IDENTITY = "identity"
    BACKUP = "backup"
    AI_ML = "ai_ml"
    ANALYTICS = "analytics"
    MEDIA = "media"
    HOSTING = "hosting"
    INTEGRATION = "integration"
    OTHER = "other"

class ComponentConfidence(str, Enum):
    """Confidence level in the component recommendation."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class EvidenceSupport(str, Enum):
    """Level of evidence support for a component recommendation."""

    STRONG = "strong"  # Multiple live provider evidence items
    MODERATE = "moderate"  # Some live or strong indexed evidence
    WEAK = "weak"  # Only indexed/curated evidence
    INSUFFICIENT = "insufficient"  # Insufficient evidence


class ComponentCandidate(BaseModel):
    """A candidate component for the architecture."""

    name: str = Field(description="Service/component name (e.g., 'Amazon SQS')")
    provider: str = Field(description="Cloud provider (aws, gcp, azure, general)")
    category: ComponentCategory = Field(description="Component category")
    role: str = Field(description="Role/purpose in the architecture")
    rationale: str = Field(description="Why this component is needed")
    evidence_refs: list[str] = Field(
        default_factory=list,
        description="Evidence IDs from EvidenceBundle supporting this recommendation",
    )
    assumptions: list[str] = Field(
        default_factory=list,
        description="Assumptions made about requirements or constraints",
    )
    tradeoffs: list[str] = Field(
        default_factory=list,
        description="Known tradeoffs or alternatives considered",
    )
    confidence: ComponentConfidence = Field(description="Confidence in this recommendation")
    evidence_support: EvidenceSupport = Field(description="Level of evidence support")
    provider_specific: bool = Field(
        default=True,
        description="Whether this is provider-specific",
    )
    alternatives: list[str] = Field(
        default_factory=list,
        description="Alternative services/components considered",
    )
    region_constraints: list[str] = Field(
        default_factory=list,
        description="Region constraints if applicable",
    )

    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class UnresolvedTradeoff(BaseModel):
    """An unresolved architectural tradeoff that needs human decision."""

    description: str = Field(description="Description of the tradeoff")
    options: list[str] = Field(description="Options being considered")
    impact: str = Field(description="Impact of the decision")
    recommended_resolution: str | None = Field(
        default=None,
        description="Recommended resolution if any",
    )
    evidence_refs: list[str] = Field(
        default_factory=list,
        description="Evidence IDs relevant to this tradeoff",
    )

    model_config = ConfigDict(extra="forbid")


class ComponentSelectionRequest(BaseModel):
    """Input request for component selection (application-level, not LLM output)."""

    requirements: dict = Field(description="Structured requirements (RequirementsExtraction as dict)")
    capacity: dict = Field(description="Deterministic capacity result (CapacityModelResult as dict)")
    evidence_bundle: dict = Field(description="EvidenceBundle as dict")
    provider: str = Field(description="Target cloud provider (aws, gcp, azure)")

    model_config = ConfigDict(extra="forbid")


class ComponentSelectionResult(BaseModel):
    """Strict domain artifact for component selection.

    This is NOT the LLM function-calling schema. The LLM emits
    ComponentSelectionLLMOutput, which normalize_component_selection()
    maps into this strict contract before validate_component_selection().
    """

    selected_components: list[ComponentCandidate] = Field(
        default_factory=list,
        description="Selected components with full details",
    )
    rejected_candidates: list[ComponentCandidate] = Field(
        default_factory=list,
        description="Candidates considered but not selected",
    )
    unresolved_tradeoffs: list[UnresolvedTradeoff] = Field(
        default_factory=list,
        description="Unresolved architectural tradeoffs",
    )
    evidence_references: list[str] = Field(
        default_factory=list,
        description="All evidence IDs referenced in the selection",
    )
    assumptions: list[str] = Field(
        default_factory=list,
        description="Assumptions made during selection",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Warnings about insufficient evidence or other concerns",
    )
    generated_at: datetime = Field(default_factory=datetime.utcnow)

    model_config = ConfigDict(extra="forbid", use_enum_values=True)


# ------------------------------------------------------------------
# LLM boundary schema
# ------------------------------------------------------------------
# Intentionally simpler and more tolerant than the strict domain model.
# This is the ONLY schema the LLM emits via function calling. Fields
# Python can determine deterministically (e.g. generated_at) are NOT
# part of this schema. Shapes observed from real model output that carry
# the same information in a looser container (single string instead of
# list[str], {name, reason} rejected candidates, description-only
# tradeoffs) are accepted here and normalized deterministically below.
# No substantive information is fabricated during normalization: absent
# lists become empty lists, and absent text stays empty.


def _to_str_list(value: str | list[str] | None) -> list[str]:
    """Normalize the model's observed representations into list[str]."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    return list(value)


_CATEGORY_VALUES = {c.value for c in ComponentCategory}

# Conservative aliases: unambiguous spellings/shorthands for existing
# taxonomy values only. Deliberately excluded: ambiguous terms
# ("streaming", "event", "firewall", "dashboard", "deployment",
# "gateway", "host", "frontend", "website", "serverless", "search")
# and all provider service names. "host" alone can mean a machine;
# "frontend"/"website" name tiers, not roles; "serverless" names a
# compute model and "search" spans analytics/monitoring, so none of
# them map anywhere rather than mapping wrong somewhere.
_CATEGORY_ALIASES = {
    "queue": "messaging",
    "queues": "messaging",
    "message": "messaging",
    "messages": "messaging",
    "broker": "messaging",
    "brokers": "messaging",
    "topic": "messaging",
    "topics": "messaging",
    "pubsub": "messaging",
    "pub_sub": "messaging",
    "db": "database",
    "sql": "database",
    "nosql": "database",
    "datastore": "database",
    "data_store": "database",
    "cache": "caching",
    "redis": "caching",
    "memcached": "caching",
    "object_storage": "storage",
    "objectstore": "storage",
    "blob": "storage",
    "blob_storage": "storage",
    "network": "networking",
    "networks": "networking",
    "vpc": "networking",
    "subnet": "networking",
    "subnets": "networking",
    "api_gateway": "networking",
    "api": "networking",
    "rest": "networking",
    "websocket": "networking",
    "loadbalancer": "load_balancing",
    "load_balancer": "load_balancing",
    "auth": "identity",
    "authentication": "identity",
    "authorization": "identity",
    "observability": "monitoring",
    "alerting": "monitoring",
    "logs": "monitoring",
    "tracing": "monitoring",
    "ci": "ci_cd",
    "cd": "ci_cd",
    "ml": "ai_ml",
    "machine_learning": "ai_ml",
    "frontend": "hosting",
    "website": "hosting",
    "static": "hosting",
    "cdn": "cdn",
    "content_delivery": "cdn",
    "queue": "messaging",
    "queues": "messaging",
    "topic": "messaging",
    "topics": "messaging",
    "pubsub": "messaging",
    "pub_sub": "messaging",
    "db": "database",
    "sql": "database",
    "nosql": "database",
    "datastore": "database",
    "data_store": "database",
    "cache": "caching",
    "redis": "caching",
    "memcached": "caching",
    "object_storage": "storage",
    "objectstore": "storage",
    "blob": "storage",
    "blob_storage": "storage",
    "network": "networking",
    "networks": "networking",
    "vpc": "networking",
    "subnet": "networking",
    "subnets": "networking",
    "compute": "compute",
    "serverless": "compute",
    "function": "compute",
    "functions": "compute",
    "lambda": "compute",
    "container": "compute",
    "containers": "compute",
    "load_balancer": "load_balancing",
    "auth": "identity",
    "authentication": "identity",
    "authorization": "identity",
    "sso": "identity",
    "login": "identity",
    "observability": "monitoring",
    "log": "monitoring",
    "logs": "monitoring",
    "logging": "monitoring",
    "tracing": "monitoring",
    "metrics": "monitoring",
    "alerting": "monitoring",
    "apm": "monitoring",
    "cicd": "ci_cd",
    "pipeline": "ci_cd",
    "pipelines": "ci_cd",
    "devops": "ci_cd",
    "server": "compute",
    "servers": "compute",
    "container": "compute",
    "containers": "compute",
    "kubernetes": "compute",
    "k8s": "compute",
    "vm": "compute",
    "vms": "compute",
    "ec2": "compute",
    "lambda": "compute",
    "faas": "compute",
    "function": "compute",
    "functions": "compute",
    "iam": "security",
    "kms": "security",
    "secret": "security",
    "secrets": "security",
    "encryption": "security",
    "tls": "security",
    "ssl": "security",
    "certificate": "security",
    "certificates": "security",
    "rbac": "security",
    "mfa": "security",
    "zero_trust": "security",
    "dr": "backup",
    "disaster_recovery": "backup",
    "snapshot": "backup",
    "snapshots": "backup",
    "restore": "backup",
    "ai": "ai_ml",
    "ml": "ai_ml",
    "llm": "ai_ml",
    "genai": "ai_ml",
    "machine_learning": "ai_ml",
    "artificial_intelligence": "ai_ml",
    "inference": "ai_ml",
    "etl": "analytics",
    "elt": "analytics",
    "bi": "analytics",
    "warehouse": "analytics",
    "data_warehouse": "analytics",
    "olap": "analytics",
    "big_data": "analytics",
    "video": "media",
    "audio": "media",
    "image": "media",
    "images": "media",
    "transcoding": "media",
    "transcode": "media",
    "workflow": "integration",
    "workflows": "integration",
    "orchestration": "integration",
    "webhook": "integration",
    "webhooks": "integration",
    "content_delivery": "cdn",
    "edge": "cdn",
    "web_hosting": "hosting",
    "static_hosting": "hosting",
    "site_hosting": "hosting",
}


def _normalize_category_text(text: str) -> str:
    """Lowercase and collapse any non-alphanumeric run into one underscore."""
    out: list[str] = []
    prev_underscore = True
    for ch in text.strip().lower():
        if ch.isalnum():
            out.append(ch)
            prev_underscore = False
        elif not prev_underscore:
            out.append("_")
            prev_underscore = True
    return "".join(out).strip("_")


def _coerce_category(value: object) -> object:
    """Normalize reasonable model category variations into the taxonomy.

    Handles separator variants ("CI/CD" -> "ci_cd", "AI/ML" -> "ai_ml"),
    pipe-joined multi-categories ("messaging|streaming" keeps the first
    valid entry in model order), and unambiguous aliases ("queue" ->
    "messaging"). Anything else is returned untouched so pydantic still
    rejects genuinely invalid values loudly.
    """
    if isinstance(value, ComponentCategory):
        return value
    if not isinstance(value, str):
        return value
    # Whole-string first so separator variants resolve before any
    # multi-category split ("CI/CD" -> "ci_cd", "AI/ML" -> "ai_ml").
    whole = _normalize_category_text(value)
    if whole in _CATEGORY_VALUES:
        return whole
    if whole in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[whole]
    # Pipe-joined multi-categories keep the first valid entry in model
    # order ("messaging|streaming" -> "messaging").
    for part in value.replace("/", "|").split("|"):
        text = _normalize_category_text(part)
        if not text:
            continue
        if text in _CATEGORY_VALUES:
            return text
        if text in _CATEGORY_ALIASES:
            return _CATEGORY_ALIASES[text]
    return value


LenientCategory = Annotated[ComponentCategory, BeforeValidator(_coerce_category)]


def _dict_to_str(item: dict) -> object:
    """Extract a string from a dict-shaped list item, else return it untouched.

    Observed model shapes: {"option": ...}, {"choice": ...},
    {"name": ..., "value": ...}, {"message": ...}, and per-component
    tradeoff objects {"description": ..., "options": [...]}. Extraction
    preserves model content in a flat string; unrecognized shapes are
    left for pydantic to reject loudly.
    """
    for key in ("option", "choice", "label", "text", "message"):
        if isinstance(item.get(key), str) and item[key].strip():
            text = item[key].strip().lower()
            for ch in ("\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2015", "\u2212"):
                text = text.replace(ch, " ")
            for ch in ("\u00a0", "\u2000", "\u2001", "\u2003", "\u2004", "\u2005", "\u2006", "\u2007", "\u2008", "\u2009", "\u200a", "\u202f", "\u205f", "\u3000"):
                text = text.replace(ch, " ")
            return " ".join(text.split())
    description = item.get("description")
    if isinstance(description, str) and description.strip():
        options = item.get("options")
        if isinstance(options, list):
            parts = [
                option.strip()
                for option in options
                if isinstance(option, str) and option.strip()
            ]
            if parts:
                return f"{description.strip()} (options: {'; '.join(parts)})"
        return description.strip()
    name = item.get("name")
    value = item.get("value")
    if isinstance(name, str) and name.strip():
        if isinstance(value, str) and value.strip():
            return f"{name.strip()}={value.strip()}"
        return name
    if isinstance(value, str) and value.strip():
        return value
    return item


def _coerce_str_list(value: object) -> object:
    """Accept str, list[str], or lists containing dict-shaped items.

    Normalizes containers only; unrecognized item shapes are returned
    untouched so pydantic still rejects them loudly.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return [
            _dict_to_str(item) if isinstance(item, dict) else item
            for item in value
        ]
    return value


LenientStrList = Annotated[list[str] | str, BeforeValidator(_coerce_str_list)]


def _coerce_impact(value: object) -> object:
    """Accept an impact string or a list of impact strings.

    The model sometimes emits impact as a list; joining non-blank
    elements with "; " preserves every element deterministically in
    the canonical string form (an all-blank list folds to "", the
    schema's own absence-of-information value). Anything else is
    returned untouched so pydantic still rejects it loudly.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        if all(isinstance(item, str) for item in value):
            return "; ".join(
                item.strip() for item in value if item.strip()
            )
    return value


LenientImpact = Annotated[str | list[str], BeforeValidator(_coerce_impact)]


class LLMComponentCandidate(BaseModel):
    """LLM-facing selected-component representation.

    Required informational fields mirror ComponentCandidate; container
    fields accept either the strict shape or the looser shapes observed
    from model output. Unknown extra keys are ignored, not rejected.
    """

    name: str = Field(description="Service/component name (e.g., 'Amazon SQS')")
    provider: str = Field(description="Cloud provider (aws, gcp, azure, general)")
    category: LenientCategory = Field(description="Component category")
    role: str = Field(description="Role/purpose in the architecture")
    rationale: str = Field(description="Why this component is needed")
    evidence_refs: LenientStrList = Field(
        default_factory=list,
        description="Evidence IDs from EvidenceBundle supporting this recommendation",
    )
    assumptions: LenientStrList = Field(
        default_factory=list,
        description="Assumptions made about requirements or constraints",
    )
    tradeoffs: LenientStrList = Field(
        default_factory=list,
        description="Known tradeoffs or alternatives considered",
    )
    confidence: ComponentConfidence = Field(description="Confidence in this recommendation")
    evidence_support: EvidenceSupport = Field(description="Level of evidence support")
    provider_specific: bool = Field(
        default=True,
        description="Whether this is provider-specific",
    )
    alternatives: LenientStrList = Field(
        default_factory=list,
        description="Alternative services/components considered",
    )
    region_constraints: LenientStrList = Field(
        default_factory=list,
        description="Region constraints if applicable, as a single string or a list of strings",
    )

    model_config = ConfigDict(extra="ignore", use_enum_values=True)


class LLMRejectedCandidate(BaseModel):
    """LLM-facing rejected-candidate representation.

    Observed model output for rejected options is a compact
    {name, reason} object, not a full ComponentCandidate. Optional
    detail fields are preserved when the model supplies them.
    """

    name: str = Field(description="Service/component name that was considered")
    reason: str = Field(
        default="",
        description="Why this candidate was not selected",
    )
    provider: str = Field(default="", description="Cloud provider if known")
    category: LenientCategory = Field(
        default=ComponentCategory.OTHER,
        description="Component category if known",
    )
    role: str = Field(default="", description="Role/purpose if known")
    rationale: str = Field(default="", description="Why it was considered if known")
    confidence: ComponentConfidence = Field(
        default=ComponentConfidence.LOW,
        description="Confidence in this assessment",
    )
    evidence_support: EvidenceSupport = Field(
        default=EvidenceSupport.INSUFFICIENT,
        description="Level of evidence support",
    )

    model_config = ConfigDict(extra="ignore", use_enum_values=True)


class LLMUnresolvedTradeoff(BaseModel):
    """LLM-facing tradeoff representation.

    The model sometimes emits only a description. Options/impact default
    to absence-of-information so parsing succeeds; nothing is invented.
    """

    description: str = Field(description="Description of the tradeoff")
    options: LenientStrList = Field(
        default_factory=list,
        description="Options being considered, as strings or {option: ...} objects",
    )
    impact: LenientImpact = Field(
        default="",
        description=(
            "Impact of the decision, as a single string "
            "(a list of strings is joined deterministically)",
        ),
    )
    recommended_resolution: str | None = Field(
        default=None,
        description="Recommended resolution if any",
    )
    evidence_refs: LenientStrList = Field(
        default_factory=list,
        description="Evidence IDs relevant to this tradeoff",
    )

    model_config = ConfigDict(extra="ignore")


class ComponentSelectionLLMOutput(BaseModel):
    """LLM function-calling contract for component selection.

    Tolerant boundary schema. Has NO generated_at (assigned
    deterministically during normalization). Maps 1:1 onto
    ComponentSelectionResult via normalize_component_selection().
    """

    selected_components: list[LLMComponentCandidate] = Field(
        default_factory=list,
        description="Selected components with full details",
    )
    rejected_candidates: list[LLMRejectedCandidate] = Field(
        default_factory=list,
        description="Candidates considered but not selected, as {name, reason} objects",
    )
    unresolved_tradeoffs: list[LLMUnresolvedTradeoff] = Field(
        default_factory=list,
        description="Unresolved architectural tradeoffs",
    )
    evidence_references: LenientStrList = Field(
        default_factory=list,
        description="All evidence IDs referenced in the selection",
    )
    assumptions: LenientStrList = Field(
        default_factory=list,
        description="Assumptions made during selection",
    )
    warnings: LenientStrList = Field(
        default_factory=list,
        description="Warnings about insufficient evidence or other concerns",
    )

    model_config = ConfigDict(extra="ignore", use_enum_values=True)


def normalize_component_selection(
    llm_output: ComponentSelectionLLMOutput,
) -> ComponentSelectionResult:
    """Deterministically map LLM boundary output onto the strict domain artifact.

    - str -> list[str] normalization for container fields
    - empty/absent lists stay empty; absent text stays empty
    - rejected {name, reason} maps onto ComponentCandidate with the
      rejection reason preserved as the rationale (falling back to any
      rationale the model did supply); provider/category/role default to
      absence-of-information, never invented facts
    - generated_at is assigned here by Python, never by the model
    """
    selected = [
        ComponentCandidate(
            name=c.name,
            provider=c.provider,
            category=c.category,
            role=c.role,
            rationale=c.rationale,
            evidence_refs=_to_str_list(c.evidence_refs),
            assumptions=_to_str_list(c.assumptions),
            tradeoffs=_to_str_list(c.tradeoffs),
            confidence=c.confidence,
            evidence_support=c.evidence_support,
            provider_specific=c.provider_specific,
            alternatives=_to_str_list(c.alternatives),
            region_constraints=_to_str_list(c.region_constraints),
        )
        for c in llm_output.selected_components
    ]

    rejected = [
        ComponentCandidate(
            name=c.name,
            provider=c.provider,
            category=c.category,
            role=c.role,
            rationale=c.rationale or c.reason,
            evidence_refs=[],
            assumptions=[],
            tradeoffs=[],
            confidence=c.confidence,
            evidence_support=c.evidence_support,
            provider_specific=bool(c.provider),
            alternatives=[],
            region_constraints=[],
        )
        for c in llm_output.rejected_candidates
    ]

    tradeoffs = [
        UnresolvedTradeoff(
            description=t.description,
            options=_to_str_list(t.options),
            impact=t.impact,
            recommended_resolution=t.recommended_resolution,
            evidence_refs=_to_str_list(t.evidence_refs),
        )
        for t in llm_output.unresolved_tradeoffs
    ]

    return ComponentSelectionResult(
        selected_components=selected,
        rejected_candidates=rejected,
        unresolved_tradeoffs=tradeoffs,
        evidence_references=_to_str_list(llm_output.evidence_references),
        assumptions=_to_str_list(llm_output.assumptions),
        warnings=_to_str_list(llm_output.warnings),
        generated_at=datetime.utcnow(),
    )


class ComponentSelectionValidationError(Exception):
    """Raised when component selection validation fails."""


def validate_component_selection(
    result: ComponentSelectionResult,
    evidence_bundle: dict,
    provider: str,
) -> list[str]:
    """
    Deterministic validation for component selection.

    Checks:
    - selected component provider matches requested provider
    - evidence references point to evidence present in the bundle
    - no missing required fields (enforced by pydantic, double-checked)
    - no duplicate selected components
    """
    errors: list[str] = []
    evidence_ids = {
        e.get("evidence_id", "") for e in evidence_bundle.get("evidence", [])
    }

    for comp in result.selected_components:
        # Provider must match the requested single-cloud provider,
        # unless the component is explicitly marked cross-cloud/general.
        comp_provider = (comp.provider or "").lower()
        if comp.provider_specific and comp_provider != provider.lower():
            errors.append(
                f"Component '{comp.name}' provider '{comp.provider}' "
                f"does not match requested provider '{provider}'"
            )

        # Evidence references must exist in the bundle.
        for ev_id in comp.evidence_refs:
            if ev_id not in evidence_ids:
                errors.append(
                    f"Component '{comp.name}' references evidence '{ev_id}' "
                    "which is not in the evidence bundle"
                )

        # Provider-specific recommendations must cite evidence, unless
        # explicitly marked insufficient (grounding rule: mark, don't fabricate).
        if (
            comp.provider_specific
            and not comp.evidence_refs
            and comp.evidence_support != EvidenceSupport.INSUFFICIENT
        ):
            errors.append(
                f"Component '{comp.name}' is provider-specific "
                "but has no evidence references and is not marked "
                "evidence_support=insufficient"
            )

        # Required fields (pydantic enforces, but verify non-empty).
        for field in ("name", "provider", "role", "rationale"):
            if not getattr(comp, field, ""):
                errors.append(
                    f"Component '{getattr(comp, 'name', 'unknown')}' "
                    f"missing required field: {field}"
                )

    # No duplicate selected components.
    names = [c.name for c in result.selected_components]
    seen: set[str] = set()
    duplicates: set[str] = set()
    for name in names:
        if name in seen:
            duplicates.add(name)
        seen.add(name)
    if duplicates:
        errors.append(
            f"Duplicate component names: {', '.join(sorted(duplicates))}"
        )

    # Every referenced evidence ID must be declared in evidence_references.
    declared = set(result.evidence_references)
    for comp in result.selected_components:
        for ev_id in comp.evidence_refs:
            if ev_id not in declared:
                errors.append(
                    f"Component '{comp.name}' references evidence '{ev_id}' "
                    "which is not declared in evidence_references"
                )

    return errors
