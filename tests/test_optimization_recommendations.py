"""Unit tests for optimization recommendation generation.

No LLM calls, no network: snapshots are hand-built namespaces. The
generator never prices, composes, or gates; it only turns a stored
BLOCKED verdict into unevaluated diagnostic recommendations.

Production note: under the current quality gate a BLOCKED verdict never
carries critical critic findings (the gate routes those to
REQUIRES_REVISION or REQUIRES_USER_DECISION before the ceiling check),
so the tests marked "synthetic" supply gate states the production
pipeline cannot currently produce. They pin the generator's defensive
behavior in case gate ordering ever changes; a blocked budget breach is
the only production path that yields recommendations.
"""
import unittest
from types import SimpleNamespace

from ai_architect.domain.architecture.recommendation import generate_recommendations
from ai_architect.infrastructure.checkpoint.design import (
    PersistedStateError,
    _snapshot_from_channels,
)


def _gate(outcome="blocked", reasons=None):
    return SimpleNamespace(outcome=outcome, reasons=list(reasons or []))


def _architecture(artifact_id="arch-1", cost_links=None):
    return SimpleNamespace(
        artifact_id=artifact_id, cost_links=list(cost_links or [])
    )


def _cost_result(lines=None):
    return SimpleNamespace(lines=list(lines or []))


def _critique(findings=None):
    return SimpleNamespace(findings=list(findings or []))


def _snapshot(**overrides):
    state = {
        "session_id": "design-s1",
        "user_request": "build it",
        "quality_gate": None,
        "architecture": None,
        "cost_result": None,
        "critique": None,
    }
    state.update(overrides)
    return SimpleNamespace(**state)


class OptimizationRecommendationTests(unittest.TestCase):
    def test_budget_breach_recommends_costliest_priced_component(self):
        snap = _snapshot(
            quality_gate=_gate(
                reasons=[
                    "Cost ceiling breached: on_demand: $2,039.27/mo exceeds the "
                    "$50.00/mo ceiling."
                ]
            ),
            architecture=_architecture(
                cost_links=[
                    SimpleNamespace(
                        component_name="Amazon API Gateway (HTTP)",
                        priced=True,
                        monthly_cost_usd=2000.0,
                    ),
                    SimpleNamespace(
                        component_name="Amazon SQS (Standard)",
                        priced=True,
                        monthly_cost_usd=24.0,
                    ),
                ]
            ),
            cost_result=_cost_result(
                [
                    SimpleNamespace(
                        component_name="Amazon API Gateway (HTTP)",
                        monthly_cost_usd=2000.0,
                    ),
                    SimpleNamespace(
                        component_name="Amazon SQS (Standard)",
                        monthly_cost_usd=24.0,
                    ),
                ]
            ),
        )
        recs = generate_recommendations(snap.architecture, snap.quality_gate, snap.critique, snap.cost_result)
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec["finding_category"], "cost_ceiling")
        self.assertEqual(rec["target_component"], "Amazon API Gateway (HTTP)")
        self.assertFalse(rec["evaluated"])
        self.assertEqual(rec["status"], "unevaluated")
        self.assertEqual(rec["affects_requirement"], "budget_constraints")
        self.assertIn("not represent a feasible", rec["disclaimer"])
        self.assertIn("budget-compliant", rec["disclaimer"])
        self.assertTrue(rec["recommendation_id"].startswith("rec-"))

    def test_recommendation_ids_are_deterministic(self):
        snap = _snapshot(
            quality_gate=_gate(
                reasons=[
                    "Cost ceiling breached: on_demand: $2,039.27/mo exceeds the "
                    "$50.00/mo ceiling."
                ]
            ),
            architecture=_architecture(
                cost_links=[
                    SimpleNamespace(
                        component_name="Amazon API Gateway (HTTP)",
                        priced=True,
                        monthly_cost_usd=2000.0,
                    )
                ]
            ),
            cost_result=_cost_result(
                [
                    SimpleNamespace(
                        component_name="Amazon API Gateway (HTTP)",
                        monthly_cost_usd=2000.0,
                    )
                ]
            ),
        )
        first = generate_recommendations(snap.architecture, snap.quality_gate, snap.critique, snap.cost_result)[0]["recommendation_id"]
        second = generate_recommendations(snap.architecture, snap.quality_gate, snap.critique, snap.cost_result)[0]["recommendation_id"]
        self.assertEqual(first, second)

    def test_synthetic_critical_finding_recommends_named_component(self):
        """Synthetic state: BLOCKED plus a critical finding (not producible).

        The current gate cannot emit this combination; this pins the
        defensive branch's behavior for a named component.
        """
        snap = _snapshot(
            quality_gate=_gate(reasons=["Single entry point is a spof."]),
            architecture=_architecture(),
            critique=_critique(
                [
                    SimpleNamespace(
                        severity="critical",
                        category="spof",
                        statement="Single entry point with no failover.",
                        about_component_ref="Amazon API Gateway (HTTP)",
                    )
                ]
            ),
        )
        recs = generate_recommendations(snap.architecture, snap.quality_gate, snap.critique, snap.cost_result)
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec["finding_category"], "spof")
        self.assertEqual(rec["target_component"], "Amazon API Gateway (HTTP)")
        self.assertEqual(rec["proposed_change"], "component_swap")

    def test_synthetic_cross_cutting_critical_has_no_target_component(self):
        """Synthetic state: BLOCKED plus a component-less critical finding."""
        snap = _snapshot(
            quality_gate=_gate(reasons=["Observability gaps remain."]),
            architecture=_architecture(),
            critique=_critique(
                [
                    SimpleNamespace(
                        severity="critical",
                        category="observability",
                        statement="No telemetry emitted.",
                        about_component_ref="",
                    )
                ]
            ),
        )
        recs = generate_recommendations(snap.architecture, snap.quality_gate, snap.critique, snap.cost_result)
        self.assertEqual(len(recs), 1)
        self.assertIsNone(recs[0]["target_component"])

    def test_synthetic_warning_findings_do_not_generate_recommendations(self):
        """Synthetic state: BLOCKED plus warning-only findings."""
        snap = _snapshot(
            quality_gate=_gate(reasons=["Observability gaps remain."]),
            architecture=_architecture(),
            critique=_critique(
                [
                    SimpleNamespace(
                        severity="warning",
                        category="observability",
                        statement="No telemetry emitted.",
                        about_component_ref="Amazon S3",
                    )
                ]
            ),
        )
        self.assertEqual(generate_recommendations(snap.architecture, snap.quality_gate, snap.critique, snap.cost_result), [])

    def test_non_blocked_outcome_yields_none(self):
        for outcome in (
            "approved_for_review",
            "requires_revision",
            "requires_user_decision",
        ):
            snap = _snapshot(quality_gate=_gate(outcome=outcome))
            self.assertEqual(generate_recommendations(snap.architecture, snap.quality_gate, snap.critique, snap.cost_result), [])

    def test_no_gate_yields_none(self):
        s = _snapshot()
        self.assertEqual(
            generate_recommendations(s.architecture, s.quality_gate, s.critique, s.cost_result),
            [],
        )

    def test_missing_architecture_reason_yields_none(self):
        snap = _snapshot(
            quality_gate=_gate(
                reasons=["No architecture artifact exists to evaluate."]
            )
        )
        self.assertEqual(generate_recommendations(snap.architecture, snap.quality_gate, snap.critique, snap.cost_result), [])

    def test_missing_critique_reason_yields_none(self):
        snap = _snapshot(
            quality_gate=_gate(
                reasons=["No design critique exists for this architecture."]
            )
        )
        self.assertEqual(generate_recommendations(snap.architecture, snap.quality_gate, snap.critique, snap.cost_result), [])

    def test_stale_critique_reason_yields_none(self):
        snap = _snapshot(
            quality_gate=_gate(
                reasons=[
                    "The stored critique reviews a different architecture "
                    "artifact ('other'); it is stale and cannot gate this one."
                ]
            )
        )
        self.assertEqual(generate_recommendations(snap.architecture, snap.quality_gate, snap.critique, snap.cost_result), [])

    def test_budget_breach_with_no_priced_component_yields_none(self):
        snap = _snapshot(
            quality_gate=_gate(
                reasons=[
                    "Cost ceiling breached: on_demand: $2,039.27/mo exceeds the "
                    "$50.00/mo ceiling."
                ]
            ),
            architecture=_architecture(),
            cost_result=_cost_result(),
        )
        self.assertEqual(generate_recommendations(snap.architecture, snap.quality_gate, snap.critique, snap.cost_result), [])

    def test_every_recommendation_carries_the_disclaimer(self):
        """Mixed state: a production budget breach plus a synthetic
        critical finding; every entry, whichever branch produced it,
        must carry the fixed disclaimer."""
        snap = _snapshot(
            quality_gate=_gate(
                reasons=[
                    "Cost ceiling breached: on_demand: $2,039.27/mo exceeds the "
                    "$50.00/mo ceiling.",
                    "Single entry point is a spof.",
                ]
            ),
            architecture=_architecture(
                cost_links=[
                    SimpleNamespace(
                        component_name="Amazon API Gateway (HTTP)",
                        priced=True,
                        monthly_cost_usd=2000.0,
                    )
                ]
            ),
            critique=_critique(
                [
                    SimpleNamespace(
                        severity="critical",
                        category="spof",
                        statement="Single entry point is a spof.",
                        about_component_ref="Amazon API Gateway (HTTP)",
                    )
                ]
            ),
        )
        recs = generate_recommendations(snap.architecture, snap.quality_gate, snap.critique, snap.cost_result)
        self.assertEqual(len(recs), 2)
        for rec in recs:
            self.assertIn("not represent a feasible", rec["disclaimer"])
            self.assertIn("budget-compliant", rec["disclaimer"])
            self.assertFalse(rec["evaluated"])
            self.assertEqual(rec["status"], "unevaluated")


class OptimizationRecommendationPersistenceTests(unittest.TestCase):
    """Stored recommendations are rebuilt fail-closed like other fields."""

    def test_valid_stored_record_round_trips(self):
        snapshot = _snapshot_from_channels(
            "design-s1",
            {
                "optimization_recommendations": {
                    "recommendations": [],
                    "derived_from_artifact_id": "arch-1",
                    "generated_at": "2026-10-17T12:00:00",
                }
            },
        )
        self.assertIsNotNone(snapshot.optimization_recommendations)
        self.assertEqual(
            snapshot.optimization_recommendations["derived_from_artifact_id"],
            "arch-1",
        )

    def test_corrupt_stored_record_fails_closed(self):
        with self.assertRaises(PersistedStateError):
            _snapshot_from_channels(
                "design-s1",
                {"optimization_recommendations": "not-an-object"},
            )


if __name__ == "__main__":
    unittest.main()
