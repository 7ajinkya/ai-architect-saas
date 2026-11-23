from langchain_core.runnables import Runnable

from ai_architect.agents.cost_estimation.models import (
    CostUsageEstimationProposal,
)
from ai_architect.infrastructure.llm.factory import create_llm
from ai_architect.infrastructure.llm.profiles import LLMProfile


def create_usage_estimation_agent() -> Runnable:
    llm = create_llm(LLMProfile.MEDIUM)

    return llm.with_structured_output(
        CostUsageEstimationProposal,
        method="function_calling",
    )


def create_usage_estimation_agent_raw() -> Runnable:
    """Create agent without structured output for raw response."""
    llm = create_llm(LLMProfile.MEDIUM)
    return llm
