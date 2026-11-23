"""Architecture Composer Slice B tests: LLM boundary + normalization.

All LLM calls are faked; the run path is exercised through the same
deterministic normalization/validation as production. No network, no
Ollama calls.

Run: python -m unittest discover tests (with the project .venv)
"""
import copy
import json
import unittest
import uuid
from datetime import datetime
from unittest.mock import patch

from langchain_core.messages import AIMessage

from ai_architect.agents.architecture import run as run_module
from ai_architect.agents.architecture.run import (
    ArchitectureLLMOutput,
    FALLBACK_NOTE,
    LLMArchitectureDecision,
    LLMArchitectureEdge,
    LLMArchitectureNode,
    normalize_architecture,
)
from ai_architect.agents.component_selection.models import (
    ComponentCandidate,
    ComponentSelectionResult,
    UnresolvedTradeoff,
)
from ai_architect.domain.architecture.models import ArchitectureResult
from ai_architect.domain.capacity.models import CapacityModelResult
from ai_architect.domain.cost.models import (
    CostEstimationResult,
    CostLineItem,
    PricingModel,
    UnpricedComponent,
)
from ai_architect.domain.requirements.models import RequirementsExtraction
from ai_architect.services.architecture.core import hash_upstream_inputs
from ai_architect.services.architecture.validator import validate_architecture

GEN = datetime(2026, 10, 4, 12, 0, 0)
PRICE_TS = datetime(2026, 10, 4, 11, 0, 0)
REQ_SESSION = "req-s1"
DESIGN_SESSION = "design-s1"
PROVIDER = "aws"
REGION = "ap-south-1"


def _extraction() -> RequirementsExtraction:
    return RequirementsExtraction(
        application_description="URL shortener for 10000 DAU",
        cloud_provider="aws",
        region="Mumbai region",
        availability="",
        latency="p99 under 500ms",
        target_users_dau="10000",
        managed_service_preference="fully managed",
    )


def _capacity() -> CapacityModelResult:
    return CapacityModelResult(
        peak_write_qps=4.63,
        peak_read_qps=46.3,
        concurrent_connections=1200.0,
        storage_growth_bytes_per_day=4000000.0,
        hot_working_set_bytes=120000000.0,
        egress_bytes_per_second=47309.6,
        formula_version="v1",
        calculation_trace={},
    )


def _selection() -> ComponentSelectionResult:
    return ComponentSelectionResult(
        selected_components=[
            ComponentCandidate(
                name="Amazon Simple Queue Service",
                provider="aws",
                category="messaging",
                role="buffer URL-create writes",
                rationale="decouples bursty writes from mapping persistence",
                evidence_refs=["ev-q-1"],
                confidence="high",
                evidence_support="strong",
            ),
            ComponentCandidate(
                name="Amazon Simple Storage Service",
                provider="aws",
                category="storage",
                role="persist redirect mappings",
                rationale="durable object storage for mappings and assets",
                evidence_refs=["ev-s-1"],
                confidence="high",
                evidence_support="strong",
            ),
            ComponentCandidate(
                name="Amazon Elastic Container Service",
                provider="aws",
                category="compute",
                role="serve redirect API",
                rationale="runs the stateless API containers",
                confidence="high",
                evidence_support="insufficient",
            ),
        ],
        rejected_candidates=[
            ComponentCandidate(
                name="Amazon SNS",
                provider="aws",
                category="messaging",
                role="fan-out notifications",
                rationale="fan-out not required for this workload",
                confidence="medium",
                evidence_support="weak",
            ),
        ],
        unresolved_tradeoffs=[
            UnresolvedTradeoff(
                description="FIFO ordering versus throughput",
                options=["standard queue", "fifo queue"],
                impact="ordering guarantees under burst",
            ),
        ],
        evidence_references=["ev-q-1", "ev-s-1"],
        generated_at=GEN,
    )


def _cost() -> CostEstimationResult:
    return CostEstimationResult(
        lines=[
            CostLineItem(
                component_name="Amazon Simple Queue Service",
                pricing_model=PricingModel.ON_DEMAND,
                unit="Requests",
                monthly_usage=1000000.0,
                unit_price_usd=0.0000004,
                monthly_cost_usd=0.40,
                usage_formula="avg total QPS times seconds per month",
                pricing_source="aws-price-list-api",
                pricing_retrieved_at=PRICE_TS,
                region_code=REGION,
            ),
            CostLineItem(
                component_name="Amazon Simple Storage Service",
                pricing_model=PricingModel.ON_DEMAND,
                unit="GB-Mo",
                monthly_usage=120.0,
                unit_price_usd=0.023,
                monthly_cost_usd=2.76,
                usage_formula="one month of accumulated growth",
                pricing_source="aws-price-list-api",
                pricing_retrieved_at=PRICE_TS,
                region_code=REGION,
            ),
        ],
        totals_usd_per_month={"on_demand": 3.16},
        warnings=[],
        unpriced=[
            UnpricedComponent(
                component_name="Amazon Elastic Container Service",
                reason=(
                    "Cannot price: no Price List mapping and instance "
                    "type and count are not determined."
                ),
            ),
        ],
        generated_at=GEN,
        pricing_as_of=PRICE_TS,
    )


def _llm_output() -> ArchitectureLLMOutput:
    return ArchitectureLLMOutput(
        nodes=[
            LLMArchitectureNode(
                component_name="Amazon Elastic Container Service",
                role_in_system="serve the shorten and redirect API",
                capacity_keys=["peak_read_qps", "concurrent_connections"],
            ),
            LLMArchitectureNode(
                component_name="Amazon Simple Queue Service",
                role_in_system="buffer URL-create writes for workers",
                capacity_keys=["peak_write_qps"],
                scaling_note="managed scaling follows queue depth",
                evidence_refs=["ev-q-1"],
            ),
            LLMArchitectureNode(
                component_name="Amazon Simple Storage Service",
                role_in_system="persist redirect mappings and assets",
                capacity_keys=["storage_growth_bytes_per_day"],
                availability_note="relies on provider durability design",
                evidence_refs=["ev-s-1"],
            ),
        ],
        edges=[
            LLMArchitectureEdge(
                from_component="client",
                to_component="Amazon Elastic Container Service",
                kind="request",
                protocol="HTTPS",
                description="shorten and redirect API calls",
                load_key="peak_read_qps",
            ),
            LLMArchitectureEdge(
                from_component="Amazon Elastic Container Service",
                to_component="Amazon Simple Queue Service",
                kind="async",
                description="enqueue URL-create jobs for workers",
                load_key="peak_write_qps",
                evidence_refs=["ev-q-1"],
            ),
            LLMArchitectureEdge(
                from_component="Amazon Elastic Container Service",
                to_component="Amazon Simple Storage Service",
                kind="write",
                description="persist new redirect mappings",
            ),
            LLMArchitectureEdge(
                from_component="Amazon Elastic Container Service",
                to_component="Amazon Simple Storage Service",
                kind="read",
                description="look up redirect targets",
                load_key="peak_read_qps",
            ),
        ],
        entry_points=["Amazon Elastic Container Service"],
        data_stores=["Amazon Simple Storage Service"],
        boundaries=["region:ap-south-1", "single-region"],
        decisions=[
            LLMArchitectureDecision(
                decision="buffer writes through a managed queue",
                rationale="queue decouples bursty writes from persistence",
                alternatives_considered=[
                    {
                        "component_ref": "Amazon SNS",
                        "note": "rejected: fan-out not required",
                    }
                ],
                evidence_refs=["ev-q-1"],
            ),
        ],
        assumptions=["read path assumes no separate cache tier"],
        warnings=[],
        new_tradeoffs=[],
    )


def _upstream():
    return {
        "extraction": _extraction(),
        "capacity": _capacity(),
        "selection": _selection(),
        "cost": _cost(),
    }


class _FakeBoundAgent:
    def __init__(self, outcome):
        self._outcome = outcome
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        return self._outcome


class _FakeRawAgent:
    def __init__(self, content="should never be used"):
        self._content = content
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        from langchain_core.messages import AIMessage as _AIMessage

        return _AIMessage(content=self._content)


def _run_with(parsed=None, raw_text="", parsing_error=None):
    bound = _FakeBoundAgent(
        {
            "raw": AIMessage(content=raw_text),
            "parsed": parsed,
            "parsing_error": parsing_error,
        }
    )
    raw = _FakeRawAgent()
    up = _upstream()
    with (
        patch.object(
            run_module, "create_architecture_composer_agent_with_raw",
            return_value=bound,
        ),
        patch.object(
            run_module, "create_architecture_composer_agent_raw",
            return_value=raw,
        ),
    ):
        result = run_module.run_architecture_composer(
            up["extraction"], up["capacity"], up["selection"], up["cost"],
            REGION, DESIGN_SESSION, requirements_session_id=REQ_SESSION,
        )
    return result, bound, raw


def _check_valid(up, result):
    assert isinstance(result, ArchitectureResult)
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


class ComposerSliceBTests(unittest.TestCase):
    def test_01_valid_output_produces_valid_result(self):
        up = _upstream()
        result, _, _ = _run_with(parsed=_llm_output())
        self.assertIsInstance(result, ArchitectureResult)
        self.assertEqual(len(result.nodes), 3)
        self.assertEqual(len(result.edges), 4)
        self.assertEqual(
            _check_valid(up, result), [],
            msg="; ".join(_check_valid(up, result)),
        )

    def test_02_unknown_component_rejected(self):
        bad = _llm_output()
        bad.nodes.append(
            LLMArchitectureNode(
                component_name="Amazon Neptune",
                role_in_system="graph queries",
                capacity_keys=["peak_read_qps"],
            )
        )
        with self.assertRaises(ValueError) as ctx:
            _run_with(parsed=bad)
        self.assertIn("unknown component", str(ctx.exception).lower())

    def test_03_invented_evidence_rejected(self):
        bad = _llm_output()
        bad.nodes[1].evidence_refs = ["ev-forged"]
        with self.assertRaises(ValueError) as ctx:
            _run_with(parsed=bad)
        self.assertIn("ev-forged", str(ctx.exception))

    def test_04_invalid_capacity_key_rejected(self):
        envelope = json.dumps(
            {
                "nodes": [
                    {
                        "component_name": "Amazon Simple Queue Service",
                        "role_in_system": "buffer writes",
                        "capacity_keys": ["bogus_qps"],
                    }
                ],
                "edges": [],
                "entry_points": [],
                "data_stores": [],
                "boundaries": [],
                "decisions": [],
                "assumptions": [],
                "warnings": [],
                "new_tradeoffs": [],
            }
        )
        with self.assertRaises(ValueError):
            _run_with(raw_text=envelope)

    def test_04b_disconnected_node_rejected(self):
        bad = _llm_output()
        bad.edges = [
            edge for edge in bad.edges
            if "Amazon Simple Queue Service"
            not in (edge.from_component, edge.to_component)
        ]
        with self.assertRaises(ValueError) as ctx:
            _run_with(parsed=bad)
        self.assertIn("disconnected", str(ctx.exception).lower())

    def test_05_capacity_key_resolves_exact_value(self):
        up = _upstream()
        result, _, _ = _run_with(parsed=_llm_output())
        queue = next(
            node for node in result.nodes
            if node.component_name == "Amazon Simple Queue Service"
        )
        self.assertEqual(len(queue.capacity), 1)
        self.assertEqual(queue.capacity[0].metric, "peak_write_qps")
        self.assertEqual(
            queue.capacity[0].value, up["capacity"].peak_write_qps
        )
        self.assertEqual(queue.capacity[0].unit, "requests/s")

    def test_06_numeric_injection_ignored(self):
        raw = _llm_output().model_dump(mode="json")
        raw["nodes"][1]["peak_write_qps"] = 99999.0
        raw["nodes"][1]["monthly_cost_usd"] = 1.23
        raw["timestamp"] = "2030-01-01T00:00:00"
        parsed = ArchitectureLLMOutput.model_validate(raw)
        up = _upstream()
        result = normalize_architecture(
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
        queue = next(
            node for node in result.nodes
            if node.component_name == "Amazon Simple Queue Service"
        )
        self.assertEqual(
            queue.capacity[0].value, up["capacity"].peak_write_qps
        )

    def test_07_cost_links_from_actual_cost_result(self):
        up = _upstream()
        result, _, _ = _run_with(parsed=_llm_output())
        queue = next(
            link for link in result.cost_links
            if link.component_name == "Amazon Simple Queue Service"
        )
        self.assertTrue(queue.priced)
        self.assertEqual(queue.pricing_model, "on_demand")
        self.assertAlmostEqual(queue.monthly_cost_usd, 0.40)
        self.assertEqual(queue.pricing_source, "aws-price-list-api")
        self.assertEqual(
            result.cost_totals_snapshot, {"on_demand": 3.16}
        )

    def test_08_unpriced_stays_unpriced(self):
        up = _upstream()
        result, _, _ = _run_with(parsed=_llm_output())
        compute = next(
            link for link in result.cost_links
            if link.component_name == "Amazon Elastic Container Service"
        )
        self.assertFalse(compute.priced)
        self.assertIsNone(compute.monthly_cost_usd)
        self.assertEqual(
            compute.unpriced_reason,
            up["cost"].unpriced[0].reason,
        )

    def test_09_boundary_has_no_price_or_cost_fields(self):
        forbidden = {
            "price", "prices", "pricing", "monthly_cost_usd", "monthly_cost",
            "unit_price_usd", "cost", "costs", "timestamp", "timestamps",
            "generated_at", "retrieved_at", "artifact_id", "version",
            "upstream_hash", "upstream_inputs_hash", "cost_links",
            "capacity_value", "capacity_values",
        }
        fields = set(ArchitectureLLMOutput.model_fields)
        fields |= set(LLMArchitectureNode.model_fields)
        fields |= set(LLMArchitectureEdge.model_fields)
        fields |= set(LLMArchitectureDecision.model_fields)
        self.assertEqual(fields & forbidden, set())

    def test_10_unsupported_availability_claim_rejected(self):
        bad = _llm_output()
        bad.nodes[2].availability_note = "guarantees 99.99% uptime SLA"
        with self.assertRaises(ValueError) as ctx:
            _run_with(parsed=bad)
        self.assertIn("sla", str(ctx.exception).lower())

    def test_11_text_envelope_fallback_same_path(self):
        up = _upstream()
        envelope = json.dumps(_llm_output().model_dump(mode="json"))
        result, bound, raw = _run_with(raw_text=envelope)
        self.assertEqual(raw.calls, 0)
        self.assertIn(FALLBACK_NOTE, result.warnings)
        self.assertEqual(_check_valid(up, result), [])

    def test_12_identity_metadata_from_python(self):
        up = _upstream()
        result, _, _ = _run_with(parsed=_llm_output())
        uuid.UUID(result.artifact_id, version=4)
        self.assertEqual(result.version, 1)
        self.assertIsNone(result.supersedes_artifact_id)
        self.assertEqual(
            result.inputs_ref.upstream_inputs_hash,
            hash_upstream_inputs(
                up["extraction"], up["capacity"], up["selection"],
                up["cost"], REGION,
            ),
        )
        for model in (
            ArchitectureLLMOutput, LLMArchitectureNode,
            LLMArchitectureEdge, LLMArchitectureDecision,
        ):
            for reserved in (
                "artifact_id", "version", "upstream_inputs_hash",
                "generated_at",
            ):
                self.assertNotIn(reserved, model.model_fields)

    def test_13_multi_edge_normalization(self):
        result, _, _ = _run_with(parsed=_llm_output())
        by_id = {edge.edge_id: edge for edge in result.edges}
        self.assertEqual(
            [edge.edge_id for edge in result.edges],
            ["e1", "e2", "e3", "e4"],
        )
        node_ids = {node.node_id for node in result.nodes}
        self.assertEqual(by_id["e1"].from_node, "client")
        self.assertIn(by_id["e1"].to_node, node_ids)
        self.assertEqual(
            [str(edge.kind) for edge in result.edges],
            ["request", "async", "write", "read"],
        )
        self.assertEqual(by_id["e1"].protocol, "HTTPS")
        self.assertEqual(by_id["e3"].protocol, "")
        self.assertIsNotNone(by_id["e1"].load)
        self.assertIsNone(by_id["e3"].load)
        self.assertEqual(by_id["e2"].load.metric, "peak_write_qps")

    def test_14_validation_failure_is_explicit(self):
        bad = _llm_output()
        bad.edges[0].to_component = "ghost-service"
        with self.assertRaises(ValueError) as ctx:
            _run_with(parsed=bad)
        self.assertIn("unknown", str(ctx.exception).lower())
        # No partial artifact escapes: normalize raises before returning.
        up = _upstream()
        with self.assertRaises(ValueError):
            normalize_architecture(
                bad,
                extraction=up["extraction"],
                capacity=up["capacity"],
                selection=up["selection"],
                cost=up["cost"],
                region_code=REGION,
                requirements_session_id=REQ_SESSION,
                design_session_id=DESIGN_SESSION,
                provider=PROVIDER,
            )


if __name__ == "__main__":
    unittest.main()
