"""R1 regression tests: bounded Component Selection recovery.

All LLM calls are faked; no Ollama, Qdrant, AWS, or network access.

Covers, with deterministic scripted model responses:
1. Markdown first attempt -> strict-format retry -> valid selection succeeds
   (retry invoked exactly once);
2. Off-taxonomy categories (edge_compute / visualization, replayed from the
   live IoT failure) -> strict-format retry -> valid taxonomy succeeds,
   without adding aliases for those categories;
3. Malformed warnings container (list of dicts, replayed from the live IoT
   failure) -> strict-format retry -> valid shape succeeds;
4. Recovery exhaustion (invalid + invalid) -> deterministic loud failure
   with both attempts identified, no third attempt, no partial artifact;
5. First-attempt success is unchanged: exactly one LLM attempt, no retry,
   output identical to the pre-recovery behavior;
6. Closed-world/evidence behavior intact across recovery: the retry still
   terminates in the same deterministic validation boundary, and the
   taxonomy is unchanged.

Run: python -m unittest discover -s tests
"""
import json
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import ValidationError

from ai_architect.agents.component_selection import run as run_module
from ai_architect.agents.component_selection.models import (
    _CATEGORY_ALIASES,
    ComponentCategory,
    ComponentSelectionLLMOutput,
)
from ai_architect.domain.capacity.models import CapacityModelResult
from ai_architect.domain.requirements.models import RequirementsExtraction
from ai_architect.services.knowledge.contracts import Evidence, EvidenceBundle

EXPECTED_TAXONOMY = {
    "compute", "storage", "database", "messaging", "networking",
    "caching", "cdn", "dns", "load_balancing", "security",
    "monitoring", "ci_cd", "identity", "backup", "ai_ml",
    "analytics", "media", "hosting", "integration", "other",
}

# Replays the live URL-shortener failure shape: a Markdown table with no
# JSON object anywhere in it (so the brace scan raises "substring not
# found" instead of scraping prose).
MARKDOWN_TABLE = (
    "**Component-Selection Result**\n\n"
    "| # | Selected Component | Provider | Category | Role |\n"
    "|---|-------------------|----------|----------|------|\n"
    "| 1 | Amazon S3 | aws | storage | store objects |\n"
)

# Replays the live IoT failure shapes: off-taxonomy categories and a
# warnings container holding dicts instead of strings.
BAD_ENUM_JSON = json.dumps(
    {
        "selected_components": [
            {
                "name": "AWS IoT Core",
                "provider": "aws",
                "category": "edge_compute",
                "role": "ingest device telemetry",
                "rationale": "managed ingestion",
                "confidence": "high",
                "evidence_support": "strong",
                "evidence_refs": ["ev-1"],
            },
            {
                "name": "Amazon QuickSight",
                "provider": "aws",
                "category": "visualization",
                "role": "dashboards",
                "rationale": "managed BI",
                "confidence": "medium",
                "evidence_support": "moderate",
                "evidence_refs": ["ev-1"],
            },
        ],
        "rejected_candidates": [],
        "unresolved_tradeoffs": [],
        "evidence_references": ["ev-1"],
        "assumptions": [],
        "warnings": [
            {"type": "insufficient_evidence", "detail": "auth layer"},
            {"type": "missing_capacity", "detail": "service limits"},
        ],
    }
)

VALID_JSON = {
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


def _parsed_valid() -> ComponentSelectionLLMOutput:
    return ComponentSelectionLLMOutput.model_validate(VALID_JSON)


def _text_outcome(content: str) -> dict:
    return {
        "raw": AIMessage(content=content),
        "parsed": None,
        "parsing_error": None,
    }


def _parsed_outcome(output: ComponentSelectionLLMOutput) -> dict:
    return {"raw": AIMessage(content=""), "parsed": output, "parsing_error": None}


class _ScriptedBoundAgent:
    """Replays one scripted outcome per invoke; records messages per call."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0
        self.seen_messages = []

    def invoke(self, messages):
        self.calls += 1
        self.seen_messages.append(messages)
        outcome = self._outcomes[min(self.calls - 1, len(self._outcomes) - 1)]
        return outcome() if callable(outcome) else outcome


class _FakeRawAgent:
    def __init__(self, content: str = ""):
        self._content = content
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        return AIMessage(content=self._content)


def _run_with(bound, raw):
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


class StrictFormatRetryTests(unittest.TestCase):
    def test_markdown_first_attempt_recovers_on_retry(self):
        bound = _ScriptedBoundAgent(
            [_text_outcome(MARKDOWN_TABLE), _parsed_outcome(_parsed_valid())]
        )
        raw = _FakeRawAgent()
        result = _run_with(bound, raw)
        self.assertEqual(
            [c.name for c in result.selected_components], ["Amazon S3"]
        )
        # Exactly one recovery attempt: two bound invocations, no third.
        self.assertEqual(bound.calls, 2)
        # The transport fallback is untouched: non-empty text never uses it.
        self.assertEqual(raw.calls, 0)
        # Original context preserved, retry nudge appended.
        retry_messages = bound.seen_messages[1]
        self.assertEqual(len(retry_messages), 3)
        self.assertIsInstance(retry_messages[0], SystemMessage)
        self.assertIsInstance(retry_messages[1], HumanMessage)
        self.assertIn("## Evidence Bundle", retry_messages[1].content)
        self.assertIn("ONLY the ComponentSelectionLLMOutput function call", retry_messages[2].content)
        self.assertIn("Markdown", retry_messages[2].content)

    def test_off_taxonomy_categories_recover_without_new_aliases(self):
        # No alias may exist for the live IoT drift values.
        for bad in ("edge_compute", "visualization", "edge_computing",
                    "event_management"):
            self.assertNotIn(bad, _CATEGORY_ALIASES)
        with self.assertRaises(ValidationError):
            ComponentSelectionLLMOutput.model_validate(
                json.loads(BAD_ENUM_JSON)
            )
        bound = _ScriptedBoundAgent(
            [_text_outcome(BAD_ENUM_JSON), _parsed_outcome(_parsed_valid())]
        )
        raw = _FakeRawAgent()
        result = _run_with(bound, raw)
        self.assertEqual(
            [c.name for c in result.selected_components], ["Amazon S3"]
        )
        self.assertEqual(bound.calls, 2)
        self.assertEqual(raw.calls, 0)
        for comp in result.selected_components:
            self.assertIn(str(comp.category), EXPECTED_TAXONOMY)

    def test_malformed_warnings_container_recovers(self):
        payload = dict(VALID_JSON)
        payload["warnings"] = [{"type": "x", "detail": "y"}]
        with self.assertRaises(ValidationError):
            ComponentSelectionLLMOutput.model_validate(payload)
        bound = _ScriptedBoundAgent(
            [
                _text_outcome(json.dumps(payload)),
                _parsed_outcome(_parsed_valid()),
            ]
        )
        raw = _FakeRawAgent()
        result = _run_with(bound, raw)
        self.assertEqual(bound.calls, 2)
        self.assertTrue(
            all(isinstance(w, str) for w in result.warnings),
            "warnings must be plain strings after recovery",
        )


class CorrectionRetryTests(unittest.TestCase):
    def _evidence_mismatch_output(self) -> ComponentSelectionLLMOutput:
        payload = json.loads(json.dumps(VALID_JSON))
        payload["selected_components"][0]["evidence_refs"] = ["ev-nope"]
        payload["evidence_references"] = ["ev-nope"]
        return ComponentSelectionLLMOutput.model_validate(payload)

    def test_validation_failure_recovers_with_feedback(self):
        bound = _ScriptedBoundAgent(
            [
                _parsed_outcome(self._evidence_mismatch_output()),
                _parsed_outcome(_parsed_valid()),
            ]
        )
        raw = _FakeRawAgent()
        result = _run_with(bound, raw)
        self.assertEqual(
            [c.name for c in result.selected_components], ["Amazon S3"]
        )
        self.assertEqual(bound.calls, 2)
        self.assertEqual(raw.calls, 0)
        # Correction prompt carries the contract: taxonomy, evidence IDs,
        # closed-world rules, and the exact rejection reasons.
        retry_messages = bound.seen_messages[1]
        self.assertEqual(len(retry_messages), 3)
        nudge = retry_messages[2].content
        self.assertIn("REJECTED by deterministic validation", nudge)
        self.assertIn("messaging", nudge)
        self.assertIn("other", nudge)
        self.assertIn("ev-1", nudge)
        self.assertIn("ev-nope", nudge)
        self.assertIn("ONLY the ComponentSelectionLLMOutput function call", nudge)
        # Original context preserved.
        self.assertIn("## Evidence Bundle", retry_messages[1].content)


class ExhaustionTests(unittest.TestCase):
    def test_parse_exhaustion_fails_loudly_with_both_attempts(self):
        bound = _ScriptedBoundAgent(
            [_text_outcome(MARKDOWN_TABLE), _text_outcome(MARKDOWN_TABLE)]
        )
        raw = _FakeRawAgent()
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
            with self.assertRaises(run_module.ComponentSelectionError) as ctx:
                run_module.run_component_selection(
                    _requirements(), _capacity(), _bundle(), "aws"
                )
        exc = ctx.exception
        self.assertIsInstance(exc, ValueError)
        message = str(exc)
        self.assertIn("even after one strict-format retry", message)
        self.assertIn("First:", message)
        self.assertIn("Retry:", message)
        # No third attempt, no partial artifact.
        self.assertEqual(bound.calls, 2)
        diagnostics = exc.diagnostics
        self.assertEqual(len(diagnostics), 2)
        self.assertEqual(
            [(d["attempt"], d["kind"]) for d in diagnostics],
            [(1, "parse"), (2, "parse")],
        )
        for entry in diagnostics:
            self.assertIn("errors", entry)
            self.assertTrue(json.dumps(entry, default=str))

    def test_validation_exhaustion_fails_loudly_with_both_attempts(self):
        bad = CorrectionRetryTests()._evidence_mismatch_output()
        bound = _ScriptedBoundAgent(
            [_parsed_outcome(bad), _parsed_outcome(bad)]
        )
        raw = _FakeRawAgent()
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
            with self.assertRaises(run_module.ComponentSelectionError) as ctx:
                run_module.run_component_selection(
                    _requirements(), _capacity(), _bundle(), "aws"
                )
        exc = ctx.exception
        message = str(exc)
        self.assertIn("after one correction retry", message)
        self.assertIn("Component selection validation failed", message)
        self.assertEqual(bound.calls, 2)
        diagnostics = exc.diagnostics
        self.assertEqual(len(diagnostics), 2)
        self.assertEqual(
            [(d["attempt"], d["kind"]) for d in diagnostics],
            [(1, "validation"), (2, "validation")],
        )


class FirstAttemptSuccessTests(unittest.TestCase):
    def test_valid_first_attempt_uses_exactly_one_call(self):
        bound = _ScriptedBoundAgent([_parsed_outcome(_parsed_valid())])
        raw = _FakeRawAgent()
        result = _run_with(bound, raw)
        self.assertEqual(bound.calls, 1)
        self.assertEqual(raw.calls, 0)
        self.assertEqual(len(bound.seen_messages[0]), 2)
        # Output identical to the pre-recovery behavior: the fixture cites
        # its evidence, so no warnings are attached at all.
        self.assertEqual(result.warnings, [])
        self.assertEqual(
            [c.name for c in result.selected_components], ["Amazon S3"]
        )
        self.assertEqual(result.selected_components[0].evidence_refs, ["ev-1"])
        self.assertEqual(result.evidence_references, ["ev-1"])


class ClosedWorldPreservationTests(unittest.TestCase):
    def test_taxonomy_is_unchanged(self):
        self.assertEqual(
            {c.value for c in ComponentCategory}, EXPECTED_TAXONOMY
        )

    def test_retry_cannot_smuggle_provider_mismatch(self):
        payload = json.loads(json.dumps(VALID_JSON))
        payload["selected_components"][0]["provider"] = "gcp"
        bad = ComponentSelectionLLMOutput.model_validate(payload)
        bound = _ScriptedBoundAgent(
            [_parsed_outcome(bad), _parsed_outcome(bad)]
        )
        raw = _FakeRawAgent()
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
            with self.assertRaises(run_module.ComponentSelectionError) as ctx:
                run_module.run_component_selection(
                    _requirements(), _capacity(), _bundle(), "aws"
                )
        self.assertIn("does not match requested provider", str(ctx.exception))
        self.assertEqual(bound.calls, 2)

    def test_max_attempts_constant_is_two(self):
        self.assertEqual(run_module.MAX_SELECTION_ATTEMPTS, 2)


def _amplify_candidate(category: str) -> dict:
    """Replays the live Amplify failure shape: full candidate object."""
    return {
        "name": "AWS Amplify",
        "provider": "aws",
        "category": category,
        "role": "static web hosting for the frontend SPA",
        "rationale": "managed static hosting with CI",
        "confidence": "high",
        "evidence_support": "strong",
        "evidence_refs": ["ev-1"],
    }


def _amplify_output(category: str) -> ComponentSelectionLLMOutput:
    return ComponentSelectionLLMOutput.model_validate({
        "selected_components": [_amplify_candidate(category)],
        "rejected_candidates": [],
        "unresolved_tradeoffs": [],
        "evidence_references": ["ev-1"],
        "assumptions": [],
        "warnings": [],
    })


class HostingCategoryTests(unittest.TestCase):
    """AWS Amplify (Hosting) selects under the canonical role category."""

    def test_hosting_accepted_first_try(self):
        bound = _ScriptedBoundAgent(
            [_parsed_outcome(_amplify_output("hosting"))]
        )
        raw = _FakeRawAgent()
        result = _run_with(bound, raw)
        self.assertEqual(
            [c.name for c in result.selected_components], ["AWS Amplify"]
        )
        self.assertEqual(
            str(result.selected_components[0].category), "hosting"
        )
        self.assertEqual(bound.calls, 1)

    def test_web_hosting_alias_coerces(self):
        bound = _ScriptedBoundAgent(
            [_parsed_outcome(_amplify_output("web_hosting"))]
        )
        raw = _FakeRawAgent()
        result = _run_with(bound, raw)
        self.assertEqual(
            str(result.selected_components[0].category), "hosting"
        )
        self.assertEqual(bound.calls, 1)

    def test_bare_host_still_rejected(self):
        # "host" alone is ambiguous (machine vs hosting service), so it
        # must keep failing loudly rather than mapping somewhere.
        with self.assertRaises(ValidationError):
            _amplify_output("host")

    def test_prompt_lists_hosting_for_first_attempt_compliance(self):
        # The strict-format retry repeats unknown categories when the
        # prompt omits them; the taxonomy must be visible up front.
        from ai_architect.agents.component_selection.prompt import (
            COMPONENT_SELECTION_SYSTEM_PROMPT,
        )

        self.assertIn("hosting", COMPONENT_SELECTION_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
