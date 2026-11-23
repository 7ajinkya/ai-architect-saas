"""Regression tests: selected components must be independently provisionable.

All LLM calls are faked; no Ollama, Qdrant, AWS, or network access.

Live failures covered:
- The model selected "Amazon CloudWatch Metrics (embedded)" for a
  requirement that only asked for CloudWatch Logs. Standard metrics are
  emitted by the services themselves and are not a separately provisionable
  resource.
- The model selected "Amazon CloudWatch Metric Alarm (Lambda error rate)"
  even though the verified alarm capability is the standard four-alarm
  API/Lambda bundle and error-rate/throttling variants are out of scope.

Both components were deterministically refused by provisioning and left the
plan incomplete.

Covers, with deterministic scripted model responses:
1. An explicitly marked embedded capability is rejected and can recover via
   the existing single correction retry;
2. Exhaustion fails loudly with both attempts identified;
3. Legitimate parenthetical variants such as "(On-Demand)" and "(Hosting)"
   remain accepted;
4. The recognized marker set stays narrow and exact;
5. The prompt and correction nudge state the provisionability rule;
6. The frozen evidence validator is unchanged: the new guard adds the
   rejection rather than weakening an existing check;
7. Unsupported CloudWatch alarm variants are rejected and can recover via
   the correction retry;
8. The standard four-alarm bundle remains accepted, but only when the design
   also has exactly one HTTP API and one Lambda.

Run: python -m unittest discover -s tests
"""
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage

from ai_architect.agents.component_selection import run as run_module
from ai_architect.agents.component_selection.models import (
    ComponentSelectionLLMOutput,
    normalize_component_selection,
    validate_component_selection,
)
from ai_architect.agents.component_selection.prompt import (
    COMPONENT_SELECTION_SYSTEM_PROMPT,
)
from ai_architect.domain.capacity.models import CapacityModelResult
from ai_architect.domain.requirements.models import RequirementsExtraction
from ai_architect.services.knowledge.contracts import Evidence, EvidenceBundle


def _component(
    name: str,
    *,
    category: str = "monitoring",
    evidence_support: str = "insufficient",
    evidence_refs: list[str] | None = None,
) -> dict:
    return {
        "name": name,
        "provider": "aws",
        "category": category,
        "role": "observability" if category == "monitoring" else "store data",
        "rationale": "required by the workload",
        "confidence": "high",
        "evidence_support": evidence_support,
        "evidence_refs": evidence_refs or [],
    }


def _parsed(*components: dict) -> ComponentSelectionLLMOutput:
    declared = sorted(
        {
            ref
            for component in components
            for ref in component.get("evidence_refs", [])
        }
    )
    return ComponentSelectionLLMOutput.model_validate(
        {
            "selected_components": [dict(component) for component in components],
            "rejected_candidates": [],
            "unresolved_tradeoffs": [],
            "evidence_references": declared,
            "assumptions": [],
            "warnings": [],
        }
    )


def _embedded() -> dict:
    return _component("Amazon CloudWatch Metrics (embedded)")


def _unsupported_alarm() -> dict:
    return _component(
        "Amazon CloudWatch Metric Alarm (Lambda error rate)",
        evidence_support="strong",
        evidence_refs=["ev-1"],
    )


def _supported_alarm() -> dict:
    return _component(
        "Amazon CloudWatch Alarms (for API Gateway and Lambda metrics)",
        evidence_support="strong",
        evidence_refs=["ev-1"],
    )


def _api_gateway() -> dict:
    return _component(
        "Amazon API Gateway (HTTP API)",
        category="networking",
        evidence_support="strong",
        evidence_refs=["ev-1"],
    )


def _logs() -> dict:
    return _component(
        "Amazon CloudWatch Logs",
        evidence_support="strong",
        evidence_refs=["ev-1"],
    )


def _lambda() -> dict:
    return _component(
        "AWS Lambda",
        category="compute",
        evidence_support="strong",
        evidence_refs=["ev-1"],
    )


def _valid() -> dict:
    return _component(
        "Amazon S3",
        category="storage",
        evidence_support="strong",
        evidence_refs=["ev-1"],
    )


def _requirements() -> RequirementsExtraction:
    return RequirementsExtraction(
        application_description="notes API with CloudWatch Logs",
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


def _outcome(output: ComponentSelectionLLMOutput) -> dict:
    return {
        "raw": AIMessage(content=""),
        "parsed": output,
        "parsing_error": None,
    }


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


class EmbeddedCapabilityGuardTests(unittest.TestCase):
    def test_embedded_capability_recovers_with_feedback(self):
        bound = _ScriptedBoundAgent(
            [
                _outcome(_parsed(_embedded())),
                _outcome(_parsed(_valid())),
            ]
        )
        raw = _FakeRawAgent()
        result = _run_with(bound, raw)
        self.assertEqual(bound.calls, 2)
        self.assertEqual(raw.calls, 0)
        self.assertEqual(
            [component.name for component in result.selected_components],
            ["Amazon S3"],
        )
        nudge = bound.seen_messages[1][2].content
        self.assertIn("not an independently provisionable component", nudge)
        self.assertIn("(embedded)", nudge)
        self.assertIn("independently provisionable", nudge)

    def test_embedded_capability_exhaustion_fails_loudly(self):
        bad = _parsed(_embedded())
        bound = _ScriptedBoundAgent([_outcome(bad), _outcome(bad)])
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
        self.assertIn("after one correction retry", str(exc))
        self.assertIn("not an independently provisionable component", str(exc))
        self.assertEqual(bound.calls, 2)
        self.assertEqual(
            [(entry["attempt"], entry["kind"]) for entry in exc.diagnostics],
            [(1, "validation"), (2, "validation")],
        )

    def test_guard_adds_rejection_without_changing_frozen_validator(self):
        result = normalize_component_selection(_parsed(_embedded()))
        frozen_errors = validate_component_selection(
            result=result,
            evidence_bundle=_bundle().model_dump(mode="json"),
            provider="aws",
        )
        self.assertEqual(frozen_errors, [])
        guard_errors = run_module._non_provisionable_component_errors(result)
        self.assertEqual(len(guard_errors), 1)
        self.assertIn("Amazon CloudWatch Metrics (embedded)", guard_errors[0])


class AlarmVariantGuardTests(unittest.TestCase):
    def test_unsupported_alarm_recovers_with_feedback(self):
        bound = _ScriptedBoundAgent(
            [
                _outcome(_parsed(_unsupported_alarm())),
                _outcome(_parsed(_valid())),
            ]
        )
        raw = _FakeRawAgent()
        result = _run_with(bound, raw)
        self.assertEqual(bound.calls, 2)
        self.assertEqual(raw.calls, 0)
        self.assertEqual(
            [component.name for component in result.selected_components],
            ["Amazon S3"],
        )
        nudge = bound.seen_messages[1][2].content
        self.assertIn("not a supported CloudWatch alarm capability", nudge)
        self.assertIn("standard CloudWatch alarms bundle", nudge)
        self.assertIn("Omit unrequested alerting", nudge)

    def test_supported_alarm_bundle_is_accepted(self):
        bound = _ScriptedBoundAgent(
            [
                _outcome(
                    _parsed(
                        _api_gateway(),
                        _lambda(),
                        _logs(),
                        _supported_alarm(),
                    )
                )
            ]
        )
        raw = _FakeRawAgent()
        result = _run_with(bound, raw)
        self.assertEqual(bound.calls, 1)
        self.assertEqual(
            {component.name for component in result.selected_components},
            {
                "Amazon API Gateway (HTTP API)",
                "AWS Lambda",
                "Amazon CloudWatch Logs",
                "Amazon CloudWatch Alarms "
                "(for API Gateway and Lambda metrics)",
            },
        )

    def test_alarm_bundle_requires_one_api_and_one_lambda(self):
        result = normalize_component_selection(
            _parsed(_lambda(), _supported_alarm())
        )
        errors = run_module._unsupported_alarm_component_errors(result)
        self.assertEqual(len(errors), 1)
        self.assertIn("requires exactly one HTTP API and one Lambda", errors[0])
        self.assertIn("found 0 API and 1 Lambda", errors[0])

    def test_scoped_and_single_metric_alarm_variants_are_rejected(self):
        for name in (
            "Amazon CloudWatch Alarm (Lambda error rate)",
            "Amazon CloudWatch Metric Alarm (Lambda error rate)",
            "Amazon CloudWatch Composite Alarms",
            "Amazon CloudWatch Alarms (error rate)",
            "Amazon CloudWatch Alarms (throttles)",
            "Amazon CloudWatch Alarms (anomaly detection)",
            "Amazon CloudWatch Alarms (metric math)",
        ):
            with self.subTest(name=name):
                result = normalize_component_selection(
                    _parsed(_component(name))
                )
                errors = run_module._unsupported_alarm_component_errors(result)
                self.assertEqual(len(errors), 1)
                self.assertIn(name, errors[0])
                self.assertIn("not a supported CloudWatch alarm", errors[0])


class MarkerScopeTests(unittest.TestCase):
    def test_legitimate_parenthetical_variants_are_accepted(self):
        bound = _ScriptedBoundAgent(
            [
                _outcome(
                    _parsed(
                        _component(
                            "Amazon DynamoDB (On-Demand)",
                            category="database",
                            evidence_support="strong",
                            evidence_refs=["ev-1"],
                        ),
                        _component(
                            "AWS Amplify (Hosting)",
                            category="hosting",
                            evidence_support="strong",
                            evidence_refs=["ev-1"],
                        ),
                    )
                )
            ]
        )
        raw = _FakeRawAgent()
        result = _run_with(bound, raw)
        self.assertEqual(bound.calls, 1)
        self.assertEqual(
            [component.name for component in result.selected_components],
            ["Amazon DynamoDB (On-Demand)", "AWS Amplify (Hosting)"],
        )

    def test_exact_markers_rejected(self):
        for marker in ("embedded", "built-in", "builtin", "implicit", "automatic"):
            with self.subTest(marker=marker):
                result = normalize_component_selection(
                    _parsed(_component(f"Example Service ({marker})"))
                )
                errors = run_module._non_provisionable_component_errors(result)
                self.assertEqual(len(errors), 1)
                self.assertIn(f"marker '{marker}'", errors[0])

    def test_partial_marker_words_are_not_rejected(self):
        for name in (
            "Amazon CloudWatch Logs",
            "Amazon EC2 (automatic scaling)",
            "Amazon S3 (Built-In Encryption Enabled)",
        ):
            with self.subTest(name=name):
                result = normalize_component_selection(_parsed(_component(name)))
                self.assertEqual(
                    run_module._non_provisionable_component_errors(result), []
                )


class PromptGuidanceTests(unittest.TestCase):
    def test_prompt_requires_independently_provisionable_components(self):
        self.assertIn("INDEPENDENTLY PROVISIONABLE", COMPONENT_SELECTION_SYSTEM_PROMPT)
        self.assertIn("automatically emitted telemetry", COMPONENT_SELECTION_SYSTEM_PROMPT)
        self.assertIn("Do not add optional alerting", COMPONENT_SELECTION_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
