"""Regression: Settings attribute names the callers actually depend on.

The Ollama settings were renamed from ``ollama_*`` to
``ai_architect_ollama_*``. Two callers were not updated, so ``ai-architect
health`` and the Ollama embeddings branch both raised ``AttributeError``
while the test suite stayed green: no test exercised either path.

This test pins the invariant that caused it. It asserts the specific
attributes reachable code depends on really exist, rather than snapshotting
the whole Settings surface, so renaming an unrelated field does not break
it and a rename that breaks a caller does.

Deliberately narrow: it does not redesign health, does not reach the
network, and does not construct any model client.

Run: poetry run python -m unittest tests.test_settings_attribute_contract
"""
import unittest

from ai_architect.config.settings import Settings, settings


class SettingsAttributeTests(unittest.TestCase):
    def test_health_command_dependencies_exist(self):
        """cli.main.run_health reads exactly these two."""
        for attribute in (
            "ai_architect_ollama_base_url",
            "qdrant_url",
        ):
            with self.subTest(attribute=attribute):
                self.assertTrue(hasattr(settings, attribute))
                self.assertIsInstance(getattr(settings, attribute), str)

    def test_ollama_base_url_is_the_renamed_field(self):
        """The pre-rename name must stay gone, so nothing drifts back."""
        self.assertFalse(hasattr(settings, "ollama_base_url"))
        self.assertTrue(
            settings.ai_architect_ollama_base_url.startswith("http")
        )

    def test_embeddings_factory_ollama_branch_resolves_base_url(self):
        """The dormant ollama branch must not hold a stale reference."""
        import ai_architect.infrastructure.embeddings.factory as factory

        with open(factory.__file__, encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("settings.ollama_base_url", text)
        self.assertIn("settings.ai_architect_ollama_base_url", text)

    def test_no_stale_unprefixed_ollama_references_in_repo_modules(self):
        """Sweep the settings consumers that survived the rename."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "ai_architect"
        stale = []
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "settings.ollama_base_url" in text:
                stale.append(str(path.relative_to(root)))
        self.assertEqual(stale, [])


class SettingsAliasTests(unittest.TestCase):
    def test_env_aliases_still_supported(self):
        """Renaming the attribute must not break existing .env files."""
        field = Settings.model_fields["ai_architect_ollama_base_url"]
        aliases = {
            choice
            for choice in field.validation_alias.choices
            if isinstance(choice, str)
        }
        self.assertIn("AI_ARCHITECT_OLLAMA_BASE_URL", aliases)
        self.assertIn("OLLAMA_BASE_URL", aliases)


if __name__ == "__main__":
    unittest.main()