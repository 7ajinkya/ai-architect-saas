from langchain_core.runnables import Runnable

from ai_architect.domain.requirements.models import RequirementsExtraction
from ai_architect.infrastructure.llm.factory import create_llm
from ai_architect.infrastructure.llm.profiles import LLMProfile


def create_requirements_agent() -> Runnable:
    llm = create_llm(LLMProfile.MEDIUM)

    return llm.with_structured_output(
        RequirementsExtraction,
        method="function_calling",
    )


def create_requirements_agent_raw() -> Runnable:
    """Create agent without structured output for raw response."""
    llm = create_llm(LLMProfile.MEDIUM)
    return llm


def create_requirements_agent_with_raw() -> Runnable:
    """Create the requirements agent, returning raw + parsed output.

    Same function-calling mechanism and boundary schema as
    create_requirements_agent(), but with include_raw=True so the
    run path can distinguish a model transport flake (text envelope,
    no tool call) from a genuine validation failure. Uses MEDIUM
    profile only.
    """
    llm = create_llm(LLMProfile.MEDIUM)

    return llm.with_structured_output(
        RequirementsExtraction,
        method="function_calling",
        include_raw=True,
    )