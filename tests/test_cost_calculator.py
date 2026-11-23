"""Cost calculator tests: deterministic arithmetic, provenance, unpriced paths.

All pricing facts are hand-built fixtures (no network, no LLM).
Run: poetry run python -m unittest discover tests
"""
import unittest
from datetime import datetime

from ai_architect.agents.capacity.result import CapacityModelingResult
from ai_architect.agents.capacity.models import CapacityModelProposal
from ai_architect.agents.component_selection.models import (
    ComponentCandidate,
    ComponentSelectionResult,
)
from ai_architect.domain.cost.models import (
    CostEstimationResult,
    CoverageStatus,
    PricedComponent,
    PriceTier,
    PricingFact,
    PricingModel,
)
from ai_architect.services.cost.calculator import (
    BYTES_PER_GB,
    SECONDS_PER_MONTH,
    CostCalculator,
)


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
    from ai_architect.services.capacity.calculator import CapacityCalculator as Calc
    return CapacityModelingResult(
        proposal=proposal, calculation=Calc().calculate(proposal))


def _sqs_component() -> ComponentCandidate:
    return ComponentCandidate(
        name="Amazon Simple Queue Service (SQS)", provider="aws",
        category="messaging", role="q", rationale="r",
        confidence="high", evidence_support="strong")


def _sqs_fact() -> PricingFact:
    return PricingFact(
        service_code="AWSQueueService", service_name="Amazon Simple Queue Service",
        region_code="ap-south-1", pricing_model=PricingModel.ON_DEMAND,
        usage_key="requests", unit="Requests",
        tiers=[PriceTier(begin=0.0, end=None, unit="Requests",
                         unit_price_usd=0.0000004,
                         description="$0.40 per million",
                         rate_code="r1")],
        source="aws-price-list-api",
        retrieved_at=datetime.utcnow(),
    )


def _selection(comps):
    return ComponentSelectionResult(selected_components=comps)


class CalculatorTests(unittest.TestCase):
    def test_sqs_monthly_arithmetic_and_provenance(self):
        calc = CostCalculator()
        priced = [PricedComponent(
            component_name="Amazon Simple Queue Service (SQS)",
            category="messaging", service_code="AWSQueueService",
            facts=[_sqs_fact()])]
        result = calc.calculate(
            _capacity(), _selection([_sqs_component()]), priced, "ap-south-1")
        self.assertIsInstance(result, CostEstimationResult)
        self.assertEqual(len(result.lines), 1)
        line = result.lines[0]
        # avg QPS = (peak_write + peak_read) / peak_factor.
        # peak_write = 5e6*10/86400*3 = 1736.111..., peak_read = *4.
        avg_total = (1736.1111111111112 * 5) / 3.0
        expected_usage = avg_total * SECONDS_PER_MONTH
        self.assertAlmostEqual(line.monthly_usage, expected_usage, places=1)
        self.assertAlmostEqual(
            line.monthly_cost_usd, expected_usage * 0.0000004, places=2)
        self.assertEqual(line.pricing_model, PricingModel.ON_DEMAND.value)
        self.assertEqual(line.pricing_source, "aws-price-list-api")
        self.assertEqual(line.region_code, "ap-south-1")
        self.assertTrue(line.usage_formula)
        self.assertIn("on_demand", result.totals_usd_per_month)
        self.assertAlmostEqual(
            result.totals_usd_per_month["on_demand"], line.monthly_cost_usd)

    def test_instance_based_component_explicitly_unpriced(self):
        eks = ComponentCandidate(
            name="Amazon Elastic Kubernetes Service (EKS)", provider="aws",
            category="compute", role="q", rationale="r",
            confidence="high", evidence_support="strong")
        priced = [PricedComponent(
            component_name="Amazon Elastic Kubernetes Service (EKS)",
            category="compute", service_code="",
            unavailable_reason="Cannot price: no Price List mapping and "
            "instance type and count are not determined.")]
        result = CostCalculator().calculate(
            _capacity(), _selection([eks]), priced, "ap-south-1")
        self.assertEqual(result.lines, [])
        self.assertEqual(len(result.unpriced), 1)
        self.assertIn("instance type and count",
                      result.unpriced[0].reason)

    def test_unknown_usage_key_never_invented(self):
        fact = _sqs_fact()
        fact.usage_key = "mystery-dimension"
        priced = [PricedComponent(
            component_name="Amazon Simple Queue Service (SQS)",
            category="messaging", service_code="AWSQueueService",
            facts=[fact])]
        result = CostCalculator().calculate(
            _capacity(), _selection([_sqs_component()]), priced, "ap-south-1")
        self.assertEqual(result.lines, [])
        # Two gaps now: the registry-declared 'requests' dimension has no
        # retrieved fact (price unavailable), and the undeclared mystery
        # fact has no derivation rule.
        self.assertEqual(len(result.unpriced), 2)
        self.assertTrue(
            any("mystery-dimension" in u.reason for u in result.unpriced))
        statuses = {entry.dimension: entry.status for entry in result.coverage}
        self.assertEqual(
            statuses["requests"], CoverageStatus.PRICE_UNAVAILABLE)
        self.assertEqual(
            statuses["mystery-dimension"], CoverageStatus.USAGE_UNKNOWN)

    def test_missing_priced_entry_is_unpriced_not_zero(self):
        result = CostCalculator().calculate(
            _capacity(), _selection([_sqs_component()]), [], "ap-south-1")
        self.assertEqual(result.lines, [])
        self.assertEqual(result.totals_usd_per_month, {})
        self.assertEqual(len(result.unpriced), 1)

    def test_reserved_absence_warned_and_on_demand_totaled(self):
        priced = [PricedComponent(
            component_name="Amazon Simple Queue Service (SQS)",
            category="messaging", service_code="AWSQueueService",
            facts=[_sqs_fact()])]
        result = CostCalculator().calculate(
            _capacity(), _selection([_sqs_component()]), priced, "ap-south-1")
        self.assertNotIn("reserved_1yr", result.totals_usd_per_month)
        self.assertNotIn("reserved_3yr", result.totals_usd_per_month)
        self.assertTrue(any("reserved" in w.lower() for w in result.warnings))

    def test_reserved_only_component_unpriced_not_silently_omitted(self):
        reserved = _sqs_fact()
        reserved.pricing_model = PricingModel.RESERVED_1YR
        priced = [PricedComponent(
            component_name="Amazon Simple Queue Service (SQS)",
            category="messaging", service_code="AWSQueueService",
            facts=[reserved])]
        result = CostCalculator().calculate(
            _capacity(), _selection([_sqs_component()]), priced, "ap-south-1")
        self.assertEqual(result.lines, [])
        self.assertEqual(result.totals_usd_per_month, {})
        self.assertEqual(len(result.unpriced), 1)
        reason = result.unpriced[0].reason
        self.assertIn("reserved", reason.lower())
        self.assertIn("no rate is invented", reason.lower())
        self.assertFalse(result.fully_priced)

    def test_reserved_beside_on_demand_baseline_still_priced(self):
        reserved = _sqs_fact()
        reserved.pricing_model = PricingModel.RESERVED_1YR
        priced = [PricedComponent(
            component_name="Amazon Simple Queue Service (SQS)",
            category="messaging", service_code="AWSQueueService",
            facts=[_sqs_fact(), reserved])]
        result = CostCalculator().calculate(
            _capacity(), _selection([_sqs_component()]), priced, "ap-south-1")
        self.assertEqual(len(result.lines), 1)
        self.assertEqual(result.unpriced, [])
        self.assertTrue(result.fully_priced)
        self.assertIn("on_demand", result.totals_usd_per_month)

    def test_component_with_no_facts_unpriced_not_omitted(self):
        priced = [PricedComponent(
            component_name="Amazon Simple Queue Service (SQS)",
            category="messaging", service_code="AWSQueueService",
            facts=[])]
        result = CostCalculator().calculate(
            _capacity(), _selection([_sqs_component()]), priced, "ap-south-1")
        self.assertEqual(result.lines, [])
        self.assertEqual(len(result.unpriced), 1)
        self.assertIn(
            "No pricing facts were retrieved", result.unpriced[0].reason)
        self.assertFalse(result.fully_priced)


def _transfer_component() -> ComponentCandidate:
    return ComponentCandidate(
        name="Amazon CloudFront", provider="aws",
        category="cdn", role="edge cache + transfer", rationale="r",
        confidence="high", evidence_support="strong")


def _transfer_fact() -> PricingFact:
    return PricingFact(
        service_code="AmazonCloudFront", service_name="Amazon CloudFront",
        region_code="ap-south-1", pricing_model=PricingModel.ON_DEMAND,
        usage_key="transfer_gb", unit="GB",
        tiers=[PriceTier(begin=0.0, end=None, unit="GB",
                         unit_price_usd=0.085,
                         description="$0.085 per GB",
                         rate_code="cf-t")],
        source="aws-price-list-api",
        retrieved_at=datetime.utcnow(),
    )


def _apigw_component() -> ComponentCandidate:
    return ComponentCandidate(
        name="Amazon API Gateway", provider="aws",
        category="networking", role="origin", rationale="r",
        confidence="high", evidence_support="strong")


def _apigw_fact() -> PricingFact:
    return PricingFact(
        service_code="AmazonApiGateway", service_name="Amazon API Gateway",
        region_code="ap-south-1", pricing_model=PricingModel.ON_DEMAND,
        usage_key="rest_requests", unit="Requests",
        tiers=[PriceTier(begin=0.0, end=None, unit="Requests",
                         unit_price_usd=0.0000035,
                         description="$3.50 per million",
                         rate_code="agw-r")],
        source="aws-price-list-api",
        retrieved_at=datetime.utcnow(),
    )


def _ddb_component() -> ComponentCandidate:
    return ComponentCandidate(
        name="Amazon DynamoDB", provider="aws",
        category="database", role="kv", rationale="r",
        confidence="high", evidence_support="strong")


def _ddb_wru_fact() -> PricingFact:
    return PricingFact(
        service_code="AmazonDynamoDB", service_name="Amazon DynamoDB",
        region_code="ap-south-1", pricing_model=PricingModel.ON_DEMAND,
        usage_key="wru", unit="WriteRequestUnits",
        tiers=[PriceTier(begin=0.0, end=None, unit="WriteRequestUnits",
                         unit_price_usd=0.0000006250,
                         description="$0.625 per million WRU",
                         rate_code="ddb-w")],
        source="aws-price-list-api",
        retrieved_at=datetime.utcnow(),
    )


def _ddb_rru_fact() -> PricingFact:
    return PricingFact(
        service_code="AmazonDynamoDB", service_name="Amazon DynamoDB",
        region_code="ap-south-1", pricing_model=PricingModel.ON_DEMAND,
        usage_key="rru", unit="ReadRequestUnits",
        tiers=[PriceTier(begin=0.0, end=None, unit="ReadRequestUnits",
                         unit_price_usd=0.0000001250,
                         description="$0.125 per million RRU",
                         rate_code="ddb-r")],
        source="aws-price-list-api",
        retrieved_at=datetime.utcnow(),
    )


class EgressAvgFixRegressionTests(unittest.TestCase):
    """Slice: peak-derived egress -> average-derived egress + disclosure."""

    def test_transfer_uses_average_egress_exactly_one_third(self):
        capacity = _capacity()
        self.assertEqual(capacity.proposal.peak_factor, 3.0)
        priced = [PricedComponent(
            component_name="Amazon CloudFront",
            category="cdn", service_code="AmazonCloudFront",
            facts=[_transfer_fact()])]
        result = CostCalculator().calculate(
            capacity, _selection([_transfer_component()]), priced, "ap-south-1")
        self.assertEqual(len(result.lines), 1)
        line = result.lines[0]
        peak_egress = capacity.calculation.egress_bytes_per_second
        old_peak_monthly = peak_egress * SECONDS_PER_MONTH / BYTES_PER_GB
        expected_avg_monthly = old_peak_monthly / 3.0
        self.assertAlmostEqual(line.monthly_usage, expected_avg_monthly, places=4)
        self.assertAlmostEqual(line.monthly_usage * 3.0, old_peak_monthly, places=4)
        self.assertAlmostEqual(
            line.monthly_cost_usd, expected_avg_monthly * 0.085, places=2)
        self.assertIn("peak_factor", line.usage_formula)
        self.assertIn("avg B/s", line.usage_formula)
        self.assertIn("peak B/s", line.usage_formula)
        self.assertEqual(line.pricing_source, "aws-price-list-api")

    def test_request_volume_numerically_unchanged_by_egress_fix(self):
        capacity = _capacity()
        sqs_only = CostCalculator().calculate(
            capacity, _selection([_sqs_component()]),
            [PricedComponent(
                component_name="Amazon Simple Queue Service (SQS)",
                category="messaging", service_code="AWSQueueService",
                facts=[_sqs_fact()])],
            "ap-south-1")
        combined = CostCalculator().calculate(
            capacity,
            _selection([_sqs_component(), _transfer_component()]),
            [PricedComponent(
                component_name="Amazon Simple Queue Service (SQS)",
                category="messaging", service_code="AWSQueueService",
                facts=[_sqs_fact()]),
             PricedComponent(
                component_name="Amazon CloudFront",
                category="cdn", service_code="AmazonCloudFront",
                facts=[_transfer_fact()])],
            "ap-south-1")
        sqs_line = next(
            line for line in combined.lines
            if line.component_name == "Amazon Simple Queue Service (SQS)")
        self.assertAlmostEqual(
            sqs_line.monthly_usage, sqs_only.lines[0].monthly_usage, places=6)
        self.assertAlmostEqual(
            sqs_line.monthly_cost_usd,
            sqs_only.lines[0].monthly_cost_usd, places=6)
        # avg QPS derivation unchanged: (peak_write + peak_read)/3 * seconds.
        avg_total = (1736.1111111111112 * 5) / 3.0
        self.assertAlmostEqual(
            sqs_line.monthly_usage, avg_total * SECONDS_PER_MONTH, places=1)

    def test_wru_rru_unchanged_by_egress_fix(self):
        capacity = _capacity()
        priced = [PricedComponent(
            component_name="Amazon DynamoDB",
            category="database", service_code="AmazonDynamoDB",
            facts=[_ddb_wru_fact(), _ddb_rru_fact()])]
        result = CostCalculator().calculate(
            capacity, _selection([_ddb_component()]), priced, "ap-south-1")
        by_unit = {line.unit: line for line in result.lines}
        self.assertEqual(len(result.lines), 2)
        # average_message_size_bytes=1024 -> 1 WRU and 1 RRU per request.
        # Derive directly from capacity to avoid hardcoding drift:
        calc = capacity.calculation
        exp_wru = (calc.peak_write_qps / 3.0) * SECONDS_PER_MONTH
        exp_rru = (calc.peak_read_qps / 3.0) * SECONDS_PER_MONTH
        self.assertAlmostEqual(
            by_unit["WriteRequestUnits"].monthly_usage, exp_wru, places=1)
        self.assertAlmostEqual(
            by_unit["ReadRequestUnits"].monthly_usage, exp_rru, places=1)
        self.assertAlmostEqual(
            by_unit["WriteRequestUnits"].unit_price_usd, 0.0000006250)
        self.assertAlmostEqual(
            by_unit["ReadRequestUnits"].unit_price_usd, 0.0000001250)
        self.assertAlmostEqual(
            by_unit["WriteRequestUnits"].monthly_cost_usd, exp_wru * 0.0000006250,
            places=2)
        self.assertAlmostEqual(
            by_unit["ReadRequestUnits"].monthly_cost_usd, exp_rru * 0.0000001250,
            places=2)

    def test_shared_volume_warning_for_two_request_components(self):
        capacity = _capacity()
        result = CostCalculator().calculate(
            capacity,
            _selection([_sqs_component(), _apigw_component()]),
            [PricedComponent(
                component_name="Amazon Simple Queue Service (SQS)",
                category="messaging", service_code="AWSQueueService",
                facts=[_sqs_fact()]),
             PricedComponent(
                component_name="Amazon API Gateway",
                category="networking", service_code="AmazonApiGateway",
                facts=[_apigw_fact()])],
            "ap-south-1")
        self.assertEqual(len(result.lines), 2)
        # Disclosure only: identical monthly volumes across layers.
        self.assertAlmostEqual(
            result.lines[0].monthly_usage, result.lines[1].monthly_usage,
            places=6)
        shared = [w for w in result.warnings if "Shared request-volume" in w]
        self.assertEqual(len(shared), 1)
        self.assertIn("Amazon Simple Queue Service (SQS)", shared[0])
        self.assertIn("Amazon API Gateway", shared[0])
        self.assertIn("Cache-hit", shared[0])
        self.assertIn("origin-forward", shared[0])
        self.assertIn("fan-out", shared[0])

    def test_shared_volume_warning_absent_for_single_component(self):
        capacity = _capacity()
        result = CostCalculator().calculate(
            capacity, _selection([_sqs_component()]),
            [PricedComponent(
                component_name="Amazon Simple Queue Service (SQS)",
                category="messaging", service_code="AWSQueueService",
                facts=[_sqs_fact()])],
            "ap-south-1")
        self.assertFalse(
            any("Shared request-volume" in w for w in result.warnings))

    def test_transfer_plus_single_request_does_not_trigger_shared_warning(self):
        capacity = _capacity()
        result = CostCalculator().calculate(
            capacity,
            _selection([_sqs_component(), _transfer_component()]),
            [PricedComponent(
                component_name="Amazon Simple Queue Service (SQS)",
                category="messaging", service_code="AWSQueueService",
                facts=[_sqs_fact()]),
             PricedComponent(
                 component_name="Amazon CloudFront",
                 category="cdn", service_code="AmazonCloudFront",
                 facts=[_transfer_fact()])],
            "ap-south-1")
        self.assertEqual(len(result.lines), 2)
        self.assertFalse(
            any("Shared request-volume" in w for w in result.warnings))


class LambdaPricingTests(unittest.TestCase):
    """Lambda request pricing is derivable from capacity; duration (GB-second)
    is not derivable from existing workload artifacts and stays unpriced."""

    def _lambda_component(self) -> ComponentCandidate:
        return ComponentCandidate(
            name="AWS Lambda", provider="aws",
            category="compute", role="serverless backend logic",
            rationale="r", confidence="high", evidence_support="strong")

    def _lambda_requests_fact(self) -> PricingFact:
        return PricingFact(
            service_code="AWSLambda", service_name="AWS Lambda",
            region_code="us-east-1", pricing_model=PricingModel.ON_DEMAND,
            usage_key="requests", unit="Requests",
            tiers=[PriceTier(begin=0.0, end=None, unit="Requests",
                             unit_price_usd=0.0000002,
                             description="$0.20 per million",
                             rate_code="lam-r")],
            source="aws-price-list-api",
            retrieved_at=datetime.utcnow(),
        )

    def _lambda_gb_second_fact(self) -> PricingFact:
        return PricingFact(
            service_code="AWSLambda", service_name="AWS Lambda",
            region_code="us-east-1", pricing_model=PricingModel.ON_DEMAND,
            usage_key="gb_second", unit="GB-second",
            tiers=[PriceTier(begin=0.0, end=None, unit="GB-second",
                             unit_price_usd=0.0000166667,
                             description="$0.0000166667 per GB-second",
                             rate_code="lam-gbs")],
            source="aws-price-list-api",
            retrieved_at=datetime.utcnow(),
        )

    def test_lambda_requests_priced_from_capacity(self):
        capacity = _capacity()
        priced = [PricedComponent(
            component_name="AWS Lambda", category="compute",
            service_code="AWSLambda",
            facts=[self._lambda_requests_fact()])]
        result = CostCalculator().calculate(
            capacity, _selection([self._lambda_component()]), priced,
            "us-east-1")
        self.assertEqual(len(result.lines), 1)
        line = result.lines[0]
        self.assertEqual(line.component_name, "AWS Lambda")
        self.assertEqual(line.unit, "Requests")
        self.assertEqual(line.pricing_model, PricingModel.ON_DEMAND)
        # avg total QPS * seconds per month
        avg_total = (
            capacity.calculation.peak_write_qps
            + capacity.calculation.peak_read_qps
        ) / capacity.proposal.peak_factor
        expected_usage = avg_total * SECONDS_PER_MONTH
        self.assertAlmostEqual(line.monthly_usage, expected_usage, places=1)
        self.assertAlmostEqual(
            line.monthly_cost_usd, expected_usage * 0.0000002, places=6)
        self.assertIn("on_demand", result.totals_usd_per_month)

    def test_lambda_gb_second_unpriced_no_duration_input(self):
        """Duration (GB-second) cannot be derived from capacity outputs:
        memory allocation and per-invocation duration are unknown. The
        registry also discloses the missing 'requests' price fact."""
        capacity = _capacity()
        priced = [PricedComponent(
            component_name="AWS Lambda", category="compute",
            service_code="AWSLambda",
            facts=[self._lambda_gb_second_fact()])]
        result = CostCalculator().calculate(
            capacity, _selection([self._lambda_component()]), priced,
            "us-east-1")
        self.assertEqual(result.lines, [])
        self.assertEqual(len(result.unpriced), 2)
        gb_second_entry = next(
            u for u in result.unpriced if "gb_second" in u.reason)
        self.assertIn("cannot be deterministically derived",
                      gb_second_entry.reason)
        self.assertEqual(result.totals_usd_per_month, {})

    def test_fully_priced_when_all_components_priced(self):
        capacity = _capacity()
        priced = [PricedComponent(
            component_name="Amazon Simple Queue Service (SQS)",
            category="messaging", service_code="AWSQueueService",
            facts=[_sqs_fact()])]
        result = CostCalculator().calculate(
            capacity, _selection([_sqs_component()]), priced, "ap-south-1")
        self.assertTrue(result.fully_priced)
        self.assertEqual(result.unpriced, [])
        self.assertEqual(len(result.lines), 1)

    def test_not_fully_priced_when_any_component_unpriced(self):
        capacity = _capacity()
        priced = [PricedComponent(
            component_name="AWS Lambda", category="compute",
            service_code="AWSLambda",
            facts=[self._lambda_gb_second_fact()])]
        result = CostCalculator().calculate(
            capacity, _selection([self._lambda_component()]), priced,
            "us-east-1")
        self.assertFalse(result.fully_priced)
        # gb_second (estimable) + requests (price unavailable).
        self.assertEqual(len(result.unpriced), 2)
        self.assertTrue(all(
            u.component_name == "AWS Lambda" for u in result.unpriced))

    def test_missing_pricing_is_not_zero(self):
        """Unpriced components never contribute to totals."""
        capacity = _capacity()
        priced = [PricedComponent(
            component_name="AWS Lambda", category="compute",
            service_code="AWSLambda",
            unavailable_reason="Cannot price: no Price List mapping")]
        result = CostCalculator().calculate(
            capacity, _selection([self._lambda_component()]), priced,
            "us-east-1")
        self.assertEqual(result.lines, [])
        self.assertEqual(result.totals_usd_per_month, {})
        self.assertFalse(result.fully_priced)


if __name__ == "__main__":
    unittest.main()
