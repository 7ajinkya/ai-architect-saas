"""Provider-agnostic model capability registry.

Agents ask for a logical model profile and a logical reasoning effort;
this layer resolves both into a concrete provider/model and the
provider-specific wire parameters. Agents never name a model, a
provider, or an API endpoint.

Capability honesty rules:

- A capability that could not be verified from published metadata is
  ``None`` (unknown), never ``False``. Unknown means "do not send this
  parameter", which preserves the existing behaviour.
- Availability is separate from capability: a model can be known and
  currently unavailable.
- The curated table below is a convenience snapshot, not the product.
  Adding or removing a model is a registry edit, never an agent edit.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ai_architect.infrastructure.llm.providers import (
    LLMProvider,
    OpenCodeApiStyle,
)

# Provenance of the curated capability snapshot.
CAPABILITY_SOURCE = "models.dev/api.json#opencode"
AVAILABILITY_SOURCE = "opencode zen /zen/v1/models"
ACCESS_SOURCE = "verified live probe against zen chat/completions"


class ReasoningEffort(str, Enum):
    """Logical, provider-independent reasoning effort vocabulary."""

    DEFAULT = "default"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


# Increasing reasoning depth, used to cap a request deterministically.
_EFFORT_ORDER = (
    ReasoningEffort.LOW,
    ReasoningEffort.MEDIUM,
    ReasoningEffort.HIGH,
    ReasoningEffort.XHIGH,
    ReasoningEffort.MAX,
)


class ModelAvailability(str, Enum):
    """Whether a known model is currently offered by its provider."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class ModelAccess(str, Enum):
    """Whether this adapter can actually call the model.

    Being listed by the provider is not the same as being reachable.
    Zen serves most free-tier models only from inside the OpenCode
    client, which rejects direct API calls with ``FreeTierError``. That
    is a separate axis from capability and from availability.
    """

    API_CALLABLE = "api_callable"
    CLIENT_ONLY = "opencode_client_only"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ModelProfile:
    """Verified (or explicitly unknown) capabilities of one model."""

    model_id: str
    provider: LLMProvider
    display_name: str = ""
    api_style: Optional[OpenCodeApiStyle] = None
    availability: ModelAvailability = ModelAvailability.UNKNOWN
    access: ModelAccess = ModelAccess.UNKNOWN

    # None means UNKNOWN, not False.
    supports_reasoning: Optional[bool] = None
    supported_effort_levels: Optional[frozenset] = None
    default_effort: Optional[ReasoningEffort] = None
    supports_tool_calls: Optional[bool] = None
    supports_structured_output: Optional[bool] = None
    supports_vision: Optional[bool] = None
    context_window: Optional[int] = None

    capability_source: str = "unknown"

    @property
    def key(self) -> tuple:
        return (self.provider, self.model_id)

    @property
    def known(self) -> bool:
        return self.capability_source != "unknown"


@dataclass(frozen=True)
class EffortResolution:
    """Outcome of mapping a requested effort onto model capabilities."""

    requested: ReasoningEffort
    effective: Optional[ReasoningEffort]
    wire_value: Optional[str]
    fallback_applied: bool
    reason: str


def resolve_effort(
    profile: ModelProfile,
    requested: ReasoningEffort,
) -> EffortResolution:
    """Resolve a requested effort deterministically for a model.

    Policies:

    - ``DEFAULT`` uses the model's default effort, if known.
    - Known supported level: used as-is.
    - Known but unsupported: capped to the highest supported level at or
      below the request, else the lowest supported level. The cap is
      always reported; it is never silent.
    - Unknown capabilities: nothing is sent, so the pre-registry
      behaviour is preserved exactly.
    """
    supported = profile.supported_effort_levels

    if requested is ReasoningEffort.DEFAULT:
        if profile.default_effort is None:
            return EffortResolution(
                requested=requested,
                effective=None,
                wire_value=None,
                fallback_applied=False,
                reason=(
                    "DEFAULT effort requested but the model declares no "
                    "default effort; no effort parameter sent."
                ),
            )
        supported = supported or frozenset({profile.default_effort})
        if profile.default_effort not in supported:
            return EffortResolution(
                requested=requested,
                effective=None,
                wire_value=None,
                fallback_applied=False,
                reason=(
                    "Model default effort is inconsistent with its "
                    "supported levels; no effort parameter sent."
                ),
            )
        return EffortResolution(
            requested=requested,
            effective=profile.default_effort,
            wire_value=profile.default_effort.value,
            fallback_applied=False,
            reason="model default effort applied",
        )

    if not supported:
        return EffortResolution(
            requested=requested,
            effective=None,
            wire_value=None,
            fallback_applied=True,
            reason=(
                f"Reasoning effort levels for {profile.model_id!r} are "
                "unknown from published metadata; no effort parameter sent "
                "and existing model behaviour preserved."
            ),
        )

    if requested in supported:
        return EffortResolution(
            requested=requested,
            effective=requested,
            wire_value=requested.value,
            fallback_applied=False,
            reason="requested effort is supported",
        )

    # Cap deterministically to the highest supported level below the
    # request. If the request sits below the model's floor there is
    # nothing to cap down to, and raising the effort to the model's
    # minimum would silently cost more than was asked for, so nothing
    # is sent instead.
    below = [e for e in _EFFORT_ORDER if e in supported and _rank(e) < _rank(requested)]
    if not below:
        floor = min(supported, key=_rank)
        return EffortResolution(
            requested=requested,
            effective=None,
            wire_value=None,
            fallback_applied=True,
            reason=(
                f"{requested.value} is below the lowest level "
                f"{floor.value} supported by {profile.model_id!r}; no "
                "effort parameter sent rather than raising effort above "
                "the request."
            ),
        )
    chosen = below[-1]
    return EffortResolution(
        requested=requested,
        effective=chosen,
        wire_value=chosen.value,
        fallback_applied=True,
        reason=(
            f"{requested.value} is not supported by {profile.model_id!r}; "
            f"capped to {chosen.value}."
        ),
    )


def _rank(effort: ReasoningEffort) -> int:
    return _EFFORT_ORDER.index(effort)


@dataclass
class LLMRequestMetadata:
    """Effective request facts for tracing, audit and diagnostics.

    Never contains credentials.
    """

    provider: str
    model: str
    api_style: str
    requested_effort: str
    effective_effort: Optional[str]
    fallback_applied: bool
    fallback_reason: str
    effort_sent: bool
    capability_source: str
    access: str

    def as_dict(self) -> dict:
        return {
            "llm_provider": self.provider,
            "llm_model": self.model,
            "llm_api_style": self.api_style,
            "llm_requested_effort": self.requested_effort,
            "llm_effective_effort": self.effective_effort,
            "llm_effort_fallback_applied": self.fallback_applied,
            "llm_effort_fallback_reason": self.fallback_reason,
            "llm_effort_sent": self.effort_sent,
            "llm_capability_source": self.capability_source,
            "llm_access": self.access,
        }


def _known_levels(values: Optional[list]) -> list:
    """Keep only levels expressible in the logical vocabulary.

    A provider may publish levels this system cannot express (for
    example ``minimal``). Those are ignored rather than invented into
    the vocabulary, so an unrecognised level can never crash a lookup.
    """
    kept = []
    for value in values or []:
        try:
            kept.append(ReasoningEffort(value))
        except ValueError:
            continue
    return kept


def _profile(
    model_id: str,
    *,
    effort: Optional[list] = None,
    availability: ModelAvailability = ModelAvailability.UNKNOWN,
    access: ModelAccess = ModelAccess.UNKNOWN,
    display_name: str = "",
    api_style: Optional[OpenCodeApiStyle] = None,
    supports_reasoning: Optional[bool] = True,
    supports_tool_calls: Optional[bool] = True,
    supports_vision: Optional[bool] = None,
    context_window: Optional[int] = None,
) -> ModelProfile:
    levels = _known_levels(effort)
    return ModelProfile(
        model_id=model_id,
        provider=LLMProvider.OPENCODE,
        display_name=display_name or model_id,
        api_style=api_style,
        availability=availability,
        access=access,
        supports_reasoning=supports_reasoning,
        # An empty result stays None (unknown), never an empty set that
        # would masquerade as "supports nothing".
        supported_effort_levels=frozenset(levels) if levels else None,
        default_effort=(
            min(levels, key=_rank) if levels else None
        ),
        supports_tool_calls=supports_tool_calls,
        supports_structured_output=None,  # not verified for these models
        supports_vision=supports_vision,
        context_window=context_window,
        capability_source=CAPABILITY_SOURCE,
    )


class ModelRegistry:
    """Lookup of curated model capabilities, extensible at runtime."""

    def __init__(self) -> None:
        self._profiles: dict = {}

    def register(self, profile: ModelProfile) -> ModelProfile:
        self._profiles[profile.key] = profile
        return profile

    def known_models(self, provider: LLMProvider) -> list[str]:
        return sorted(
            model_id
            for (p, model_id) in self._profiles
            if p is provider
        )

    def get_strict(
        self, provider: LLMProvider, model_id: str
    ) -> ModelProfile:
        """Return a curated profile or raise a configuration error."""
        profile = self._profiles.get((provider, model_id))
        if profile is None:
            known = ", ".join(self.known_models(provider)) or "(none)"
            raise LookupError(
                f"No capability entry for {provider.value}/{model_id}. "
                f"Known models: {known}."
            )
        return profile

    def resolve(
        self,
        provider: LLMProvider,
        model_id: str,
        api_style: Optional[OpenCodeApiStyle] = None,
    ) -> ModelProfile:
        """Return a curated profile, or an all-unknown profile.

        Unknown models must remain usable: Ollama models are chosen by
        the operator and cannot be enumerated, so an unknown model
        yields capabilities of ``None`` (nothing sent) rather than a
        hard failure.
        """
        profile = self._profiles.get((provider, model_id))
        if profile is not None:
            if api_style is not None and profile.api_style is None:
                return ModelProfile(
                    **{**profile.__dict__, "api_style": api_style}
                )
            return profile
        return ModelProfile(
            model_id=model_id,
            provider=provider,
            api_style=api_style,
            capability_source="unknown",
        )


# Curated snapshot. Capability values reflect metadata retrieved from
# the sources named above; access values were confirmed by probing the
# live endpoint. Nothing here is invented: where a value could not be
# verified it stays None/unknown, which means "do not send it".
#
# Verified live finding: of the free-tier models Zen lists, only
# space-bunny-free is callable through a direct API client. The others
# answer 403 FreeTierError ("free tier can only be used from within
# OpenCode"), so access is recorded separately from availability.
REGISTRY = ModelRegistry()

for _profile_entry in (
    _profile(
        "space-bunny-free",
        effort=["low", "medium", "high", "xhigh", "max"],
        availability=ModelAvailability.AVAILABLE,
        access=ModelAccess.API_CALLABLE,
        display_name="Space Bunny Free",
        api_style=OpenCodeApiStyle.CHAT_COMPLETIONS,
        supports_vision=True,
        context_window=1048576,
    ),
    # Listed and known, but rejected by the API: client-only free tier.
    _profile(
        "fledge-alpha-free",
        effort=["low", "high", "max"],
        availability=ModelAvailability.AVAILABLE,
        access=ModelAccess.CLIENT_ONLY,
        display_name="Fledge Alpha Free",
        api_style=OpenCodeApiStyle.CHAT_COMPLETIONS,
        supports_vision=True,
        context_window=1048576,
    ),
    _profile(
        "big-pickle",
        availability=ModelAvailability.AVAILABLE,
        access=ModelAccess.CLIENT_ONLY,
        display_name="Big Pickle",
        api_style=OpenCodeApiStyle.CHAT_COMPLETIONS,
        supports_vision=False,
        context_window=200000,
    ),
    _profile(
        "ling-3.1-flash-free",
        availability=ModelAvailability.AVAILABLE,
        access=ModelAccess.CLIENT_ONLY,
        display_name="Ling 3.1 Flash Free",
        api_style=OpenCodeApiStyle.CHAT_COMPLETIONS,
        supports_vision=False,
        context_window=262144,
    ),
    _profile(
        "longcat-2.5-preview-free",
        availability=ModelAvailability.AVAILABLE,
        access=ModelAccess.CLIENT_ONLY,
        display_name="LongCat 2.5 Preview Free",
        api_style=OpenCodeApiStyle.CHAT_COMPLETIONS,
        supports_vision=True,
        context_window=1000000,
    ),
    _profile(
        "mimo-v2.6-flash-free",
        availability=ModelAvailability.AVAILABLE,
        access=ModelAccess.CLIENT_ONLY,
        display_name="MiMo-V2.6-Flash Free",
        api_style=OpenCodeApiStyle.CHAT_COMPLETIONS,
        supports_vision=True,
        context_window=200000,
    ),
    _profile(
        "nemotron-3.5-lightning-free",
        availability=ModelAvailability.AVAILABLE,
        access=ModelAccess.CLIENT_ONLY,
        display_name="Nemotron 3.5 Lightning Free",
        api_style=OpenCodeApiStyle.CHAT_COMPLETIONS,
        supports_vision=False,
        context_window=262144,
    ),
    # Publishes a `minimal` level that this vocabulary cannot express,
    # so it is ignored; xhigh is the highest expressible level.
    _profile(
        "muse-spark-1.3-contributor-free",
        effort=["minimal", "low", "medium", "high", "xhigh"],
        availability=ModelAvailability.AVAILABLE,
        access=ModelAccess.CLIENT_ONLY,
        display_name="Muse Spark 1.3 Contributor Free",
        api_style=OpenCodeApiStyle.CHAT_COMPLETIONS,
        context_window=1048576,
    ),
    _profile(
        "muse-spark-1.2-contributor-free",
        effort=["minimal", "low", "medium", "high", "xhigh"],
        availability=ModelAvailability.AVAILABLE,
        access=ModelAccess.CLIENT_ONLY,
        display_name="Muse Spark 1.2 Contributor Free",
        api_style=OpenCodeApiStyle.CHAT_COMPLETIONS,
        context_window=1048576,
    ),
    _profile(
        "exo-free",
        effort=["high"],
        availability=ModelAvailability.AVAILABLE,
        access=ModelAccess.CLIENT_ONLY,
        display_name="Exo Free",
        api_style=OpenCodeApiStyle.CHAT_COMPLETIONS,
        context_window=1048576,
    ),
    # Known from the metadata source but not currently listed by Zen.
    _profile(
        "nemotron-3-ultra-free",
        availability=ModelAvailability.UNAVAILABLE,
        display_name="Nemotron 3 Ultra Free",
        api_style=OpenCodeApiStyle.CHAT_COMPLETIONS,
        supports_vision=False,
        context_window=1000000,
    ),
    _profile(
        "ling-3.0-flash-fin-free",
        availability=ModelAvailability.UNAVAILABLE,
        display_name="Ling 3.0 Flash Fin Free",
        api_style=OpenCodeApiStyle.CHAT_COMPLETIONS,
        supports_vision=False,
        context_window=262144,
    ),
):
    REGISTRY.register(_profile_entry)
