"""Application-level entry point for the design workflow.

CLI -> run_design_workflow -> LangGraph -> existing agents/services.

Checkpointing reuses the existing AsyncSqliteSaver mechanism and database;
the design thread owns parent workflow state while the requirements stage
keeps its own session thread (see state_governance note in graph.py).
"""

import uuid

from ai_architect.agents.design.graph import build_design_graph
from ai_architect.agents.design.state import DesignGraphState, DesignStatus
from ai_architect.config.settings import settings
from ai_architect.domain.requirements.state import RequirementsState
from ai_architect.infrastructure.checkpoint.sqlite import create_checkpointer
from ai_architect.infrastructure.llm.factory import (
    active_provider,
    configured_model,
)
from ai_architect.infrastructure.llm.profiles import LLMProfile
from ai_architect.infrastructure.llm.providers import LLMProvider

LARGE_OLLAMA_MODEL = "gpt-oss:120b-cloud"
# LARGE_OLLAMA_MODEL = "beam:cloud"


def assert_model_gate() -> None:
    """Refuse to start if the active provider is misconfigured.

    Demo standard: every Ollama tier resolves to the Large model.
    Model resolution goes through the LLM factory, so this guard has
    no provider-specific knowledge of its own beyond the pinned id.
    """
    if active_provider() is LLMProvider.OLLAMA:
        for profile_name, profile in (
            ("LOW", LLMProfile.LOW),
            ("MEDIUM", LLMProfile.MEDIUM),
            ("HIGH", LLMProfile.HIGH),
        ):
            model_name = configured_model(profile)
            if model_name != LARGE_OLLAMA_MODEL:
                raise RuntimeError(
                    f"Model gate: {profile_name} resolves to "
                    f"{model_name!r}, expected {LARGE_OLLAMA_MODEL!r}. "
                    "Refusing to invoke."
                )
        return

    for profile in LLMProfile:
        if not configured_model(profile):
            raise RuntimeError(
                f"Model gate: profile {profile.value} has no model "
                "configured for the active provider."
            )


async def run_design_workflow(
    user_request: str,
    session_id: str | None = None,
    requirements_state: RequirementsState | None = None,
) -> DesignGraphState:
    """Run Requirements -> Capacity -> Component Selection -> Cost -> Composer.

    Single entry point for every caller (CLI and HTTP API).

    ``requirements_state`` is optional and defaults to ``None``. The CLI
    passes nothing, so the requirements stage runs the existing
    interactive flow and asks the operator for clarifications on the
    console. An HTTP caller passes a requirements state it already
    resolved through its own non-interactive HITL
    (``advance_requirements_flow``); because that state is READY, the
    requirements graph routes straight to END and never reads stdin, so
    the same graph, validator and HITL policy are reused without the API
    ever re-implementing them.
    """
    assert_model_gate()

    session_id = session_id or str(uuid.uuid4())

    initial: DesignGraphState = {
        "session_id": session_id,
        "user_request": user_request,
        "requirements_state": requirements_state,
        "capacity_result": None,
        "component_selection": None,
        "cost_result": None,
        "architecture": None,
        "region_code": "",
        "status": DesignStatus.RUNNING.value,
        "error": None,
        "warnings": [],
        "critique": None,
        "revision_count": 0,
        "critic_route": "",
        "quality_gate": None,
    }

    async with create_checkpointer() as checkpointer:
        graph = build_design_graph(checkpointer)

        config = {
            "configurable": {
                "thread_id": session_id,
            }
        }

        result = await graph.ainvoke(initial, config=config)

        return result
