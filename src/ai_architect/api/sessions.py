"""Session lifecycle adapter over the existing requirements workflow.

This module owns no domain logic. It only:

- calls the existing extraction entry point and the existing
  requirements graph through advance_requirements_flow();
- applies an answer with the existing RequirementsState method, or the
  generic deferral policy when the user delegates;
- persists with the existing checkpoint helpers.

No requirements, capacity, selection, cost, or composer logic is
duplicated here.
"""

import uuid

from ai_architect.agents.requirements.run import (
    advance_requirements_flow,
    build_initial_requirements_state,
)
from ai_architect.domain.requirements.models import ClarificationCandidate
from ai_architect.domain.requirements.state import (
    RequirementsState,
    SessionStatus,
)
from ai_architect.infrastructure.checkpoint.requirements import (
    load_requirements_state,
    save_requirements_state,
)
from ai_architect.services.requirements.assumptions import (
    evaluate_deferral,
    is_deferral,
)
from ai_architect.services.requirements.validator import RequirementsValidator


class SessionError(Exception):
    """Recoverable session problem surfaced to the API caller."""

    def __init__(self, detail: str, code: str = "error", status_code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.status_code = status_code


def _clarification_from_interrupt(
    interrupt: dict,
    extraction,
) -> ClarificationCandidate:
    """Rebuild the candidate for a pending clarification from state.

    The interrupt payload carries only id/key/question, so the richer
    candidate (reason, impact) is recovered from the still-present
    clarification item on the authoritative state.
    """
    requirement_key = interrupt.get("requirement_key", "")
    for item in extraction.clarification_candidates:
        if item.requirement_key == requirement_key:
            return item
    return ClarificationCandidate(
        requirement_key=requirement_key,
        question=interrupt.get("question", ""),
    )


def _pending_item(state: RequirementsState) -> str | None:
    pending = state.pending_clarifications()
    return pending[0].id if pending else None


def pending_candidate(
    state: RequirementsState,
) -> ClarificationCandidate | None:
    """The canonical candidate behind the outstanding clarification.

    Single resolver, deliberately shared by the session view and by
    answer submission. When the richer candidate is still present in the
    extraction it is returned as-is; otherwise it is rebuilt from the
    authoritative clarification item so both callers evaluate exactly the
    same requirement key and impact.

    Sharing this is what keeps the advertised ``assumable`` flag honest:
    resolving it separately let the view default to "assumable" while
    submission correctly refused the same delegation.
    """
    pending = state.pending_clarifications()
    if not pending:
        return None
    item = pending[0]
    if state.extraction is not None:
        for candidate in state.extraction.clarification_candidates:
            if candidate.requirement_key == item.requirement_key:
                return candidate
    return ClarificationCandidate(
        requirement_key=item.requirement_key,
        requirement=item.requirement,
        question=item.question,
        reason=item.reason,
        impact=item.impact,
    )


async def create_session(user_request: str) -> RequirementsState:
    """Create a session from a natural-language request."""
    session_id = str(uuid.uuid4())
    state = build_initial_requirements_state(
        user_request, session_id=session_id
    )
    state, _ = await advance_requirements_flow(state)
    await save_requirements_state(state)
    return state


async def submit_message(session_id: str, text: str) -> RequirementsState:
    """Advance a session with a clarification answer or a delegation."""
    state = await load_requirements_state(session_id)
    if state is None:
        raise SessionError(
            f"Unknown session: {session_id}", code="session_not_found",
            status_code=404,
        )
    if state.extraction is None:
        raise SessionError(
            "Session has no requirements extraction.",
            code="state_invalid",
            status_code=409,
        )

    clarification_id = _pending_item(state)
    if clarification_id is None:
        raise SessionError(
            "Session has no pending clarification; nothing to answer.",
            code="no_pending_clarification",
            status_code=409,
        )

    item = next(
        c for c in state.clarification_items if c.id == clarification_id
    )
    # Same resolver the session view uses, so what the UI advertised and
    # what is enforced here can never diverge.
    candidate = pending_candidate(state) or _clarification_from_interrupt(
        {
            "requirement_key": item.requirement_key,
            "question": item.question,
        },
        state.extraction,
    )

    if is_deferral(text):
        decision = evaluate_deferral(candidate, state.extraction)
        if not decision.deferrable:
            # Material constraint: the question stays outstanding and the
            # user is told why rather than being given an invented value.
            raise SessionError(
                decision.reason,
                code="clarification_required",
                status_code=409,
            )
        if decision.assumption is not None:
            state.extraction.assumptions.append(decision.assumption)
        state.answer_clarification(clarification_id, decision.value)
    else:
        state.answer_clarification(clarification_id, text)

    # Re-validate so accepted assumptions and status reflect this turn.
    state.validation_result = RequirementsValidator().validate(
        state.extraction
    )
    state.status = (
        SessionStatus.READY
        if state.validation_result.status.value == "ready"
        else SessionStatus.NEEDS_CLARIFICATION
    )

    state, _ = await advance_requirements_flow(state)
    await save_requirements_state(state)
    return state


async def get_session(session_id: str) -> RequirementsState | None:
    return await load_requirements_state(session_id)
