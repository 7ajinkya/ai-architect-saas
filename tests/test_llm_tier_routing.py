"""Demo-standard model routing: every Ollama tier uses the Large model.

No network calls and no live models: resolution and gate behavior are
asserted against patched settings only.

Run: poetry run python -m unittest discover tests
"""
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from ai_architect.agents.design.run import assert_model_gate
from ai_architect.config.settings import settings
from ai_architect.infrastructure.llm import factory
from ai_architect.infrastructure.llm.profiles import LLMProfile

CLOUD = "gpt-oss:120b-cloud"


@contextmanager
def _config(**overrides):
    """Patch settings for one routing scenario (production default base)."""
    defaults = {
        "ai_architect_llm_provider": "ollama",
        "ai_architect_ollama_base_url": "http://127.0.0.1:11434",
        "ai_architect_ollama_model": CLOUD,
        "ai_architect_ollama_low_model": "",
        "ai_architect_ollama_medium_model": "",
        "ai_architect_ollama_high_model": "",
    }
    defaults.update(overrides)
    with patch.multiple(settings, **defaults):
        yield defaults


class DemoStandardRoutingTests(unittest.TestCase):
    def test_all_tiers_resolve_to_large_model(self):
        with _config():
            for profile in LLMProfile:
                with self.subTest(profile=profile.value):
                    self.assertEqual(
                        factory.configured_model(profile), CLOUD
                    )
                    self.assertEqual(
                        factory.create_llm(profile).model, CLOUD
                    )

    def test_gate_accepts_all_cloud_configuration(self):
        with _config():
            assert_model_gate()

    def test_gate_rejects_any_non_large_tier_model(self):
        for attr in (
            "ai_architect_ollama_low_model",
            "ai_architect_ollama_medium_model",
            "ai_architect_ollama_high_model",
        ):
            with self.subTest(attr=attr):
                with _config(**{attr: "qwen2.5:3b"}):
                    with self.assertRaises(RuntimeError) as ctx:
                        assert_model_gate()
                self.assertIn("Model gate", str(ctx.exception))

    def test_gate_rejects_unknown_model(self):
        with _config(ai_architect_ollama_medium_model="bogus-model:99b"):
            with self.assertRaises(RuntimeError) as ctx:
                assert_model_gate()
        self.assertIn("MEDIUM", str(ctx.exception))

    def test_gate_rejects_empty_model(self):
        with _config(ai_architect_ollama_model=""):
            with self.assertRaises(RuntimeError):
                assert_model_gate()


if __name__ == "__main__":
    unittest.main()
