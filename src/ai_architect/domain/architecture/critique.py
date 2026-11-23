"""Design-critique domain contract (Slice C: models only, no LLM).

The Critic reviews; it never mutates. Its output is a typed,
deterministic-consumable result:

- LLM proposes: findings (category, severity, statement, rationale,
  subject reference, evidence refs, recommendation, suggested route).
- Python assigns: artifact identity, reviewed-artifact linkage,
  deterministic summary counts.
- Python validates: severity/category enums, non-empty statement /
  rationale / recommendation, closed-world subject references,
  closed-world evidence references, no absolute numeric claims.

The Quality Gate reasons over a finished critique plus the existing
deterministic validation outcome. Gate outcomes use product-level
language (approved for review / needs revision / needs a human
decision / blocked) rather than reusing the lifecycle
running/ready/failed vocabulary, because READY must not automatically
mean GOOD ENOUGH TO PROVISION.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

CRITIQUE_SCHEMA_VERSION = "critique/v1"

# Bounded critique/revision cycles per design run. Separate from the
# composer correction retry: that mechanism repairs malformed output,
# this one bounds the adversarial loop.
MAX_CRITIQUE_CYCLES = 2


class FindingSeverity(str, Enum):
    """How much a finding blocks provisioning."""

    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class FindingCategory(str, Enum):
    """The fixed adversarial checklist. No free-form categories."""

    SPOF = "spof"
    COST_CEILING = "cost_ceiling"
    CAPACITY_HEADROOM = "capacity_headroom"
    OBSERVABILITY = "observability"
    DURABILITY = "durability"
    SECURITY = "security"
    SCALING_CLIFF = "scaling_cliff"


class SuggestedRoute(str, Enum):
    """Where a finding should be addressed. Interpreted by code."""

    COMPOSER_REVISION = "composer_revision"
    COMPONENT_RESELECTION = "component_reselection"


class CritiqueFinding(BaseModel):
    """One adversarial finding about the composed architecture."""

    category: FindingCategory
    severity: FindingSeverity
    statement: str
    rationale: str
    about_component_ref: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    recommendation: str
    suggested_route: SuggestedRoute = SuggestedRoute.COMPOSER_REVISION

    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class DesignCritique(BaseModel):
    """Immutable structured critique of one architecture artifact."""

    schema_version: str = CRITIQUE_SCHEMA_VERSION
    artifact_id: str
    session_id: str
    reviewed_artifact_id: str
    reviewed_version: int = Field(ge=1)
    findings: list[CritiqueFinding] = Field(default_factory=list)
    summary: str = ""
    generated_at: datetime = Field(default_factory=datetime.utcnow)

    model_config = ConfigDict(extra="forbid")

    @property
    def criticals(self) -> list[CritiqueFinding]:
        """Findings that block provisioning until resolved or accepted."""
        return [
            finding for finding in self.findings
            if str(finding.severity) == FindingSeverity.CRITICAL.value
        ]

    @property
    def needs_reselection(self) -> bool:
        """True when a critical finding faults the component set itself."""
        return any(
            str(finding.severity) == FindingSeverity.CRITICAL.value
            and str(finding.suggested_route)
            == SuggestedRoute.COMPONENT_RESELECTION.value
            for finding in self.findings
        )


class QualityGateOutcome(str, Enum):
    """Explicit gate verdict. READY is not GOOD ENOUGH TO PROVISION."""

    APPROVED_FOR_REVIEW = "approved_for_review"
    REQUIRES_REVISION = "requires_revision"
    REQUIRES_USER_DECISION = "requires_user_decision"
    BLOCKED = "blocked"


class QualityGateResult(BaseModel):
    """Deterministic gate verdict over validation + critique + limits."""

    outcome: QualityGateOutcome
    reasons: list[str] = Field(default_factory=list)
    critical_count: int = Field(ge=0, default=0)
    warning_count: int = Field(ge=0, default=0)
    evaluated_artifact_id: str = ""
    evaluated_at: datetime = Field(default_factory=datetime.utcnow)

    model_config = ConfigDict(extra="forbid", use_enum_values=True)
