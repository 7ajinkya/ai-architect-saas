from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ai_architect.domain.requirements.state import (
    RequirementsState,
    SessionStatus,
)
from ai_architect.services.requirements.validator import RequirementsValidator


class RequirementsGraphState(TypedDict):
    requirements_state: RequirementsState
    clarification_answer: str | None


def validate_requirements(
    state: RequirementsGraphState,
) -> dict:
    requirements_state = RequirementsState.ensure(
        state["requirements_state"]
    )

    if requirements_state.extraction is None:
        raise ValueError(
            "Requirements extraction is missing from session state."
        )

    validation = RequirementsValidator().validate(
        requirements_state.extraction
    )

    requirements_state.validation_result = validation

    if validation.status.value == "ready":
        requirements_state.status = SessionStatus.READY
    else:
        requirements_state.status = SessionStatus.NEEDS_CLARIFICATION

    return {
        "requirements_state": requirements_state,
    }


def ask_clarification(
    state: RequirementsGraphState,
) -> dict:
    requirements_state = RequirementsState.ensure(
        state["requirements_state"]
    )

    clarification = next(
        (
            item
            for item in requirements_state.clarification_items
            if item.status.value == "pending"
        ),
        None,
    )

    if clarification is None:
        raise ValueError(
            "No pending clarification found."
        )

    answer = interrupt(
        {
            "clarification_id": clarification.id,
            "requirement_key": clarification.requirement_key,
            "question": clarification.question,
        }
    )

    return {
        "clarification_answer": answer,
    }


def apply_clarification(
    state: RequirementsGraphState,
) -> dict:
    requirements_state = RequirementsState.ensure(
        state["requirements_state"]
    )

    clarification = next(
        (
            item
            for item in requirements_state.clarification_items
            if item.status.value == "pending"
        ),
        None,
    )

    if clarification is None:
        raise ValueError(
            "No pending clarification found."
        )

    answer = state["clarification_answer"]

    if answer is None:
        raise ValueError(
            "Clarification answer is missing."
        )

    requirements_state.answer_clarification(
        clarification_id=clarification.id,
        answer=answer,
    )

    return {
        "requirements_state": requirements_state,
        "clarification_answer": None,
    }


def route_after_validation(
    state: RequirementsGraphState,
) -> str:
    if state["requirements_state"].status == SessionStatus.READY:
        return END

    return "ask_clarification"


def build_requirements_graph(checkpointer):
    builder = StateGraph(RequirementsGraphState)

    builder.add_node(
        "validate_requirements",
        validate_requirements,
    )

    builder.add_node(
        "ask_clarification",
        ask_clarification,
    )

    builder.add_node(
        "apply_clarification",
        apply_clarification,
    )

    builder.add_edge(
        START,
        "validate_requirements",
    )

    builder.add_conditional_edges(
        "validate_requirements",
        route_after_validation,
    )

    builder.add_edge(
        "ask_clarification",
        "apply_clarification",
    )

    builder.add_edge(
        "apply_clarification",
        "validate_requirements",
    )

    return builder.compile(
        checkpointer=checkpointer,
    )