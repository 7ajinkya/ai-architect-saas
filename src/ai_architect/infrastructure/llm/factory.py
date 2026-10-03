from langchain_ollama import ChatOllama

from ai_architect.config.settings import settings
from ai_architect.infrastructure.llm.profiles import LLMProfile


def create_llm(profile: LLMProfile) -> ChatOllama:
    model_map = {
        LLMProfile.LOW: settings.ollama_low_model,
        LLMProfile.MEDIUM: settings.ollama_medium_model,
        LLMProfile.HIGH: settings.ollama_high_model,
    }

    return ChatOllama(
        model=model_map[profile],
        base_url=settings.ollama_base_url,
    )