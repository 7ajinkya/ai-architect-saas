"""Provider-agnostic LLM factory.

Agents call `create_llm(profile)` and receive a LangChain chat model.
Which provider backs that model, and how the requested reasoning effort
maps onto that model's real capabilities, is decided here from
configuration and the capability registry only. No agent contains
provider-specific logic, model names, or effort strings.
"""

from langchain_core.language_models import BaseChatModel

from ai_architect.config.settings import settings
from ai_architect.infrastructure.llm.capabilities import (
    LLMRequestMetadata,
    ModelAccess,
    ModelRegistry,
    ReasoningEffort,
    REGISTRY,
    resolve_effort,
)
from ai_architect.infrastructure.llm.profiles import LLMProfile
from ai_architect.infrastructure.llm.providers import (
    LLMConfigurationError,
    LLMProvider,
    OpenCodeApiStyle,
    parse_provider,
    require_opencode_api_key,
    resolve_opencode_api_style,
)

_EFFORT_SETTING = {
    LLMProfile.LOW: "ai_architect_llm_effort_low",
    LLMProfile.MEDIUM: "ai_architect_llm_effort_medium",
    LLMProfile.HIGH: "ai_architect_llm_effort_high",
}


def parse_effort(value: str) -> ReasoningEffort:
    """Parse a configured effort, failing loudly on a typo."""
    text = (value or "").strip().lower()
    if not text:
        return ReasoningEffort.DEFAULT
    try:
        return ReasoningEffort(text)
    except ValueError as exc:
        allowed = ", ".join(e.value for e in ReasoningEffort)
        raise LLMConfigurationError(
            f"Invalid reasoning effort {value!r}. Allowed: {allowed}."
        ) from exc


def requested_effort(profile: LLMProfile) -> ReasoningEffort:
    """Effort requested for a profile: per-profile override, else global."""
    override = getattr(settings, _EFFORT_SETTING[profile], "") or ""
    if override.strip():
        return parse_effort(override)
    return parse_effort(settings.ai_architect_llm_effort)


def _tier_model(base: str, low: str, medium: str, high: str,
                profile: LLMProfile) -> str:
    """Per-tier model, falling back to the single shared model."""
    tier = {
        LLMProfile.LOW: low,
        LLMProfile.MEDIUM: medium,
        LLMProfile.HIGH: high,
    }[profile].strip()
    return tier or base.strip()


def _ollama_model(profile: LLMProfile) -> str:
    model = _tier_model(
        settings.ai_architect_ollama_model,
        settings.ai_architect_ollama_low_model,
        settings.ai_architect_ollama_medium_model,
        settings.ai_architect_ollama_high_model,
        profile,
    )
    if not model:
        raise LLMConfigurationError(
            "No Ollama model configured. Set "
            "AI_ARCHITECT_OLLAMA_MODEL or a per-tier override."
        )
    return model


def _opencode_model(profile: LLMProfile) -> str:
    model = _tier_model(
        settings.ai_architect_opencode_model,
        settings.ai_architect_opencode_low_model,
        settings.ai_architect_opencode_medium_model,
        settings.ai_architect_opencode_high_model,
        profile,
    )
    if not model:
        raise LLMConfigurationError(
            "No OpenCode model configured. Set "
            "AI_ARCHITECT_OPENCODE_MODEL or a per-tier override."
        )
    return model


def describe_request(
    profile: LLMProfile,
    registry: ModelRegistry = REGISTRY,
) -> LLMRequestMetadata:
    """Effective facts about the request the factory would make.

    Exposes provider, model, API style, requested/effective effort and
    whether a cap was applied, so any caller can audit the decision
    without constructing it and without seeing credentials.
    """
    return _plan(profile, registry)[0]


def _plan(profile: LLMProfile, registry: ModelRegistry = REGISTRY):
    """Resolve model, effort and wire kwargs for the active provider."""
    provider = active_provider()
    requested = requested_effort(profile)

    if provider is LLMProvider.OPENCODE:
        model = _opencode_model(profile)
        style = resolve_opencode_api_style(
            model, settings.ai_architect_opencode_api_style
        )
        api_style = style.value
    else:
        model = _ollama_model(profile)
        style = None
        api_style = "ollama"

    capability = registry.resolve(provider, model, style)
    resolution = resolve_effort(capability, requested)

    kwargs: dict = {}
    reason = resolution.reason
    sent = False

    if resolution.wire_value is not None:
        if provider is LLMProvider.OPENCODE and style in (
            OpenCodeApiStyle.CHAT_COMPLETIONS,
            OpenCodeApiStyle.RESPONSES,
        ):
            # Verified effort vocabulary for an OpenAI-style dialect.
            kwargs["reasoning_effort"] = resolution.wire_value
            sent = True
        elif provider is LLMProvider.OPENCODE:
            # Anthropic-style thinking budgets are a different
            # parameterization; no verified mapping, so send nothing.
            reason = (
                f"{resolution.reason} Not sent: the messages API "
                "parameterizes thinking differently and the mapping is "
                "not verified."
            )
        else:
            reason = (
                f"{resolution.reason} Not sent: the Ollama client does "
                "not accept an effort parameter."
            )

    metadata = LLMRequestMetadata(
        provider=provider.value,
        model=model,
        api_style=api_style,
        requested_effort=requested.value,
        effective_effort=resolution.effective.value if resolution.effective else None,
        fallback_applied=resolution.fallback_applied,
        fallback_reason=reason,
        effort_sent=sent,
        capability_source=capability.capability_source,
        access=capability.access.value,
    )
    return metadata, kwargs


def require_callable_model(profile: LLMProfile) -> None:
    """Fail clearly when the configured model cannot be called.

    Zen serves most free-tier models only from inside the OpenCode
    client; a direct API call returns a 403 FreeTierError that looks
    like an auth problem. Raising here gives the real reason instead.
    """
    provider = active_provider()
    if provider is not LLMProvider.OPENCODE:
        return
    model = _opencode_model(profile)
    capability = REGISTRY.resolve(
        provider, model,
        resolve_opencode_api_style(
            model, settings.ai_architect_opencode_api_style
        ),
    )
    if capability.access is ModelAccess.CLIENT_ONLY:
        raise LLMConfigurationError(
            f"Model {model!r} is listed by OpenCode but its free tier is "
            "restricted to the OpenCode client, so it cannot be called "
            "through this integration (the API answers 403 "
            "FreeTierError). Configure a model this client can reach, "
            "such as 'space-bunny-free'."
        )
    if capability.access is ModelAccess.UNKNOWN:
        require_opencode_api_key(settings.ai_architect_opencode_api_key)


def _create_opencode(profile: LLMProfile) -> BaseChatModel:
    """Build a Zen client matching the configured model's API family."""
    model = _opencode_model(profile)
    require_callable_model(profile)
    api_key = require_opencode_api_key(settings.ai_architect_opencode_api_key)
    base_url = settings.ai_architect_opencode_base_url.strip()
    style = resolve_opencode_api_style(
        model, settings.ai_architect_opencode_api_style
    )
    metadata, effort_kwargs = _plan(profile)

    if style is OpenCodeApiStyle.MESSAGES:
        # Anthropic SDK appends /v1/messages, so point it at the Zen root.
        from langchain_anthropic import ChatAnthropic

        zen_root = base_url.rsplit("/v1", 1)[0] if base_url.endswith(
            "/v1"
        ) else base_url
        return ChatAnthropic(
            model=model,
            anthropic_api_url=zen_root,
            anthropic_api_key=api_key,
            metadata=metadata.as_dict(),
            **effort_kwargs,
        )

    from langchain_openai import ChatOpenAI

    # OpenAI SDK appends /chat/completions, or /responses when the
    # Responses API is selected.
    return ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=api_key,
        use_responses_api=style is OpenCodeApiStyle.RESPONSES,
        metadata=metadata.as_dict(),
        **effort_kwargs,
    )


def active_provider() -> LLMProvider:
    """The configured provider, validated."""
    return parse_provider(settings.ai_architect_llm_provider)


def configured_model(profile: LLMProfile) -> str:
    """Resolve the model id for a profile from the active provider.

    Provider-agnostic on purpose: callers (including the design
    orchestrator's model gate) never branch on which provider is set.
    """
    if active_provider() is LLMProvider.OPENCODE:
        return _opencode_model(profile)
    return _ollama_model(profile)


def create_llm(profile: LLMProfile) -> BaseChatModel:
    """Return the chat model for a profile from the active provider."""
    provider = parse_provider(settings.ai_architect_llm_provider)
    metadata, _ = _plan(profile)

    if provider is LLMProvider.OPENCODE:
        return _create_opencode(profile)

    from langchain_ollama import ChatOllama

    base_url = settings.ai_architect_ollama_base_url.strip()
    if not base_url:
        raise LLMConfigurationError(
            "AI_ARCHITECT_OLLAMA_BASE_URL is empty; set the native Ollama "
            "URL (without /v1)."
        )
    # No effort kwargs: the Ollama client does not accept one. The
    # request facts are still attached so traces and diagnostics match
    # the OpenCode path.
    return ChatOllama(
        model=_ollama_model(profile),
        base_url=base_url,
        metadata=metadata.as_dict(),
    )
