"""Generic deferral policy: "you decide" becomes a documented assumption.

When the user explicitly delegates an architectural decision to the
architect, the pipeline records an assumption with rationale and
continues instead of looping the clarification.

Policy (domain-neutral, no workload branches):

- Material requirements never auto-resolve: cloud provider (the
  single-cloud product policy), region, and anything carrying a
  compliance impact in a regulated context. Those remain blocking.
- Every other canonical requirement may be deferred. When a generic
  architectural convention exists it is recorded as the assumed value;
  otherwise the field is left unspecified and the assumption states
  that it is unconstrained. Either way nothing is invented: an
  unspecified field stays unspecified.
"""

from dataclasses import dataclass
import re

from ai_architect.config.settings import settings
from ai_architect.domain.requirements.models import (
    Assumption,
    ClarificationCandidate,
    RequirementImpact,
    RequirementsExtraction,
)
from ai_architect.services.requirements.validator import RequirementsValidator

_REGION_CODE_PATTERN = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d+$")

# Phrases that explicitly delegate the decision to the architect.
DEFERRAL_MARKERS = (
    "you decide",
    "you pick",
    "your call",
    "your choice",
    "up to you",
    "whatever you",
    "use your judgment",
    "as you see fit",
    "choose for me",
    "you're the architect",
    "you are the architect",
    "i don't know",
    "i do not know",
    "don't know",
    "do not know",
    "no idea",
    "not sure",
    "your judgement",
    "your judgment",
    "defer to you",
    "leaving it to you",
)

ASSUMPTION_SOURCE = "architect_default"

# Canonical requirement keys with a defensible, domain-neutral
# architectural convention. Keys absent from this table are deferred as
# "unspecified / unconstrained" rather than given an invented value.
ARCHITECTURAL_DEFAULTS: dict[str, dict[str, str]] = {
    "storage_characteristics": {
        "value": (
            "managed object storage with provider-standard durability "
            "and lifecycle appropriate for the stated retention"
        ),
        "rationale": (
            "Managed object storage is the default durable store and "
            "satisfies durability and lifecycle needs for the stated "
            "retention without introducing custom storage engineering."
        ),
    },
    "deployment_scope": {
        "value": "single region",
        "rationale": (
            "Single region is the lowest-complexity scope; multi-region "
            "only where the requirements or constraints demand it."
        ),
    },
    "managed_service_preference": {
        "value": "managed services preferred where an equivalent exists",
        "rationale": (
            "Managed services are preferred to keep the design free of "
            "undifferentiated operational burden."
        ),
    },
    "availability": {
        "value": "",
        "rationale": (
            "No availability target was stated, so none is assumed and "
            "no availability guarantee is claimed downstream."
        ),
    },
    "latency": {
        "value": "",
        "rationale": (
            "No latency target was stated, so none is assumed and no "
            "latency guarantee is claimed downstream."
        ),
    },
    "target_users_dau": {
        "value": "",
        "rationale": (
            "Scale was not specified; capacity modeling applies its own "
            "documented sizing assumption instead."
        ),
    },
    "traffic_characteristics": {
        "value": "",
        "rationale": (
            "Traffic profile was not specified; capacity modeling applies "
            "its own documented peak assumptions instead."
        ),
    },
    "read_write_characteristics": {
        "value": "",
        "rationale": (
            "Read/write mix was not specified; capacity modeling applies "
            "its own documented ratio assumption instead."
        ),
    },
    "retention": {
        "value": "",
        "rationale": (
            "Retention was not specified; storage growth is modeled over "
            "the documented capacity assumption instead."
        ),
    },
}

# Never auto-resolved without a defensible default: the single-cloud
# provider policy is a product constraint, not an architect choice.
ALWAYS_MATERIAL_REQUIREMENT_KEYS = frozenset({"cloud_provider"})

# Requirement keys that may be resolved from a configured architect
# default when the user delegates.
CONFIGURABLE_DEFAULT_KEYS = frozenset({"region"})

# Signals that make a regional choice material: a stated compliance
# regime, a residency obligation, or an explicit geographic
# constraint. Deliberately excludes bare domain nouns ("healthcare",
# "banking", "financial"): a domain word is not a regional constraint,
# and matching on those would make the region policy workload-shaped.
REGIONAL_CONSTRAINT_MARKERS = (
    # stated compliance regimes
    "gdpr",
    "hipaa",
    "pci",
    "sox",
    "fedramp",
    "compliance regime",
    "regulatory requirement",
    # residency obligations
    "data residency",
    "residency",
    "residency requirement",
    "must reside",
    "must be stored in",
    "must be located in",
    "must be hosted in",
    "data must",
    "eu region",
    "eea",
    # explicit geographic constraints
    "european users",
    "europe only",
    "us only",
    "u.s. only",
    "india only",
    "united kingdom only",
    "latency-sensitive users in",
    "users are in",
)


class ArchitectDefaultError(RuntimeError):
    """A configured architect default is missing or unusable."""


@dataclass(frozen=True)
class DeferralDecision:
    """Outcome of applying the deferral policy to one clarification."""

    deferrable: bool
    reason: str
    value: str = ""
    assumption: Assumption | None = None


def _regulated_context(extraction: RequirementsExtraction) -> bool:
    return RequirementsValidator()._has_regulated_context(extraction)


def configured_default_region() -> str:
    """The configured fallback region, validated.

    Empty means the architect has no defensible regional default and
    the clarification must stay blocking. A malformed value is a
    configuration error, not something to silently ignore.
    """
    raw = (settings.ai_architect_default_region or "").strip()
    if not raw:
        return ""
    if not _REGION_CODE_PATTERN.match(raw):
        raise ArchitectDefaultError(
            f"AI_ARCHITECT_DEFAULT_REGION={raw!r} is not a region code "
            "such as 'us-east-1'. Clear it to keep region clarification "
            "blocking."
        )
    return raw


def _has_regional_constraint(
    extraction: RequirementsExtraction,
    candidate: ClarificationCandidate | None = None,
) -> bool:
    """True when region is pinned by a stated regional constraint.

    Scoped to explicit constraints (compliance regime, residency
    obligation, stated geography) in the extracted requirements and in
    the outstanding question. Uses its own marker set rather than the
    compliance-impact policy, which also matches bare domain nouns.
    """
    parts = [
        extraction.application_description,
        extraction.deployment_scope,
        extraction.region,
        extraction.compliance_requirements,
    ]
    if candidate is not None:
        parts.extend(
            [candidate.requirement, candidate.question, candidate.reason]
        )
    text = " ".join(parts).lower()
    return any(marker in text for marker in REGIONAL_CONSTRAINT_MARKERS)


def _resolve_region_default(
    candidate: ClarificationCandidate,
    extraction: RequirementsExtraction,
) -> DeferralDecision:
    """Resolve a delegated region from the configured architect default.

    Blocks when there is no configured default, or when a residency /
    compliance / geographic constraint is present that the default is
    not entitled to override.
    """
    region = configured_default_region()
    if not region:
        return DeferralDecision(
            deferrable=False,
            reason=(
                "No architect default region is configured "
                "(AI_ARCHITECT_DEFAULT_REGION), so the architect has no "
                "defensible regional choice and 'region' must be stated "
                "explicitly."
            ),
        )
    if _has_regional_constraint(extraction, candidate):
        return DeferralDecision(
            deferrable=False,
            reason=(
                "'region' carries a residency, compliance, or geographic "
                "constraint that the architect must not assume away; it "
                "must be stated explicitly."
            ),
        )
    assumption = Assumption(
        name="region",
        value=region,
        category="architectural_default",
        objective="resolve_deferred_clarification",
        scope="design",
        source=ASSUMPTION_SOURCE,
        evidence=f"user delegation: {candidate.question}".strip(),
        rationale=(
            "User delegated region selection; the configured architect "
            f"default region ({region}) was applied."
        ),
        confidence="medium",
    )
    return DeferralDecision(
        deferrable=True,
        reason="resolved from the configured architect default region",
        value=region,
        assumption=assumption,
    )


def is_deferral(text: str) -> bool:
    """True when the user explicitly delegated the decision."""
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    return any(marker in lowered for marker in DEFERRAL_MARKERS)


def evaluate_deferral(
    candidate: ClarificationCandidate,
    extraction: RequirementsExtraction,
) -> DeferralDecision:
    """Decide whether a deferred clarification can become an assumption.

    Material constraints return a non-deferrable decision with the
    reason, so the caller keeps the clarification outstanding rather
    than inventing a provider, region, or compliance position.
    """
    key = candidate.requirement_key

    if key in ALWAYS_MATERIAL_REQUIREMENT_KEYS:
        return DeferralDecision(
            deferrable=False,
            reason=(
                f"{key!r} is a material product-policy constraint (the MVP "
                "is single-cloud) that the architect cannot assume on the "
                "user's behalf; it must be stated explicitly."
            ),
        )

    if key in CONFIGURABLE_DEFAULT_KEYS:
        return _resolve_region_default(candidate, extraction)

    if candidate.impact == RequirementImpact.COMPLIANCE and _regulated_context(
        extraction
    ):
        return DeferralDecision(
            deferrable=False,
            reason=(
                f"{key!r} carries a compliance impact in a regulated "
                "context; the architect cannot assume a compliance "
                "position and it must be stated explicitly."
            ),
        )

    default = ARCHITECTURAL_DEFAULTS.get(key)
    value = default["value"] if default else ""
    rationale = (
        default["rationale"]
        if default
        else (
            f"{key!r} was not specified and the user delegated the "
            "decision; it is treated as unconstrained and no value is "
            "invented."
        )
    )

    assumption = Assumption(
        name=key,
        value=value,
        category="architectural_default",
        objective="resolve_deferred_clarification",
        scope="design",
        source=ASSUMPTION_SOURCE,
        evidence=f"user delegation: {candidate.question}".strip(),
        rationale=rationale,
    )
    return DeferralDecision(
        deferrable=True,
        reason="resolved from the architect-default assumption policy",
        value=value,
        assumption=assumption,
    )
