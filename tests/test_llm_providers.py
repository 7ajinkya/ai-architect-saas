"""Provider selection tests for Ollama and OpenCode Zen.

No network calls and no live API keys: every client is constructed
against patched settings and asserted structurally.

Run: poetry run python -m unittest discover tests
"""
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from ai_architect.config.settings import settings
from ai_architect.infrastructure.llm import factory
from ai_architect.infrastructure.llm.profiles import LLMProfile
from ai_architect.infrastructure.llm.providers import (
    LLMConfigurationError,
    LLMProvider,
    OpenCodeApiStyle,
    parse_provider,
    require_opencode_api_key,
    resolve_opencode_api_style,
)


@contextmanager
def _config(**overrides):
    """Patch settings for one provider scenario."""
    defaults = {
        "ai_architect_llm_provider": "ollama",
        "ai_architect_ollama_base_url": "http://127.0.0.1:11434",
        "ai_architect_ollama_model": "gpt-oss:120b-cloud",
        "ai_architect_ollama_low_model": "",
        "ai_architect_ollama_medium_model": "",
        "ai_architect_ollama_high_model": "",
        "ai_architect_opencode_base_url": "https://opencode.ai/zen/v1",
        "ai_architect_opencode_api_key": "",
        "ai_architect_opencode_model": "",
        "ai_architect_opencode_low_model": "",
        "ai_architect_opencode_medium_model": "",
        "ai_architect_opencode_high_model": "",
        "ai_architect_opencode_api_style": "",
    }
    defaults.update(overrides)
    with patch.multiple(settings, **defaults):
        yield defaults


class ProviderParsingTests(unittest.TestCase):
    def test_valid_providers(self):
        self.assertIs(parse_provider("ollama"), LLMProvider.OLLAMA)
        self.assertIs(parse_provider(" OpenCode "), LLMProvider.OPENCODE)

    def test_invalid_provider_rejected(self):
        for bad in ("bedrock", "", "openai", None):
            with self.assertRaises(LLMConfigurationError):
                parse_provider(bad)


class OllamaProviderTests(unittest.TestCase):
    def test_ollama_selected_and_configured(self):
        with _config():
            llm = factory.create_llm(LLMProfile.HIGH)
        self.assertEqual(type(llm).__name__, "ChatOllama")
        self.assertEqual(llm.model, "gpt-oss:120b-cloud")
        self.assertEqual(
            str(llm.base_url).rstrip("/"),
            "http://127.0.0.1:11434",
        )

    def test_ollama_per_tier_override(self):
        with _config(ai_architect_ollama_medium_model="mid-model:7b"):
            self.assertEqual(
                factory.create_llm(LLMProfile.MEDIUM).model, "mid-model:7b"
            )
            # Other profiles keep the shared model.
            self.assertEqual(
                factory.create_llm(LLMProfile.LOW).model,
                "gpt-oss:120b-cloud",
            )

    def test_ollama_missing_model_rejected(self):
        with _config(ai_architect_ollama_model=""):
            with self.assertRaises(LLMConfigurationError) as ctx:
                factory.create_llm(LLMProfile.LOW)
        self.assertIn("No Ollama model configured", str(ctx.exception))

    def test_ollama_missing_base_url_rejected(self):
        with _config(ai_architect_ollama_base_url=""):
            with self.assertRaises(LLMConfigurationError):
                factory.create_llm(LLMProfile.LOW)


class ApiStyleResolutionTests(unittest.TestCase):
    def test_documented_families(self):
        cases = {
            "gpt-5.5": OpenCodeApiStyle.RESPONSES,
            "grok-4.7": OpenCodeApiStyle.RESPONSES,
            "muse-spark-1.3": OpenCodeApiStyle.RESPONSES,
            "claude-sonnet-4-6": OpenCodeApiStyle.MESSAGES,
            "qwen3.7-plus": OpenCodeApiStyle.MESSAGES,
            "deepseek-v4-pro": OpenCodeApiStyle.CHAT_COMPLETIONS,
            "glm-5.3": OpenCodeApiStyle.CHAT_COMPLETIONS,
            "kimi-k3": OpenCodeApiStyle.CHAT_COMPLETIONS,
            "minimax-m3": OpenCodeApiStyle.CHAT_COMPLETIONS,
            "space-bunny-free": OpenCodeApiStyle.CHAT_COMPLETIONS,
            "nemotron-3-ultra-free": OpenCodeApiStyle.CHAT_COMPLETIONS,
            "big-pickle": OpenCodeApiStyle.CHAT_COMPLETIONS,
        }
        for model, expected in cases.items():
            with self.subTest(model=model):
                self.assertIs(resolve_opencode_api_style(model), expected)

    def test_documented_exceptions(self):
        # qwen3.8-max and grok-build-0.1 differ from their family.
        self.assertIs(
            resolve_opencode_api_style("qwen3.8-max"),
            OpenCodeApiStyle.CHAT_COMPLETIONS,
        )
        self.assertIs(
            resolve_opencode_api_style("grok-build-0.1"),
            OpenCodeApiStyle.CHAT_COMPLETIONS,
        )

    def test_explicit_override_wins(self):
        self.assertIs(
            resolve_opencode_api_style("claude-sonnet-4-6", "responses"),
            OpenCodeApiStyle.RESPONSES,
        )

    def test_unknown_model_never_guessed(self):
        with self.assertRaises(LLMConfigurationError) as ctx:
            resolve_opencode_api_style("some-unreleased-model")
        self.assertIn("Cannot determine", str(ctx.exception))

    def test_unimplemented_families_fail_loudly(self):
        for model in ("gemini-3-pro", "jev-1.13"):
            with self.subTest(model=model):
                with self.assertRaises(LLMConfigurationError) as ctx:
                    resolve_opencode_api_style(model)
                self.assertIn("does not implement", str(ctx.exception))

    def test_missing_model_rejected(self):
        with self.assertRaises(LLMConfigurationError):
            resolve_opencode_api_style("")

    def test_invalid_override_rejected(self):
        with self.assertRaises(LLMConfigurationError):
            resolve_opencode_api_style("gpt-5.5", "grpc")


class OpenCodeProviderTests(unittest.TestCase):
    def test_chat_completions_client(self):
        with _config(
            ai_architect_llm_provider="opencode",
            ai_architect_opencode_api_key="test-key",
            ai_architect_opencode_model="deepseek-v4-pro",
        ):
            llm = factory.create_llm(LLMProfile.HIGH)
        self.assertEqual(type(llm).__name__, "ChatOpenAI")
        self.assertEqual(llm.model, "deepseek-v4-pro")
        self.assertEqual(
            str(llm.openai_api_base).rstrip("/"),
            "https://opencode.ai/zen/v1",
        )
        self.assertFalse(llm.use_responses_api)

    def test_responses_client(self):
        with _config(
            ai_architect_llm_provider="opencode",
            ai_architect_opencode_api_key="test-key",
            ai_architect_opencode_model="gpt-5.5",
        ):
            llm = factory.create_llm(LLMProfile.HIGH)
        self.assertEqual(type(llm).__name__, "ChatOpenAI")
        self.assertTrue(llm.use_responses_api)

    def test_messages_client(self):
        with _config(
            ai_architect_llm_provider="opencode",
            ai_architect_opencode_api_key="test-key",
            ai_architect_opencode_model="claude-sonnet-4-6",
        ):
            llm = factory.create_llm(LLMProfile.HIGH)
        self.assertEqual(type(llm).__name__, "ChatAnthropic")
        self.assertEqual(llm.model, "claude-sonnet-4-6")
        self.assertEqual(str(llm.anthropic_api_url).rstrip("/"),
                         "https://opencode.ai/zen")

    def test_missing_api_key_rejected(self):
        with _config(
            ai_architect_llm_provider="opencode",
            ai_architect_opencode_model="deepseek-v4-pro",
        ):
            with self.assertRaises(LLMConfigurationError) as ctx:
                factory.create_llm(LLMProfile.LOW)
        self.assertIn("API_KEY", str(ctx.exception))

    def test_missing_model_rejected(self):
        with _config(
            ai_architect_llm_provider="opencode",
            ai_architect_opencode_api_key="test-key",
        ):
            with self.assertRaises(LLMConfigurationError) as ctx:
                factory.create_llm(LLMProfile.LOW)
        self.assertIn("No OpenCode model configured", str(ctx.exception))

    def test_api_key_never_leaks_into_error_text(self):
        secret = "sk-super-secret-value"
        with _config(
            ai_architect_llm_provider="opencode",
            ai_architect_opencode_api_key=secret,
            ai_architect_opencode_model="totally-unknown-model",
        ):
            with self.assertRaises(LLMConfigurationError) as ctx:
                factory.create_llm(LLMProfile.LOW)
        self.assertNotIn(secret, str(ctx.exception))

    def test_per_tier_override(self):
        with _config(
            ai_architect_llm_provider="opencode",
            ai_architect_opencode_api_key="test-key",
            ai_architect_opencode_model="glm-5.3",
            ai_architect_opencode_high_model="gpt-5.5",
        ):
            self.assertEqual(
                factory.create_llm(LLMProfile.HIGH).model, "gpt-5.5"
            )
            self.assertEqual(
                factory.create_llm(LLMProfile.MEDIUM).model, "glm-5.3"
            )


class ProviderSwitchTests(unittest.TestCase):
    def test_switching_provider_is_configuration_only(self):
        with _config():
            self.assertEqual(
                type(factory.create_llm(LLMProfile.LOW)).__name__, "ChatOllama"
            )
        with _config(
            ai_architect_llm_provider="opencode",
            ai_architect_opencode_api_key="test-key",
            ai_architect_opencode_model="deepseek-v4-pro",
        ):
            self.assertEqual(
                type(factory.create_llm(LLMProfile.LOW)).__name__, "ChatOpenAI"
            )

    def test_invalid_provider_rejected_by_factory(self):
        with _config(ai_architect_llm_provider="anthropic-direct"):
            with self.assertRaises(LLMConfigurationError):
                factory.create_llm(LLMProfile.LOW)


class ApiKeyHelperTests(unittest.TestCase):
    def test_blank_key_rejected(self):
        for blank in ("", "   ", None):
            with self.assertRaises(LLMConfigurationError):
                require_opencode_api_key(blank)

    def test_key_returned_trimmed(self):
        self.assertEqual(
            require_opencode_api_key("  abc  "), "abc"
        )


if __name__ == "__main__":
    unittest.main()
