from enum import Enum
from typing import NotRequired, TypedDict

from ai_architect.agents.capacity.result import CapacityModelingResult
from ai_architect.agents.component_selection.models import (
    ComponentSelectionResult,
)
from ai_architect.domain.architecture.models import ArchitectureResult
from ai_architect.domain.cost.models import CostEstimationResult
from ai_architect.domain.requirements.state import RequirementsState


class DesignStatus(str, Enum):
    """Lifecycle of one top-level design workflow run."""

    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"


class DesignGraphState(TypedDict):
    """Top-level design workflow state (LangGraph channels).

    Carries only stage results owned by existing capabilities:
    - requirements_state: canonical output of the existing requirements
      workflow (extraction + HITL clarifications). Single source of truth
      for requirements; the orchestrator never duplicates it.
    - capacity_result: CapacityModelingResult (agent proposal +
      deterministic calculation).
    - component_selection: strict ComponentSelectionResult domain artifact.
    - cost_result: CostEstimationResult (deterministic pricing +
      deterministic calculation).
    - architecture: ArchitectureResult (composer synthesis over the
      above; validated before storage).
    - region_code: the canonical region resolved once from the user's
      stated region, so Cost Estimation and the Composer cannot disagree
      about where the design is deployed. Empty until resolved.

    No fields for future slices (critique, Terraform) are added here.
    """

    session_id: str
    user_request: str
    requirements_state: RequirementsState | None
    capacity_result: CapacityModelingResult | None
    component_selection: ComponentSelectionResult | None
    cost_result: CostEstimationResult | None
    architecture: ArchitectureResult | None
    region_code: str
    status: str
    error: str | None
    warnings: list[str]
    # Internal failure diagnostics for the Architecture Composer only.
    # Plain-JSON list of per-attempt {attempt, rejected_output,
    # errors, truncated} dicts, written solely on the composer failure
    # path. Never part of the public session view; the snapshot loader
    # deliberately ignores it.
    composer_diagnostics: NotRequired[list[dict] | None]
