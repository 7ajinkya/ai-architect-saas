from langchain_core.runnables import Runnable

from ai_architect.agents.capacity.models import CapacityModelProposal
from ai_architect.infrastructure.llm.factory import create_llm
from ai_architect.infrastructure.llm.profiles import LLMProfile


def create_capacity_modeling_agent() -> Runnable:
    llm = create_llm(LLMProfile.MEDIUM)

    return llm.with_structured_output(
        CapacityModelProposal,
        method="function_calling",
    )