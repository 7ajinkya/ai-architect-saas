from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ai_architect.agents.capacity.models import CapacityModelProposal
from ai_architect.agents.capacity.result import CapacityModelingResult
from ai_architect.agents.component_selection.models import (    ComponentCandidate,
    ComponentCategory,
    ComponentConfidence,
    ComponentSelectionLLMOutput,
    ComponentSelectionResult,
    EvidenceSupport,
    LLMComponentCandidate,
    LLMRejectedCandidate,
    LLMUnresolvedTradeoff,
    UnresolvedTradeoff,
)
from ai_architect.domain.architecture.critique import (
    CritiqueFinding,
    DesignCritique,
    FindingCategory,
    FindingSeverity,
    QualityGateOutcome,
    QualityGateResult,
    SuggestedRoute,
)
from ai_architect.domain.architecture.models import (
    ArchitectureDecision,
    ArchitectureEdge,
    ArchitectureInputsRef,
    ArchitectureNode,
    ArchitectureResult,
    CapacityMetricKey,
    CapacityValue,
    CostLink,
    EdgeKind,
)
from ai_architect.domain.capacity.models import (
    CapacityModelInput,
    CapacityModelResult,
)
from ai_architect.domain.cost.models import (
    CostCompleteness,
    CostEstimationResult,
    CostLineItem,
    CoverageEntry,
    CoverageStatus,
    DimensionDisposition,
    EstimatedCostRange,
    EstimationConfidence,
    PricedComponent,
    PriceTier,
    PricingFact,
    PricingModel,
    UnpricedComponent,
    UsageEstimate,
    UsageProvenance,
)
from ai_architect.domain.requirements.models import (
    AdditionalRequirement,
    Assumption,
    AssumptionConfidence,
    ClarificationCandidate,
    RequirementImpact,
    RequirementsExtraction,
    ValidationResult,
    ValidationStatus,
)
from ai_architect.domain.requirements.state import (
    ClarificationItem,
    ClarificationStatus,
    RequirementsState,
    SessionStatus,
)


# Anchored to the repository root, not the process working directory: a
# relative path silently splits session state across databases whenever
# the server is launched from another folder (a second checkout then
# shows different sessions with no error). Tests override this module
# attribute with a tmp path per test, which keeps working unchanged.
CHECKPOINT_DB_PATH = str(
    Path(__file__).resolve().parents[4] / "ai_architect_checkpoints.sqlite"
)

# Domain types our graph states checkpoint. Declared explicitly (rather
# than allow-all) so deserialization of anything else still warns/fails
# loudly instead of being silently trusted.
_MSGPACK_ALLOWLISTED_TYPES = (
    RequirementsState,
    ClarificationItem,
    ClarificationStatus,
    SessionStatus,
    RequirementsExtraction,
    ClarificationCandidate,
    RequirementImpact,
    ValidationResult,
    ValidationStatus,
    Assumption,
    AssumptionConfidence,
    AdditionalRequirement,
    CapacityModelInput,
    CapacityModelResult,
    CapacityModelProposal,
    CapacityModelingResult,
    ComponentSelectionResult,
    ComponentSelectionLLMOutput,
    ComponentCandidate,
    LLMComponentCandidate,
    LLMRejectedCandidate,
    LLMUnresolvedTradeoff,
    UnresolvedTradeoff,
    ComponentCategory,
    ComponentConfidence,
    EvidenceSupport,
    ArchitectureDecision,
    ArchitectureEdge,
    ArchitectureInputsRef,
    ArchitectureNode,
    ArchitectureResult,
    CapacityMetricKey,
    CapacityValue,
    CostLink,
    CritiqueFinding,
    DesignCritique,
    EdgeKind,
    FindingCategory,
    FindingSeverity,
    CostCompleteness,
    CostEstimationResult,
    CostLineItem,
    CoverageEntry,
    CoverageStatus,
    DimensionDisposition,
    EstimatedCostRange,
    EstimationConfidence,
    PricedComponent,
    PriceTier,
    PricingFact,
    PricingModel,
    QualityGateOutcome,
    QualityGateResult,
    SuggestedRoute,
    UnpricedComponent,
    UsageEstimate,
    UsageProvenance,
)


@asynccontextmanager
async def create_checkpointer() -> AsyncIterator[AsyncSqliteSaver]:
    # Explicit allowlist (not permissive-True): our domain types
    # deserialize silently, langchain/langgraph safe built-ins remain
    # covered by SAFE_MSGPACK_TYPES, and anything else still warns
    # loudly instead of being silently trusted. NOTE: do not use
    # with_msgpack_allowlist() here — on a default serializer it is a
    # no-op that keeps the warn-on-every-type behavior.
    serde = JsonPlusSerializer(
        allowed_msgpack_modules=list(_MSGPACK_ALLOWLISTED_TYPES)
    )
    async with aiosqlite.connect(CHECKPOINT_DB_PATH) as conn:
        yield AsyncSqliteSaver(conn, serde=serde)
