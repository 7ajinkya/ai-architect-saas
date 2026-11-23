"""Provider-independence guard.

Agents must resolve their chat model through the existing factory and
must not branch on which provider is configured. This test fails if
provider-specific logic leaks into an agent module.

Run: poetry run python -m unittest discover tests
"""
import ast
import pathlib
import unittest

from ai_architect.infrastructure.llm.capabilities import (
    REGISTRY,
    ReasoningEffort,
)
from ai_architect.infrastructure.llm.providers import LLMProvider

REPO_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
AGENTS_DIR = REPO_SRC / "ai_architect" / "agents"

# Provider names, config keys and SDK symbols that must never appear
# inside an agent module.
FORBIDDEN_IN_AGENTS = (
    "opencode",
    "ChatOpenAI",
    "ChatAnthropic",
    "ai_architect_opencode",
    "ai_architect_ollama",
    "ollama_base_url",
    "OPENCODE",
)


class ProviderIndependenceTests(unittest.TestCase):
    def _agent_modules(self):
        return sorted(AGENTS_DIR.rglob("*.py"))

    def test_agents_reference_no_provider_specific_symbols(self):
        offenders = {}
        for path in self._agent_modules():
            text = path.read_text(encoding="utf-8")
            hits = [
                token for token in FORBIDDEN_IN_AGENTS
                if token in text
            ]
            if hits:
                offenders[str(path)] = hits
        self.assertEqual(
            offenders, {},
            msg=f"provider-specific references found in agents: {offenders}",
        )

    def test_agents_name_no_concrete_model(self):
        """Adding a model must never require editing an agent."""
        offenders = {}
        for path in self._agent_modules():
            text = path.read_text(encoding="utf-8")
            hits = [
                model_id
                for model_id in REGISTRY.known_models(LLMProvider.OPENCODE)
                if model_id in text
            ]
            if hits:
                offenders[str(path)] = hits
        self.assertEqual(
            offenders, {},
            msg=f"concrete model names found in agents: {offenders}",
        )

    def test_agents_hard_code_no_reasoning_effort(self):
        """Effort is configuration; agents must not embed wire strings.

        Bare words like "low"/"high" are legitimate English in prompt
        prose, so only unambiguous effort wiring is forbidden, plus an
        effort value appearing on a line that actually talks about
        effort.
        """
        offenders = {}
        wire_values = tuple(
            effort.value for effort in ReasoningEffort
            if effort is not ReasoningEffort.DEFAULT
        )
        identifiers = (
            "reasoning_effort",
            "ai_architect_llm_effort",
            "ReasoningEffort",
        )
        for path in self._agent_modules():
            text = path.read_text(encoding="utf-8")
            hits = [token for token in identifiers if token in text]
            for line in text.splitlines():
                if "effort" in line.lower():
                    hits += [
                        f"{value} ({line.strip()[:60]})"
                        for value in wire_values
                        if f'"{value}"' in line or f"'{value}'" in line
                    ]
            if hits:
                offenders[str(path)] = hits
        self.assertEqual(
            offenders, {},
            msg=f"provider or effort coupling found in agents: {offenders}",
        )

    def test_agents_do_not_import_capability_registry(self):
        """Capability resolution belongs to the factory, not to agents."""
        offenders = []
        for path in self._agent_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if "capabilities" in name:
                        offenders.append(f"{path.name}: {name}")
        self.assertEqual(offenders, [], msg=str(offenders))

    def test_agents_do_not_import_provider_modules(self):
        offenders = []
        for path in self._agent_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if "langchain_openai" in name or "langchain_anthropic" in name:
                        offenders.append(f"{path.name}: {name}")
        self.assertEqual(offenders, [], msg=str(offenders))

    def test_agents_source_llm_only_through_the_factory(self):
        """Every agent LLM module must call create_llm, not build clients."""
        llm_modules = [
            p for p in self._agent_modules() if p.name == "agent.py"
        ]
        self.assertTrue(llm_modules, "expected agent.py modules")
        for path in llm_modules:
            text = path.read_text(encoding="utf-8")
            self.assertIn(
                "create_llm",
                text,
                msg=f"{path.name} does not use the LLM factory",
            )
            self.assertNotIn(
                "ChatOllama(",
                text,
                msg=f"{path.name} constructs a provider client directly",
            )


if __name__ == "__main__":
    unittest.main()
