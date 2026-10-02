from langchain_core.runnables import Runnable

from ai_architect.infrastructure.llm.factory import create_llm
from ai_architect.domain.requirements.models import RequirementsExtraction


def create_requirements_agent() -> Runnable:
    llm = create_llm()

    return llm.with_structured_output(
        RequirementsExtraction,
        method="function_calling",
    )
