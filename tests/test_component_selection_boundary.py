"""Regression tests for the Component Selection LLM boundary.

Live failures covered:
- Healthcare: unresolved_tradeoffs[*].impact emitted as a list instead
  of a string (13 validation errors, stage failed before normalize).
- Log ingestion: a log-pipeline component categorized outside the
  enum/alias set.

Also locks the parsed=None paths: they must raise explicit ValueError,
never a NoneType AttributeError.

Run: poetry run python -m unittest discover tests
"""
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage
from pydantic import ValidationError

from ai_architect.agents.component_selection import run as run_module
from ai_architect.agents.component_selection.models import (
    ComponentSelectionLLMOutput,
    normalize_component_selection,
)
from ai_architect.domain.capacity.models import CapacityModelResult
from ai_architect.domain.requirements.models import RequirementsExtraction
from ai_architect.services.knowledge.contracts import Evidence, EvidenceBundle


def _output(**overrides):
    payload = {
        "selected_components": [
            {
                "name": "Amazon CloudWatch",
                "provider": "aws",
                "category": "monitoring",
                "role": "collect logs",
                "rationale": "central log aggregation",
                "confidence": "high",
                "evidence_support": "insufficient",
            }
        ],
        "rejected_candidates": [],
        "unresolved_tradeoffs": [
            {
                "description": "Log retention window",
                "options": ["30 days", "90 days"],
                "impact": "storage cost and compliance coverage",
            }
        ],
        "evidence_references": [],
        "assumptions": [],
        "warnings": [],
    }
    payload.update(overrides)
    return payload


def _normalize_payload(payload):
    return normalize_component_selection(
        ComponentSelectionLLMOutput.model_validate(payload)
    )


class ImpactNormalizationTests(unittest.TestCase):
    def test_impact_list_joined_deterministically(self):
        out = _output()
        out["unresolved_tradeoffs"][0]["impact"] = [
            "higher storage cost",
            "longer compliance coverage",
        ]
        result = _normalize_payload(out)
        self.assertEqual(
            result.unresolved_tradeoffs[0].impact,
            "higher storage cost; longer compliance coverage",
        )

    def test_string_impact_unchanged(self):
        result = _normalize_payload(_output())
        self.assertEqual(
            result.unresolved_tradeoffs[0].impact,
            "storage cost and compliance coverage",
        )

    def test_empty_impact_list_becomes_empty_string(self):
        out = _output()
        out["unresolved_tradeoffs"][0]["impact"] = []
        result = _normalize_payload(out)
        self.assertEqual(result.unresolved_tradeoffs[0].impact, "")

    def test_invalid_impact_shapes_rejected_loudly(self):
        for bad in ({"when": "later"}, 42, ["ok", 42], [None]):
            out = _output()
            out["unresolved_tradeoffs"][0]["impact"] = bad
            with self.subTest(impact=bad):
                with self.assertRaises(ValidationError):
                    ComponentSelectionLLMOutput.model_validate(out)

    def test_blank_only_impact_list_folds_to_empty(self):
        out = _output()
        out["unresolved_tradeoffs"][0]["impact"] = ["  ", ""]
        result = _normalize_payload(out)
        self.assertEqual(result.unresolved_tradeoffs[0].impact, "")


class CategoryAliasTests(unittest.TestCase):
    def test_log_family_maps_to_monitoring(self):
        for category in ("logs", "log", "LOGS"):
            out = _output()
            out["selected_components"][0]["category"] = category
            result = _normalize_payload(out)
            self.assertEqual(
                result.selected_components[0].category, "monitoring"
            )

    def test_genuinely_unknown_category_rejected(self):
        out = _output()
        out["selected_components"][0]["category"] = "quantum_flux"
        with self.assertRaises(ValidationError):
            ComponentSelectionLLMOutput.model_validate(out)

    def test_ambiguous_streaming_still_rejected(self):
        # Deliberately excluded from the alias table: messaging,
        # analytics, and integration all stream.
        out = _output()
        out["selected_components"][0]["category"] = "streaming"
        with self.assertRaises(ValidationError):
            ComponentSelectionLLMOutput.model_validate(out)


def _requirements() -> RequirementsExtraction:
    return RequirementsExtraction(
        application_description="log ingestion",
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
    return EvidenceBundle(query="q", evidence=[], total_found=0)


class _FakeBoundAgent:
    def __init__(self, outcome):
        self._outcome = outcome

    def invoke(self, messages):
        if callable(self._outcome):
            return self._outcome()
        return self._outcome


class _FakeRawAgent:
    def __init__(self, content: str):
        self._content = content

    def invoke(self, messages):
        return AIMessage(content=self._content)


class NullResultPathTests(unittest.TestCase):
    def _run(self, bound_outcome, raw_content=""):
        bound = _FakeBoundAgent(bound_outcome)
        raw = _FakeRawAgent(raw_content)
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
            return run_module.run_component_selection(
                _requirements(), _capacity(), _bundle(), "aws"
            )

    def test_none_outcome_fails_explicitly_not_nonetype(self):
        with self.assertRaises(ValueError) as ctx:
            self._run(None)
        self.assertNotIn("NoneType", str(ctx.exception))

    def test_empty_envelope_and_empty_retry_fail_explicitly(self):
        with self.assertRaises(ValueError) as ctx:
            self._run(
                {
                    "raw": AIMessage(content="", tool_calls=[]),
                    "parsed": None,
                    "parsing_error": None,
                },
                raw_content="",
            )
        self.assertIn("empty text", str(ctx.exception))

    def test_parsing_error_fails_explicitly(self):
        with self.assertRaises(ValueError) as ctx:
            self._run(
                {
                    "raw": AIMessage(content=""),
                    "parsed": None,
                    "parsing_error": "boom",
                }
            )
        self.assertIn("could not be parsed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
