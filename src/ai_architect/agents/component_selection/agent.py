from langchain_core.runnables import Runnable

from ai_architect.agents.component_selection.models import (
    ComponentSelectionLLMOutput,
)
from ai_architect.infrastructure.llm.factory import create_llm
from ai_architect.infrastructure.llm.profiles import LLMProfile


def create_component_selection_agent() -> Runnable:
    """Create the component selection agent with structured output.

    Function-calling uses the tolerant LLM boundary schema
    (ComponentSelectionLLMOutput); run.py maps it deterministically
    onto the strict ComponentSelectionResult domain artifact.
    """
    llm = create_llm(LLMProfile.HIGH)

    return llm.with_structured_output(
        ComponentSelectionLLMOutput,
        method="function_calling",
    )


def create_component_selection_agent_raw() -> Runnable:
    """Create agent without structured output for raw response."""
    llm = create_llm(LLMProfile.HIGH)
    return llm


def create_component_selection_agent_with_raw() -> Runnable:
    """Create the component selection agent, returning raw + parsed output.

    Same function-calling mechanism and boundary schema as
    create_component_selection_agent(), but with include_raw=True so the
    run path can distinguish a model transport flake (text envelope, no
    tool call) from a genuine validation failure. Uses HIGH profile only.
    """
    llm = create_llm(LLMProfile.HIGH)

    return llm.with_structured_output(
        ComponentSelectionLLMOutput,
        method="function_calling",
        include_raw=True,
    )
