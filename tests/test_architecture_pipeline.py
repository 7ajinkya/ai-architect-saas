"""Architecture Composer Slice C tests: pipeline integration.

Exercises the real design-graph composer stage through the real
Slice B run path (mocked LLM transport only), plus checkpoint
round-trip through the real allowlist and presentation-only CLI
rendering. No network, no Ollama calls.

Run: python -m unittest discover tests (with the project .venv)
"""
import asyncio
import io
import unittest
from datetime import datetime
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from rich.console import Console

import ai_architect.cli.design as design_cli
from ai_architect.agents.architecture.run import (
    ArchitectureLLMOutput,
    LLMArchitectureDecision,
    LLMArchitectureEdge,
    LLMArchitectureNode,
    normalize_architecture,
)
from ai_architect.agents.architecture import run as composer_run_module
from ai_architect.agents.capacity.models import CapacityModelProposal
from ai_architect.agents.capacity.result import CapacityModelingResult
from ai_architect.agents.component_selection.models import (
    ComponentCandidate,
    ComponentSelectionResult,
    UnresolvedTradeoff,
)
from ai_architect.agents.design.graph import (
    architecture_composer_stage,
    build_design_graph,
)
from ai_architect.agents.design.state import DesignStatus
from ai_architect.domain.capacity.models import CapacityModelResult
from ai_architect.domain.cost.models import (
    CostEstimationResult,
    CostLineItem,
    PricingModel,
    UnpricedComponent,
)
from ai_architect.domain.requirements.models import RequirementsExtraction
from ai_architect.domain.requirements.state import (
    RequirementsState,
    SessionStatus,
)
from ai_architect.infrastructure.checkpoint.sqlite import (
    _MSGPACK_ALLOWLISTED_TYPES,
)
from ai_architect.domain.architecture.models import ArchitectureResult
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


def _capacity_result() -> CapacityModelingResult:
    proposal = CapacityModelProposal(
        daily_active_users=10000,
        messages_per_user_per_day=10.0,
        peak_factor=3.0,
        read_write_ratio=10.0,
        average_message_size_bytes=1024,
        connection_factor=0.12,
        storage_retention_days=365,
        rationale={"daily_active_users": "explicitly provided"},
    )
    calculation = CapacityModelResult(
        peak_write_qps=4.63,
        peak_read_qps=46.3,
        concurrent_connections=1200.0,
        storage_growth_bytes_per_day=4000000.0,
        hot_working_set_bytes=120000000.0,
        egress_bytes_per_second=47309.6,
        formula_version="v1",
        calculation_trace={},
    )
    return CapacityModelingResult(
        proposal=proposal, calculation=calculation
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


def _design_state(**overrides):
    state = {
        "session_id": DESIGN_SESSION,
        "user_request": "URL shortener for 10000 DAU",
        "requirements_state": RequirementsState(
            session_id=REQ_SESSION,
            original_request="URL shortener for 10000 DAU",
            extraction=_extraction(),
            status=SessionStatus.READY,
        ),
        "capacity_result": _capacity_result(),
        "component_selection": _selection(),
        "cost_result": _cost(),
        "architecture": None,
        "status": DesignStatus.RUNNING.value,
        "error": None,
        "warnings": [],
    }
    state.update(overrides)
    return state


class _FakeBoundAgent:
    def __init__(self, outcome):
        self._outcome = outcome

    def invoke(self, messages):
        return self._outcome


class _FakeRawAgent:
    def invoke(self, messages):
        from langchain_core.messages import AIMessage as _AIMessage

        return _AIMessage(content="should never be used")


def _patched_composer(parsed=None, raw_text="", parsing_error=None):
    bound = _FakeBoundAgent(
        {
            "raw": AIMessage(content=raw_text),
            "parsed": parsed,
            "parsing_error": parsing_error,
        }
    )
    return (
        patch.object(
            composer_run_module,
            "create_architecture_composer_agent_with_raw",
            return_value=bound,
        ),
        patch.object(
            composer_run_module,
            "create_architecture_composer_agent_raw",
            return_value=_FakeRawAgent(),
        ),
    )


class ComposerPipelineTests(unittest.TestCase):
    def test_01_stage_produces_valid_architecture(self):
        with_composer, with_raw = _patched_composer(
            parsed=_llm_output()
        )
        with with_composer, with_raw:
            update = asyncio.run(
                architecture_composer_stage(_design_state())
            )
        self.assertEqual(update["status"], DesignStatus.RUNNING.value)
        self.assertNotIn("error", update)
        result = update["architecture"]
        self.assertIsInstance(result, ArchitectureResult)

        selection = _selection()
        capacity = _capacity_result().calculation
        cost = _cost()
        self.assertEqual(
            validate_architecture(
                result,
                selection=selection,
                capacity=capacity,
                cost=cost,
                extraction=_extraction(),
                requirements_session_id=REQ_SESSION,
                design_session_id=DESIGN_SESSION,
                provider=PROVIDER,
                region_code=REGION,
            ),
            [],
        )
        # Nodes correspond exactly to selected components.
        self.assertEqual(
            {node.component_name for node in result.nodes},
            {comp.name for comp in selection.selected_components},
        )
        # At least one valid non-control flow exists.
        self.assertTrue(
            any(str(edge.kind) != "control" for edge in result.edges)
        )
        # Capacity resolved from the deterministic calculation.
        queue = next(
            node for node in result.nodes
            if node.component_name == "Amazon Simple Queue Service"
        )
        self.assertEqual(
            queue.capacity[0].value, capacity.peak_write_qps
        )
        # Cost links come from the deterministic cost result.
        link = next(
            item for item in result.cost_links
            if item.component_name == "Amazon Simple Queue Service"
        )
        self.assertAlmostEqual(link.monthly_cost_usd, 0.40)
        unpriced = next(
            item for item in result.cost_links
            if item.component_name == "Amazon Elastic Container Service"
        )
        self.assertFalse(unpriced.priced)
        self.assertEqual(unpriced.unpriced_reason, cost.unpriced[0].reason)
        # Evidence remains within the selection set; identity exists.
        self.assertTrue(
            set(result.evidence_references)
            <= set(selection.evidence_references)
        )
        self.assertTrue(result.artifact_id)
        self.assertEqual(result.version, 1)
        self.assertTrue(result.inputs_ref.upstream_inputs_hash)

    def test_02_stage_failure_stores_no_partial(self):
        with_composer, with_raw = _patched_composer(
            parsed=None, raw_text="", parsing_error=None
        )
        with with_composer, with_raw:
            update = asyncio.run(
                architecture_composer_stage(_design_state())
            )
        self.assertEqual(update["status"], DesignStatus.FAILED.value)
        self.assertIn("error", update)
        self.assertNotIn("architecture", update)

    def test_03_missing_upstream_fails(self):
        with_composer, with_raw = _patched_composer(
            parsed=_llm_output()
        )
        with with_composer, with_raw:
            update = asyncio.run(
                architecture_composer_stage(
                    _design_state(cost_result=None)
                )
            )
        self.assertEqual(update["status"], DesignStatus.FAILED.value)
        self.assertNotIn("architecture", update)

    def test_04_graph_contains_composer_stage(self):
        graph = build_design_graph(checkpointer=None)
        nodes = set(graph.get_graph().nodes.keys())
        self.assertIn("architecture_composer", nodes)
        self.assertEqual(
            nodes,
            {
                "requirements",
                "capacity",
                "component_selection",
                "cost_estimation",
                "architecture_composer",
                "design_critic",
                "composer_revision",
                "quality_gate",
                "__start__",
                "__end__",
            },
        )

    def test_05_checkpoint_round_trip(self):
        self.assertIn(ArchitectureResult, _MSGPACK_ALLOWLISTED_TYPES)
        up = {
            "extraction": _extraction(),
            "capacity": _capacity_result().calculation,
            "selection": _selection(),
            "cost": _cost(),
        }
        result = normalize_architecture(
            _llm_output(),
            extraction=up["extraction"],
            capacity=up["capacity"],
            selection=up["selection"],
            cost=up["cost"],
            region_code=REGION,
            requirements_session_id=REQ_SESSION,
            design_session_id=DESIGN_SESSION,
            provider=PROVIDER,
        )
        serde = JsonPlusSerializer(
            allowed_msgpack_modules=list(_MSGPACK_ALLOWLISTED_TYPES)
        )
        payload_type, payload = serde.dumps_typed(result)
        restored = serde.loads_typed((payload_type, payload))
        self.assertEqual(restored, result)

    def test_06_cli_renders_architecture(self):
        up = {
            "extraction": _extraction(),
            "capacity": _capacity_result().calculation,
            "selection": _selection(),
            "cost": _cost(),
        }
        result = normalize_architecture(
            _llm_output(),
            extraction=up["extraction"],
            capacity=up["capacity"],
            selection=up["selection"],
            cost=up["cost"],
            region_code=REGION,
            requirements_session_id=REQ_SESSION,
            design_session_id=DESIGN_SESSION,
            provider=PROVIDER,
        )
        # file= buffer avoids the Windows legacy-console encoding of
        # the module's pre-existing literal glyphs; render output is
        # still fully captured for assertions.
        recorder = Console(record=True, width=120, file=io.StringIO())
        with patch.object(design_cli, "console", recorder):
            design_cli.render_architecture(result)
        text = recorder.export_text()
        self.assertIn("Amazon Simple Queue Service", text)
        self.assertIn("Amazon Elastic Container Service", text)
        self.assertIn(result.artifact_id, text)
        self.assertIn("on_demand", text)
        self.assertNotIn("Terraform", text)


if __name__ == "__main__":
    unittest.main()
