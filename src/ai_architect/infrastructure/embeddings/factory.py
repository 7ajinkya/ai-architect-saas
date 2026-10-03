from ai_architect.config.settings import settings


def create_embeddings():
    provider = settings.embedding_provider.lower()
    model = settings.embedding_model

    if provider == "huggingface":
        from langchain_huggingface import HuggingFaceEmbeddings
        return HuggingFaceEmbeddings(model_name=model)

    if provider == "openai":
        from langchain_openai import OpenAIEmbeddings
        return OpenAIEmbeddings(model=model)

    if provider == "ollama":
        from langchain_ollama import OllamaEmbeddings
        return OllamaEmbeddings(model=model, base_url=settings.ollama_base_url)

    raise ValueError(f"Unsupported embedding provider: {provider}")