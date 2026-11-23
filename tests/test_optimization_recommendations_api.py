"""API round-trip tests for optimization recommendations.

Uses the real FastAPI TestClient and a temporary checkpoint database,
following the pattern in test_budget_pipeline_integrity.py. No LLM
calls, no network.
"""
import asyncio
import os
import tempfile
import unittest
import uuid
from datetime import datetime

import aiosqlite
from tests.api_auth import api_client

from ai_architect.api.main import create_app
from ai_architect.domain.architecture.critique import (
    DesignCritique,
    QualityGateOutcome,
    QualityGateResult,
)
from ai_architect.domain.architecture.models import (
    ArchitectureInputsRef,
    ArchitectureResult,
    CostLink,
)
from ai_architect.domain.cost.models import (
    CostEstimationResult,
    CostLineItem,
    PricingModel,
)
from ai_architect.domain.requirements.models import (
    RequirementsExtraction,
    ValidationResult,
    ValidationStatus,
)
from ai_architect.domain.requirements.state import RequirementsState
from ai_architect.infrastructure.checkpoint import sqlite as ckpt_sqlite
from ai_architect.infrastructure.checkpoint.design import (
    load_design_snapshot,
    save_design_run_marker,
    save_session_record,
)

NOW = datetime(2026, 10, 17, 12, 0, 0)


def _requirements_state(session_id: str) -> RequirementsState:
    extraction = RequirementsExtraction(
        application_description="Scalable, serverless messaging application.",
        cloud_provider="aws",
        region="us-east-1",
        budget_constraints="Target a monthly infrastructure cost below $50.",
    )
    validation = ValidationResult(
        status=ValidationStatus.READY,
        blocking_clarifications=[],
        accepted_assumptions=[],
    )
    return RequirementsState.from_extraction(
        session_id=session_id,
        original_request="design a messaging app",
        extraction=extraction,
        validation_result=validation,
    )


def _architecture(artifact_id: str, session_id: str) -> ArchitectureResult:
    return ArchitectureResult(
        schema_version="architecture/v1",
        artifact_id=artifact_id,
        session_id=session_id,
        version=1,
        nodes=[],
        edges=[],
        entry_points=[],
        data_store_node_ids=[],
        boundaries=["region:us-east-1"],
        cost_links=[
            CostLink(
                component_name="Amazon API Gateway (HTTP)",
                priced=True,
                pricing_model=PricingModel.ON_DEMAND,
                unit="Requests",
                monthly_cost_usd=2039.27,
                pricing_source="aws-price-list-api",
                unpriced_reason=None,
            )
        ],
        cost_totals_snapshot={"on_demand": 2039.27},
        inputs_ref=ArchitectureInputsRef(
            requirements_session_id=session_id,
            capacity_formula_version="v1",
            selection_generated_at=NOW,
            cost_generated_at=NOW,
            cost_pricing_as_of=NOW,
            region_code="us-east-1",
            upstream_inputs_hash="hash-original",
        ),
    )


def _cost_result() -> CostEstimationResult:
    return CostEstimationResult(
        lines=[
            CostLineItem(
                component_name="Amazon API Gateway (HTTP)",
                pricing_model=PricingModel.ON_DEMAND,
                unit="Requests",
                monthly_usage=6000000.0,
                unit_price_usd=0.00035,
                monthly_cost_usd=2039.27,
                usage_formula="(peak_write_qps + peak_read_qps) / peak_factor * 2592000 s/month",
                pricing_source="aws-price-list-api",
                pricing_retrieved_at=NOW,
                region_code="us-east-1",
            )
        ],
        totals_usd_per_month={"on_demand": 2039.27},
        unpriced=[],
        fully_priced=False,
    )


def _gate(artifact_id: str) -> QualityGateResult:
    return QualityGateResult(
        outcome=QualityGateOutcome.BLOCKED,
        reasons=[
            "Cost ceiling breached: on_demand: $2,039.27/mo exceeds the "
            "$50.00/mo ceiling."
        ],
        evaluated_artifact_id=artifact_id,
    )


def _critique(artifact_id: str) -> DesignCritique:
    return DesignCritique(
        artifact_id="crit-1",
        session_id="req-s1",
        reviewed_artifact_id=artifact_id,
        reviewed_version=1,
        findings=[],
        summary="clean",
    )


class OptimizationRecommendationsAPITests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = ckpt_sqlite.CHECKPOINT_DB_PATH
        ckpt_sqlite.CHECKPOINT_DB_PATH = os.path.join(
            self._tmp.name, "checkpoints.sqlite"
        )
        self.client = api_client(create_app())

    def tearDown(self):
        ckpt_sqlite.CHECKPOINT_DB_PATH = self._orig_path
        self._tmp.cleanup()

    async def _persist_blocked_session_async(self) -> str:
        session_id = str(uuid.uuid4())
        requirements_session_id = str(uuid.uuid4())
        artifact_id = str(uuid.uuid4())

        # Create the checkpoint thread (marker) with requirements state.
        await save_design_run_marker(
            session_id,
            status="ready",
            user_request="design a messaging app",
            requirements_state=_requirements_state(requirements_session_id),
        )
        # Persist the finished design artifacts.
        await save_session_record(
            session_id,
            field="architecture",
            record=_architecture(artifact_id, session_id).model_dump(),
        )
        await save_session_record(
            session_id,
            field="quality_gate",
            record=_gate(artifact_id).model_dump(),
        )
        await save_session_record(
            session_id,
            field="critique",
            record=_critique(artifact_id).model_dump(),
        )
        # Persist cost result through the checkpoint channel directly so
        # the generator can read the priced lines.
        await save_session_record(
            session_id,
            field="cost_result",
            record=_cost_result().model_dump(),
        )
        return session_id

    def _persist_blocked_session(self) -> str:
        return asyncio.run(self._persist_blocked_session_async())

    async def _checkpoint_row_count_async(self) -> int:
        async with aiosqlite.connect(ckpt_sqlite.CHECKPOINT_DB_PATH) as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM checkpoints")
            row = await cursor.fetchone()
        return int(row[0])

    def _checkpoint_row_count(self) -> int:
        return asyncio.run(self._checkpoint_row_count_async())

    def test_blocked_budget_breach_returns_cost_ceiling_recommendation(self):
        session_id = self._persist_blocked_session()
        response = self.client.get(
            f"/design/sessions/{session_id}/optimization-recommendations"
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["session_id"], session_id)
        self.assertEqual(len(body["recommendations"]), 1)
        rec = body["recommendations"][0]
        self.assertEqual(rec["finding_category"], "cost_ceiling")
        self.assertEqual(rec["target_component"], "Amazon API Gateway (HTTP)")
        self.assertFalse(rec["evaluated"])
        self.assertEqual(rec["status"], "unevaluated")
        self.assertTrue(rec["recommendation_id"].startswith("rec-"))
        self.assertIn("not represent a feasible", rec["disclaimer"])
        self.assertIn("budget-compliant", rec["disclaimer"])
        self.assertEqual(rec["affects_requirement"], "budget_constraints")

    def test_repeated_gets_regenerate_identical_recommendations(self):
        """Regeneration determinism: two GETs yield the same payload.

        This exercises the generator, not persistence; the stored-record
        read-back is covered by
        test_get_persists_record_readable_via_snapshot.
        """
        session_id = self._persist_blocked_session()
        first = self.client.get(
            f"/design/sessions/{session_id}/optimization-recommendations"
        )
        self.assertEqual(first.status_code, 200)
        second = self.client.get(
            f"/design/sessions/{session_id}/optimization-recommendations"
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["recommendations"], second.json()["recommendations"])
        self.assertEqual(first.json()["derived_from_artifact_id"], second.json()["derived_from_artifact_id"])

    def test_get_persists_record_readable_via_snapshot(self):
        """The response is backed by a stored record, read back via the
        checkpoint loader (the same path the design workspace uses)."""
        session_id = self._persist_blocked_session()
        response = self.client.get(
            f"/design/sessions/{session_id}/optimization-recommendations"
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()

        snapshot = asyncio.run(load_design_snapshot(session_id))
        self.assertIsNotNone(snapshot)
        stored = snapshot.optimization_recommendations
        self.assertIsNotNone(stored)
        self.assertEqual(
            stored["derived_from_artifact_id"], body["derived_from_artifact_id"]
        )
        self.assertEqual(stored["recommendations"], body["recommendations"])
        self.assertIn("generated_at", stored)

    def test_repeat_identical_get_does_not_append_checkpoint(self):
        """L1: the first GET persists a record; an identical repeat GET
        performs no further checkpoint write."""
        session_id = self._persist_blocked_session()
        before = self._checkpoint_row_count()

        first = self.client.get(
            f"/design/sessions/{session_id}/optimization-recommendations"
        )
        self.assertEqual(first.status_code, 200)
        after_first = self._checkpoint_row_count()
        self.assertGreater(after_first, before)

        second = self.client.get(
            f"/design/sessions/{session_id}/optimization-recommendations"
        )
        self.assertEqual(second.status_code, 200)
        after_second = self._checkpoint_row_count()
        self.assertEqual(after_second, after_first)

    def test_session_without_design_artifacts_returns_empty(self):
        session_id = str(uuid.uuid4())
        requirements_session_id = str(uuid.uuid4())
        asyncio.run(
            save_design_run_marker(
                session_id,
                status="ready",
                user_request="design a messaging app",
                requirements_state=_requirements_state(requirements_session_id),
            )
        )
        response = self.client.get(
            f"/design/sessions/{session_id}/optimization-recommendations"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["recommendations"], [])

    def test_unknown_session_returns_404(self):
        response = self.client.get(
            "/design/sessions/00000000-0000-0000-0000-000000000000/optimization-recommendations"
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
