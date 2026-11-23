"""URL-shortener pricing audit lock (session ca1af25e, 10M URLs / 5k peak RPS).

Replays the audited run's deterministic inputs through CostCalculator
with hand-recorded live price facts (no network, no LLM) and pins the
audit conclusions:

- API Gateway (HTTP variant): 5.418B requests via marginal tiering =
  $4,906.20. Usage is AVERAGE QPS sustained, i.e. peak sum divided by
  the peak factor -- not sustained peak.
- DynamoDB WRU + RRU are distinct usage dimensions, not duplication.
- Reserved usage keys without a derivation rule become explicit
  unpriced entries, never estimates.
- Displayed total equals the priced subtotal; unpriced entries never
  enter it; priced/unpriced counts are internally consistent.

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
from ai_architect.services.capacity.calculator import (
    CapacityCalculator as CapCalc,
)
from ai_architect.services.cost.calculator import (
    SECONDS_PER_MONTH,
    CostCalculator,
)

REGION = "us-east-1"

# Recorded live HTTP-API tiers (Price List API, us-east-1):
# first 300M requests @ $1.00/M, remainder @ $0.90/M.
HTTP_TIERS = [
    PriceTier(begin=0.0, end=300_000_000.0, unit="Requests",
              unit_price_usd=0.000001),
    PriceTier(begin=300_000_000.0, end=None, unit="Requests",
              unit_price_usd=9e-07),
]


def _capacity() -> CapacityModelingResult:
    """The audited run's capacity proposal, recomputed deterministically."""
    proposal = CapacityModelProposal(
        daily_active_users=1000000,
        messages_per_user_per_day=8.6,
        peak_factor=3.0,
        read_write_ratio=20.0,
        average_message_size_bytes=256,
        connection_factor=0.1,
        storage_retention_days=1825,
        rationale={},
    )
    return CapacityModelingResult(
        proposal=proposal, calculation=CapCalc().calculate(proposal))


def _fact(usage_key: str, unit: str, tiers: list) -> PricingFact:
    return PricingFact(
        service_code="test", region_code=REGION,
        pricing_model=PricingModel.ON_DEMAND, usage_key=usage_key,
        unit=unit, tiers=tiers, source="aws-price-list-api",
        retrieved_at=datetime.utcnow(),
    )


def _candidate(name: str, category: str = "other") -> ComponentCandidate:
    return ComponentCandidate(
        name=name, provider="aws", category=category, role="r",
        rationale="r", confidence="high", evidence_support="strong")


def _selection() -> ComponentSelectionResult:
    return ComponentSelectionResult(selected_components=[
        _candidate("Amazon API Gateway", "networking"),
        _candidate("Amazon DynamoDB", "database"),
        _candidate("Amazon S3 (static website hosting)", "storage"),
        _candidate("AWS Lambda", "compute"),
        _candidate("Amazon CloudWatch", "monitoring"),
    ])


def _priced() -> list[PricedComponent]:
    wru = _fact("wru", "WriteRequestUnits", [PriceTier(
        begin=0.0, end=None, unit="WriteRequestUnits",
        unit_price_usd=6.25e-07)])
    rru = _fact("rru", "ReadRequestUnits", [PriceTier(
        begin=0.0, end=None, unit="ReadRequestUnits",
        unit_price_usd=1.25e-07)])
    reserved_w = _fact("wcu_reserved", "WriteCapacityUnits", [PriceTier(
        begin=0.0, end=None, unit="WriteCapacityUnits",
        unit_price_usd=0.0)])
    reserved_r = _fact("rcu_reserved", "ReadCapacityUnits", [PriceTier(
        begin=0.0, end=None, unit="ReadCapacityUnits",
        unit_price_usd=0.0)])
    storage = _fact("storage_gbmo", "GB-Mo", [PriceTier(
        begin=0.0, end=None, unit="GB-Mo", unit_price_usd=0.023)])
    return [
        PricedComponent(
            component_name="Amazon API Gateway", category="networking",
            service_code="AmazonApiGateway",
            facts=[_fact("http_requests", "Requests", HTTP_TIERS)]),
        PricedComponent(
            component_name="Amazon DynamoDB", category="database",
            service_code="AmazonDynamoDB",
            facts=[wru, rru, reserved_w, reserved_r]),
        PricedComponent(
            component_name="Amazon S3 (static website hosting)",
            category="storage", service_code="AmazonS3",
            facts=[storage]),
        PricedComponent(
            component_name="AWS Lambda", category="compute",
            unavailable_reason=(
                "Cannot price: no Price List mapping and instance type "
                "and count are not determined.")),
        PricedComponent(
            component_name="Amazon CloudWatch", category="monitoring",
            unavailable_reason=(
                "Cannot price: no Price List mapping and metric and log "
                "volumes are not determined.")),
    ]


class UrlShortenerPricingAuditTests(unittest.TestCase):
    def test_capacity_matches_audited_run(self):
        calc = _capacity().calculation
        self.assertAlmostEqual(calc.peak_write_qps, 298.611111, places=3)
        self.assertAlmostEqual(calc.peak_read_qps, 5972.222222, places=3)

    def test_api_gateway_uses_average_not_peak(self):
        result = CostCalculator().calculate(
            _capacity(), _selection(), _priced(), REGION)
        line = next(
            ln for ln in result.lines
            if ln.component_name == "Amazon API Gateway")
        # (298.611 + 5972.222) / 3.0 avg QPS sustained for the month.
        avg_total = (298.6111111111111 + 5972.222222222222) / 3.0
        self.assertAlmostEqual(
            line.monthly_usage, avg_total * SECONDS_PER_MONTH, delta=1.0)
        self.assertLess(line.monthly_usage, 6_000_000_000.0)
        # Marginal tiering: 300M @ $1.00/M + remainder @ $0.90/M.
        self.assertAlmostEqual(line.monthly_cost_usd, 4906.20, places=2)
        self.assertIn("applicable tiers", line.usage_formula)

    def test_dynamodb_write_and_read_are_distinct_dimensions(self):
        result = CostCalculator().calculate(
            _capacity(), _selection(), _priced(), REGION)
        dims = {ln.unit: ln for ln in result.lines
                if ln.component_name == "Amazon DynamoDB"}
        self.assertEqual(
            set(dims), {"WriteRequestUnits", "ReadRequestUnits"})
        self.assertAlmostEqual(
            dims["WriteRequestUnits"].monthly_cost_usd, 161.25, places=2)
        self.assertAlmostEqual(
            dims["ReadRequestUnits"].monthly_cost_usd, 645.00, places=2)

    def test_total_is_priced_subtotal_and_counts_agree(self):
        result = CostCalculator().calculate(
            _capacity(), _selection(), _priced(), REGION)
        self.assertEqual(len(result.lines), 4)
        self.assertAlmostEqual(
            result.totals_usd_per_month["on_demand"], 5713.969104,
            places=4)
        self.assertAlmostEqual(
            result.totals_usd_per_month["on_demand"],
            sum(ln.monthly_cost_usd for ln in result.lines),
            places=6)
        # Unpriced entries never enter the total.
        unpriced_names = [u.component_name for u in result.unpriced]
        self.assertIn("AWS Lambda", unpriced_names)
        self.assertIn("Amazon CloudWatch", unpriced_names)
        ddb_gaps = [u for u in result.unpriced
                    if u.component_name == "Amazon DynamoDB"]
        # Two reserved-capacity usage keys have no derivation rule; the
        # storage dimension is derivable but no storage price fact was
        # retrieved in this fixture. Both stay explicit, never estimated.
        self.assertEqual(len(ddb_gaps), 3)
        rule_gaps = [
            entry for entry in ddb_gaps
            if "No usage derivation rule" in entry.reason
        ]
        price_gaps = [
            entry for entry in ddb_gaps
            if "No price fact was retrieved" in entry.reason
        ]
        self.assertEqual(len(rule_gaps), 2)
        self.assertEqual(len(price_gaps), 1)
        # Displayed total covers priced lines only.
        priced_names = {ln.component_name for ln in result.lines}
        self.assertTrue(priced_names.isdisjoint(
            {"AWS Lambda", "Amazon CloudWatch"}))


if __name__ == "__main__":
    unittest.main()
