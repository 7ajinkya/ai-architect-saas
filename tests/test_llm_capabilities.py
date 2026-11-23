"""Model capability registry and reasoning-effort resolution tests.

No network calls and no live API keys: capabilities come from the
curated registry and clients are asserted structurally.

Run: poetry run python -m unittest discover tests
"""
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from ai_architect.config.settings import settings
from ai_architect.infrastructure.llm import factory
from ai_architect.infrastructure.llm.capabilities import (
    CAPABILITY_SOURCE,
    EffortResolution,
    LLMRequestMetadata,
    ModelAccess,
    ModelAvailability,
    ModelProfile,
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
)


@contextmanager
def _config(**overrides):
    defaults = {
        "ai_architect_llm_provider": "ollama",
        "ai_architect_ollama_base_url": "http://127.0.0.1:11434",
        "ai_architect_ollama_model": "gpt-oss:120b-cloud",
        "ai_architect_ollama_low_model": "",
        "ai_architect_ollama_medium_model": "",
        "ai_architect_ollama_high_model": "",
        "ai_architect_opencode_base_url": "https://opencode.ai/zen/v1",
        "ai_architect_opencode_api_key": "test-key",
        "ai_architect_opencode_model": "",
        "ai_architect_opencode_low_model": "",
        "ai_architect_opencode_medium_model": "",
        "ai_architect_opencode_high_model": "",
        "ai_architect_opencode_api_style": "",
        "ai_architect_llm_effort": "default",
        "ai_architect_llm_effort_low": "",
        "ai_architect_llm_effort_medium": "",
        "ai_architect_llm_effort_high": "",
    }
    defaults.update(overrides)
    with patch.multiple(settings, **defaults):
        yield defaults


def _profile(**overrides) -> ModelProfile:
    defaults = {
        "model_id": "test-model",
        "provider": LLMProvider.OPENCODE,
    }
    defaults.update(overrides)
    return ModelProfile(**defaults)


class TestRegistryLookup(unittest.TestCase):
    def test_known_model_returns_verified_profile(self):
        profile = REGISTRY.resolve(
            LLMProvider.OPENCODE, "space-bunny-free"
        )
        self.assertTrue(profile.known)
        self.assertEqual(profile.capability_source, CAPABILITY_SOURCE)
        self.assertIs(profile.supports_reasoning, True)
        self.assertEqual(
            profile.availability, ModelAvailability.AVAILABLE
        )

    def test_effort_levels_match_published_metadata(self):
        profile = REGISTRY.resolve(
            LLMProvider.OPENCODE, "space-bunny-free"
        )
        self.assertEqual(
            profile.supported_effort_levels,
            frozenset(
                {
                    ReasoningEffort.LOW,
                    ReasoningEffort.MEDIUM,
                    ReasoningEffort.HIGH,
                    ReasoningEffort.XHIGH,
                    ReasoningEffort.MAX,
                }
            ),
        )

    def test_unknown_model_yields_unknown_not_false(self):
        profile = REGISTRY.resolve(LLMProvider.OPENCODE, "totally-made-up")
        self.assertFalse(profile.known)
        self.assertIsNone(profile.supports_reasoning)
        self.assertIsNone(profile.supported_effort_levels)
        self.assertIsNone(profile.supports_tool_calls)
        self.assertIsNone(profile.context_window)
        self.assertEqual(profile.availability, ModelAvailability.UNKNOWN)

    def test_strict_lookup_raises_for_unknown_model(self):
        with self.assertRaises(LookupError) as ctx:
            REGISTRY.get_strict(LLMProvider.OPENCODE, "totally-made-up")
        self.assertIn("space-bunny-free", str(ctx.exception))

    def test_strict_lookup_raises_across_providers(self):
        """An Ollama model is never a Zen entry."""
        with self.assertRaises(LookupError):
            REGISTRY.get_strict(LLMProvider.OPENCODE, "gpt-oss:120b-cloud")

    def test_availability_is_independent_of_capability(self):
        """Known but unavailable models keep their known capabilities."""
        profile = REGISTRY.resolve(
            LLMProvider.OPENCODE, "nemotron-3-ultra-free"
        )
        self.assertEqual(
            profile.availability, ModelAvailability.UNAVAILABLE
        )
        self.assertIs(profile.supports_reasoning, True)
        self.assertIsNotNone(profile.context_window)

    def test_api_style_backfilled_without_mutating_registry(self):
        resolved = REGISTRY.resolve(
            LLMProvider.OPENCODE,
            "big-pickle",
            OpenCodeApiStyle.CHAT_COMPLETIONS,
        )
        self.assertEqual(resolved.api_style, OpenCodeApiStyle.CHAT_COMPLETIONS)
        stored = REGISTRY.get_strict(LLMProvider.OPENCODE, "big-pickle")
        self.assertEqual(stored.api_style, OpenCodeApiStyle.CHAT_COMPLETIONS)

    def test_registry_is_extensible_without_agent_changes(self):
        registry = ModelRegistry()
        registry.register(
            _profile(
                model_id="brand-new-free",
                supported_effort_levels=frozenset({ReasoningEffort.LOW}),
                capability_source="manual",
            )
        )
        self.assertEqual(registry.known_models(LLMProvider.OPENCODE), ["brand-new-free"])
        profile = registry.resolve(LLMProvider.OPENCODE, "brand-new-free")
        self.assertIs(profile.supports_reasoning, None)

    def test_unverified_effort_levels_remain_unknown(self):
        """big-pickle has no published effort vocabulary."""
        profile = REGISTRY.resolve(LLMProvider.OPENCODE, "big-pickle")
        self.assertIsNone(profile.supported_effort_levels)
        self.assertIsNone(profile.default_effort)

    def test_no_structured_output_invented(self):
        for model_id in REGISTRY.known_models(LLMProvider.OPENCODE):
            profile = REGISTRY.get_strict(LLMProvider.OPENCODE, model_id)
            self.assertIsNone(
                profile.supports_structured_output,
                f"{model_id} must not claim an unverified capability",
            )


class TestEffortResolution(unittest.TestCase):
    def test_supported_effort_used_as_is(self):
        profile = _profile(
            supported_effort_levels=frozenset(
                {
                    ReasoningEffort.LOW,
                    ReasoningEffort.MEDIUM,
                    ReasoningEffort.HIGH,
                }
            )
        )
        result = resolve_effort(profile, ReasoningEffort.HIGH)
        self.assertEqual(result.effective, ReasoningEffort.HIGH)
        self.assertEqual(result.wire_value, "high")
        self.assertFalse(result.fallback_applied)

    def test_unsupported_effort_caps_to_highest_supported_below(self):
        profile = _profile(
            supported_effort_levels=frozenset(
                {
                    ReasoningEffort.LOW,
                    ReasoningEffort.MEDIUM,
                    ReasoningEffort.HIGH,
                }
            )
        )
        result = resolve_effort(profile, ReasoningEffort.MAX)
        self.assertEqual(result.effective, ReasoningEffort.HIGH)
        self.assertEqual(result.wire_value, "high")
        self.assertTrue(result.fallback_applied)
        self.assertIn("max", result.reason)
        self.assertIn("capped to high", result.reason)

    def test_xhigh_caps_to_high_when_unsupported(self):
        profile = _profile(
            supported_effort_levels=frozenset(
                {
                    ReasoningEffort.LOW,
                    ReasoningEffort.MEDIUM,
                    ReasoningEffort.HIGH,
                }
            )
        )
        result = resolve_effort(profile, ReasoningEffort.XHIGH)
        self.assertEqual(result.effective, ReasoningEffort.HIGH)
        self.assertTrue(result.fallback_applied)

    def test_non_contiguous_support_does_not_invent_a_level(self):
        """fledge-alpha-free publishes low|high|max, no medium.

        Capping is downward only, so `medium` resolves to `low`. The
        engine never invents `medium` and never silently raises the
        request to `high`.
        """
        profile = REGISTRY.resolve(LLMProvider.OPENCODE, "fledge-alpha-free")
        self.assertEqual(
            profile.supported_effort_levels,
            frozenset({ReasoningEffort.LOW, ReasoningEffort.HIGH, ReasoningEffort.MAX}),
        )
        result = resolve_effort(profile, ReasoningEffort.MEDIUM)
        self.assertEqual(result.effective, ReasoningEffort.LOW)
        self.assertTrue(result.fallback_applied)
        self.assertIn("capped to low", result.reason)

    def test_capping_never_increases_beyond_the_request(self):
        """No configured model may end up above what was asked for."""
        order = [
            ReasoningEffort.LOW,
            ReasoningEffort.MEDIUM,
            ReasoningEffort.HIGH,
            ReasoningEffort.XHIGH,
            ReasoningEffort.MAX,
        ]
        for model_id in REGISTRY.known_models(LLMProvider.OPENCODE):
            profile = REGISTRY.get_strict(LLMProvider.OPENCODE, model_id)
            if not profile.supported_effort_levels:
                continue
            for requested in order:
                result = resolve_effort(profile, requested)
                if result.effective is None:
                    continue
                self.assertLessEqual(
                    order.index(result.effective),
                    order.index(requested),
                    f"{model_id}/{requested.value} raised effort to "
                    f"{result.effective.value}",
                )

    def test_request_below_model_floor_sends_nothing(self):
        """Below the floor, sending nothing beats raising the effort."""
        profile = _profile(
            supported_effort_levels=frozenset(
                {ReasoningEffort.MEDIUM, ReasoningEffort.HIGH}
            )
        )
        result = resolve_effort(profile, ReasoningEffort.LOW)
        self.assertIsNone(result.wire_value)
        self.assertIsNone(result.effective)
        self.assertTrue(result.fallback_applied)
        self.assertIn("below the lowest level", result.reason)

    def test_single_level_model_below_floor_sends_nothing(self):
        """exo-free supports only high; asking for low must not escalate."""
        profile = REGISTRY.resolve(LLMProvider.OPENCODE, "exo-free")
        result = resolve_effort(profile, ReasoningEffort.LOW)
        self.assertIsNone(result.wire_value)
        self.assertTrue(result.fallback_applied)
        high = resolve_effort(profile, ReasoningEffort.HIGH)
        self.assertEqual(high.effective, ReasoningEffort.HIGH)
        self.assertFalse(high.fallback_applied)

    def test_unknown_capabilities_send_nothing(self):
        profile = _profile()
        result = resolve_effort(profile, ReasoningEffort.HIGH)
        self.assertIsNone(result.wire_value)
        self.assertIsNone(result.effective)
        self.assertTrue(result.fallback_applied)
        self.assertIn("unknown", result.reason)

    def test_default_effort_uses_model_default(self):
        profile = _profile(
            supported_effort_levels=frozenset(
                {ReasoningEffort.LOW, ReasoningEffort.HIGH}
            ),
            default_effort=ReasoningEffort.LOW,
        )
        result = resolve_effort(profile, ReasoningEffort.DEFAULT)
        self.assertEqual(result.effective, ReasoningEffort.LOW)
        self.assertFalse(result.fallback_applied)

    def test_default_effort_with_unknown_default_sends_nothing(self):
        profile = _profile()
        result = resolve_effort(profile, ReasoningEffort.DEFAULT)
        self.assertIsNone(result.wire_value)
        self.assertFalse(result.fallback_applied)

    def test_default_effort_below_model_minimum_is_capped(self):
        profile = _profile(
            supported_effort_levels=frozenset(
                {ReasoningEffort.LOW, ReasoningEffort.HIGH}
            ),
            default_effort=ReasoningEffort.MEDIUM,
        )
        result = resolve_effort(profile, ReasoningEffort.DEFAULT)
        self.assertIsNone(result.wire_value)

    def test_effort_vocabulary_is_ordered_by_depth(self):
        ranks = [e.value for e in (
            ReasoningEffort.LOW,
            ReasoningEffort.MEDIUM,
            ReasoningEffort.HIGH,
            ReasoningEffort.XHIGH,
            ReasoningEffort.MAX,
        )]
        self.assertEqual(
            ranks,
            ["low", "medium", "high", "xhigh", "max"],
        )


class TestEffortSettings(unittest.TestCase):
    def test_parse_effort_defaults_on_blank(self):
        self.assertIs(factory.parse_effort(""), ReasoningEffort.DEFAULT)

    def test_parse_effort_is_case_insensitive(self):
        self.assertIs(factory.parse_effort(" HIGH "), ReasoningEffort.HIGH)

    def test_parse_effort_rejects_unknown_value(self):
        with self.assertRaises(LLMConfigurationError) as ctx:
            factory.parse_effort("turbo")
        self.assertIn("max", str(ctx.exception))

    def test_global_effort_used_when_no_override(self):
        with _config(ai_architect_llm_effort="high"):
            self.assertIs(
                factory.requested_effort(LLMProfile.MEDIUM), ReasoningEffort.HIGH
            )

    def test_per_profile_effort_overrides_global(self):
        with _config(
            ai_architect_llm_effort="high",
            ai_architect_llm_effort_low="low",
        ):
            self.assertIs(
                factory.requested_effort(LLMProfile.LOW), ReasoningEffort.LOW
            )
            self.assertIs(
                factory.requested_effort(LLMProfile.HIGH), ReasoningEffort.HIGH
            )


class TestFactoryEffortWiring(unittest.TestCase):
    def test_ollama_never_sends_effort(self):
        with _config(ai_architect_llm_effort="max"):
            metadata, kwargs = factory._plan(LLMProfile.LOW)
        self.assertEqual(kwargs, {})
        self.assertFalse(metadata.effort_sent)
        self.assertEqual(metadata.provider, "ollama")

    def test_opencode_sends_verified_effort(self):
        with _config(
            ai_architect_llm_provider="opencode",
            ai_architect_opencode_model="space-bunny-free",
            ai_architect_llm_effort="high",
        ):
            metadata, kwargs = factory._plan(LLMProfile.LOW)
        self.assertEqual(kwargs.get("reasoning_effort"), "high")
        self.assertTrue(metadata.effort_sent)
        self.assertEqual(metadata.effective_effort, "high")
        self.assertFalse(metadata.fallback_applied)
        self.assertEqual(metadata.api_style, "chat_completions")

    def test_opencode_caps_and_reports_unsupported_effort(self):
        with _config(
            ai_architect_llm_provider="opencode",
            ai_architect_opencode_model="fledge-alpha-free",
            ai_architect_llm_effort="medium",
        ):
            metadata, kwargs = factory._plan(LLMProfile.LOW)
        self.assertEqual(kwargs.get("reasoning_effort"), "low")
        self.assertTrue(metadata.effort_sent)
        self.assertTrue(metadata.fallback_applied)
        self.assertEqual(metadata.requested_effort, "medium")
        self.assertEqual(metadata.effective_effort, "low")
        self.assertIn("capped to low", metadata.fallback_reason)

    def test_unknown_model_sends_no_effort(self):
        with _config(
            ai_architect_llm_provider="opencode",
            ai_architect_opencode_model="big-pickle",
            ai_architect_llm_effort="high",
        ):
            metadata, kwargs = factory._plan(LLMProfile.LOW)
        self.assertEqual(kwargs, {})
        self.assertFalse(metadata.effort_sent)
        self.assertTrue(metadata.fallback_applied)

    def test_messages_style_does_not_send_unverified_effort(self):
        with _config(
            ai_architect_llm_provider="opencode",
            ai_architect_opencode_model="claude-sonnet-4-5",
            ai_architect_opencode_api_style="messages",
            ai_architect_llm_effort="high",
        ):
            metadata, kwargs = factory._plan(LLMProfile.LOW)
        self.assertEqual(kwargs, {})
        self.assertFalse(metadata.effort_sent)
        self.assertEqual(metadata.api_style, "messages")

    def test_metadata_exposes_effective_request_facts(self):
        with _config(
            ai_architect_llm_provider="opencode",
            ai_architect_opencode_model="space-bunny-free",
            ai_architect_llm_effort="xhigh",
        ):
            payload = factory.describe_request(LLMProfile.MEDIUM)
        self.assertIsInstance(payload, LLMRequestMetadata)
        self.assertEqual(payload.provider, "opencode")
        self.assertEqual(payload.model, "space-bunny-free")
        self.assertEqual(payload.requested_effort, "xhigh")
        self.assertEqual(payload.effective_effort, "xhigh")

    def test_metadata_never_contains_credentials(self):
        with _config(
            ai_architect_llm_provider="opencode",
            ai_architect_opencode_api_key="super-secret-value",
            ai_architect_opencode_model="space-bunny-free",
            ai_architect_llm_effort="max",
        ):
            rendered = repr(factory.describe_request(LLMProfile.LOW).as_dict())
        self.assertNotIn("super-secret-value", rendered)

    def test_max_effort_accepted_by_space_bunny(self):
        """space-bunny-free publishes max, so it must not be capped."""
        with _config(
            ai_architect_llm_provider="opencode",
            ai_architect_opencode_model="space-bunny-free",
            ai_architect_llm_effort="max",
        ):
            metadata, kwargs = factory._plan(LLMProfile.HIGH)
        self.assertEqual(kwargs.get("reasoning_effort"), "max")
        self.assertFalse(metadata.fallback_applied)
        self.assertEqual(metadata.effective_effort, "max")


class TestClientConstruction(unittest.TestCase):
    def test_opencode_client_carries_effort_and_metadata(self):
        from langchain_openai import ChatOpenAI

        with _config(
            ai_architect_llm_provider="opencode",
            ai_architect_opencode_model="space-bunny-free",
            ai_architect_llm_effort="high",
        ):
            client = factory.create_llm(LLMProfile.LOW)
        self.assertIsInstance(client, ChatOpenAI)
        self.assertEqual(client.reasoning_effort, "high")
        self.assertEqual(client.metadata["llm_model"], "space-bunny-free")
        self.assertEqual(client.metadata["llm_effective_effort"], "high")

    def test_ollama_client_has_no_effort_and_keeps_metadata(self):
        from langchain_ollama import ChatOllama

        with _config(ai_architect_llm_effort="max"):
            client = factory.create_llm(LLMProfile.LOW)
        self.assertIsInstance(client, ChatOllama)
        self.assertNotIn("reasoning_effort", client.model_fields_set)
        self.assertEqual(client.metadata["llm_provider"], "ollama")

    def test_registry_can_be_injected(self):
        registry = ModelRegistry()
        registry.register(
            _profile(
                model_id="space-bunny-free",
                supported_effort_levels=frozenset({ReasoningEffort.LOW}),
                default_effort=ReasoningEffort.LOW,
                capability_source="injected",
            )
        )
        with _config(
            ai_architect_llm_provider="opencode",
            ai_architect_opencode_model="space-bunny-free",
            ai_architect_llm_effort="max",
        ):
            metadata = factory.describe_request(LLMProfile.LOW, registry)
        self.assertEqual(metadata.effective_effort, "low")
        self.assertTrue(metadata.fallback_applied)
        self.assertEqual(metadata.capability_source, "injected")


class TestResolutionShape(unittest.TestCase):
    def test_effort_resolution_is_immutable(self):
        result = EffortResolution(
            requested=ReasoningEffort.LOW,
            effective=ReasoningEffort.LOW,
            wire_value="low",
            fallback_applied=False,
            reason="ok",
        )
        with self.assertRaises(Exception):
            result.wire_value = "high"


class TestModelAccess(unittest.TestCase):
    def test_only_space_bunny_is_verified_api_callable(self):
        callable_models = [
            model_id
            for model_id in REGISTRY.known_models(LLMProvider.OPENCODE)
            if REGISTRY.get_strict(
                LLMProvider.OPENCODE, model_id
            ).access is ModelAccess.API_CALLABLE
        ]
        self.assertEqual(callable_models, ["space-bunny-free"])

    def test_listed_but_client_only_models_are_marked(self):
        profile = REGISTRY.resolve(LLMProvider.OPENCODE, "big-pickle")
        self.assertEqual(profile.availability, ModelAvailability.AVAILABLE)
        self.assertEqual(profile.access, ModelAccess.CLIENT_ONLY)

    def test_client_only_model_fails_before_any_request(self):
        with _config(
            ai_architect_llm_provider="opencode",
            ai_architect_opencode_model="big-pickle",
        ):
            with self.assertRaises(LLMConfigurationError) as ctx:
                factory.create_llm(LLMProfile.LOW)
        message = str(ctx.exception)
        self.assertIn("403", message)
        self.assertIn("space-bunny-free", message)

    def test_callable_model_passes_the_access_check(self):
        with _config(
            ai_architect_llm_provider="opencode",
            ai_architect_opencode_model="space-bunny-free",
        ):
            factory.require_callable_model(LLMProfile.LOW)

    def test_ollama_is_never_gated_by_access(self):
        with _config(ai_architect_llm_provider="ollama"):
            factory.require_callable_model(LLMProfile.LOW)

    def test_unknown_model_access_stays_unknown(self):
        profile = REGISTRY.resolve(LLMProvider.OPENCODE, "never-heard-of-it")
        self.assertEqual(profile.access, ModelAccess.UNKNOWN)

    def test_access_reaches_request_metadata(self):
        with _config(
            ai_architect_llm_provider="opencode",
            ai_architect_opencode_model="space-bunny-free",
        ):
            payload = factory.describe_request(LLMProfile.LOW).as_dict()
        self.assertEqual(payload["llm_access"], "api_callable")


class TestUnmappedPublishedLevels(unittest.TestCase):
    def test_unexpressible_level_is_ignored_not_invented(self):
        """`minimal` exists upstream but is not in the logical vocabulary."""
        profile = REGISTRY.resolve(
            LLMProvider.OPENCODE, "muse-spark-1.3-contributor-free"
        )
        self.assertNotIn("minimal", {e.value for e in profile.supported_effort_levels})
        self.assertEqual(profile.supported_effort_levels,
                         frozenset({ReasoningEffort.LOW, ReasoningEffort.MEDIUM,
                                    ReasoningEffort.HIGH, ReasoningEffort.XHIGH}))

    def test_xhigh_caps_max_for_muse_spark(self):
        profile = REGISTRY.resolve(
            LLMProvider.OPENCODE, "muse-spark-1.3-contributor-free"
        )
        result = resolve_effort(profile, ReasoningEffort.MAX)
        self.assertEqual(result.effective, ReasoningEffort.XHIGH)
        self.assertTrue(result.fallback_applied)

    def test_fully_unexpressible_vocabulary_stays_unknown(self):
        from ai_architect.infrastructure.llm.capabilities import _profile

        profile = _profile("weird", effort=["minimal"])
        self.assertIsNone(profile.supported_effort_levels)
        self.assertIsNone(profile.default_effort)

    def test_default_effort_is_the_lowest_supported_level(self):
        from ai_architect.infrastructure.llm.capabilities import _profile

        profile = _profile(
            "weird2", effort=["minimal", "low", "medium", "high", "xhigh"]
        )
        self.assertEqual(profile.default_effort, ReasoningEffort.LOW)


if __name__ == "__main__":
    unittest.main()
