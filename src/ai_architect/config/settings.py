from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()

class Settings(BaseSettings):
    ollama_base_url: str
    ollama_model: str

    langsmith_tracing: bool
    langsmith_api_key: str | None = None
    langsmith_project: str = "ai-architect"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

settings = Settings()
