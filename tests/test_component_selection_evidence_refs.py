"""S3 regression tests: declared evidence references must exist in the bundle.

All LLM calls are faked; no Ollama, Qdrant, AWS, or network access.

Background: the frozen validator checks that each component's evidence
refs exist in the bundle and are declared in the top-level
evidence_references list, but never checked the declared list itself
against the bundle. A selection marking every component
evidence_support=insufficient with no refs could declare hallucinated
top-level evidence IDs that then propagated into architecture
traceability unchecked.

Covers, with deterministic scripted model responses:
1. Fully grounded declared references pass unchanged (one call);
2. A declared-but-unknown evidence ID - previously accepted by the
   frozen validator - is now rejected and can recover via the existing
   single correction retry;
3. Mixed declared references are rejected only for the unknown ID;
4. Exhaustion fails loudly with both attempts identified;
5. Blank declared entries are formatting noise and are ignored;
6. Duplicate unknown declared IDs are reported once;
7. The frozen component-level checks are unchanged (the new guard adds
   to the error list, it does not replace anything).

Run: python -m unittest discover -s tests
"""
import json
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage

from ai_architect.agents.component_selection import run as run_module
from ai_architect.agents.component_selection.models import (
    ComponentSelectionLLMOutput,
    normalize_component_selection,
    validate_component_selection,
)
from ai_architect.domain.capacity.models import CapacityModelResult
from ai_architect.domain.requirements.models import RequirementsExtraction
from ai_architect.services.knowledge.contracts import Evidence, EvidenceBundle

VALID_COMPONENT = {
    "name": "Amazon S3",
    "provider": "aws",
    "category": "storage",
    "role": "store objects",
    "rationale": "object storage",
    "confidence": "high",
    "evidence_support": "strong",
    "evidence_refs": ["ev-1"],
}

# The audit-gap fixture: every component is explicitly marked
# insufficient (so the frozen validator's grounding rule passes) while
# the top-level list declares an evidence ID that is not in the bundle.
INSUFFICIENT_COMPONENT = {
    "name": "Amazon S3",
    "provider": "aws",
    "category": "storage",
    "role": "store objects",
    "rationale": "object storage",
    "confidence": "high",
    "evidence_support": "insufficient",
    "evidence_refs": [],
}


def _output(components: list[dict], declared: list[str]) -> dict:
    return {
        "selected_components": components,
        "rejected_candidates": [],
        "unresolved_tradeoffs": [],
        "evidence_references": declared,
        "assumptions": [],
        "warnings": [],
    }


def _parsed(payload: dict) -> ComponentSelectionLLMOutput:
    return ComponentSelectionLLMOutput.model_validate(payload)


def _valid_output(declared: list[str] | None = None) -> ComponentSelectionLLMOutput:
    return _parsed(
        _output([dict(VALID_COMPONENT)], declared or ["ev-1"])
    )


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
        return outcome


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
            run_module,
            "create_component_selection_agent_with_raw",
            return_value=bound,
        ),
        patch.object(
            run_module,
            "create_component_selection_agent_raw",
            return_value=raw,
        ),
    ):
        return run_module.run_component_selection(
            _requirements(), _capacity(), _bundle(), "aws"
        )


class GroundedDeclaredReferencesTests(unittest.TestCase):
    def test_valid_declared_references_pass_without_retry(self):
        bound = _ScriptedBoundAgent([_parsed_outcome(_valid_output())])
        raw = _FakeRawAgent()
        result = _run_with(bound, raw)
        self.assertEqual(bound.calls, 1)
        self.assertEqual(raw.calls, 0)
        self.assertEqual(result.evidence_references, ["ev-1"])
        self.assertEqual(result.warnings, [])

    def test_blank_declared_entries_are_ignored(self):
        output = _parsed(
            _output([dict(VALID_COMPONENT)], ["", "   ", "ev-1"])
        )
        bound = _ScriptedBoundAgent([_parsed_outcome(output)])
        raw = _FakeRawAgent()
        result = _run_with(bound, raw)
        self.assertEqual(bound.calls, 1)
        self.assertEqual(
            [c.name for c in result.selected_components], ["Amazon S3"]
        )


class UnknownDeclaredReferenceTests(unittest.TestCase):
    def test_gap_fixture_passes_frozen_validator_but_not_the_guard(self):
        # Pins the audit gap: the frozen validator alone accepts the
        # fixture that declares a hallucinated top-level evidence ID.
        bad = _parsed(
            _output([dict(INSUFFICIENT_COMPONENT)], ["ev-1", "ev-99"])
        )
        frozen_errors = validate_component_selection(
            result=normalize_component_selection(bad),
            evidence_bundle=_bundle().model_dump(mode="json"),
            provider="aws",
        )
        self.assertEqual(frozen_errors, [])
        guard_errors = run_module._undeclared_evidence_errors(
            normalize_component_selection(bad), _bundle()
        )
        self.assertEqual(len(guard_errors), 1)
        self.assertIn("evidence_references declares 'ev-99'", guard_errors[0])

    def test_unknown_declared_reference_recovers_with_feedback(self):
        bad = _parsed(
            _output([dict(INSUFFICIENT_COMPONENT)], ["ev-1", "ev-99"])
        )
        bound = _ScriptedBoundAgent(
            [_parsed_outcome(bad), _parsed_outcome(_valid_output())]
        )
        raw = _FakeRawAgent()
        result = _run_with(bound, raw)
        self.assertEqual(bound.calls, 2)
        self.assertEqual(raw.calls, 0)
        self.assertEqual(
            [c.name for c in result.selected_components], ["Amazon S3"]
        )
        # The correction nudge carries the rejection reason and the
        # allowed evidence IDs from the bundle.
        nudge = bound.seen_messages[1][2].content
        self.assertIn("REJECTED by deterministic validation", nudge)
        self.assertIn("evidence_references declares 'ev-99'", nudge)
        self.assertIn("ev-1", nudge)

    def test_mixed_declared_references_rejected_for_unknown_only(self):
        bad = _parsed(
            _output([dict(VALID_COMPONENT)], ["ev-1", "ev-ghost"])
        )
        bound = _ScriptedBoundAgent(
            [_parsed_outcome(bad), _parsed_outcome(_valid_output())]
        )
        raw = _FakeRawAgent()
        _run_with(bound, raw)
        self.assertEqual(bound.calls, 2)
        nudge = bound.seen_messages[1][2].content
        self.assertIn("declares 'ev-ghost'", nudge)
        self.assertNotIn("declares 'ev-1'", nudge)

    def test_duplicate_unknown_declared_reference_reported_once(self):
        bad = _parsed(
            _output(
                [dict(INSUFFICIENT_COMPONENT)],
                ["ev-99", "ev-99"],
            )
        )
        guard_errors = run_module._undeclared_evidence_errors(
            normalize_component_selection(bad), _bundle()
        )
        self.assertEqual(len(guard_errors), 1)

    def test_unknown_declared_reference_exhaustion_fails_loudly(self):
        bad = _parsed(
            _output([dict(INSUFFICIENT_COMPONENT)], ["ev-1", "ev-99"])
        )
        bound = _ScriptedBoundAgent(
            [_parsed_outcome(bad), _parsed_outcome(bad)]
        )
        raw = _FakeRawAgent()
        with (
            patch.object(
                run_module,
                "create_component_selection_agent_with_raw",
                return_value=bound,
            ),
            patch.object(
                run_module,
                "create_component_selection_agent_raw",
                return_value=raw,
            ),
        ):
            with self.assertRaises(
                run_module.ComponentSelectionError
            ) as ctx:
                run_module.run_component_selection(
                    _requirements(), _capacity(), _bundle(), "aws"
                )
        exc = ctx.exception
        self.assertIsInstance(exc, ValueError)
        message = str(exc)
        self.assertIn("after one correction retry", message)
        self.assertIn("evidence_references declares 'ev-99'", message)
        self.assertEqual(bound.calls, 2)
        diagnostics = exc.diagnostics
        self.assertEqual(len(diagnostics), 2)
        self.assertEqual(
            [(d["attempt"], d["kind"]) for d in diagnostics],
            [(1, "validation"), (2, "validation")],
        )


class FrozenComponentLevelChecksPreservedTests(unittest.TestCase):
    def test_component_ref_unknown_in_bundle_still_rejected(self):
        # A component citing an unknown evidence ID keeps failing through
        # the pre-existing frozen check (not the new guard).
        payload = _output([dict(VALID_COMPONENT)], ["ev-nope"])
        payload["selected_components"][0]["evidence_refs"] = ["ev-nope"]
        bad = _parsed(payload)
        bound = _ScriptedBoundAgent(
            [_parsed_outcome(bad), _parsed_outcome(_valid_output())]
        )
        raw = _FakeRawAgent()
        result = _run_with(bound, raw)
        self.assertEqual(bound.calls, 2)
        nudge = bound.seen_messages[1][2].content
        self.assertIn(
            "Component 'Amazon S3' references evidence 'ev-nope'", nudge
        )
        self.assertEqual(
            [c.name for c in result.selected_components], ["Amazon S3"]
        )

    def test_undeclared_component_ref_still_rejected(self):
        # A component citing an ID absent from evidence_references keeps
        # failing through the pre-existing frozen check.
        payload = _output([dict(VALID_COMPONENT)], ["ev-1"])
        payload["selected_components"][0]["evidence_refs"] = ["ev-2"]
        bad = _parsed(payload)
        bound = _ScriptedBoundAgent(
            [_parsed_outcome(bad), _parsed_outcome(_valid_output())]
        )
        raw = _FakeRawAgent()
        _run_with(bound, raw)
        self.assertEqual(bound.calls, 2)
        nudge = bound.seen_messages[1][2].content
        self.assertIn(
            "'ev-2' which is not declared in evidence_references", nudge
        )


if __name__ == "__main__":
    unittest.main()
