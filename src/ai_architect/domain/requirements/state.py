from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ai_architect.domain.requirements.models import (
    AdditionalRequirement,
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

    @classmethod
    def ensure(
        cls,
        value: "RequirementsState | dict",
    ) -> "RequirementsState":
        """Reconstruct typed state at the domain boundary.

        Checkpoint deserialization falls back to unvalidated construction
        when stored data fails validation, which leaves dict innards that
        later explode as AttributeError (e.g. 'dict' object has no
        attribute 'status'). Revalidating here turns that into a clear
        failure instead of silent corruption downstream, and guarantees
        validation_result stays a real ValidationResult instance.
        """
        if isinstance(value, cls):
            value = value.model_dump(mode="python")
        try:
            return cls.model_validate(value)
        except ValidationError as e:
            raise ValueError(
                "Stored requirements state failed validation and cannot "
                f"be safely reconstructed: {e}"
            ) from e

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

            field = RequirementsExtraction.model_fields.get(
                item.requirement_key
            )
            if field is None:
                raise ValueError(
                    f"Unsupported requirement key: {item.requirement_key}"
                )

            if item.requirement_key == "additional_requirements":
                # A free-text answer must never be assigned raw to a list
                # field (pydantic does not validate on assignment, which
                # silently corrupts the model and breaks checkpoint
                # serialization). Structure it with the existing schema.
                self.extraction.additional_requirements.append(
                    AdditionalRequirement(
                        name=item.requirement or item.requirement_key,
                        value=answer,
                        source="user_provided",
                        evidence=answer,
                    )
                )
            elif field.annotation is str:
                setattr(
                    self.extraction,
                    item.requirement_key,
                    answer,
                )
            else:
                raise ValueError(
                    "Cannot apply a free-text answer to non-scalar field "
                    f"{item.requirement_key!r}."
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
