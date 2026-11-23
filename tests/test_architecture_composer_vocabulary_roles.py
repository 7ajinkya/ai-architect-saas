"""Regression tests for Composer vocabulary leakage and data-store roles.

Two browser failures motivated these tests:

1. The Composer copied ``Amazon API Gateway REST API`` from a selected
   component's upstream ``alternatives`` prose into a decision
   alternative. That token was never vocabulary and must fail closed.
2. The Composer put the selected CloudWatch Log Group in ``data_stores``
   because it retains logs. The deterministic validator permits only
   database/storage/caching/backup roles.

The tests also pin the prompt/retry handoff: the upstream alternative
prose is omitted from the Composer view, canonical IDs are rendered with
their selected categories, and the single correction retry carries both
the vocabulary and the category rule.
"""
import unittest
from datetime import datetime
from unittest.mock import patch

from langchain_core.messages import AIMessage

from ai_architect.agents.architecture import run as run_module
from ai_architect.agents.architecture.run import (
    ArchitectureLLMOutput,
    build_component_references,
)
from ai_architect.agents.component_selection.models import (
    ComponentCandidate,
    ComponentSelectionResult,
)
from ai_architect.domain.capacity.models import CapacityModelResult
from ai_architect.domain.cost.models import CostEstimationResult
from ai_architect.domain.requirements.models import RequirementsExtraction
from ai_architect.services.architecture.validator import validate_architecture

GEN = datetime(2026, 10, 20, 12, 0, 0)
REQ_SESSION = "req-vocab-roles"
DESIGN_SESSION = "design-vocab-roles"
PROVIDER = "aws"
REGION = "ap-south-1"


def _comp(name, category, role, evidence_ref="", alternatives=None):
    return ComponentCandidate(
        name=name,
        provider="aws",
        category=category,
        role=role,
        rationale=f"{role} for the workload",
        evidence_refs=[evidence_ref] if evidence_ref else [],
        alternatives=alternatives or [],
        confidence="high",
        evidence_support="strong" if evidence_ref else "insufficient",
    )


def _selection() -> ComponentSelectionResult:
    return ComponentSelectionResult(
        selected_components=[
            _comp(
                "Amazon API Gateway (HTTP API)",
                "load_balancing",
                "HTTP ingress",
                "ev-1",
                alternatives=["Amazon API Gateway REST API"],
            ),
            _comp("AWS Lambda", "compute", "business logic", "ev-2"),
            _comp(
                "Amazon DynamoDB Table",
                "database",
                "appointment store",
                "ev-3",
            ),
            _comp(
                "Amazon CloudWatch Log Group",
                "monitoring",
                "application logs",
                "ev-4",
            ),
        ],
        rejected_candidates=[
            _comp("Amazon SQS", "messaging", "queue"),
            _comp("Amazon SNS", "messaging", "notifications"),
            _comp("Amazon Cognito", "identity", "user authentication"),
        ],
        evidence_references=["ev-1", "ev-2", "ev-3", "ev-4"],
        generated_at=GEN,
    )


def _extraction() -> RequirementsExtraction:
    return RequirementsExtraction(
        application_description="appointment booking platform",
        cloud_provider="aws",
        region="Mumbai region",
        availability="high availability",
        latency="low latency",
        target_users_dau="50000",
    )


def _capacity() -> CapacityModelResult:
    return CapacityModelResult(
        peak_write_qps=69.4,
        peak_read_qps=347.2,
        concurrent_connections=6000.0,
        storage_growth_bytes_per_day=8000000.0,
        hot_working_set_bytes=240000000.0,
        egress_bytes_per_second=355532.8,
        formula_version="v1",
        calculation_trace={},
    )


def _cost() -> CostEstimationResult:
    return CostEstimationResult(generated_at=GEN, pricing_as_of=GEN)


def _upstream():
    return {
        "extraction": _extraction(),
        "capacity": _capacity(),
        "selection": _selection(),
        "cost": _cost(),
    }


def _valid_shape():
    return {
        "nodes": [
            {
                "component_ref": "amazon-api-gateway-http-api",
                "role_in_system": "HTTP ingress for booking requests",
                "capacity_keys": ["peak_read_qps"],
            },
            {
                "component_ref": "aws-lambda",
                "role_in_system": "run booking business logic",
                "capacity_keys": ["peak_write_qps"],
            },
            {
                "component_ref": "amazon-dynamodb-table",
                "role_in_system": "persist appointments",
                "capacity_keys": ["storage_growth_bytes_per_day"],
            },
            {
                "component_ref": "amazon-cloudwatch-log-group",
                "role_in_system": "collect application logs",
                "capacity_keys": ["peak_write_qps"],
            },
        ],
        "edges": [
            {
                "from": "client",
                "to": "amazon-api-gateway-http-api",
                "kind": "request",
                "protocol": "HTTPS",
                "description": "booking API calls",
                "load_key": "peak_read_qps",
            },
            {
                "from": "amazon-api-gateway-http-api",
                "to": "aws-lambda",
                "kind": "request",
                "description": "invoke booking handler",
            },
            {
                "from": "aws-lambda",
                "to": "amazon-dynamodb-table",
                "kind": "write",
                "description": "store appointment",
            },
            {
                "from": "aws-lambda",
                "to": "amazon-cloudwatch-log-group",
                "kind": "control",
                "description": "emit application logs",
            },
        ],
        "entry_points": ["amazon-api-gateway-http-api"],
        "data_stores": ["amazon-dynamodb-table"],
        "boundaries": ["region:ap-south-1"],
        "decisions": [],
        "assumptions": [],
        "warnings": [],
        "new_tradeoffs": [],
    }


def _normalize(shape):
    up = _upstream()
    parsed = ArchitectureLLMOutput.model_validate(shape)
    return run_module.normalize_architecture(
        parsed,
        extraction=up["extraction"],
        capacity=up["capacity"],
        selection=up["selection"],
        cost=up["cost"],
        region_code=REGION,
        requirements_session_id=REQ_SESSION,
        design_session_id=DESIGN_SESSION,
        provider=PROVIDER,
    )


def _validate(result):
    up = _upstream()
    return validate_architecture(
        result,
        selection=up["selection"],
        capacity=up["capacity"],
        cost=up["cost"],
        extraction=up["extraction"],
        requirements_session_id=REQ_SESSION,
        design_session_id=DESIGN_SESSION,
        provider=PROVIDER,
        region_code=REGION,
    )


class ComposerVocabularyRoleTests(unittest.TestCase):
    def _run(self, *shapes):
        outputs = [
            ArchitectureLLMOutput.model_validate(shape) for shape in shapes
        ]

        class _SequenceBound:
            def __init__(self):
                self.calls = 0
                self.messages = []

            def invoke(self, messages):
                self.messages = messages
                index = min(self.calls, len(outputs) - 1)
                self.calls += 1
                return {
                    "raw": AIMessage(content=""),
                    "parsed": outputs[index],
                    "parsing_error": None,
                }

        class _FakeRaw:
            def invoke(self, messages):
                raise AssertionError("unbound retry must not trigger")

        agent = _SequenceBound()
        up = _upstream()
        with (
            patch.object(
                run_module,
                "create_architecture_composer_agent_with_raw",
                return_value=agent,
            ),
            patch.object(
                run_module,
                "create_architecture_composer_agent_raw",
                return_value=_FakeRaw(),
            ),
        ):
            result = run_module.run_architecture_composer(
                up["extraction"],
                up["capacity"],
                up["selection"],
                up["cost"],
                REGION,
                DESIGN_SESSION,
                requirements_session_id=REQ_SESSION,
            )
            return result, agent, up

    def test_initial_prompt_strips_upstream_alternatives_and_marks_categories(
        self,
    ):
        selection = _selection()
        payload = run_module._composer_selection_payload(selection)
        self.assertNotIn("alternatives", payload["selected_components"][0])
        # The upstream artifact is untouched; only the prompt view drops it.
        self.assertEqual(
            selection.selected_components[0].alternatives,
            ["Amazon API Gateway REST API"],
        )

        result, agent, _ = self._run(_valid_shape())
        prompt = agent.messages[1].content
        self.assertNotIn("Amazon API Gateway REST API", prompt)
        self.assertIn(
            "amazon-api-gateway-http-api -> Amazon API Gateway (HTTP API) "
            "[load_balancing]",
            prompt,
        )
        self.assertIn(
            "amazon-cloudwatch-log-group -> Amazon CloudWatch Log Group "
            "[monitoring]",
            prompt,
        )
        self.assertIn("descriptive prose, NOT component vocabulary", prompt)
        self.assertEqual(_validate(result), [])

    def test_upstream_alternative_fails_closed_then_retry_recovers(self):
        bad = _valid_shape()
        bad["decisions"] = [
            {
                "decision": "use an HTTP API",
                "rationale": "the selected interface fits the workload",
                "about_component_ref": "amazon-api-gateway-http-api",
                "alternatives_considered": [
                    {
                        "component_ref": "Amazon API Gateway REST API",
                        "note": "a richer interface is not selected",
                    }
                ],
            }
        ]
        result, agent, _ = self._run(bad, _valid_shape())

        self.assertEqual(agent.calls, 2)
        with self.assertRaises(ValueError) as first:
            _normalize(bad)
        self.assertIn("Amazon API Gateway REST API", str(first.exception))
        self.assertIn(
            "alternative prose, which is descriptive and not a component "
            "reference",
            str(first.exception),
        )
        self.assertIn("configuration variant", str(first.exception))
        self.assertIn("typo", str(first.exception))
        # The retry prompt carries the exact rejection and both rules.
        retry_prompt = agent.messages[-1].content
        self.assertIn("REJECTED", retry_prompt)
        self.assertIn("Amazon API Gateway REST API", retry_prompt)
        self.assertIn(
            "alternative prose, which is descriptive and not a component "
            "reference",
            retry_prompt,
        )
        self.assertIn("[monitoring]", retry_prompt)
        self.assertIn(
            "amazon-api-gateway-http-api -> Amazon API Gateway (HTTP API) "
            "[load_balancing]",
            retry_prompt,
        )
        self.assertIn(
            "amazon-cloudwatch-log-group -> Amazon CloudWatch Log Group "
            "[monitoring]",
            retry_prompt,
        )
        self.assertIn("data_stores may contain ONLY nodes", retry_prompt)
        self.assertIn(
            "Never copy a service name from the upstream Component "
            "Selection per-component alternatives/tradeoffs prose",
            retry_prompt,
        )
        self.assertEqual(_validate(result), [])
        cloudwatch = next(
            node
            for node in result.nodes
            if node.component_name == "Amazon CloudWatch Log Group"
        )
        self.assertEqual(cloudwatch.category, "monitoring")
        self.assertEqual(result.data_store_node_ids, ["amazon-dynamodb-table"])

    def test_monitoring_component_is_not_a_data_store(self):
        shape = _valid_shape()
        shape["data_stores"] = ["amazon-cloudwatch-log-group"]
        with self.assertRaises(ValueError) as ctx:
            _normalize(shape)
        message = str(ctx.exception)
        self.assertIn("category 'monitoring'", message)
        self.assertIn("data stores must be one of", message)
        self.assertIn(
            "monitoring, logging, or observability component is not a "
            "data store",
            message,
        )

    def test_reference_error_distinguishes_variant_from_typo(self):
        shape = _valid_shape()
        shape["nodes"][1]["component_ref"] = "aws-lambd"
        with self.assertRaises(ValueError) as ctx:
            _normalize(shape)
        message = str(ctx.exception)
        self.assertIn("configuration variant", message)
        self.assertIn("typo", message)
        self.assertIn("aws-lambda -> AWS Lambda", message)

    def test_valid_composition_keeps_cloudwatch_as_a_node(self):
        result = _normalize(_valid_shape())
        self.assertEqual(_validate(result), [])
        cloudwatch = next(
            node
            for node in result.nodes
            if node.component_name == "Amazon CloudWatch Log Group"
        )
        self.assertEqual(cloudwatch.category, "monitoring")
        self.assertEqual(result.data_store_node_ids, ["amazon-dynamodb-table"])
        references = build_component_references(
            [comp.name for comp in _selection().selected_components]
        )
        self.assertEqual(
            references["Amazon CloudWatch Log Group"],
            "amazon-cloudwatch-log-group",
        )
        self.assertEqual(
            references["Amazon DynamoDB Table"],
            "amazon-dynamodb-table",
        )


if __name__ == "__main__":
    unittest.main()
