"""Architecture Composer Slice A tests: contract, helpers, validation.

All upstream artifacts are hand-built with the real model constructors
(no network, no LLM). The valid fixture mirrors the small-workload shape
already proven end to end: queue + object storage + compute, one
explicitly unpriced component.

Run: python -m unittest discover tests (with the project .venv)
"""
import copy
import unittest
import uuid
from datetime import datetime

from pydantic import ValidationError

from ai_architect.agents.component_selection.models import (
    ComponentCandidate,
    ComponentSelectionResult,
    UnresolvedTradeoff,
)
from ai_architect.domain.architecture.models import (
    SCHEMA_VERSION,
    ArchitectureDecision,
    ArchitectureEdge,
    ArchitectureNode,
    ArchitectureResult,
    CapacityMetricKey,
    CapacityValue,
)
from ai_architect.domain.capacity.models import CapacityModelResult
from ai_architect.domain.cost.models import (
    CostEstimationResult,
    CostLineItem,
    PricingModel,
    UnpricedComponent,
)
from ai_architect.domain.requirements.models import RequirementsExtraction
from ai_architect.services.architecture.core import (
    build_cost_links,
    build_inputs_ref,
    hash_upstream_inputs,
    new_artifact_id,
    resolve_capacity_value,
)
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
        traffic_characteristics="bursty reads",
        read_write_characteristics="read heavy",
        storage_characteristics="small redirect mappings",
        retention="365 days",
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
        assumptions=["queue assumes standard ordering is sufficient"],
        warnings=[],
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
        assumptions=[],
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


def _fixtures():
    extraction = _extraction()
    capacity = _capacity()
    selection = _selection()
    cost = _cost()
    result = ArchitectureResult(
        artifact_id=new_artifact_id(),
        session_id=DESIGN_SESSION,
        version=1,
        supersedes_artifact_id=None,
        nodes=[
            ArchitectureNode(
                node_id="api",
                component_name="Amazon Elastic Container Service",
                category="compute",
                role_in_system="serve the shorten and redirect API",
                capacity=[
                    resolve_capacity_value(
                        CapacityMetricKey.PEAK_READ_QPS, capacity
                    ),
                    resolve_capacity_value(
                        CapacityMetricKey.CONCURRENT_CONNECTIONS, capacity
                    ),
                ],
                evidence_refs=[],
            ),
            ArchitectureNode(
                node_id="queue",
                component_name="Amazon Simple Queue Service",
                category="messaging",
                role_in_system="buffer URL-create writes for workers",
                capacity=[
                    resolve_capacity_value(
                        CapacityMetricKey.PEAK_WRITE_QPS, capacity
                    ),
                ],
                scaling_note="managed scaling follows queue depth",
                evidence_refs=["ev-q-1"],
            ),
            ArchitectureNode(
                node_id="store",
                component_name="Amazon Simple Storage Service",
                category="storage",
                role_in_system="persist redirect mappings and assets",
                capacity=[
                    resolve_capacity_value(
                        CapacityMetricKey.STORAGE_GROWTH_BYTES_PER_DAY,
                        capacity,
                    ),
                ],
                availability_note="relies on provider durability design",
                evidence_refs=["ev-s-1"],
            ),
        ],
        edges=[
            ArchitectureEdge(
                edge_id="e1",
                from_node="client",
                to_node="api",
                kind="request",
                protocol="HTTPS",
                description="shorten and redirect API calls",
                load=resolve_capacity_value(
                    CapacityMetricKey.PEAK_READ_QPS, capacity
                ),
            ),
            ArchitectureEdge(
                edge_id="e2",
                from_node="api",
                to_node="queue",
                kind="async",
                protocol="",
                description="enqueue URL-create jobs for workers",
                load=resolve_capacity_value(
                    CapacityMetricKey.PEAK_WRITE_QPS, capacity
                ),
                evidence_refs=["ev-q-1"],
            ),
            ArchitectureEdge(
                edge_id="e3",
                from_node="api",
                to_node="store",
                kind="write",
                protocol="",
                description="persist new redirect mappings",
            ),
            ArchitectureEdge(
                edge_id="e4",
                from_node="api",
                to_node="store",
                kind="read",
                protocol="",
                description="look up redirect targets",
                load=resolve_capacity_value(
                    CapacityMetricKey.PEAK_READ_QPS, capacity
                ),
            ),
        ],
        entry_points=["api"],
        data_store_node_ids=["store"],
        boundaries=["region:ap-south-1", "single-region"],
        cost_links=build_cost_links(selection, cost),
        cost_totals_snapshot=dict(cost.totals_usd_per_month),
        decisions=[
            ArchitectureDecision(
                decision="buffer writes through a managed queue",
                rationale="queue decouples bursty writes from persistence",
                alternatives_considered=[
                    "Amazon SNS (rejected: fan-out not required)",
                ],
                evidence_refs=["ev-q-1"],
            ),
        ],
        assumptions=["read path assumes no separate cache tier"],
        warnings=[],
        unresolved_tradeoffs=list(selection.unresolved_tradeoffs),
        evidence_references=["ev-q-1", "ev-s-1"],
        inputs_ref=build_inputs_ref(
            requirements_session_id=REQ_SESSION,
            capacity=capacity,
            selection=selection,
            cost=cost,
            extraction=extraction,
            region_code=REGION,
        ),
        generated_at=GEN,
    )
    return {
        "result": result,
        "selection": selection,
        "capacity": capacity,
        "cost": cost,
        "extraction": extraction,
    }


def _validate(fx, result=None, **overrides):
    params = {
        "selection": fx["selection"],
        "capacity": fx["capacity"],
        "cost": fx["cost"],
        "extraction": fx["extraction"],
        "requirements_session_id": REQ_SESSION,
        "design_session_id": DESIGN_SESSION,
        "provider": PROVIDER,
        "region_code": REGION,
    }
    params.update(overrides)
    return validate_architecture(
        result if result is not None else fx["result"], **params
    )


class ArchitectureContractTests(unittest.TestCase):
    def test_01_valid_architecture_accepted(self):
        self.assertEqual(_validate(_fixtures()), [])

    def test_02_unknown_component_rejected(self):
        fx = _fixtures()
        result = copy.deepcopy(fx["result"])
        result.nodes[0].component_name = "Amazon Neptune"
        errors = _validate(fx, result)
        self.assertTrue(errors)
        self.assertTrue(any("unknown" in e.lower() for e in errors))

    def test_03_renamed_component_rejected(self):
        fx = _fixtures()
        result = copy.deepcopy(fx["result"])
        result.nodes[1].component_name = "Amazon SQS"
        errors = _validate(fx, result)
        self.assertTrue(errors)
        self.assertTrue(any("unknown" in e.lower() for e in errors))

    def test_04_duplicate_component_rejected(self):
        fx = _fixtures()
        result = copy.deepcopy(fx["result"])
        result.nodes[1].component_name = result.nodes[0].component_name
        errors = _validate(fx, result)
        self.assertTrue(errors)
        self.assertTrue(any("repeat" in e.lower() for e in errors))

    def test_05_duplicate_node_id_rejected(self):
        fx = _fixtures()
        result = copy.deepcopy(fx["result"])
        result.nodes[1].node_id = result.nodes[0].node_id
        errors = _validate(fx, result)
        self.assertTrue(errors)
        self.assertTrue(any("node_id" in e.lower() for e in errors))

    def test_06_dangling_edge_rejected(self):
        fx = _fixtures()
        result = copy.deepcopy(fx["result"])
        result.edges[0].to_node = "ghost"
        errors = _validate(fx, result)
        self.assertTrue(errors)
        self.assertTrue(any("dangling" in e.lower() for e in errors))

    def test_06b_disconnected_node_rejected(self):
        fx = _fixtures()
        result = copy.deepcopy(fx["result"])
        result.edges = [
            edge for edge in result.edges if edge.to_node != "store"
        ]
        errors = _validate(fx, result)
        self.assertTrue(errors)
        self.assertTrue(
            any(
                "disconnected" in error.lower() and "'store'" in error
                for error in errors
            ),
            msg=errors,
        )

    def test_07_invalid_entry_point_rejected(self):
        fx = _fixtures()
        result = copy.deepcopy(fx["result"])
        result.entry_points = ["ghost"]
        errors = _validate(fx, result)
        self.assertTrue(errors)
        self.assertTrue(any("entry point" in e.lower() for e in errors))

    def test_08_invalid_data_store_rejected(self):
        fx = _fixtures()
        with self.subTest("unknown node"):
            result = copy.deepcopy(fx["result"])
            result.data_store_node_ids = ["ghost"]
            errors = _validate(fx, result)
            self.assertTrue(errors)
            self.assertTrue(
                any("data store" in e.lower() for e in errors)
            )
        with self.subTest("wrong category"):
            result = copy.deepcopy(fx["result"])
            result.data_store_node_ids = ["api"]
            errors = _validate(fx, result)
            self.assertTrue(errors)
            self.assertTrue(
                any("data store" in e.lower() for e in errors)
            )

    def test_09_invented_evidence_rejected(self):
        fx = _fixtures()
        result = copy.deepcopy(fx["result"])
        result.nodes[1].evidence_refs.append("ev-forged")
        result.evidence_references.append("ev-forged")
        errors = _validate(fx, result)
        self.assertTrue(errors)
        self.assertTrue(any("ev-forged" in e for e in errors))

    def test_10_capacity_value_mismatch_rejected(self):
        fx = _fixtures()
        result = copy.deepcopy(fx["result"])
        result.nodes[1].capacity[0].value *= 2.0
        errors = _validate(fx, result)
        self.assertTrue(errors)
        self.assertTrue(any("capacity" in e.lower() for e in errors))

    def test_11_invalid_capacity_metric_rejected(self):
        with self.assertRaises(ValidationError):
            CapacityValue(metric="bogus_qps", value=1.0, unit="requests/s")
        with self.assertRaises(ValueError):
            resolve_capacity_value("bogus_qps", _capacity())

    def test_12_priced_cost_mismatch_rejected(self):
        fx = _fixtures()
        result = copy.deepcopy(fx["result"])
        result.cost_links[0].monthly_cost_usd += 1.0
        errors = _validate(fx, result)
        self.assertTrue(errors)
        self.assertTrue(any("cost link" in e.lower() for e in errors))

    def test_13_unpriced_missing_reason_rejected(self):
        fx = _fixtures()
        result = copy.deepcopy(fx["result"])
        unpriced = next(
            link for link in result.cost_links if not link.priced
        )
        unpriced.unpriced_reason = None
        errors = _validate(fx, result)
        self.assertTrue(errors)
        self.assertTrue(any("unpriced reason" in e.lower() for e in errors))

    def test_14_cost_totals_mismatch_rejected(self):
        fx = _fixtures()
        result = copy.deepcopy(fx["result"])
        result.cost_totals_snapshot["on_demand"] += 1.0
        errors = _validate(fx, result)
        self.assertTrue(errors)
        self.assertTrue(any("totals snapshot" in e.lower() for e in errors))

    def test_15_provider_mismatch_rejected(self):
        fx = _fixtures()
        errors = _validate(fx, provider="gcp")
        self.assertTrue(errors)
        self.assertTrue(any("provider" in e.lower() for e in errors))

    def test_16_region_mismatch_rejected(self):
        fx = _fixtures()
        errors = _validate(fx, region_code="us-east-1")
        self.assertTrue(errors)
        self.assertTrue(any("region" in e.lower() for e in errors))

    def test_17_upstream_hash_stable(self):
        first = hash_upstream_inputs(
            _extraction(), _capacity(), _selection(), _cost(), REGION
        )
        second = hash_upstream_inputs(
            _extraction(), _capacity(), _selection(), _cost(), REGION
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)

    def test_18_upstream_hash_changes_with_inputs(self):
        fx = _fixtures()
        base = hash_upstream_inputs(
            fx["extraction"], fx["capacity"], fx["selection"],
            fx["cost"], REGION,
        )
        changed = fx["extraction"].model_copy(
            update={"target_users_dau": "20000"}
        )
        other = hash_upstream_inputs(
            changed, fx["capacity"], fx["selection"], fx["cost"], REGION
        )
        self.assertNotEqual(base, other)

    def test_19_artifact_identity_metadata_accepted(self):
        fx = _fixtures()
        result = fx["result"]
        self.assertEqual(_validate(fx), [])
        self.assertEqual(result.schema_version, SCHEMA_VERSION)
        self.assertTrue(result.artifact_id)
        uuid.UUID(result.artifact_id, version=4)
        self.assertEqual(result.version, 1)
        self.assertIsNone(result.supersedes_artifact_id)
        self.assertIsInstance(result.generated_at, datetime)
        self.assertEqual(
            result.inputs_ref.upstream_inputs_hash,
            hash_upstream_inputs(
                fx["extraction"], fx["capacity"], fx["selection"],
                fx["cost"], REGION,
            ),
        )
        self.assertNotEqual(new_artifact_id(), new_artifact_id())

    def test_20_fabricated_numeric_claims_rejected(self):
        fx = _fixtures()
        with self.subTest("price and throughput"):
            result = copy.deepcopy(fx["result"])
            result.nodes[1].scaling_note = "handles 9000 QPS at $5 per month"
            errors = _validate(fx, result)
            self.assertTrue(errors)
            self.assertTrue(
                any("price" in e.lower() or "throughput" in e.lower()
                    for e in errors)
            )
        with self.subTest("unsupported SLA"):
            result = copy.deepcopy(fx["result"])
            result.nodes[2].availability_note = (
                "guarantees 99.99% uptime SLA"
            )
            errors = _validate(fx, result)
            self.assertTrue(errors)
            self.assertTrue(any("sla" in e.lower() for e in errors))

    def test_21_multiline_cost_aggregates_deterministically(self):
        fx = _fixtures()
        cost = copy.deepcopy(fx["cost"])
        extra = copy.deepcopy(cost.lines[0])
        extra.unit = "GB-Mo"
        extra.monthly_usage = 10.0
        extra.unit_price_usd = 0.01
        extra.monthly_cost_usd = 0.10
        cost.lines.append(extra)
        links = build_cost_links(fx["selection"], cost)
        queue = next(
            link for link in links
            if link.component_name == "Amazon Simple Queue Service"
        )
        self.assertTrue(queue.priced)
        self.assertAlmostEqual(queue.monthly_cost_usd, 0.50)
        self.assertEqual(queue.unit, "mixed")
        self.assertEqual(queue.pricing_model, "on_demand")


    def test_22_qualitative_throughput_prose_allowed(self):
        fx = _fixtures()
        for note in (
            "scales with throughput",
            "handles peak write load",
            "scales with read traffic",
            "managed scaling follows queue depth",
        ):
            with self.subTest(note=note):
                result = copy.deepcopy(fx["result"])
                result.nodes[1].scaling_note = note
                errors = _validate(fx, result)
                self.assertEqual(errors, [])

    def test_23_numeric_throughput_claims_rejected(self):
        fx = _fixtures()
        for note in (
            "handles 9000 QPS",
            "sustains 9000 requests/s",
            "sustains 9000 requests per second",
            "burst headroom to 350 RPS",
            "peaks at 9k QPS",
        ):
            with self.subTest(note=note):
                result = copy.deepcopy(fx["result"])
                result.nodes[1].scaling_note = note
                errors = _validate(fx, result)
                self.assertTrue(
                    any("states throughput" in e for e in errors),
                    msg=f"no throughput error for {note!r}: {errors}",
                )

    def test_24_numeric_price_claims_rejected(self):
        fx = _fixtures()
        for assumption in (
            "caching cuts cost to $5 per month",
            "egress billed at 12/mo per zone",
        ):
            with self.subTest(assumption=assumption):
                result = copy.deepcopy(fx["result"])
                result.assumptions = [assumption]
                errors = _validate(fx, result)
                self.assertTrue(
                    any("states a price" in e for e in errors),
                    msg=f"no price error for {assumption!r}: {errors}",
                )

    def test_25_decision_alternative_naming_rules(self):
        fx = _fixtures()
        with self.subTest("unknown provider service rejected"):
            result = copy.deepcopy(fx["result"])
            result.decisions[0].alternatives_considered = [
                "Amazon ElastiCache instead of the queue"
            ]
            errors = _validate(fx, result)
            self.assertTrue(
                any("outside the selected" in e for e in errors),
                msg=f"unexpected errors: {errors}",
            )
        for allowed in (
            "Amazon SNS (rejected: fan-out is not required)",
            "Amazon Simple Queue Service with batching",
            "a managed cache instead of DynamoDB",
            "an alternative managed messaging service",
        ):
            with self.subTest(allowed=allowed):
                result = copy.deepcopy(fx["result"])
                result.decisions[0].alternatives_considered = [allowed]
                self.assertEqual(_validate(fx, result), [])


    def test_26_known_alias_references_allowed(self):
        fx = _fixtures()
        for allowed in (
            "AWS SQS FIFO queue instead of standard",
            "SNS fan-out for notifications",
            "AWS S3 versioning for the redirect objects",
        ):
            with self.subTest(allowed=allowed):
                result = copy.deepcopy(fx["result"])
                result.decisions[0].alternatives_considered = [allowed]
                self.assertEqual(_validate(fx, result), [])

    def test_27_alias_of_unknown_service_still_rejected(self):
        fx = _fixtures()
        for rejected in (
            "AWS DynamoDB global tables instead",
            "Amazon ElastiCache for hot keys",
            "AWS Lambda for edge compute",
        ):
            with self.subTest(rejected=rejected):
                result = copy.deepcopy(fx["result"])
                result.decisions[0].alternatives_considered = [rejected]
                errors = _validate(fx, result)
                self.assertTrue(
                    any("outside the selected" in e for e in errors),
                    msg=f"unexpected errors: {errors}",
                )


if __name__ == "__main__":
    unittest.main()
