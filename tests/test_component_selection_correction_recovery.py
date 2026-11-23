"""Regression tests: the single correction retry must not break grounding.

All LLM calls are faked; no Ollama, Qdrant, AWS, or network access.

Live failure covered (session 7b5d61d4-8e2c-45fe-a713-a363b3135a61):
the first attempt selected the CloudWatch alarms bundle without a
Lambda, which the deterministic guard correctly rejected. The
validator-feedback retry then fixed that and regressed a previously
valid area: the model copied the correction prompt's allowed-ID line
("id-a, id-b") into ``evidence_references`` as ONE entry, so every
component citing ``id-a`` was reported as "not declared in
evidence_references" and the packed string was "not in the evidence
bundle" - a run whose first attempt was already fully grounded failed
closed on a pure formatting slip.

Covers, with deterministic scripted model responses:
1. A packed evidence-ID entry whose parts are all real bundle IDs is
   split into one entry per ID (top-level list and per-component refs);
2. The repair is bounded: an entry with any unknown part is left alone
   and still fails closed;
3. The correction prompt lists allowed evidence IDs one per line and
   states the one-ID-per-entry rule;
4. The correction prompt carries the provisionable closed world;
5. The alarm-bundle error names the exact component names to add and
   the selected components that are not provisionable;
6. The closed world reaches the selection prompt for AWS only;
7. The system prompt no longer offers a dashboard as a provisionable
   monitoring component.

Run: python -m unittest discover -s tests
"""
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage

from ai_architect.agents.component_selection import run as run_module
from ai_architect.agents.component_selection.models import (
    ComponentSelectionLLMOutput,
    normalize_component_selection,
)
from ai_architect.agents.component_selection.prompt import (
    COMPONENT_SELECTION_SYSTEM_PROMPT,
)
from ai_architect.domain.capacity.models import CapacityModelResult
from ai_architect.domain.requirements.models import RequirementsExtraction
from ai_architect.services.knowledge.contracts import Evidence, EvidenceBundle

# Two live-provider evidence IDs, in the shape the AWS MCP provider
# emits them: "<source>:<tool>:<timestamp>".
EV_A = "aws-mcp:aws___search_documentation:1792654975.090909"
EV_B = "aws-mcp:aws___search_documentation:1792654984.017666"


def _component(
    name: str,
    *,
    category: str = "storage",
    evidence_refs: list[str] | None = None,
) -> dict:
    return {
        "name": name,
        "provider": "aws",
        "category": category,
        "role": "role",
        "rationale": "rationale",
        "confidence": "high",
        "evidence_support": "strong",
        "evidence_refs": evidence_refs if evidence_refs is not None else [EV_A],
    }


def _parsed(
    components: list[dict],
    declared: list[str],
) -> ComponentSelectionLLMOutput:
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


def _requirements() -> RequirementsExtraction:
    return RequirementsExtraction(
        application_description="multi-tenant practice management",
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
                evidence_id=EV_A,
                content="c",
                source="s",
                source_type="live_provider",
                provider="aws",
                document_id="d",
                title="t",
                authority="live_provider",
            ),
            Evidence(
                evidence_id=EV_B,
                content="c",
                source="s",
                source_type="live_provider",
                provider="aws",
                document_id="d2",
                title="t2",
                authority="live_provider",
            ),
        ],
        total_found=2,
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


def _run_with(bound, raw, provider: str = "aws"):
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
            _requirements(), _capacity(), _bundle(), provider
        )


class PackedEvidenceIdRepairTests(unittest.TestCase):
    def test_packed_declared_entry_is_split_into_one_entry_per_id(self):
        result = normalize_component_selection(
            _parsed(
                [_component("Amazon S3", evidence_refs=[EV_A, EV_B])],
                [f"{EV_A}, {EV_B}"],
            )
        )
        split = run_module._split_joined_evidence_ids(result, _bundle())
        self.assertEqual(split, [f"{EV_A}, {EV_B}"])
        self.assertEqual(result.evidence_references, [EV_A, EV_B])

    def test_packed_component_refs_are_split(self):
        result = normalize_component_selection(
            _parsed(
                [_component("Amazon S3", evidence_refs=[f"{EV_A}, {EV_B}"])],
                [EV_A, EV_B],
            )
        )
        run_module._split_joined_evidence_ids(result, _bundle())
        self.assertEqual(result.selected_components[0].evidence_refs, [EV_A, EV_B])

    def test_entry_with_an_unknown_part_is_left_untouched(self):
        result = normalize_component_selection(
            _parsed(
                [_component("Amazon S3")],
                [f"{EV_A}, ev-not-in-bundle"],
            )
        )
        split = run_module._split_joined_evidence_ids(result, _bundle())
        self.assertEqual(split, [])
        self.assertEqual(
            result.evidence_references, [f"{EV_A}, ev-not-in-bundle"]
        )

    def test_packed_retry_output_passes_validation(self):
        # The live failure shape: attempt 1 fails the alarm-bundle rule,
        # attempt 2 fixes it and packs the declared evidence IDs.
        first = _parsed(
            [
                _component("Amazon API Gateway (HTTP API)", category="networking"),
                _component("Amazon CloudWatch Alarms", category="monitoring"),
            ],
            [EV_A, EV_B],
        )
        second = _parsed(
            [
                _component("Amazon API Gateway (HTTP API)", category="networking"),
                _component("AWS Lambda", category="compute"),
                _component("Amazon CloudWatch Logs", category="monitoring"),
                _component("Amazon CloudWatch Alarms", category="monitoring"),
                _component("Amazon S3", evidence_refs=[EV_A, EV_B]),
            ],
            [f"{EV_A}, {EV_B}"],
        )
        bound = _ScriptedBoundAgent([_outcome(first), _outcome(second)])
        result = _run_with(bound, _FakeRawAgent())
        self.assertEqual(bound.calls, 2)
        self.assertEqual(result.evidence_references, [EV_A, EV_B])
        self.assertEqual(
            result.selected_components[-1].evidence_refs, [EV_A, EV_B]
        )

    def test_packed_retry_output_still_fails_closed_for_unknown_ids(self):
        first = _parsed(
            [
                _component("Amazon API Gateway (HTTP API)", category="networking"),
                _component("Amazon CloudWatch Alarms", category="monitoring"),
            ],
            [EV_A, EV_B],
        )
        second = _parsed(
            [
                _component("Amazon API Gateway (HTTP API)", category="networking"),
                _component("AWS Lambda", category="compute"),
                _component("Amazon CloudWatch Alarms", category="monitoring"),
            ],
            [f"{EV_A}, ev-not-in-bundle"],
        )
        bound = _ScriptedBoundAgent([_outcome(first), _outcome(second)])
        with self.assertRaises(run_module.ComponentSelectionError) as ctx:
            _run_with(bound, _FakeRawAgent())
        self.assertIn("after one correction retry", str(ctx.exception))
        self.assertIn("not declared in evidence_references", str(ctx.exception))


class CorrectionPromptShapeTests(unittest.TestCase):
    def test_allowed_evidence_ids_are_listed_one_per_line(self):
        nudge = run_module._correction_prompt(
            ["some rejection reason"],
            [EV_A, EV_B],
            "aws",
        )
        self.assertIn(f"- {EV_A}\n- {EV_B}", nudge)
        self.assertNotIn(f"Allowed evidence IDs: {EV_A}, {EV_B}", nudge)

    def test_prompt_states_the_one_id_per_entry_rule(self):
        nudge = run_module._correction_prompt([], [EV_A], "aws")
        self.assertIn("ONE ID in ONE list entry", nudge)
        self.assertIn("never join two IDs with a comma", nudge)

    def test_prompt_carries_the_provisionable_closed_world(self):
        nudge = run_module._correction_prompt([], [EV_A], "aws")
        self.assertIn("Provisionable components (closed world)", nudge)
        self.assertIn("Amazon CloudWatch Alarms", nudge)
        self.assertNotIn("Amazon CloudWatch Dashboard", nudge)


class AlarmBundleErrorActionabilityTests(unittest.TestCase):
    def test_error_names_the_exact_components_to_add(self):
        result = normalize_component_selection(
            _parsed(
                [
                    _component("Amazon API Gateway (HTTP API)", category="networking"),
                    _component("Amazon CloudWatch Alarms", category="monitoring"),
                ],
                [EV_A],
            )
        )
        errors = run_module._unsupported_alarm_component_errors(result)
        self.assertEqual(len(errors), 1)
        self.assertIn("requires exactly one HTTP API and one Lambda", errors[0])
        self.assertIn("found 1 API and 0 Lambda", errors[0])
        self.assertIn("one Lambda named exactly 'AWS Lambda'", errors[0])

    def test_error_names_unmatched_selected_components(self):
        result = normalize_component_selection(
            _parsed(
                [
                    _component("Amazon API Gateway (HTTP API)", category="networking"),
                    _component("Amazon CloudWatch Alarms", category="monitoring"),
                    _component("Amazon CloudWatch Dashboard", category="monitoring"),
                ],
                [EV_A],
            )
        )
        errors = run_module._unsupported_alarm_component_errors(result)
        self.assertEqual(len(errors), 1)
        self.assertIn("not provisionable", errors[0])
        self.assertIn("Amazon CloudWatch Dashboard", errors[0])


class SnsSubscriberErrorActionabilityTests(unittest.TestCase):
    def test_sns_without_sqs_is_rejected_with_the_exact_fix(self):
        result = normalize_component_selection(
            _parsed(
                [
                    _component("AWS Lambda", category="compute"),
                    _component("Amazon SNS", category="messaging"),
                ],
                [EV_A],
            )
        )
        errors = run_module._unsupported_messaging_topology_errors(result)
        self.assertEqual(len(errors), 1)
        self.assertIn("exactly one Amazon SQS subscriber", errors[0])
        self.assertIn("no Amazon SQS queue", errors[0])
        self.assertIn("Add exactly one Amazon SQS queue", errors[0])

    def test_sns_with_one_sqs_is_accepted(self):
        result = normalize_component_selection(
            _parsed(
                [
                    _component("Amazon SNS", category="messaging"),
                    _component("Amazon SQS", category="messaging"),
                ],
                [EV_A],
            )
        )
        self.assertEqual(
            run_module._unsupported_messaging_topology_errors(result), []
        )

    def test_sns_with_several_sqs_is_rejected(self):
        result = normalize_component_selection(
            _parsed(
                [
                    _component("Amazon SNS", category="messaging"),
                    _component("Amazon SQS", category="messaging"),
                    _component("Amazon SQS", category="messaging"),
                ],
                [EV_A],
            )
        )
        errors = run_module._unsupported_messaging_topology_errors(result)
        self.assertEqual(len(errors), 1)
        self.assertIn("2 Amazon SQS queues", errors[0])
        self.assertIn("exactly one", errors[0])

    def test_system_prompt_states_the_sns_subscriber_rule(self):
        self.assertIn(
            "topic with exactly one SQS",
            COMPONENT_SELECTION_SYSTEM_PROMPT,
        )
        self.assertIn(
            "Selecting SNS without SQS will be rejected",
            COMPONENT_SELECTION_SYSTEM_PROMPT,
        )


class LogGroupTopologyErrorActionabilityTests(unittest.TestCase):
    def test_lambda_without_a_log_group_is_rejected_with_the_exact_fix(self):
        result = normalize_component_selection(
            _parsed(
                [
                    _component("AWS Lambda", category="compute"),
                    _component(
                        "Amazon API Gateway (HTTP API)",
                        category="networking",
                    ),
                ],
                [EV_A],
            )
        )
        errors = run_module._required_log_group_errors(result)
        self.assertEqual(len(errors), 1)
        self.assertIn("'Amazon CloudWatch Logs'", errors[0])
        self.assertIn("exactly one shared log group", errors[0])
        self.assertIn("Add exactly one", errors[0])

    def test_lambda_with_one_log_group_is_accepted(self):
        result = normalize_component_selection(
            _parsed(
                [
                    _component("AWS Lambda", category="compute"),
                    _component("Amazon CloudWatch Logs", category="monitoring"),
                ],
                [EV_A],
            )
        )
        self.assertEqual(run_module._required_log_group_errors(result), [])

    def test_several_log_groups_are_rejected(self):
        result = normalize_component_selection(
            _parsed(
                [
                    _component("AWS Lambda", category="compute"),
                    _component("Amazon CloudWatch Logs", category="monitoring"),
                    _component("Amazon CloudWatch Logs", category="monitoring"),
                ],
                [EV_A],
            )
        )
        errors = run_module._required_log_group_errors(result)
        self.assertEqual(len(errors), 1)
        self.assertIn("2 'Amazon CloudWatch Logs' components", errors[0])
        self.assertIn("exactly one", errors[0])

    def test_a_design_without_lambda_or_api_is_not_forced_to_log(self):
        result = normalize_component_selection(
            _parsed(
                [
                    _component("Amazon S3", category="storage"),
                    _component("Amazon DynamoDB", category="database"),
                ],
                [EV_A],
            )
        )
        self.assertEqual(run_module._required_log_group_errors(result), [])

    def test_system_prompt_states_the_log_group_requirement(self):
        self.assertIn(
            "MUST also select exactly one \"Amazon CloudWatch Logs\"",
            COMPONENT_SELECTION_SYSTEM_PROMPT,
        )
        self.assertIn(
            "Do not add a second log group",
            COMPONENT_SELECTION_SYSTEM_PROMPT,
        )


class ClosedWorldPromptTests(unittest.TestCase):
    def test_closed_world_is_present_for_aws(self):
        bound = _ScriptedBoundAgent(
            [_outcome(_parsed([_component("Amazon S3")], [EV_A]))]
        )
        _run_with(bound, _FakeRawAgent())
        prompt = bound.seen_messages[0][1].content
        self.assertIn("Provisionable components (closed world)", prompt)
        self.assertIn("Amazon CloudWatch Alarms", prompt)
        self.assertIn("Amazon API Gateway (HTTP API)", prompt)
        self.assertNotIn("Amazon CloudWatch Dashboard", prompt)

    def test_closed_world_is_not_injected_for_other_providers(self):
        gcp_component = _component("Google Cloud Storage")
        gcp_component["provider"] = "gcp"
        bound = _ScriptedBoundAgent(
            [_outcome(_parsed([gcp_component], [EV_A]))]
        )
        _run_with(bound, _FakeRawAgent(), provider="gcp")
        prompt = bound.seen_messages[0][1].content
        self.assertNotIn("Provisionable components (closed world)", prompt)

    def test_system_prompt_does_not_offer_dashboards_as_provisionable(self):
        self.assertNotIn("a log group, alarm, dashboard, or trace group", COMPONENT_SELECTION_SYSTEM_PROMPT)
        self.assertIn("closed world", COMPONENT_SELECTION_SYSTEM_PROMPT)

    def test_aws_closed_world_is_the_provisioned_registry(self):
        from ai_architect.services.provisioning.registry import (
            provisionable_component_names,
        )

        names = provisionable_component_names()
        self.assertIn("Amazon S3", names)
        self.assertIn("Amazon CloudWatch Alarms", names)
        self.assertIn("Amazon CloudWatch Logs", names)
        self.assertNotIn("Amazon CloudWatch Dashboard", names)
        for name in names:
            with self.subTest(name=name):
                # Every advertised name resolves back to a contract.
                from ai_architect.services.provisioning.registry import (
                    match_contract,
                )

                self.assertIsNotNone(match_contract(name))


if __name__ == "__main__":
    unittest.main()
