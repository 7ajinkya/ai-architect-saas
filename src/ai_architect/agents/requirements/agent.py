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