from langchain_ollama import ChatOllama

from ai_architect.config.settings import settings

def create_llm() -> ChatOllama:
    
    return ChatOllama(
        model=settings.ollama_model,
        base_url=settings.ollama_base_url,
    )
