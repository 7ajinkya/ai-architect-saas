"""Design Quality Gate tests: deterministic verdicts over finished work.

No LLM calls: critiques and architectures are hand-built fixtures.
The gate never revises, never invents, and never raises for content.

Run: poetry run python -m unittest discover tests
"""
import unittest
from datetime import datetime
from types import SimpleNamespace

from ai_architect.domain.architecture.critique import (
    CritiqueFinding,
    DesignCritique,
    QualityGateOutcome,
)
from ai_architect.domain.architecture.models import CostLink
from ai_architect.services.architecture.quality_gate import (
    evaluate_quality_gate,
    parse_budget_ceiling,
)


def _finding(severity="warning", subject=""):
    return CritiqueFinding(
        category="spof",
        severity=severity,
        statement="Single entry with no failover",
        rationale="Entry failure takes the system down",
        about_component_ref=subject,
        evidence_refs=[],
        recommendation="Add a second entry",
        suggested_route="composer_revision",
    )


def _critique(findings, reviewed="artifact-1"):
    return DesignCritique(
        artifact_id="critique-1",
        session_id="design-s1",
        reviewed_artifact_id=reviewed,
        reviewed_version=1,
        findings=findings,
        summary="summary",
        generated_at=datetime(2026, 10, 4, 12, 0, 0),
    )


class _Architecture:
    def __init__(self, artifact_id="artifact-1", totals=None, cost_links=None):
        self.artifact_id = artifact_id
        self.cost_totals_snapshot = totals or {}
        self.cost_links = cost_links or []


class QualityGateTests(unittest.TestCase):
    def test_01_no_architecture_is_blocked(self):
        result = evaluate_quality_gate(
            architecture=None, critique=None, revision_count=0,
            max_cycles=2, budget_constraints="",
        )
        self.assertEqual(
            result.outcome, QualityGateOutcome.BLOCKED
        )
        self.assertTrue(result.reasons)

    def test_02_missing_critique_is_blocked(self):
        result = evaluate_quality_gate(
            architecture=_Architecture(), critique=None,
            revision_count=0, max_cycles=2, budget_constraints="",
        )
        self.assertEqual(result.outcome, QualityGateOutcome.BLOCKED)

    def test_03_stale_critique_is_blocked(self):
        result = evaluate_quality_gate(
            architecture=_Architecture("artifact-2"),
            critique=_critique([], reviewed="artifact-1"),
            revision_count=0, max_cycles=2, budget_constraints="",
        )
        self.assertEqual(result.outcome, QualityGateOutcome.BLOCKED)
        self.assertIn("stale", "; ".join(result.reasons).lower())

    def test_04_clean_critique_is_approved_for_review(self):
        result = evaluate_quality_gate(
            architecture=_Architecture(),
            critique=_critique([_finding("warning"), _finding("info")]),
            revision_count=1, max_cycles=2, budget_constraints="",
        )
        self.assertEqual(
            result.outcome, QualityGateOutcome.APPROVED_FOR_REVIEW
        )
        self.assertEqual(result.critical_count, 0)
        self.assertEqual(result.warning_count, 1)

    def test_05_criticals_with_cycles_left_require_revision(self):
        result = evaluate_quality_gate(
            architecture=_Architecture(),
            critique=_critique([_finding("critical")]),
            revision_count=0, max_cycles=2, budget_constraints="",
        )
        self.assertEqual(
            result.outcome, QualityGateOutcome.REQUIRES_REVISION
        )
        self.assertEqual(result.critical_count, 1)

    def test_06_exhausted_cycles_need_user_decision(self):
        result = evaluate_quality_gate(
            architecture=_Architecture(),
            critique=_critique([_finding("critical")]),
            revision_count=2, max_cycles=2, budget_constraints="",
        )
        self.assertEqual(
            result.outcome, QualityGateOutcome.REQUIRES_USER_DECISION
        )

    def test_07_cost_ceiling_breach_is_blocked(self):
        result = evaluate_quality_gate(
            architecture=_Architecture(totals={"on_demand": 1500.0}),
            critique=_critique([]),
            revision_count=0, max_cycles=2,
            budget_constraints="Keep it under $1,000/mo",
        )
        self.assertEqual(
            result.outcome, QualityGateOutcome.BLOCKED
        )
        self.assertIn("1,000", "; ".join(result.reasons))
        self.assertIn("blocked", "; ".join(result.reasons).lower())

    def test_08_cost_within_ceiling_approves(self):
        result = evaluate_quality_gate(
            architecture=_Architecture(totals={"on_demand": 500.0}),
            critique=_critique([]),
            revision_count=0, max_cycles=2,
            budget_constraints="$1,000/mo ceiling",
        )
        self.assertEqual(
            result.outcome, QualityGateOutcome.APPROVED_FOR_REVIEW
        )

    def test_09_unparseable_budget_is_not_a_ceiling(self):
        self.assertIsNone(parse_budget_ceiling(""))
        self.assertIsNone(parse_budget_ceiling("keep it cheap"))
        self.assertEqual(parse_budget_ceiling("$1,000/mo"), 1000.0)
        result = evaluate_quality_gate(
            architecture=_Architecture(totals={"on_demand": 999999.0}),
            critique=_critique([]),
            revision_count=0, max_cycles=2,
            budget_constraints="keep it cheap",
        )
        self.assertEqual(
            result.outcome, QualityGateOutcome.APPROVED_FOR_REVIEW
        )


class QualityGatePricingCompletenessTests(unittest.TestCase):
    """Partial pricing must never receive the same budget assurance as a
    complete estimate. Missing costs are never interpreted as zero."""

    def _priced_link(self, component_name, priced=True):
        return CostLink(
            component_name=component_name,
            priced=priced,
            unpriced_reason=None if priced else "no pricing data available",
        )

    def test_fully_priced_within_ceiling_approves_with_complete_assurance(
        self,
    ):
        arch = _Architecture(
            totals={"on_demand": 500.0},
            cost_links=[self._priced_link("Amazon API Gateway")],
        )
        result = evaluate_quality_gate(
            architecture=arch,
            critique=_critique([]),
            revision_count=0,
            max_cycles=2,
            budget_constraints="$1,000/mo ceiling",
        )
        self.assertEqual(
            result.outcome, QualityGateOutcome.APPROVED_FOR_REVIEW
        )
        self.assertTrue(
            any("stay within" in r and "incomplete" not in r
                for r in result.reasons),
            msg=f"expected complete-assurance reason, got: {result.reasons}",
        )
        self.assertIn(
            "Priced totals stay within the $1,000.00/mo ceiling.",
            result.reasons,
        )
        self.assertFalse(
            any("incomplete" in r.lower() for r in result.reasons),
            msg=f"unexpected incompleteness reason: {result.reasons}",
        )

    def test_empty_totals_with_ceiling_gets_no_within_claim(self):
        result = evaluate_quality_gate(
            architecture=_Architecture(totals={}),
            critique=_critique([]),
            revision_count=0,
            max_cycles=2,
            budget_constraints="$1,000/mo ceiling",
        )
        self.assertEqual(
            result.outcome, QualityGateOutcome.APPROVED_FOR_REVIEW
        )
        self.assertFalse(
            any("stay within" in r for r in result.reasons),
            msg=f"no within-ceiling claim may exist without priced "
                f"totals: {result.reasons}",
        )
        self.assertTrue(
            any("cannot be asserted" in r for r in result.reasons),
            msg=f"expected unasserted-compliance reason: {result.reasons}",
        )

    def test_partially_priced_within_ceiling_approves_with_caveat(
        self,
    ):
        arch = _Architecture(
            totals={"on_demand": 1.0},
            cost_links=[
                self._priced_link("Amazon API Gateway", priced=True),
                self._priced_link("AWS Lambda", priced=False),
            ],
        )
        result = evaluate_quality_gate(
            architecture=arch,
            critique=_critique([]),
            revision_count=0,
            max_cycles=2,
            budget_constraints="$1,000/mo ceiling",
        )
        self.assertEqual(
            result.outcome, QualityGateOutcome.APPROVED_FOR_REVIEW
        )
        self.assertTrue(
            any("incomplete" in r.lower() and "AWS Lambda" in r
                for r in result.reasons),
            msg=f"expected incompleteness reason naming AWS Lambda, "
                f"got: {result.reasons}",
        )
        self.assertNotIn(
            "Priced totals stay within the $1,000.00/mo ceiling.",
            result.reasons,
            msg="complete-assurance wording must not be used for a "
                "partial estimate",
        )
        self.assertTrue(
            any("Priced lines stay within" in r
                and "cannot be asserted" in r
                for r in result.reasons),
            msg=f"expected conditional ceiling-assurance reason, "
                f"got: {result.reasons}",
        )

    def test_partially_priced_no_ceiling_still_reports_incompleteness(
        self,
    ):
        arch = _Architecture(
            totals={"on_demand": 1.0},
            cost_links=[self._priced_link("AWS Lambda", priced=False)],
        )
        result = evaluate_quality_gate(
            architecture=arch,
            critique=_critique([]),
            revision_count=0,
            max_cycles=2,
            budget_constraints="",
        )
        self.assertEqual(
            result.outcome, QualityGateOutcome.APPROVED_FOR_REVIEW
        )
        self.assertTrue(
            any("incomplete" in r.lower() and "AWS Lambda" in r
                for r in result.reasons),
            msg=f"expected incompleteness reason, got: {result.reasons}",
        )
        self.assertTrue(
            any("No parseable budget ceiling" in r for r in result.reasons),
            msg=f"expected ceiling-missing reason, got: {result.reasons}",
        )

    def test_partially_priced_over_ceiling_is_blocked(
        self,
    ):
        """Priced totals already exceed the ceiling; unpriced components
        only make the true cost higher, so the breach stands."""
        arch = _Architecture(
            totals={"on_demand": 1500.0},
            cost_links=[self._priced_link("AWS Lambda", priced=False)],
        )
        result = evaluate_quality_gate(
            architecture=arch,
            critique=_critique([]),
            revision_count=0,
            max_cycles=2,
            budget_constraints="Keep it under $1,000/mo",
        )
        self.assertEqual(
            result.outcome, QualityGateOutcome.BLOCKED
        )
        self.assertTrue(
            any("exceeds" in r and "1,000" in r for r in result.reasons),
            msg=f"expected ceiling breach reason, got: {result.reasons}",
        )

    def test_mixed_line_component_flagged_via_cost_unpriced(self):
        """A component with one priced line and one unknown dimension
        must not be treated as completely priced. The link says
        priced=True (Lambda has a priced requests line); the cost
        estimator's unpriced dimension entry is what keeps the gap
        visible."""
        arch = _Architecture(
            totals={"on_demand": 12.0},
            cost_links=[self._priced_link("AWS Lambda", priced=True)],
        )
        result = evaluate_quality_gate(
            architecture=arch,
            critique=_critique([]),
            revision_count=0,
            max_cycles=2,
            budget_constraints="$1,000/mo ceiling",
            cost_unpriced=[SimpleNamespace(
                component_name="AWS Lambda",
                reason="No usage derivation rule for usage key "
                       "'gb_second'; not priced rather than estimated.")],
        )
        self.assertEqual(
            result.outcome, QualityGateOutcome.APPROVED_FOR_REVIEW)
        self.assertTrue(
            any("incomplete" in r.lower() and "AWS Lambda" in r
                for r in result.reasons),
            msg=f"expected mixed-dimension caveat, got: {result.reasons}",
        )
        self.assertTrue(
            any("stay within" in r for r in result.reasons),
            msg=f"expected ceiling assurance, got: {result.reasons}",
        )

    def test_cost_unpriced_union_reports_each_component_once(self):
        """Links and cost-unpriced entries describe the same component
        (zero priced lines) plus one mixed-dimension component: the
        caveat counts components once, never duplicate entries."""
        arch = _Architecture(
            totals={"on_demand": 1.0},
            cost_links=[self._priced_link("AWS Lambda", priced=False)],
        )
        result = evaluate_quality_gate(
            architecture=arch,
            critique=_critique([]),
            revision_count=0,
            max_cycles=2,
            budget_constraints="$1,000/mo ceiling",
            cost_unpriced=[
                SimpleNamespace(component_name="AWS Lambda",
                                reason="no pricing facts retrieved"),
                SimpleNamespace(component_name="Amazon SQS (Standard)",
                                reason="No usage derivation rule for "
                                       "usage key 'mystery'"),
            ],
        )
        incomplete = [
            r for r in result.reasons if r.startswith("Pricing incomplete")
        ]
        self.assertEqual(len(incomplete), 1)
        self.assertIn("2 component(s)", incomplete[0])
        self.assertEqual(incomplete[0].count("AWS Lambda"), 1)
        self.assertIn("Amazon SQS (Standard)", incomplete[0])

    def test_blank_unpriced_entries_are_ignored(self):
        arch = _Architecture(
            totals={"on_demand": 1.0},
            cost_links=[self._priced_link("AWS Lambda", priced=True)],
        )
        result = evaluate_quality_gate(
            architecture=arch,
            critique=_critique([]),
            revision_count=0,
            max_cycles=2,
            budget_constraints="$1,000/mo ceiling",
            cost_unpriced=[SimpleNamespace(component_name="", reason="x")],
        )
        self.assertFalse(
            any("incomplete" in r.lower() for r in result.reasons),
            msg=f"blank entry must not create a caveat: {result.reasons}",
        )


if __name__ == "__main__":
    unittest.main()
