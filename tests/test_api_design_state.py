"""Slice 3 tests: backend design-state read API.

The API is an adapter, so these tests assert two things: that it
re-serializes stored artifacts faithfully (including deterministic node
ids and unpriced honesty), and that it never becomes a second
orchestration engine.

Fixtures are deliberately generic and arbitrary. No workload names, no
workload-specific branches, and two structurally different shapes are
used to prove the API is shape-agnostic.

Persisted state is written through the real checkpointer, so the loader's
checkpoint selection logic is exercised rather than mocked.

Run: poetry run python -m unittest discover tests
"""
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

from tests.api_auth import api_client

from ai_architect.agents.architecture.run import build_component_references
from ai_architect.api.main import create_app
from ai_architect.api import sessions as api_sessions_mod
from ai_architect.domain.architecture.models import (
    ArchitectureDecision,
    ArchitectureEdge,
    ArchitectureInputsRef,
    ArchitectureNode,
    ArchitectureResult,
    CapacityValue,
    CapacityMetricKey,
    CostLink,
    EdgeKind,
)
from ai_architect.domain.cost.models import (
    CostCompleteness,
    CostEstimationResult,
    CostLineItem,
    CoverageEntry,
    CoverageStatus,
    DimensionDisposition,
    EstimatedCostRange,
    EstimationConfidence,
    PricingModel,
    UnpricedComponent,
    UsageEstimate,
    UsageProvenance,
)
from ai_architect.domain.requirements.models import (
    Assumption,
    RequirementsExtraction,
)
from ai_architect.domain.requirements.state import RequirementsState
from ai_architect.infrastructure.checkpoint import sqlite as ckpt_sqlite
from ai_architect.infrastructure.checkpoint.design import load_design_snapshot
from ai_architect.infrastructure.checkpoint.sqlite import create_checkpointer
from ai_architect.services.requirements.validator import RequirementsValidator

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _extraction() -> RequirementsExtraction:
    return RequirementsExtraction(
        application_description="generic multi-tier workload",
        cloud_provider="aws",
        region="us-east-1",
        target_users_dau="10000",
        storage_characteristics="durable block storage",
        retention="90 days",
        availability="99.9%",
        assumptions=[
            Assumption(
                name="region",
                value="us-east-1",
                source="architect_default",
                confidence="medium",
                rationale="User delegated region selection.",
            )
        ],
    )


def _requirements_state(session_id: str) -> RequirementsState:
    extraction = _extraction()
    validation = RequirementsValidator().validate(extraction)
    return RequirementsState.from_extraction(
        session_id=session_id,
        original_request="design a generic workload",
        extraction=extraction,
        validation_result=validation,
    )


def _inputs_ref(requirements_session_id: str) -> ArchitectureInputsRef:
    return ArchitectureInputsRef(
        requirements_session_id=requirements_session_id,
        capacity_formula_version="capacity/v1",
        selection_generated_at=NOW,
        cost_generated_at=NOW,
        cost_pricing_as_of=NOW,
        region_code="us-east-1",
        upstream_inputs_hash="fixture-upstream-hash",
    )


def _cost_result() -> CostEstimationResult:
    """One priced line, one explicitly unpriced component, priced coverage."""
    return CostEstimationResult(
        lines=[
            CostLineItem(
                component_name="alpha-compute",
                pricing_model=PricingModel.ON_DEMAND,
                unit="requests",
                monthly_usage=1_000_000.0,
                unit_price_usd=0.0000025,
                monthly_cost_usd=2.5,
                usage_formula="1,000,000 requests * 0.0000025",
                pricing_source="aws-price-list-api",
                pricing_retrieved_at=NOW,
                region_code="us-east-1",
            )
        ],
        totals_usd_per_month={"on_demand": 2.5},
        assumptions=["Prices retrieved live for region us-east-1."],
        warnings=["One component could not be priced."],
        unpriced=[
            UnpricedComponent(
                component_name="beta-archive",
                reason="No live Price List fact matched this component.",
            )
        ],
        coverage=[
            CoverageEntry(
                component_name="alpha-compute",
                dimension="requests",
                label="requests",
                status=CoverageStatus.PRICED,
                reason="Included in the estimated monthly total.",
            )
        ],
        generated_at=NOW,
        pricing_as_of=NOW,
        fully_priced=False,
    )


def _estimated_cost_result(*, partial: bool = False) -> CostEstimationResult:
    """An estimated result with usage scenarios, assumptions, and coverage."""
    completeness = (
        CostCompleteness.PARTIALLY_UNRESOLVED
        if partial
        else CostCompleteness.ESTIMATED_WITH_ASSUMPTIONS
    )
    coverage = [
        CoverageEntry(
            component_name="alpha-compute",
            dimension="requests",
            label="requests",
            status=CoverageStatus.PRICED,
            reason="Included in the priced subtotal.",
            disposition=DimensionDisposition.PRICEABLE,
        ),
        CoverageEntry(
            component_name="alpha-compute",
            dimension="gb_second",
            label="function duration (GB-seconds)",
            status=CoverageStatus.ESTIMATED,
            reason="Estimated usage priced deterministically.",
            disposition=DimensionDisposition.ESTIMABLE,
        ),
    ]
    if partial:
        coverage.append(
            CoverageEntry(
                component_name="alpha-compute",
                dimension="provisioned_concurrency",
                label="provisioned concurrency",
                status=CoverageStatus.USAGE_UNKNOWN,
                reason="No usage derivation or estimate for this dimension.",
                disposition=DimensionDisposition.ESTIMABLE,
            )
        )
    return CostEstimationResult(
        lines=[
            CostLineItem(
                component_name="alpha-compute",
                pricing_model=PricingModel.ON_DEMAND,
                unit="requests",
                monthly_usage=1_000_000.0,
                unit_price_usd=0.0000025,
                monthly_cost_usd=2.5,
                usage_formula="1,000,000 requests * 0.0000025",
                pricing_source="aws-price-list-api",
                pricing_retrieved_at=NOW,
                region_code="us-east-1",
            )
        ],
        totals_usd_per_month={"on_demand": 2.5},
        assumptions=[
            "Prices retrieved live for region us-east-1.",
            "Function duration assumed at 300 ms.",
        ],
        warnings=[],
        unpriced=[],
        coverage=coverage,
        fully_priced=False,
        completeness=completeness,
        estimated_lines=[
            CostLineItem(
                component_name="alpha-compute",
                pricing_model=PricingModel.ON_DEMAND,
                unit="GB-Seconds",
                monthly_usage=1_000_000.0,
                unit_price_usd=0.0000055,
                monthly_cost_usd=5.5,
                usage_formula="1,000,000 GB-Seconds * 0.0000055",
                pricing_source="aws-price-list-api",
                pricing_retrieved_at=NOW,
                region_code="us-east-1",
                usage_provenance=UsageProvenance.ESTIMATOR_ASSUMPTION,
            )
        ],
        estimates=[
            UsageEstimate(
                component_name="alpha-compute",
                dimension="gb_second",
                unit="GB-Seconds",
                low_quantity=500_000.0,
                typical_quantity=1_000_000.0,
                high_quantity=2_000_000.0,
                provenance=UsageProvenance.ESTIMATOR_ASSUMPTION,
                confidence=EstimationConfidence.MEDIUM,
                rationale="Assumes 300 ms average duration at 1 GB memory.",
                assumptions=["Average function duration is 300 ms."],
                facts_used=["1,000,000 requests per month"],
                unresolved_questions=["Is memory configured at 1 GB?"],
            )
        ],
        estimated_totals_usd_per_month={"on_demand": 5.5},
        best_estimate_total_usd_per_month=None if partial else 8.0,
        estimated_range_usd_per_month=(
            None
            if partial
            else EstimatedCostRange(low_usd=4.0, high_usd=13.0)
        ),
        unresolved_dimension_count=1 if partial else 0,
        generated_at=NOW,
        pricing_as_of=NOW,
    )


def _architecture(
    requirements_session_id: str,
    *,
    component_names: list[str] | None = None,
    cost: CostEstimationResult | None = None,
) -> ArchitectureResult:
    """Generic architecture fixture built from arbitrary component names.

    Node ids come from the real deterministic reference builder, so the
    API test proves ids survive the read path unchanged.
    """
    component_names = component_names or ["alpha-compute", "beta-store"]
    references = build_component_references(component_names)

    nodes = [
        ArchitectureNode(
            node_id=references[name],
            component_name=name,
            category="compute" if index == 0 else "database",
            role_in_system=f"stage {index}",
            capacity=[
                CapacityValue(
                    metric=CapacityMetricKey.PEAK_READ_QPS,
                    value=1200.0,
                    unit="qps",
                )
            ],
            scaling_note="scales horizontally",
            availability_note="multi-az",
            evidence_refs=[f"evidence-{references[name]}"],
        )
        for index, name in enumerate(component_names)
    ]
    edges = [
        ArchitectureEdge(
            edge_id=f"{references[component_names[0]]}->{references[name]}",
            from_node=references[component_names[0]],
            to_node=references[name],
            kind=EdgeKind.REQUEST,
            protocol="https",
            description=f"primary flow into {name}",
            evidence_refs=[f"evidence-{references[name]}"],
        )
        for name in component_names[1:]
    ]
    cost_links = [
        CostLink(
            component_name=name,
            priced=name == component_names[0],
            monthly_cost_usd=2.5 if name == component_names[0] else None,
            unpriced_reason=(
                None
                if name == component_names[0]
                else "No live Price List fact matched this component."
            ),
        )
        for name in component_names
    ]
    return ArchitectureResult(
        artifact_id=str(uuid.uuid4()),
        session_id=requirements_session_id,
        version=1,
        nodes=nodes,
        edges=edges,
        entry_points=[references[component_names[0]]],
        data_store_node_ids=[
            references[name]
            for name in component_names
            if name != component_names[0]
        ],
        cost_links=cost_links,
        cost_totals_snapshot={"on_demand": 2.5},
        decisions=[
            ArchitectureDecision(
                decision="use a relational store",
                rationale="fixture decision",
            )
        ],
        assumptions=["Region chosen by delegation."],
        warnings=["Fixture warning retained."],
        evidence_references=["evidence-alpha-compute"],
        inputs_ref=_inputs_ref(requirements_session_id),
        generated_at=NOW,
    )


def _seed_design_checkpoint(
    session_id: str,
    *,
    architecture: ArchitectureResult | None = None,
    cost: CostEstimationResult | None = None,
    corrupt: str | None = None,
    status: str = "ready",
    nested_requirements_session_id: str | None = None,
) -> None:
    """Persist a design checkpoint exactly as the design graph would."""
    requirements_state = _requirements_state(session_id)
    if nested_requirements_session_id:
        # A completed design run gives the requirements stage its own thread.
        requirements_state = requirements_state.model_copy(
            update={"session_id": nested_requirements_session_id}
        )

    channels = {
        "session_id": session_id,
        "user_request": "design a generic workload",
        "requirements_state": requirements_state.model_dump(mode="json"),
        "capacity_result": None,
        "component_selection": None,
        "cost_result": cost.model_dump(mode="json") if cost else None,
        "architecture": (
            architecture.model_dump(mode="json") if architecture else None
        ),
        "status": status,
        "error": None,
        "warnings": [],
    }
    if corrupt == "architecture":
        # Structurally invalid: nodes is not a list of node objects.
        channels["architecture"] = {"artifact_id": "broken"}

    checkpoint = {
        "v": 4,
        "id": "1f000000-0000-7000-8000-000000000001",
        "ts": "2026-01-01T00:00:00+00:00",
        "channel_values": channels,
        "channel_versions": {},
        "versions_seen": {},
        "updated_channels": sorted(channels),
    }

    import asyncio

    async def _put() -> None:
        async with create_checkpointer() as checkpointer:
            await checkpointer.aput(
                {
                    "configurable": {
                        "thread_id": session_id,
                        "checkpoint_ns": "",
                    }
                },
                checkpoint,
                {"source": "update", "step": 0, "writes": {}},
                {},
            )

    asyncio.run(_put())


class DesignStateApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = ckpt_sqlite.CHECKPOINT_DB_PATH
        ckpt_sqlite.CHECKPOINT_DB_PATH = os.path.join(
            self._tmp.name, "checkpoints.sqlite"
        )
        self._ckpt_sqlite = ckpt_sqlite
        self.client = api_client(create_app())

    def tearDown(self):
        self._ckpt_sqlite.CHECKPOINT_DB_PATH = self._orig_path
        self._tmp.cleanup()

    # --- session lifecycle ------------------------------------------
    def _create_requirements_session(self) -> str:
        with patch.object(
            api_sessions_mod,
            "build_initial_requirements_state",
            side_effect=lambda request, session_id=None: _requirements_state(
                session_id or str(uuid.uuid4())
            ),
        ):
            response = self.client.post(
                "/design/sessions",
                json={"user_request": "design a generic workload"},
            )
        self.assertEqual(response.status_code, 200)
        return response.json()["session_id"]

    def test_01_create_session_returns_requirements_stages(self):
        session_id = self._create_requirements_session()
        self.assertTrue(session_id)

        fetched = self.client.get(f"/design/sessions/{session_id}")
        self.assertEqual(fetched.status_code, 200)
        body = fetched.json()
        self.assertEqual(body["session_id"], session_id)
        self.assertEqual(body["stages"]["has_design_run"], False)
        self.assertIsNone(body["stages"]["architecture"])
        self.assertIsNone(body["stages"]["cost"])

    def test_02_requirements_and_assumptions_serialize(self):
        session_id = self._create_requirements_session()
        body = self.client.get(f"/design/sessions/{session_id}").json()

        self.assertEqual(
            body["requirements"]["region"], "us-east-1"
        )
        self.assertEqual(len(body["assumptions"]), 1)
        assumption = body["assumptions"][0]
        self.assertEqual(assumption["name"], "region")
        self.assertEqual(assumption["source"], "architect_default")
        self.assertEqual(assumption["confidence"], "medium")
        self.assertEqual(
            assumption["rationale"], "User delegated region selection."
        )

    def test_03_unknown_session_returns_404(self):
        for path in (
            "/design/sessions/missing",
            "/design/sessions/missing/architecture",
            "/design/sessions/missing/cost",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(
                    response.json()["detail"]["code"], "session_not_found"
                )

    def test_04_session_without_design_run_reports_no_artifact(self):
        session_id = self._create_requirements_session()
        architecture = self.client.get(
            f"/design/sessions/{session_id}/architecture"
        )
        cost = self.client.get(f"/design/sessions/{session_id}/cost")
        self.assertEqual(architecture.status_code, 404)
        self.assertEqual(
            architecture.json()["detail"]["code"], "architecture_not_available"
        )
        self.assertEqual(cost.status_code, 404)
        self.assertEqual(
            cost.json()["detail"]["code"], "cost_not_available"
        )

    # --- architecture artifact --------------------------------------
    def _seed_full_session(self, component_names=None) -> tuple[str, ArchitectureResult]:
        session_id = str(uuid.uuid4())
        cost = _cost_result()
        architecture = _architecture(
            session_id,
            component_names=component_names,
            cost=cost,
        )
        _seed_design_checkpoint(
            session_id, architecture=architecture, cost=cost
        )
        return session_id, architecture

    def test_05_architecture_endpoint_returns_artifact(self):
        session_id, architecture = self._seed_full_session()
        response = self.client.get(
            f"/design/sessions/{session_id}/architecture"
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["artifact_id"], architecture.artifact_id)
        self.assertEqual(body["schema_version"], architecture.schema_version)
        self.assertEqual(body["version"], architecture.version)
        self.assertEqual(body["inputs_ref"]["region_code"], "us-east-1")

    def test_06_deterministic_node_ids_preserved(self):
        session_id, architecture = self._seed_full_session(
            ["alpha-compute", "beta-store"]
        )
        body = self.client.get(
            f"/design/sessions/{session_id}/architecture"
        ).json()
        # Derived by the real builder upstream, unchanged by the API.
        self.assertEqual(
            [node["node_id"] for node in body["nodes"]],
            [node.node_id for node in architecture.nodes],
        )
        self.assertEqual(
            [node["node_id"] for node in body["nodes"]],
            ["alpha-compute", "beta-store"],
        )

    def test_07_edges_serialize_with_endpoints_and_kind(self):
        session_id, architecture = self._seed_full_session()
        body = self.client.get(
            f"/design/sessions/{session_id}/architecture"
        ).json()
        self.assertEqual(len(body["edges"]), len(architecture.edges))
        edge = body["edges"][0]
        self.assertEqual(edge["from_node"], "alpha-compute")
        self.assertEqual(edge["to_node"], "beta-store")
        self.assertEqual(edge["kind"], "request")
        self.assertEqual(edge["protocol"], "https")
        node_ids = {node["node_id"] for node in body["nodes"]}
        self.assertIn(edge["from_node"], node_ids)
        self.assertIn(edge["to_node"], node_ids)

    def test_08_entry_points_and_data_stores_exposed(self):
        session_id, _ = self._seed_full_session()
        body = self.client.get(
            f"/design/sessions/{session_id}/architecture"
        ).json()
        self.assertEqual(body["entry_points"], ["alpha-compute"])
        self.assertEqual(body["data_store_node_ids"], ["beta-store"])

    def test_09_node_capacity_and_evidence_exposed(self):
        session_id, _ = self._seed_full_session()
        node = self.client.get(
            f"/design/sessions/{session_id}/architecture"
        ).json()["nodes"][0]
        self.assertEqual(node["capacity"][0]["metric"], "peak_read_qps")
        self.assertEqual(node["capacity"][0]["value"], 1200.0)
        self.assertEqual(node["capacity"][0]["unit"], "qps")
        self.assertEqual(node["evidence_refs"], ["evidence-alpha-compute"])
        self.assertTrue(node["scaling_note"])
        self.assertTrue(node["availability_note"])

    def test_10_decisions_assumptions_warnings_exposed(self):
        session_id, _ = self._seed_full_session()
        body = self.client.get(
            f"/design/sessions/{session_id}/architecture"
        ).json()
        self.assertEqual(len(body["decisions"]), 1)
        self.assertEqual(
            body["decisions"][0]["decision"], "use a relational store"
        )
        self.assertEqual(body["assumptions"], ["Region chosen by delegation."])
        self.assertEqual(body["warnings"], ["Fixture warning retained."])
        self.assertEqual(
            body["evidence_references"], ["evidence-alpha-compute"]
        )

    def test_11_two_structurally_different_shapes_both_render(self):
        """A three-node chain and a four-node fan-out, one API."""
        shapes = {
            "chain": ["one", "two", "three"],
            "fanout": ["hub", "spoke-a", "spoke-b", "spoke-c"],
        }
        for label, names in shapes.items():
            with self.subTest(shape=label):
                session_id, architecture = self._seed_full_session(names)
                response = self.client.get(
                    f"/design/sessions/{session_id}/architecture"
                )
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(len(body["nodes"]), len(names))
                self.assertEqual(
                    [n["node_id"] for n in body["nodes"]], names
                )
                self.assertEqual(len(body["edges"]), len(names) - 1)

    # --- cost artifact ----------------------------------------------
    def test_12_cost_endpoint_returns_totals_and_unpriced(self):
        session_id, _ = self._seed_full_session()
        response = self.client.get(f"/design/sessions/{session_id}/cost")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["totals_usd_per_month"], {"on_demand": 2.5})
        self.assertEqual(body["lines"][0]["monthly_cost_usd"], 2.5)
        self.assertEqual(body["lines"][0]["pricing_source"], "aws-price-list-api")
        self.assertEqual(len(body["unpriced"]), 1)
        self.assertEqual(body["unpriced"][0]["component_name"], "beta-archive")
        self.assertTrue(body["unpriced"][0]["reason"])
        self.assertEqual(len(body["coverage"]), 1)
        self.assertEqual(body["coverage"][0]["component_name"], "alpha-compute")
        self.assertEqual(body["coverage"][0]["dimension"], "requests")
        self.assertEqual(body["coverage"][0]["status"], "priced")

    def test_13_session_summary_reports_unpriced_without_zero(self):
        session_id, _ = self._seed_full_session()
        body = self.client.get(f"/design/sessions/{session_id}").json()
        summary = body["stages"]
        self.assertTrue(summary["has_design_run"])
        self.assertEqual(summary["cost"]["totals_usd_per_month"], {"on_demand": 2.5})
        self.assertEqual(len(summary["cost"]["priced"]), 1)
        unpriced = summary["cost"]["unpriced"]
        self.assertEqual(len(unpriced), 1)
        self.assertEqual(unpriced[0]["component_name"], "beta-archive")
        # No zero-cost fabrication for an unpriced component.
        self.assertNotIn("monthly_cost_usd", unpriced[0])

    def test_14_session_summary_reports_architecture_shape(self):
        session_id, architecture = self._seed_full_session()
        summary = self.client.get(f"/design/sessions/{session_id}").json()[
            "stages"
        ]["architecture"]
        self.assertEqual(summary["artifact_id"], architecture.artifact_id)
        self.assertEqual(summary["node_count"], 2)
        self.assertEqual(summary["edge_count"], 1)
        self.assertEqual(summary["entry_points"], ["alpha-compute"])
        self.assertEqual(summary["region_code"], "us-east-1")
        self.assertEqual(summary["warning_count"], 1)

    def test_15_cost_links_preserve_unpriced_reason(self):
        session_id, _ = self._seed_full_session()
        links = self.client.get(
            f"/design/sessions/{session_id}/architecture"
        ).json()["cost_links"]
        by_name = {link["component_name"]: link for link in links}
        self.assertTrue(by_name["alpha-compute"]["priced"])
        self.assertEqual(
            by_name["alpha-compute"]["monthly_cost_usd"], 2.5
        )
        self.assertFalse(by_name["beta-store"]["priced"])
        self.assertIsNone(by_name["beta-store"]["monthly_cost_usd"])
        self.assertTrue(by_name["beta-store"]["unpriced_reason"])

    def test_16_cost_summary_exposes_fully_priced_false_when_unpriced(self):
        session_id, _ = self._seed_full_session()
        summary = self.client.get(f"/design/sessions/{session_id}").json()[
            "stages"
        ]["cost"]
        self.assertFalse(summary["fully_priced"])
        self.assertEqual(len(summary["unpriced"]), 1)
        self.assertEqual(summary["unpriced"][0]["component_name"], "beta-archive")

    def test_17_cost_summary_exposes_fully_priced_true_when_all_priced(self):
        session_id = str(uuid.uuid4())
        cost = CostEstimationResult(
            lines=[
                CostLineItem(
                    component_name="alpha-compute",
                    pricing_model=PricingModel.ON_DEMAND,
                    unit="requests",
                    monthly_usage=1_000_000.0,
                    unit_price_usd=0.0000025,
                    monthly_cost_usd=2.5,
                    usage_formula="1,000,000 requests * 0.0000025",
                    pricing_source="aws-price-list-api",
                    pricing_retrieved_at=NOW,
                    region_code="us-east-1",
                )
            ],
            totals_usd_per_month={"on_demand": 2.5},
            assumptions=[],
            warnings=[],
            unpriced=[],
            generated_at=NOW,
            pricing_as_of=NOW,
            fully_priced=True,
        )
        architecture = _architecture(session_id, component_names=["alpha-compute"], cost=cost)
        _seed_design_checkpoint(session_id, architecture=architecture, cost=cost)
        summary = self.client.get(f"/design/sessions/{session_id}").json()[
            "stages"
        ]["cost"]
        self.assertTrue(summary["fully_priced"])
        self.assertEqual(len(summary["unpriced"]), 0)

    def test_23_coverage_disclosure_is_served_verbatim(self):
        """All three coverage statuses survive both read paths unchanged."""
        session_id = str(uuid.uuid4())
        reason = (
            "No usage derivation rule for usage key 'gb_second'; "
            "not priced rather than estimated."
        )
        cost = CostEstimationResult(
            lines=[
                CostLineItem(
                    component_name="alpha-compute",
                    pricing_model=PricingModel.ON_DEMAND,
                    unit="requests",
                    monthly_usage=1_000_000.0,
                    unit_price_usd=0.0000025,
                    monthly_cost_usd=2.5,
                    usage_formula="1,000,000 requests * 0.0000025",
                    pricing_source="aws-price-list-api",
                    pricing_retrieved_at=NOW,
                    region_code="us-east-1",
                )
            ],
            totals_usd_per_month={"on_demand": 2.5},
            assumptions=[],
            warnings=[],
            unpriced=[
                UnpricedComponent(
                    component_name="gamma-worker", reason=reason
                )
            ],
            coverage=[
                CoverageEntry(
                    component_name="alpha-compute",
                    dimension="requests",
                    label="requests",
                    status=CoverageStatus.PRICED,
                    reason="Included in the estimated monthly total.",
                ),
                CoverageEntry(
                    component_name="gamma-worker",
                    dimension="gb_second",
                    label="function duration (GB-seconds)",
                    status=CoverageStatus.USAGE_UNKNOWN,
                    reason=reason,
                ),
                CoverageEntry(
                    component_name="delta-store",
                    dimension="storage_gbmo",
                    label="table storage (GB-month)",
                    status=CoverageStatus.NOT_MODELED,
                    reason="Table storage is billed per GB-month and is "
                    "not modeled by this pipeline.",
                ),
            ],
            generated_at=NOW,
            pricing_as_of=NOW,
            fully_priced=False,
        )
        architecture = _architecture(
            session_id, component_names=["alpha-compute"], cost=cost
        )
        _seed_design_checkpoint(session_id, architecture=architecture, cost=cost)

        raw = self.client.get(f"/design/sessions/{session_id}/cost").json()
        self.assertEqual(len(raw["coverage"]), 3)
        by_key = {
            (entry["component_name"], entry["dimension"]): entry
            for entry in raw["coverage"]
        }
        self.assertEqual(
            by_key[("alpha-compute", "requests")]["status"], "priced")
        unknown = by_key[("gamma-worker", "gb_second")]
        self.assertEqual(unknown["status"], "usage_unknown")
        self.assertEqual(unknown["label"], "function duration (GB-seconds)")
        # The coverage reason is the same string as the unpriced reason.
        self.assertEqual(unknown["reason"], raw["unpriced"][0]["reason"])
        not_modeled = by_key[("delta-store", "storage_gbmo")]
        self.assertEqual(not_modeled["status"], "not_modeled")
        # Disclosure entries never carry a dollar amount.
        self.assertNotIn("monthly_cost_usd", not_modeled)

        summary = self.client.get(
            f"/design/sessions/{session_id}"
        ).json()["stages"]["cost"]
        self.assertEqual(summary["coverage"], raw["coverage"])

    def test_24_cost_summary_serves_completeness_verbatim(self):
        """A full-coverage artifact serves the new completeness fields
        unchanged from the backend; the API never recomputes them."""
        session_id = str(uuid.uuid4())
        cost = CostEstimationResult(
            lines=[
                CostLineItem(
                    component_name="alpha-compute",
                    pricing_model=PricingModel.ON_DEMAND,
                    unit="requests",
                    monthly_usage=1_000_000.0,
                    unit_price_usd=0.0000025,
                    monthly_cost_usd=2.5,
                    usage_formula="1,000,000 requests * 0.0000025",
                    pricing_source="aws-price-list-api",
                    pricing_retrieved_at=NOW,
                    region_code="us-east-1",
                )
            ],
            totals_usd_per_month={"on_demand": 2.5},
            assumptions=[],
            warnings=[],
            unpriced=[],
            coverage=[
                CoverageEntry(
                    component_name="alpha-compute",
                    dimension="requests",
                    label="requests",
                    status=CoverageStatus.PRICED,
                    reason="Included in the estimated monthly total.",
                    disposition=DimensionDisposition.PRICEABLE,
                )
            ],
            generated_at=NOW,
            pricing_as_of=NOW,
            fully_priced=True,
            completeness=CostCompleteness.FULLY_PRICED,
            best_estimate_total_usd_per_month=2.5,
            estimated_range_usd_per_month=EstimatedCostRange(
                low_usd=2.5, high_usd=2.5
            ),
            unresolved_dimension_count=0,
        )
        architecture = _architecture(
            session_id, component_names=["alpha-compute"], cost=cost
        )
        _seed_design_checkpoint(session_id, architecture=architecture, cost=cost)
        summary = self.client.get(f"/design/sessions/{session_id}").json()[
            "stages"
        ]["cost"]
        self.assertEqual(summary["completeness"], "fully_priced")
        self.assertEqual(summary["best_estimate_total_usd_per_month"], 2.5)
        self.assertEqual(
            summary["estimated_range_usd_per_month"],
            {"low_usd": 2.5, "high_usd": 2.5},
        )
        self.assertEqual(summary["unresolved_dimension_count"], 0)
        self.assertEqual(summary["coverage"][0]["disposition"], "priceable")

    def test_25_partial_estimate_publishes_no_total(self):
        """While dimensions are unresolved the summary keeps serving the
        partial subtotal with no best-estimate total, matching the raw
        artifact verbatim."""
        session_id, _ = self._seed_full_session()
        raw = self.client.get(f"/design/sessions/{session_id}/cost").json()
        summary = self.client.get(f"/design/sessions/{session_id}").json()[
            "stages"
        ]["cost"]
        for key in (
            "completeness",
            "best_estimate_total_usd_per_month",
            "estimated_range_usd_per_month",
            "unresolved_dimension_count",
            "fully_priced",
            "totals_usd_per_month",
            "coverage",
        ):
            self.assertEqual(summary[key], raw[key])
        self.assertIsNone(summary["best_estimate_total_usd_per_month"])
        self.assertIsNone(summary["estimated_range_usd_per_month"])

    def test_26_session_serializes_estimated_usage(self):
        """A completed cost stage with usage estimates must not NameError."""
        session_id = str(uuid.uuid4())
        cost = _estimated_cost_result()
        architecture = _architecture(
            session_id, component_names=["alpha-compute"], cost=cost
        )
        _seed_design_checkpoint(
            session_id, architecture=architecture, cost=cost
        )

        response = self.client.get(f"/design/sessions/{session_id}")
        self.assertEqual(response.status_code, 200)
        summary = response.json()["stages"]["cost"]
        self.assertEqual(summary["completeness"], "estimated_with_assumptions")
        self.assertFalse(summary["fully_priced"])
        self.assertEqual(summary["assumptions"], cost.assumptions)
        self.assertEqual(summary["best_estimate_total_usd_per_month"], 8.0)
        self.assertEqual(
            summary["estimated_range_usd_per_month"],
            {"low_usd": 4.0, "high_usd": 13.0},
        )
        self.assertEqual(summary["unresolved_dimension_count"], 0)
        self.assertEqual(len(summary["estimated_lines"]), 1)
        self.assertEqual(
            summary["estimated_lines"][0]["monthly_cost_usd"], 5.5
        )

        self.assertEqual(len(summary["estimates"]), 1)
        estimate = summary["estimates"][0]
        self.assertEqual(estimate["component_name"], "alpha-compute")
        self.assertEqual(estimate["dimension"], "gb_second")
        self.assertEqual(estimate["unit"], "GB-Seconds")
        self.assertEqual(estimate["low_quantity"], 500_000.0)
        self.assertEqual(estimate["typical_quantity"], 1_000_000.0)
        self.assertEqual(estimate["high_quantity"], 2_000_000.0)
        self.assertEqual(estimate["provenance"], "estimator_assumption")
        self.assertEqual(estimate["confidence"], "medium")
        self.assertEqual(
            estimate["rationale"],
            "Assumes 300 ms average duration at 1 GB memory.",
        )

        coverage = {
            entry["dimension"]: entry for entry in summary["coverage"]
        }
        self.assertEqual(coverage["requests"]["status"], "priced")
        self.assertEqual(coverage["gb_second"]["status"], "estimated")
        self.assertEqual(
            coverage["gb_second"]["disposition"], "estimable"
        )

    def test_27_partially_unresolved_estimate_preserves_coverage(self):
        """Partial estimates still serve scenarios, gaps, and no total."""
        session_id = str(uuid.uuid4())
        cost = _estimated_cost_result(partial=True)
        architecture = _architecture(
            session_id, component_names=["alpha-compute"], cost=cost
        )
        _seed_design_checkpoint(
            session_id, architecture=architecture, cost=cost
        )

        response = self.client.get(f"/design/sessions/{session_id}")
        self.assertEqual(response.status_code, 200)
        summary = response.json()["stages"]["cost"]
        self.assertEqual(summary["completeness"], "partially_unresolved")
        self.assertFalse(summary["fully_priced"])
        self.assertIsNone(summary["best_estimate_total_usd_per_month"])
        self.assertIsNone(summary["estimated_range_usd_per_month"])
        self.assertEqual(summary["unresolved_dimension_count"], 1)
        self.assertEqual(len(summary["estimates"]), 1)
        self.assertEqual(len(summary["estimated_lines"]), 1)
        coverage = {
            entry["dimension"]: entry for entry in summary["coverage"]
        }
        self.assertEqual(coverage["gb_second"]["status"], "estimated")
        self.assertEqual(
            coverage["provisioned_concurrency"]["status"], "usage_unknown"
        )

    # --- failure handling -------------------------------------------
    def test_16_invalid_artifact_fails_safely(self):
        session_id = str(uuid.uuid4())
        _seed_design_checkpoint(session_id, corrupt="architecture")

        response = self.client.get(f"/design/sessions/{session_id}")
        self.assertEqual(response.status_code, 409)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "persisted_state_invalid")
        self.assertIn("architecture", detail["detail"])

    def test_17_invalid_artifact_does_not_leak_on_subresources(self):
        session_id = str(uuid.uuid4())
        _seed_design_checkpoint(session_id, corrupt="architecture")
        for path in ("architecture", "cost"):
            with self.subTest(path=path):
                response = self.client.get(
                    f"/design/sessions/{session_id}/{path}"
                )
                self.assertEqual(response.status_code, 409)

    def test_18_failed_design_run_reports_status_and_error(self):
        session_id = str(uuid.uuid4())
        _seed_design_checkpoint(session_id, status="failed")
        body = self.client.get(f"/design/sessions/{session_id}").json()
        # Requirements status and design-run status are separate facts.
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["stages"]["design_status"], "failed")
        self.assertFalse(body["stages"]["has_design_run"])

    def test_19_loader_prefers_design_checkpoint_over_requirements(self):
        """A design checkpoint must win over an older requirements-only one."""
        import asyncio

        session_id = str(uuid.uuid4())
        cost = _cost_result()
        architecture = _architecture(session_id)
        _seed_design_checkpoint(
            session_id, architecture=architecture, cost=cost
        )
        # Simulate the colliding synthetic requirements checkpoint, which
        # sorts later by checkpoint_id and would mask design state under a
        # naive aget_tuple read.
        state = _requirements_state(session_id)
        asyncio.run(self._put_requirements_only(session_id, state))

        snapshot = asyncio.run(load_design_snapshot(session_id))
        self.assertIsNotNone(snapshot.architecture)
        self.assertEqual(
            snapshot.architecture.artifact_id, architecture.artifact_id
        )

        body = self.client.get(f"/design/sessions/{session_id}").json()
        self.assertTrue(body["stages"]["has_design_run"])

    async def _put_requirements_only(self, session_id, state) -> None:
        checkpoint = {
            "v": 1,
            "id": "ffffffff-ffff-7fff-8fff-ffffffffffff",
            "ts": "",
            "channel_values": {
                "requirements_state": state.model_dump(mode="json")
            },
            "channel_versions": {},
            "versions_seen": {},
            "updated_channels": ["requirements_state"],
        }
        async with create_checkpointer() as checkpointer:
            await checkpointer.aput(
                {
                    "configurable": {
                        "thread_id": session_id,
                        "checkpoint_ns": "",
                    }
                },
                checkpoint,
                {"source": "update", "step": 0, "writes": {}},
                {},
            )

    def test_20_blank_session_id_is_not_found(self):
        response = self.client.get("/design/sessions/%20")
        self.assertIn(response.status_code, (404, 400, 422))

    def test_21_session_id_echoes_the_addressed_design_session(self):
        """A completed run's requirements stage has its own thread id.

        The response must identify the session that was addressed, and
        report the nested requirements id separately, so the UI's session
        identity matches the URL it opened.
        """
        import asyncio

        design_session_id = str(uuid.uuid4())
        cost = _cost_result()
        architecture = _architecture(design_session_id)
        _seed_design_checkpoint(
            design_session_id,
            architecture=architecture,
            cost=cost,
            nested_requirements_session_id="nested-requirements-thread",
        )

        body = self.client.get(
            f"/design/sessions/{design_session_id}"
        ).json()
        self.assertEqual(body["session_id"], design_session_id)
        self.assertEqual(
            body["requirements_session_id"], "nested-requirements-thread"
        )

        # The nested requirements thread is addressable on its own too,
        # exactly as the real workflow persists it.
        asyncio.run(
            self._put_requirements_only(
                "nested-requirements-thread", _requirements_state(
                    "nested-requirements-thread"
                )
            )
        )
        nested = self.client.get("/design/sessions/nested-requirements-thread")
        self.assertEqual(nested.status_code, 200)
        self.assertFalse(nested.json()["stages"]["has_design_run"])

    def test_22_requirements_only_session_reports_no_nested_id(self):
        session_id = self._create_requirements_session()
        body = self.client.get(f"/design/sessions/{session_id}").json()
        self.assertEqual(body["session_id"], session_id)
        self.assertEqual(body["requirements_session_id"], "")


class ApiAdapterBoundaryTests(unittest.TestCase):
    """The API must stay a serialization adapter."""

    def test_api_module_has_no_orchestration_imports(self):
        """The API may delegate to the one design entrypoint, nothing else.

        Starting a design run legitimately calls the top-level
        ``run_design_workflow``, exactly as the CLI does, so the API is
        still an adapter rather than a second orchestration engine. What
        must never appear is any *stage* runner, service or LLM client:
        those would mean the API re-implements a pipeline stage.
        """
        import ast
        import pathlib

        api_dir = (
            pathlib.Path(__file__).resolve().parents[1]
            / "src"
            / "ai_architect"
            / "api"
        )
        allowed = ("ai_architect.agents.design.run",)
        forbidden = (
            "ai_architect.agents.capacity.run",
            "ai_architect.agents.capacity.agent",
            "ai_architect.agents.component_selection.run",
            "ai_architect.agents.component_selection.agent",
            "ai_architect.agents.architecture.run",
            "ai_architect.agents.architecture.agent",
            "ai_architect.agents.design.graph",
            "ai_architect.services.cost",
            "ai_architect.services.architecture",
            "ai_architect.services.capacity",
            "ai_architect.infrastructure.llm",
            "langchain_openai",
            "langchain_anthropic",
            "langchain_ollama",
        )
        offenders = []
        delegated = []
        for path in sorted(api_dir.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if any(name.startswith(f) for f in forbidden):
                        offenders.append(f"{path.name}: {name}")
                    if any(name.startswith(a) for a in allowed):
                        delegated.append(f"{path.name}: {name}")
        self.assertEqual(offenders, [], msg=str(offenders))
        # Exactly one module may reach the design engine.
        self.assertEqual(
            delegated,
            ["design_run.py: ai_architect.agents.design.run"],
            msg=str(delegated),
        )

    def test_api_module_contains_no_workload_names(self):
        import pathlib

        api_dir = (
            pathlib.Path(__file__).resolve().parents[1]
            / "src"
            / "ai_architect"
            / "api"
        )
        banned = (
            "healthcare",
            "ecommerce",
            "url_shortener",
            "booking",
            "workload ==",
        )
        offenders = {}
        for path in sorted(api_dir.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            hits = [token for token in banned if token in text]
            if hits:
                offenders[path.name] = hits
        self.assertEqual(offenders, {}, msg=str(offenders))


if __name__ == "__main__":
    unittest.main()
