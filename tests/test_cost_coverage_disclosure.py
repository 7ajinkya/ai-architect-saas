"""Pricing coverage disclosure tests: omissions are explicit, never silent.

Pins the cost artifact's per-dimension coverage report:

- DynamoDB table storage is priceable (deterministic growth rule) but
  stays an explicit price gap when no storage SKU fact was retrieved;
- S3 request classes and CloudWatch Logs dimensions are estimable, and
  stay usage-unknown when no price fact or validated estimate exists;
- a priceable-but-underivable dimension (Lambda duration) appears both
  as an unpriced entry and a usage_unknown coverage row with the same
  reason (1:1);
- a retrieved fact always wins over a declaration, so a later mapping
  prices the dimension and leaves no stale gap claim;
- every coverage entry is disclosure only: totals keep equaling the sum
  of priced lines, and fully_priced flips False on any gap.

All pricing facts are hand-built fixtures (no network, no LLM).
Run: poetry run python -m unittest discover tests
"""
import unittest
from datetime import datetime

from ai_architect.agents.capacity.models import CapacityModelProposal
from ai_architect.agents.capacity.result import CapacityModelingResult
from ai_architect.agents.component_selection.models import (
    ComponentCandidate,
    ComponentSelectionResult,
)
from ai_architect.domain.cost.models import (
    PricedComponent,
    PriceTier,
    PricingFact,
    PricingModel,
)
from ai_architect.services.capacity.calculator import CapacityCalculator
from ai_architect.services.cost.calculator import CostCalculator


def _capacity() -> CapacityModelingResult:
    proposal = CapacityModelProposal(
        daily_active_users=5000000,
        messages_per_user_per_day=10.0,
        peak_factor=3.0,
        read_write_ratio=4.0,
        average_message_size_bytes=1024,
        connection_factor=0.1,
        storage_retention_days=30,
        rationale={},
    )
    return CapacityModelingResult(
        proposal=proposal,
        calculation=CapacityCalculator().calculate(proposal),
    )


def _component(name: str, category: str = "database") -> ComponentCandidate:
    return ComponentCandidate(
        name=name, provider="aws", category=category, role="r",
        rationale="r", confidence="high", evidence_support="strong")


def _selection(comps):
    return ComponentSelectionResult(selected_components=comps)


def _fact(service_code: str, usage_key: str, unit: str,
          price: float) -> PricingFact:
    return PricingFact(
        service_code=service_code,
        service_name=service_code,
        region_code="us-east-1",
        pricing_model=PricingModel.ON_DEMAND,
        usage_key=usage_key,
        unit=unit,
        tiers=[PriceTier(
            begin=0.0, end=None, unit=unit, unit_price_usd=price,
            description="fixture rate", rate_code="r1")],
        source="aws-price-list-api",
        retrieved_at=datetime.utcnow(),
    )


def _priced(name: str, service_code: str, facts) -> PricedComponent:
    return PricedComponent(
        component_name=name, category="database",
        service_code=service_code, facts=facts)


def _entries(result, component_name: str):
    return [
        entry for entry in result.coverage
        if entry.component_name == component_name
    ]


class CoverageDisclosureTests(unittest.TestCase):
    def _assert_totals_equal_sum_of_lines(self, result):
        self.assertAlmostEqual(
            result.totals_usd_per_month["on_demand"],
            sum(line.monthly_cost_usd for line in result.lines),
            places=9,
        )

    def test_dynamodb_storage_is_priceable_with_explicit_price_gap(self):
        name = "Amazon DynamoDB"
        priced = [_priced(name, "AmazonDynamoDB", [
            _fact("AmazonDynamoDB", "wru", "WriteRequestUnits", 0.00000125),
            _fact("AmazonDynamoDB", "rru", "ReadRequestUnits", 0.00000025),
        ])]
        result = CostCalculator().calculate(
            _capacity(), _selection([_component(name)]), priced, "us-east-1")

        # The two request dimensions are priced; storage usage is
        # derivable but its SKU fact was not retrieved, so it is an
        # explicit price gap rather than a silent omission.
        self.assertEqual(len(result.unpriced), 1)
        self.assertEqual(len(result.lines), 2)

        entries = _entries(result, name)
        self.assertEqual(
            [entry.dimension for entry in entries],
            ["rcu_reserved", "rru", "storage_gbmo", "wcu_reserved", "wru"],
        )
        by_dimension = {entry.dimension: entry for entry in entries}
        self.assertEqual(by_dimension["wru"].status, "priced")
        self.assertEqual(by_dimension["rru"].status, "priced")
        storage = by_dimension["storage_gbmo"]
        self.assertEqual(storage.status, "price_unavailable")
        self.assertEqual(storage.disposition, "priceable")
        self.assertIn("no price fact was retrieved", storage.reason.lower())
        # The wru/rru facts prove pay_per_request billing, so the
        # provisioned reserved-capacity dimensions are not applicable.
        for dim in ("rcu_reserved", "wcu_reserved"):
            self.assertEqual(by_dimension[dim].status, "not_applicable")
            self.assertIn("pay_per_request", by_dimension[dim].reason)

        self.assertFalse(result.fully_priced)
        self._assert_totals_equal_sum_of_lines(result)

    def test_s3_request_classes_disclose_missing_estimates(self):
        name = "Amazon S3"
        priced = [_priced(name, "AmazonS3", [
            _fact("AmazonS3", "storage_gbmo", "GB-Mo", 0.023),
        ])]
        result = CostCalculator().calculate(
            _capacity(), _selection([_component(name, "storage")]),
            priced, "us-east-1")

        # Without retrieved request SKU facts no estimate can be
        # produced, so both classes stay explicitly usage-unknown.
        self.assertEqual(len(result.unpriced), 2)
        by_dimension = {
            entry.dimension: entry for entry in _entries(result, name)
        }
        self.assertEqual(
            sorted(by_dimension),
            ["s3_get_requests", "s3_put_requests", "storage_gbmo"])
        self.assertEqual(by_dimension["storage_gbmo"].status, "priced")
        for dimension in ("s3_get_requests", "s3_put_requests"):
            entry = by_dimension[dimension]
            self.assertEqual(entry.status, "usage_unknown")
            self.assertEqual(entry.disposition, "estimable")
            self.assertIn("assumption", entry.reason.lower())

        self.assertFalse(result.fully_priced)
        self._assert_totals_equal_sum_of_lines(result)

    def test_lambda_duration_is_usage_unknown_with_matching_unpriced(self):
        name = "AWS Lambda"
        priced = [_priced(name, "AWSLambda", [
            _fact("AWSLambda", "requests", "Requests", 0.0000002),
            _fact("AWSLambda", "gb_second", "GB-Second", 0.0000166667),
        ])]
        result = CostCalculator().calculate(
            _capacity(), _selection([_component(name, "compute")]),
            priced, "us-east-1")

        # The declined dimension keeps its explicit unpriced entry.
        self.assertEqual(len(result.unpriced), 1)
        self.assertEqual(result.unpriced[0].component_name, name)

        by_dimension = {
            entry.dimension: entry for entry in _entries(result, name)
        }
        self.assertEqual(by_dimension["requests"].status, "priced")
        duration = by_dimension["gb_second"]
        self.assertEqual(duration.status, "usage_unknown")
        # One declined dimension = one unpriced entry + one coverage row,
        # carrying the identical backend reason.
        self.assertEqual(duration.reason, result.unpriced[0].reason)
        self.assertFalse(result.fully_priced)
        self._assert_totals_equal_sum_of_lines(result)

    def test_fully_mapped_component_stays_fully_priced(self):
        name = "Amazon Simple Queue Service (SQS)"
        priced = [_priced(name, "AWSQueueService", [
            _fact("AWSQueueService", "requests", "Requests", 0.0000004),
        ])]
        result = CostCalculator().calculate(
            _capacity(), _selection([_component(name, "messaging")]),
            priced, "us-east-1")

        self.assertTrue(result.fully_priced)
        self.assertEqual(result.unpriced, [])
        self.assertEqual(
            [(entry.dimension, entry.status) for entry in result.coverage],
            [("requests", "priced")],
        )

    def test_retrieved_fact_replaces_unsupported_claim(self):
        """A retrieved fact always wins: no stale gap claim survives."""
        name = "Amazon DynamoDB"
        priced = [_priced(name, "AmazonDynamoDB", [
            _fact("AmazonDynamoDB", "storage_gbmo", "GB-Mo", 0.25),
        ])]
        result = CostCalculator().calculate(
            _capacity(), _selection([_component(name)]), priced, "us-east-1")

        by_dimension = {
            entry.dimension: entry for entry in _entries(result, name)
        }
        # The storage fact prices through the existing deterministic
        # rule; the priceable disposition never contradicts it.
        self.assertEqual(by_dimension["storage_gbmo"].status, "priced")
        # Every other expected dimension stays explicitly unresolved
        # instead of vanishing with the fact set.
        self.assertEqual(
            [(entry.dimension, entry.status) for entry in result.coverage],
            [
                ("rcu_reserved", "usage_unknown"),
                ("rru", "price_unavailable"),
                ("storage_gbmo", "priced"),
                ("wcu_reserved", "usage_unknown"),
                ("wru", "price_unavailable"),
            ],
        )
        self.assertEqual(len(result.unpriced), 4)
        self.assertFalse(result.fully_priced)
        self._assert_totals_equal_sum_of_lines(result)

    def test_coverage_is_sorted_deterministically(self):
        name = "Amazon DynamoDB"
        priced = [_priced(name, "AmazonDynamoDB", [
            _fact("AmazonDynamoDB", "wru", "WriteRequestUnits", 0.00000125),
        ])]
        result = CostCalculator().calculate(
            _capacity(), _selection([_component(name)]), priced, "us-east-1")
        self.assertEqual(
            result.coverage,
            sorted(
                result.coverage,
                key=lambda entry: (entry.component_name, entry.dimension),
            ),
        )


if __name__ == "__main__":
    unittest.main()
