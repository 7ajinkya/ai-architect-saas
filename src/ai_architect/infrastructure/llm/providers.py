"""LLM provider selection and OpenCode Zen API-dialect resolution.

AI Architect supports two interchangeable providers behind the existing
`create_llm()` abstraction: local Ollama and OpenCode Zen. Agents stay
provider-agnostic; nothing outside this module knows which provider is
active.

OpenCode Zen is not a single API. Per the published Zen model table,
a given model is served by one of several endpoint families under
`https://opencode.ai/zen/v1`:

- `/responses`         (OpenAI Responses API)   e.g. gpt-5.x, grok-4.x
- `/chat/completions`  (OpenAI-compatible)      e.g. deepseek-*, glm-*, kimi-*
- `/messages`          (Anthropic Messages API) e.g. claude-*, qwen3.5+
- `/models/<id>`       (Google GenAI)           e.g. gemini-*
- `/systemone`                               e.g. jev-*

So the adapter resolves the API style from the configured model and
builds the matching client. An unknown model is a loud configuration
error: it is never silently sent to the wrong endpoint family.
"""

from enum import Enum

# Models whose API style differs from their family default, taken from
# the published Zen endpoint table. Explicit config always wins over
# this table.
OPENCODE_MODEL_STYLE_EXCEPTIONS = {
    "qwen3.8-max": "chat_completions",
    "grok-build-0.1": "chat_completions",
}

# Prefix -> API style for the families Zen publishes.
OPENCODE_MODEL_STYLE_PREFIXES = (
    ("gpt-", "responses"),
    ("grok-", "responses"),
    ("muse-spark", "responses"),
    ("claude-", "messages"),
    ("qwen3", "messages"),
    ("deepseek-", "chat_completions"),
    ("minimax-", "chat_completions"),
    ("glm-", "chat_completions"),
    ("kimi-", "chat_completions"),
    ("mistral-", "chat_completions"),
    ("big-pickle", "chat_completions"),
    ("space-bunny", "chat_completions"),
    ("longcat", "chat_completions"),
    ("exo-", "chat_completions"),
    ("fledge", "chat_completions"),
    ("mimo-", "chat_completions"),
    ("ling-", "chat_completions"),
    ("nemotron", "chat_completions"),
)

# Endpoint families this adapter deliberately does not implement yet.
# Listing them keeps the failure explicit and actionable instead of
# sending an unsupported model to a wrong endpoint.
OPENCODE_UNSUPPORTED_PREFIXES = (
    ("gemini-", "Google GenAI endpoint /models/<id>"),
    ("jev-", "experimental /systemone endpoint"),
)


class LLMConfigurationError(RuntimeError):
    """Invalid or incomplete LLM provider configuration."""


class LLMProvider(str, Enum):
    OLLAMA = "ollama"
    OPENCODE = "opencode"


class OpenCodeApiStyle(str, Enum):
    CHAT_COMPLETIONS = "chat_completions"
    RESPONSES = "responses"
    MESSAGES = "messages"


def parse_provider(value: str | None) -> LLMProvider:
    """Parse the configured provider name, or fail loudly."""
    normalized = (value or "").strip().lower()
    try:
        return LLMProvider(normalized)
    except ValueError as exc:
        supported = ", ".join(p.value for p in LLMProvider)
        raise LLMConfigurationError(
            f"Unsupported LLM provider {value!r}. Supported: {supported}."
        ) from exc


def resolve_opencode_api_style(model: str, override: str = "") -> OpenCodeApiStyle:
    """Resolve the Zen API style for a model.

    Precedence: explicit config override, then the documented
    exceptions table, then family prefix, then a loud error. Never
    guesses: an unrecognised model must be configured explicitly.
    """
    model_id = (model or "").strip().lower()
    if not model_id:
        raise LLMConfigurationError(
            "No OpenCode model configured. Set "
            "AI_ARCHITECT_OPENCODE_MODEL (or a per-tier override)."
        )

    if override:
        try:
            return OpenCodeApiStyle(override.strip().lower())
        except ValueError as exc:
            supported = ", ".join(s.value for s in OpenCodeApiStyle)
            raise LLMConfigurationError(
                f"Unsupported AI_ARCHITECT_OPENCODE_API_STYLE "
                f"{override!r}. Supported: {supported}."
            ) from exc

    if model_id in OPENCODE_MODEL_STYLE_EXCEPTIONS:
        return OpenCodeApiStyle(
            OPENCODE_MODEL_STYLE_EXCEPTIONS[model_id]
        )

    for prefix, endpoint in OPENCODE_UNSUPPORTED_PREFIXES:
        if model_id.startswith(prefix):
            raise LLMConfigurationError(
                f"OpenCode model {model!r} is served by the {endpoint}, "
                "which this client does not implement yet. Choose a "
                "chat-completions, responses, or messages model, or set "
                "AI_ARCHITECT_OPENCODE_API_STYLE once support exists."
            )

    for prefix, style in OPENCODE_MODEL_STYLE_PREFIXES:
        if model_id.startswith(prefix):
            return OpenCodeApiStyle(style)

    raise LLMConfigurationError(
        f"Cannot determine the OpenCode Zen API style for model {model!r}. "
        "Set AI_ARCHITECT_OPENCODE_API_STYLE explicitly to one of "
        "chat_completions, responses, or messages."
    )


def require_opencode_api_key(api_key: str | None) -> str:
    """Return the configured Zen API key, or fail loudly."""
    key = (api_key or "").strip()
    if not key:
        raise LLMConfigurationError(
            "AI_ARCHITECT_LLM_PROVIDER=opencode requires "
            "AI_ARCHITECT_OPENCODE_API_KEY."
        )
    return key
