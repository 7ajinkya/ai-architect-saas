from langchain_core.runnables import Runnable

from ai_architect.infrastructure.llm.factory import create_llm
from ai_architect.infrastructure.llm.profiles import LLMProfile


def _architecture_boundary():
    # Local import keeps the LLM boundary schema next to its run path,
    # mirroring how each agent module owns its contract.
    from ai_architect.agents.architecture.run import ArchitectureLLMOutput

    return ArchitectureLLMOutput


def create_architecture_composer_agent() -> Runnable:
    """Create the composer agent with structured output.

    Function calling uses the tolerant LLM boundary schema; run.py maps
    it deterministically onto the strict ArchitectureResult domain
    artifact. Uses HIGH profile only.
    """
    llm = create_llm(LLMProfile.HIGH)

    return llm.with_structured_output(
        _architecture_boundary(),
        method="function_calling",
    )


def create_architecture_composer_agent_raw() -> Runnable:
    """Create agent without structured output for raw response."""
    llm = create_llm(LLMProfile.HIGH)
    return llm


def create_architecture_composer_agent_with_raw() -> Runnable:
    """Create the composer agent, returning raw + parsed output.

    Same function-calling mechanism and boundary schema as
    create_architecture_composer_agent(), but with include_raw=True so
    the run path can distinguish a model transport flake (text
    envelope, no tool call) from a genuine validation failure. Uses
    HIGH profile only.
    """
    llm = create_llm(LLMProfile.HIGH)

    return llm.with_structured_output(
        _architecture_boundary(),
        method="function_calling",
        include_raw=True,
    )
