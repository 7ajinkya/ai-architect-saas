"""Focused regression tests for the Component Selection empty-response fallback.

Covers, with all LLM calls faked:
1. tools-bound invocation returning empty content + no tool calls triggers
   exactly one unbound retry whose text parses into a validated result;
2. a successful tool call never triggers the retry;
3. a non-empty text envelope still parses without any retry.

Run: poetry run python -m unittest discover tests
"""
import json
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage

from ai_architect.agents.component_selection import run as run_module
from ai_architect.agents.component_selection.models import (
    ComponentSelectionLLMOutput,
    ComponentSelectionResult,
)
from ai_architect.domain.capacity.models import CapacityModelResult
from ai_architect.domain.requirements.models import RequirementsExtraction
from ai_architect.services.knowledge.contracts import Evidence, EvidenceBundle

FALLBACK_NOTE = (
    "Model response arrived as text instead of a function call; "
    "parsed deterministically against the same boundary schema."
)

RETRY_JSON = {
    "selected_components": [
        {
            "name": "Amazon S3",
            "provider": "aws",
            "category": "storage",
            "role": "store videos",
            "rationale": "object storage",
            "confidence": "high",
            "evidence_support": "strong",
            "evidence_refs": ["ev-1"],
        }
    ],
    "rejected_candidates": [],
    "unresolved_tradeoffs": [],
    "evidence_references": ["ev-1"],
    "assumptions": [],
    "warnings": [],
}


def _requirements() -> RequirementsExtraction:
    return RequirementsExtraction(
        application_description="video platform",
        cloud_provider="aws",
    )


def _capacity() -> CapacityModelResult:
    return CapacityModelResult(
        peak_write_qps=1.0,
        peak_read_qps=2.0,
        concurrent_connections=3.0,
        storage_growth_bytes_per_day=4.0,
        hot_working_set_bytes=5.0,
        egress_bytes_per_second=6.0,
        formula_version="v1",
        calculation_trace={},
    )


def _bundle() -> EvidenceBundle:
    return EvidenceBundle(
        query="q",
        evidence=[
            Evidence(
                evidence_id="ev-1",
                content="c",
                source="s",
                source_type="live_provider",
                provider="aws",
                document_id="d",
                title="t",
                authority="live_provider",
            )
        ],
        total_found=1,
    )


class _FakeBoundAgent:
    """Stands in for the tools-bound structured agent."""

    def __init__(self, outcome: dict):
        self._outcome = outcome
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        return self._outcome


class _FakeRawAgent:
    """Stands in for the unbound (no-tools) agent."""

    def __init__(self, content: str):
        self._content = content
        self.calls = 0
        self.seen_messages = None

    def invoke(self, messages):
        self.calls += 1
        self.seen_messages = messages
        return AIMessage(content=self._content)


class EmptyResponseFallbackTests(unittest.TestCase):
    def test_empty_tools_response_retries_unbound_and_parses(self):
        bound = _FakeBoundAgent(
            {
                "raw": AIMessage(content="", tool_calls=[]),
                "parsed": None,
                "parsing_error": None,
            }
        )
        raw = _FakeRawAgent(json.dumps(RETRY_JSON))
        with (
            patch.object(
                run_module, "create_component_selection_agent_with_raw",
                return_value=bound,
            ),
            patch.object(
                run_module, "create_component_selection_agent_raw",
                return_value=raw,
            ),
        ):
            result = run_module.run_component_selection(
                _requirements(), _capacity(), _bundle(), "aws"
            )
        self.assertIsInstance(result, ComponentSelectionResult)
        self.assertEqual(bound.calls, 1)
        self.assertEqual(raw.calls, 1)
        # Same prompt was reused for the retry.
        self.assertEqual(len(raw.seen_messages), 2)
        self.assertEqual(
            [c.name for c in result.selected_components], ["Amazon S3"]
        )
        self.assertEqual(result.selected_components[0].evidence_refs, ["ev-1"])
        self.assertIn(FALLBACK_NOTE, result.warnings)

    def test_tool_call_success_never_retries(self):
        parsed = ComponentSelectionLLMOutput(
            selected_components=[
                {
                    "name": "Amazon S3",
                    "provider": "aws",
                    "category": "storage",
                    "role": "store videos",
                    "rationale": "object storage",
                    "confidence": "high",
                    "evidence_support": "strong",
                    "evidence_refs": ["ev-1"],
                }
            ],
            evidence_references=["ev-1"],
        )
        bound = _FakeBoundAgent(
            {
                "raw": AIMessage(content=""),
                "parsed": parsed,
                "parsing_error": None,
            }
        )
        raw = _FakeRawAgent(json.dumps(RETRY_JSON))
        with (
            patch.object(
                run_module, "create_component_selection_agent_with_raw",
                return_value=bound,
            ),
            patch.object(
                run_module, "create_component_selection_agent_raw",
                return_value=raw,
            ),
        ):
            result = run_module.run_component_selection(
                _requirements(), _capacity(), _bundle(), "aws"
            )
        self.assertEqual(raw.calls, 0)
        self.assertEqual(
            [c.name for c in result.selected_components], ["Amazon S3"]
        )
        self.assertNotIn(FALLBACK_NOTE, result.warnings)

    def test_nonempty_text_envelope_parses_without_retry(self):
        bound = _FakeBoundAgent(
            {
                "raw": AIMessage(content=json.dumps(RETRY_JSON)),
                "parsed": None,
                "parsing_error": None,
            }
        )
        raw = _FakeRawAgent("should never be used")
        with (
            patch.object(
                run_module, "create_component_selection_agent_with_raw",
                return_value=bound,
            ),
            patch.object(
                run_module, "create_component_selection_agent_raw",
                return_value=raw,
            ),
        ):
            result = run_module.run_component_selection(
                _requirements(), _capacity(), _bundle(), "aws"
            )
        self.assertEqual(raw.calls, 0)
        self.assertEqual(
            [c.name for c in result.selected_components], ["Amazon S3"]
        )
        self.assertIn(FALLBACK_NOTE, result.warnings)

    def test_empty_retry_still_fails_loudly(self):
        bound = _FakeBoundAgent(
            {
                "raw": AIMessage(content="", tool_calls=[]),
                "parsed": None,
                "parsing_error": None,
            }
        )
        raw = _FakeRawAgent("")
        with (
            patch.object(
                run_module, "create_component_selection_agent_with_raw",
                return_value=bound,
            ),
            patch.object(
                run_module, "create_component_selection_agent_raw",
                return_value=raw,
            ),
        ):
            with self.assertRaises(ValueError) as ctx:
                run_module.run_component_selection(
                    _requirements(), _capacity(), _bundle(), "aws"
                )
        self.assertIn("empty text", str(ctx.exception))
        # R1 bounded recovery: the unparseable envelope triggers exactly one
        # strict-format retry, and the retry re-runs the same envelope path
        # (including its transport fallback), so the unbound agent is
        # invoked once per attempt and the run still fails loudly.
        self.assertEqual(raw.calls, 2)


if __name__ == "__main__":
    unittest.main()
