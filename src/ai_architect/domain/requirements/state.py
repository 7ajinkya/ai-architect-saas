from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from ai_architect.domain.requirements.models import (
    ClarificationCandidate,
    RequirementImpact,
    RequirementsExtraction,
    ValidationResult,
)

class ClarificationStatus(str, Enum):
    PENDING = "pending"
    ANSWERED = "answered"
    SKIPPED = "skipped"

class SessionStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    NEEDS_CLARIFICATION = "needs_clarification"
    READY = "ready"

class ClarificationItem(BaseModel):
    """
    Persistent representation of a clarification question
    and its current answer state.
    """

    id: str
    requirement_key: str
    requirement: str = ""
    question: str = ""
    reason: str = ""
    impact: RequirementImpact = RequirementImpact.UNKNOWN
    status: ClarificationStatus = ClarificationStatus.PENDING
    answer: str | None = None

    model_config = ConfigDict(extra="forbid")

class RequirementsState(BaseModel):
    """
    Persistent state for a requirements-gathering session.

    This is application/domain state. It is intentionally
    independent of LangGraph and checkpoint infrastructure.
    """

    session_id: str
    original_request: str

    extraction: RequirementsExtraction | None = None

    clarification_items: list[ClarificationItem] = Field(
        default_factory=list
    )

    validation_result: ValidationResult | None = None

    status: SessionStatus = SessionStatus.IN_PROGRESS

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def from_extraction(
        cls,
        session_id: str,
        original_request: str,
        extraction: RequirementsExtraction,
        validation_result: ValidationResult,
    ) -> "RequirementsState":
        clarification_items = [
            ClarificationItem(
                id=candidate.requirement_key,
                requirement_key=candidate.requirement_key,
                requirement=candidate.requirement,
                question=candidate.question,
                reason=candidate.reason,
                impact=candidate.impact,
            )
            for candidate in validation_result.blocking_clarifications
        ]

        status = (
            SessionStatus.READY
            if validation_result.status.value == "ready"
            else SessionStatus.NEEDS_CLARIFICATION
        )

        return cls(
            session_id=session_id,
            original_request=original_request,
            extraction=extraction,
            clarification_items=clarification_items,
            validation_result=validation_result,
            status=status,
        )

    def answer_clarification(
        self,
        clarification_id: str,
        answer: str,
    ) -> None:
        if self.extraction is None:
            raise ValueError(
                "Cannot answer clarification without requirements extraction."
            )

        for item in self.clarification_items:
            if item.id != clarification_id:
                continue

            if not hasattr(self.extraction, item.requirement_key):
                raise ValueError(
                    f"Unsupported requirement key: {item.requirement_key}"
                )

            setattr(
                self.extraction,
                item.requirement_key,
                answer,
            )

            item.answer = answer
            item.status = ClarificationStatus.ANSWERED

            # Remove the answered candidate from canonical extraction.
            self.extraction.clarification_candidates = [
                candidate
                for candidate in self.extraction.clarification_candidates
                if candidate.requirement_key != item.requirement_key
            ]

            return

        raise ValueError(
            f"Clarification not found: {clarification_id}"
        )

    def skip_clarification(self, clarification_id: str) -> None:
        for item in self.clarification_items:
            if item.id == clarification_id:
                item.answer = None
                item.status = ClarificationStatus.SKIPPED
                return

        raise ValueError(
            f"Clarification not found: {clarification_id}"
        )

    def pending_clarifications(self) -> list[ClarificationItem]:
        return [
            item
            for item in self.clarification_items
            if item.status == ClarificationStatus.PENDING
        ]
