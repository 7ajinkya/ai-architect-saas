from dotenv import load_dotenv
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


class Settings(BaseSettings):
    # --- LLM provider selection -------------------------------------
    # ollama | opencode. Every agent resolves its client through
    # infrastructure.llm.factory.create_llm(), so switching provider
    # is configuration only.
    ai_architect_llm_provider: str = "ollama"

    # --- Ollama -----------------------------------------------------
    # Native Ollama URL. Do NOT append /v1.
    # The OLLAMA_* spellings are accepted as deprecated aliases so an
    # existing .env keeps working; AI_ARCHITECT_* wins when both set.
    ai_architect_ollama_base_url: str = Field(
        default="http://127.0.0.1:11434",
        validation_alias=AliasChoices(
            "AI_ARCHITECT_OLLAMA_BASE_URL", "OLLAMA_BASE_URL"
        ),
    )
    # Single model applied to every profile; per-tier values below
    # override it when set.
    ai_architect_ollama_model: str = Field(
        default="",
        validation_alias=AliasChoices(
            "AI_ARCHITECT_OLLAMA_MODEL", "OLLAMA_MODEL"
        ),
    )
    ai_architect_ollama_low_model: str = Field(
        default="",
        validation_alias=AliasChoices(
            "AI_ARCHITECT_OLLAMA_LOW_MODEL", "OLLAMA_LOW_MODEL"
        ),
    )
    ai_architect_ollama_medium_model: str = Field(
        default="",
        validation_alias=AliasChoices(
            "AI_ARCHITECT_OLLAMA_MEDIUM_MODEL", "OLLAMA_MEDIUM_MODEL"
        ),
    )
    ai_architect_ollama_high_model: str = Field(
        default="",
        validation_alias=AliasChoices(
            "AI_ARCHITECT_OLLAMA_HIGH_MODEL", "OLLAMA_HIGH_MODEL"
        ),
    )

    # --- OpenCode Zen ----------------------------------------------
    # Zen is not a single API: each model is served by one endpoint
    # family under this base. See infrastructure.llm.providers.
    ai_architect_opencode_base_url: str = "https://opencode.ai/zen/v1"
    ai_architect_opencode_api_key: str = ""
    ai_architect_opencode_model: str = ""
    ai_architect_opencode_low_model: str = ""
    ai_architect_opencode_medium_model: str = ""
    ai_architect_opencode_high_model: str = ""
    # Optional explicit API style: chat_completions | responses |
    # messages. Leave empty to resolve from the model id.
    ai_architect_opencode_api_style: str = ""

    # --- Reasoning effort -------------------------------------------
    # Logical, provider-independent effort requested from the registry:
    # default | low | medium | high | xhigh | max. "default" means the
    # model's own default (and sends nothing if that is unknown).
    # Resolution against a model's real capabilities happens in
    # infrastructure.llm.capabilities; an unsupported request is capped
    # and reported, never silently downgraded.
    ai_architect_llm_effort: str = "default"
    # Optional per-profile override. A profile selects the MODEL; the
    # effort selects reasoning DEPTH. They are independent, so no
    # profile->effort mapping is imposed by default.
    ai_architect_llm_effort_low: str = ""
    ai_architect_llm_effort_medium: str = ""
    ai_architect_llm_effort_high: str = ""

    # --- Architect defaults -----------------------------------------
    # Configurable fallback the architect may apply when the user
    # delegates a material choice ("you decide"). Empty means the
    # architect has no defensible default and must keep asking.
    ai_architect_default_region: str = ""

    # --- LangSmith (unchanged) --------------------------------------
    langsmith_tracing: bool = False
    langsmith_api_key: str | None = None
    langsmith_project: str = "ai-architect"

    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    qdrant_collection_name: str = "architecture_knowledge"

    embedding_provider: str = "huggingface"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
