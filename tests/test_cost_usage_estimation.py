import unittest
from unittest.mock import patch

from ai_architect.agents.capacity.result import CapacityModelingResult
from ai_architect.agents.capacity.models import CapacityModelProposal
from ai_architect.domain.capacity.models import CapacityModelResult
from ai_architect.agents.component_selection.models import (
    ComponentSelectionResult,
    ComponentCandidate,
)
from ai_architect.domain.cost.models import (
    CostEstimationResult,
    UsageEstimate,
    EstimationConfidence,
    CostCompleteness,
    CoverageStatus,
    UsageProvenance,
)
from ai_architect.services.cost.calculator import CostCalculator
from ai_architect.services.cost.estimation_rules import (
    monthly_request_count,
    deterministic_estimate,
    check_gb_second_consistency,
    validate_factor_scenario,
    derive_s3_request_quantity,
    check_s3_request_consistency,
    derive_log_data_ingested_gb,
    derive_log_storage_gbmo,
    check_log_ingestion_consistency,
    check_log_storage_consistency,
)
from ai_architect.services.cost.usage_estimator import (
    UsageEstimator,
    discover_estimation_jobs,
)
from ai_architect.domain.cost.models import PricedComponent, PricingFact, PricingModel
from datetime import datetime


def _capacity_result():
    proposal = CapacityModelProposal(
        daily_active_users=1000,
        messages_per_user_per_day=1.0,
        peak_factor=2.0,
        read_write_ratio=2.0,
        average_message_size_bytes=1024,
        connection_factor=0.1,
        storage_retention_days=30,
        rationale={},
    )
    calc = CapacityModelResult(
        peak_write_qps=10.0,
        peak_read_qps=20.0,
        concurrent_connections=1.0,
        storage_growth_bytes_per_day=100.0,
        hot_working_set_bytes=100.0,
        egress_bytes_per_second=50.0,
        formula_version="v1",
        calculation_trace={},
    )
    return CapacityModelingResult(proposal=proposal, calculation=calc)


def _selection():
    return ComponentSelectionResult(
        selected_components=[
            ComponentCandidate(name="Compute Function", category="compute", role="lambda", provider="aws", rationale="r", evidence_refs=[], assumptions=[], tradeoffs=[], confidence="high", evidence_support="strong", provider_specific=True),
            ComponentCandidate(name="Ledger Store", category="database", role="dynamodb", provider="aws", rationale="r", evidence_refs=[], assumptions=[], tradeoffs=[], confidence="high", evidence_support="strong", provider_specific=True),
        ],
        rejected_candidates=[],
        unresolved_tradeoffs=[],
    )


def _selection_lambda_only():
    return ComponentSelectionResult(
        selected_components=[
            ComponentCandidate(name="Compute Function", category="compute", role="lambda", provider="aws", rationale="r", evidence_refs=[], assumptions=[], tradeoffs=[], confidence="high", evidence_support="strong", provider_specific=True),
        ],
        rejected_candidates=[],
        unresolved_tradeoffs=[],
    )


def _selection_lambda_and_logs():
    return ComponentSelectionResult(
        selected_components=[
            ComponentCandidate(name="Compute Function", category="compute", role="lambda", provider="aws", rationale="r", evidence_refs=[], assumptions=[], tradeoffs=[], confidence="high", evidence_support="strong", provider_specific=True),
            ComponentCandidate(name="Log Store", category="monitoring", role="cloudwatch logs", provider="aws", rationale="r", evidence_refs=[], assumptions=[], tradeoffs=[], confidence="high", evidence_support="strong", provider_specific=True),
        ],
        rejected_candidates=[],
        unresolved_tradeoffs=[],
    )


def _selection_s3():
    return ComponentSelectionResult(
        selected_components=[
            ComponentCandidate(name="Object Store", category="storage", role="s3", provider="aws", rationale="r", evidence_refs=[], assumptions=[], tradeoffs=[], confidence="high", evidence_support="strong", provider_specific=True),
        ],
        rejected_candidates=[],
        unresolved_tradeoffs=[],
    )


def _selection_logs_only():
    return ComponentSelectionResult(
        selected_components=[
            ComponentCandidate(name="Log Store", category="monitoring", role="cloudwatch logs", provider="aws", rationale="r", evidence_refs=[], assumptions=[], tradeoffs=[], confidence="high", evidence_support="strong", provider_specific=True),
        ],
        rejected_candidates=[],
        unresolved_tradeoffs=[],
    )


def _priced_lambda():
    fact = PricingFact(
        service_code="AWSLambda",
        region_code="us-east-1",
        pricing_model=PricingModel.ON_DEMAND,
        usage_key="gb_second",
        unit="GB-Seconds",
        tiers=[],
        source="aws-price-list-api",
        retrieved_at=datetime.utcnow(),
    )
    # add a priced tier
    from ai_architect.domain.cost.models import PriceTier
    fact.tiers.append(PriceTier(begin=0, end=None, unit="GB-Seconds", unit_price_usd=0.0000166667))
    fact2 = PricingFact(
        service_code="AWSLambda",
        region_code="us-east-1",
        pricing_model=PricingModel.ON_DEMAND,
        usage_key="requests",
        unit="Requests",
        tiers=[],
        source="aws-price-list-api",
        retrieved_at=datetime.utcnow(),
    )
    from ai_architect.domain.cost.models import PriceTier
    fact2.tiers.append(PriceTier(begin=0, end=None, unit="Requests", unit_price_usd=0.0000002))
    return PricedComponent(
        component_name="Compute Function",
        category="compute",
        service_code="AWSLambda",
        facts=[fact, fact2],
    )


def _priced_ddb():
    fact_wcu = PricingFact(
        service_code="AmazonDynamoDB",
        region_code="us-east-1",
        pricing_model=PricingModel.ON_DEMAND,
        usage_key="wcu_reserved",
        unit="WCU-Hrs",
        tiers=[],
        source="aws-price-list-api",
        retrieved_at=datetime.utcnow(),
    )
    from ai_architect.domain.cost.models import PriceTier
    fact_wcu.tiers.append(PriceTier(begin=0, end=None, unit="WCU-Hrs", unit_price_usd=0.00065))
    fact_rcu = PricingFact(
        service_code="AmazonDynamoDB",
        region_code="us-east-1",
        pricing_model=PricingModel.ON_DEMAND,
        usage_key="rcu_reserved",
        unit="RCU-Hrs",
        tiers=[],
        source="aws-price-list-api",
        retrieved_at=datetime.utcnow(),
    )
    fact_rcu.tiers.append(PriceTier(begin=0, end=None, unit="RCU-Hrs", unit_price_usd=0.0001625))
    fact_storage = PricingFact(
        service_code="AmazonDynamoDB",
        region_code="us-east-1",
        pricing_model=PricingModel.ON_DEMAND,
        usage_key="storage_gbmo",
        unit="GB-Mo",
        tiers=[],
        source="aws-price-list-api",
        retrieved_at=datetime.utcnow(),
    )
    fact_storage.tiers.append(PriceTier(begin=0, end=None, unit="GB-Mo", unit_price_usd=0.25))
    return PricedComponent(
        component_name="Ledger Store",
        category="database",
        service_code="AmazonDynamoDB",
        facts=[fact_wcu, fact_rcu, fact_storage],
        resolved_billing_model="provisioned",
    )


def _priced_cloudwatch():
    from ai_architect.domain.cost.models import PriceTier
    ingested = PricingFact(
        service_code="AmazonCloudWatch",
        region_code="us-east-1",
        pricing_model=PricingModel.ON_DEMAND,
        usage_key="log_data_ingested_gb",
        unit="GB",
        tiers=[],
        source="aws-price-list-api",
        retrieved_at=datetime.utcnow(),
    )
    ingested.tiers.append(PriceTier(begin=0, end=None, unit="GB", unit_price_usd=0.50))
    stored = PricingFact(
        service_code="AmazonCloudWatch",
        region_code="us-east-1",
        pricing_model=PricingModel.ON_DEMAND,
        usage_key="log_storage_gbmo",
        unit="GB-Mo",
        tiers=[],
        source="aws-price-list-api",
        retrieved_at=datetime.utcnow(),
    )
    stored.tiers.append(PriceTier(begin=0, end=None, unit="GB-Mo", unit_price_usd=0.03))
    return PricedComponent(
        component_name="Log Store",
        category="monitoring",
        service_code="AmazonCloudWatch",
        facts=[ingested, stored],
    )


def _priced_s3():
    from ai_architect.domain.cost.models import PriceTier
    storage = PricingFact(
        service_code="AmazonS3",
        region_code="us-east-1",
        pricing_model=PricingModel.ON_DEMAND,
        usage_key="storage_gbmo",
        unit="GB-Mo",
        tiers=[PriceTier(begin=0, end=None, unit="GB-Mo", unit_price_usd=0.023)],
        source="aws-price-list-api",
        retrieved_at=datetime.utcnow(),
    )
    put = PricingFact(
        service_code="AmazonS3",
        region_code="us-east-1",
        pricing_model=PricingModel.ON_DEMAND,
        usage_key="s3_put_requests",
        unit="Requests",
        tiers=[PriceTier(begin=0, end=None, unit="Requests", unit_price_usd=0.000005)],
        source="aws-price-list-api",
        retrieved_at=datetime.utcnow(),
    )
    get = PricingFact(
        service_code="AmazonS3",
        region_code="us-east-1",
        pricing_model=PricingModel.ON_DEMAND,
        usage_key="s3_get_requests",
        unit="Requests",
        tiers=[PriceTier(begin=0, end=None, unit="Requests", unit_price_usd=0.0000004)],
        source="aws-price-list-api",
        retrieved_at=datetime.utcnow(),
    )
    return PricedComponent(
        component_name="Object Store",
        category="storage",
        service_code="AmazonS3",
        facts=[storage, put, get],
    )


class TestEstimationRules(unittest.TestCase):
    def test_monthly_request_count(self):
        cr = _capacity_result()
        mr = monthly_request_count(cr)
        self.assertGreater(mr, 0)

    def test_deterministic_wcu(self):
        cr = _capacity_result()
        det = deterministic_estimate("wcu_reserved", cr)
        self.assertIsNotNone(det)
        self.assertEqual(det.low_quantity, det.typical_quantity)
        self.assertEqual(det.high_quantity, det.typical_quantity)

    def test_gb_second_consistency_pass(self):
        mr = 1000.0
        check_gb_second_consistency(mr, 300.0, 1024.0, mr * 0.3 * 1.0, "typical")

    def test_gb_second_consistency_fail(self):
        mr = 1000.0
        with self.assertRaises(ValueError):
            check_gb_second_consistency(mr, 300.0, 1024.0, 999999.0, "typical")

    def test_validate_factor_scenario_bounds(self):
        specs = None
        from ai_architect.services.cost.estimation_rules import FACTOR_SPECS_BY_DIMENSION
        specs = FACTOR_SPECS_BY_DIMENSION["gb_second"]
        # Need FactorSpec tuple; pass via validate_factor_scenario
        from ai_architect.services.cost.estimation_rules import validate_factor_scenario
        validate_factor_scenario("gb_second", {"average_invocation_duration_ms": 300.0, "memory_mb": 1024.0}, "typical")

    def test_s3_request_split_derivation(self):
        mr = 1000.0
        self.assertAlmostEqual(
            derive_s3_request_quantity(mr, 2.0, 0.25), 500.0)
        self.assertAlmostEqual(
            derive_s3_request_quantity(mr, 2.0, 0.75), 1500.0)
        check_s3_request_consistency(
            "s3_put_requests", mr, 2.0, 0.25, 500.0, "typical")
        check_s3_request_consistency(
            "s3_get_requests", mr, 2.0, 0.25, 1500.0, "typical")
        # Tier1 and Tier2 use opposite shares: a declared 500 GETs
        # under a 0.25 Tier1 share is inconsistent.
        with self.assertRaises(ValueError):
            check_s3_request_consistency(
                "s3_get_requests", mr, 2.0, 0.25, 500.0, "typical")

    def test_log_ingestion_and_storage_derivation(self):
        mr = 1_000_000.0
        ingestion = derive_log_data_ingested_gb(mr, 512.0)
        self.assertAlmostEqual(ingestion, 0.512)
        self.assertAlmostEqual(
            derive_log_storage_gbmo(mr, 512.0, 30.0), 0.512)
        self.assertAlmostEqual(
            derive_log_storage_gbmo(mr, 512.0, 60.0), 1.024)
        check_log_ingestion_consistency(
            mr, 512.0, ingestion, "typical")
        check_log_storage_consistency(
            mr, 512.0, 30.0, 0.512, "typical")
        with self.assertRaises(ValueError):
            check_log_ingestion_consistency(
                mr, 512.0, 2.0, "typical")
        with self.assertRaises(ValueError):
            check_log_storage_consistency(
                mr, 512.0, 60.0, 3.0, "typical")

    def test_validate_factor_scenario_out_of_bounds(self):
        from ai_architect.services.cost.estimation_rules import validate_factor_scenario
        with self.assertRaises(ValueError):
            validate_factor_scenario("gb_second", {"average_invocation_duration_ms": 0.0, "memory_mb": 1024.0}, "typical")


class TestJobDiscovery(unittest.TestCase):
    def test_discovers_jobs(self):
        cr = _capacity_result()
        sel = _selection()
        priced = [_priced_lambda(), _priced_ddb()]
        jobs, warnings = discover_estimation_jobs(cr, sel, priced)
        keys = {(j.component_name, j.dimension) for j in jobs}
        self.assertIn(("Compute Function", "gb_second"), keys)
        self.assertIn(("Ledger Store", "wcu_reserved"), keys)
        self.assertIn(("Ledger Store", "rcu_reserved"), keys)
        self.assertNotIn(("Ledger Store", "storage_gbmo"), keys)

    def test_discovers_s3_request_jobs_when_facts_exist(self):
        cr = _capacity_result()
        jobs, warnings = discover_estimation_jobs(
            cr, _selection_s3(), [_priced_s3()])
        self.assertEqual(warnings, [])
        keys = {(j.component_name, j.dimension) for j in jobs}
        self.assertEqual(
            keys,
            {
                ("Object Store", "s3_put_requests"),
                ("Object Store", "s3_get_requests"),
            },
        )
        for job in jobs:
            self.assertEqual(job.known_requests, monthly_request_count(cr))
            self.assertTrue(job.factor_specs)

    def test_discovers_cloudwatch_log_jobs_when_facts_exist(self):
        cr = _capacity_result()
        jobs, warnings = discover_estimation_jobs(
            cr, _selection_logs_only(), [_priced_cloudwatch()])
        self.assertEqual(warnings, [])
        self.assertEqual(
            {(j.component_name, j.dimension) for j in jobs},
            {
                ("Log Store", "log_data_ingested_gb"),
                ("Log Store", "log_storage_gbmo"),
            },
        )

    def test_discovers_jobs_for_descriptive_live_names(self):
        lambda_name = "AWS Lambda Function (Python\u202f3.12)"
        logs_name = (
            "Amazon CloudWatch Log Group (for Lambda & API Gateway)"
        )
        selection = ComponentSelectionResult(
            selected_components=[
                ComponentCandidate(
                    name=lambda_name, category="compute", role="lambda",
                    provider="aws", rationale="r", evidence_refs=[],
                    assumptions=[], tradeoffs=[], confidence="high",
                    evidence_support="strong", provider_specific=True),
                ComponentCandidate(
                    name=logs_name, category="monitoring",
                    role="cloudwatch logs", provider="aws", rationale="r",
                    evidence_refs=[], assumptions=[], tradeoffs=[],
                    confidence="high", evidence_support="strong",
                    provider_specific=True),
            ],
            rejected_candidates=[],
            unresolved_tradeoffs=[],
        )
        priced = [
            _priced_lambda().model_copy(
                update={"component_name": lambda_name}),
            _priced_cloudwatch().model_copy(
                update={"component_name": logs_name}),
        ]
        jobs, warnings = discover_estimation_jobs(
            _capacity_result(), selection, priced)
        self.assertEqual(warnings, [])
        keys = {(job.component_name, job.dimension) for job in jobs}
        self.assertIn((lambda_name, "gb_second"), keys)
        self.assertEqual(
            {dimension for name, dimension in keys if name == logs_name},
            {"log_data_ingested_gb", "log_storage_gbmo"},
        )


class TestUsageEstimatorDeterministic(unittest.TestCase):
    def test_deterministic_path_no_llm(self):
        cr = _capacity_result()
        sel = _selection()
        priced = [_priced_ddb()]
        estimator = UsageEstimator(proposal_fn=None)
        with patch("ai_architect.agents.cost_estimation.run.request_usage_estimation_proposal") as mock_prop:
            outcome = estimator.estimate(cr, sel, priced)
            mock_prop.assert_not_called()
        dims = {(e.component_name, e.dimension) for e in outcome.estimates}
        self.assertIn(("Ledger Store", "wcu_reserved"), dims)
        self.assertIn(("Ledger Store", "rcu_reserved"), dims)


class TestUsageEstimatorWithProposal(unittest.TestCase):
    def test_valid_proposal_accepted(self):
        cr = _capacity_result()
        sel = _selection()
        priced = [_priced_lambda()]
        mr = monthly_request_count(cr)

        def proposal_fn(jobs, cap, ctx):
            from ai_architect.agents.cost_estimation.models import (
                CostUsageEstimationProposal,
                EstimatedUsageProposal,
                EstimatedFactorScenario,
            )
            from ai_architect.domain.cost.models import EstimationConfidence
            dur, mem = 300.0, 1024.0
            qty = mr * (dur / 1000.0) * (mem / 1024.0)
            return CostUsageEstimationProposal(
                estimates=[
                    EstimatedUsageProposal(
                        component_name="Compute Function",
                        dimension="gb_second",
                        low_quantity=qty * 0.5,
                        typical_quantity=qty,
                        high_quantity=qty * 2.0,
                        confidence="medium",
                        rationale="test",
                        assumptions=["a"],
                        facts_used=["mr"],
                        unresolved_questions=[],
                        factors=EstimatedFactorScenario(
                            low={"average_invocation_duration_ms": 150.0, "memory_mb": 1024.0},
                            typical={"average_invocation_duration_ms": dur, "memory_mb": mem},
                            high={"average_invocation_duration_ms": 600.0, "memory_mb": 1024.0},
                        ),
                    )
                ]
            )

        estimator = UsageEstimator(proposal_fn=proposal_fn)
        outcome = estimator.estimate(cr, sel, priced)
        self.assertEqual(len(outcome.estimates), 1)
        e = outcome.estimates[0]
        self.assertEqual(e.provenance, UsageProvenance.ESTIMATOR_ASSUMPTION)
        self.assertAlmostEqual(e.typical_quantity, mr * 0.3 * 1.0, places=3)

    def test_invalid_quantity_rejected(self):
        cr = _capacity_result()
        sel = _selection()
        priced = [_priced_lambda()]
        mr = monthly_request_count(cr)

        def proposal_fn(jobs, cap, ctx):
            from ai_architect.agents.cost_estimation.models import (
                CostUsageEstimationProposal,
                EstimatedUsageProposal,
                EstimatedFactorScenario,
            )
            return CostUsageEstimationProposal(
                estimates=[
                    EstimatedUsageProposal(
                        component_name="Compute Function",
                        dimension="gb_second",
                        low_quantity=1.0,
                        typical_quantity=1.0,
                        high_quantity=1.0,
                        confidence="medium",
                        rationale="bad",
                        factors=EstimatedFactorScenario(
                            low={"average_invocation_duration_ms": 300.0, "memory_mb": 1024.0},
                            typical={"average_invocation_duration_ms": 300.0, "memory_mb": 1024.0},
                            high={"average_invocation_duration_ms": 300.0, "memory_mb": 1024.0},
                        ),
                    )
                ]
            )

        estimator = UsageEstimator(proposal_fn=proposal_fn)
        outcome = estimator.estimate(cr, sel, priced)
        self.assertEqual(len(outcome.estimates), 0)
        self.assertTrue(any("Rejected" in w for w in outcome.warnings))

    def test_s3_request_pair_accepted_with_shared_factors(self):
        cr = _capacity_result()
        mr = monthly_request_count(cr)

        def proposal_fn(jobs, cap, ctx):
            from ai_architect.agents.cost_estimation.models import (
                CostUsageEstimationProposal,
                EstimatedUsageProposal,
                EstimatedFactorScenario,
            )
            scenarios = {
                "low": (1.0, 0.2),
                "typical": (2.0, 0.25),
                "high": (3.0, 0.5),
            }

            def item(dimension, share_side):
                qty = {}
                for scenario, (calls, share) in scenarios.items():
                    split = share if share_side == "put" else 1.0 - share
                    qty[scenario] = mr * calls * split
                return EstimatedUsageProposal(
                    component_name="Object Store",
                    dimension=dimension,
                    low_quantity=qty["low"],
                    typical_quantity=qty["typical"],
                    high_quantity=qty["high"],
                    confidence="medium",
                    rationale="test",
                    factors=EstimatedFactorScenario(
                        low={"s3_calls_per_request": 1.0,
                             "tier1_share": 0.2},
                        typical={"s3_calls_per_request": 2.0,
                                 "tier1_share": 0.25},
                        high={"s3_calls_per_request": 3.0,
                              "tier1_share": 0.5},
                    ),
                )

            return CostUsageEstimationProposal(estimates=[
                item("s3_put_requests", "put"),
                item("s3_get_requests", "get"),
            ])

        estimator = UsageEstimator(proposal_fn=proposal_fn)
        outcome = estimator.estimate(
            cr, _selection_s3(), [_priced_s3()])
        by_dim = {e.dimension: e for e in outcome.estimates}
        self.assertEqual(
            sorted(by_dim), ["s3_get_requests", "s3_put_requests"])
        self.assertAlmostEqual(
            by_dim["s3_put_requests"].typical_quantity, mr * 0.5)
        self.assertAlmostEqual(
            by_dim["s3_get_requests"].typical_quantity, mr * 1.5)

    def test_s3_request_pair_with_divergent_factors_rejects_both(self):
        cr = _capacity_result()
        mr = monthly_request_count(cr)

        def proposal_fn(jobs, cap, ctx):
            from ai_architect.agents.cost_estimation.models import (
                CostUsageEstimationProposal,
                EstimatedUsageProposal,
                EstimatedFactorScenario,
            )
            put_qty = mr * 2.0 * 0.25
            get_qty = mr * 2.0 * 0.5
            return CostUsageEstimationProposal(estimates=[
                EstimatedUsageProposal(
                    component_name="Object Store",
                    dimension="s3_put_requests",
                    low_quantity=put_qty,
                    typical_quantity=put_qty,
                    high_quantity=put_qty,
                    confidence="medium",
                    rationale="test",
                    factors=EstimatedFactorScenario(
                        low={"s3_calls_per_request": 2.0,
                             "tier1_share": 0.25},
                        typical={"s3_calls_per_request": 2.0,
                                 "tier1_share": 0.25},
                        high={"s3_calls_per_request": 2.0,
                              "tier1_share": 0.25},
                    ),
                ),
                EstimatedUsageProposal(
                    component_name="Object Store",
                    dimension="s3_get_requests",
                    low_quantity=get_qty,
                    typical_quantity=get_qty,
                    high_quantity=get_qty,
                    confidence="medium",
                    rationale="test",
                    factors=EstimatedFactorScenario(
                        low={"s3_calls_per_request": 2.0,
                             "tier1_share": 0.5},
                        typical={"s3_calls_per_request": 2.0,
                                 "tier1_share": 0.5},
                        high={"s3_calls_per_request": 2.0,
                              "tier1_share": 0.5},
                    ),
                ),
            ])

        estimator = UsageEstimator(proposal_fn=proposal_fn)
        outcome = estimator.estimate(
            cr, _selection_s3(), [_priced_s3()])
        self.assertEqual(outcome.estimates, [])
        self.assertTrue(any(
            "both must declare identical" in w for w in outcome.warnings))
        self.assertTrue(any(
            "s3_get_requests" in w and "s3_put_requests" in w
            for w in outcome.warnings))

    def test_log_pair_with_divergent_bytes_rejects_both(self):
        cr = _capacity_result()
        mr = monthly_request_count(cr)

        def proposal_fn(jobs, cap, ctx):
            from ai_architect.agents.cost_estimation.models import (
                CostUsageEstimationProposal,
                EstimatedUsageProposal,
                EstimatedFactorScenario,
            )
            def factors(bytes_per_request):
                row = {"log_bytes_per_request": bytes_per_request}
                return EstimatedFactorScenario(
                    low=dict(row), typical=dict(row), high=dict(row))

            ingest = mr * 512.0 / 1e9
            stored = mr * 1024.0 / 1e9 * 30.0 / 30.0
            return CostUsageEstimationProposal(estimates=[
                EstimatedUsageProposal(
                    component_name="Log Store",
                    dimension="log_data_ingested_gb",
                    low_quantity=ingest,
                    typical_quantity=ingest,
                    high_quantity=ingest,
                    confidence="medium",
                    rationale="test",
                    factors=factors(512.0),
                ),
                EstimatedUsageProposal(
                    component_name="Log Store",
                    dimension="log_storage_gbmo",
                    low_quantity=stored,
                    typical_quantity=stored,
                    high_quantity=stored,
                    confidence="medium",
                    rationale="test",
                    factors=EstimatedFactorScenario(
                        low={"log_bytes_per_request": 1024.0,
                             "log_retention_days": 30.0},
                        typical={"log_bytes_per_request": 1024.0,
                                 "log_retention_days": 30.0},
                        high={"log_bytes_per_request": 1024.0,
                              "log_retention_days": 30.0},
                    ),
                ),
            ])

        estimator = UsageEstimator(proposal_fn=proposal_fn)
        outcome = estimator.estimate(
            cr, _selection_logs_only(), [_priced_cloudwatch()])
        self.assertEqual(outcome.estimates, [])
        self.assertTrue(any(
            "both must declare identical" in w for w in outcome.warnings))
        self.assertTrue(any(
            "log_bytes_per_request" in w for w in outcome.warnings))


class TestCalculatorEstimation(unittest.TestCase):
    def test_estimated_line_and_total(self):
        cr = _capacity_result()
        sel = _selection_lambda_only()
        priced = [_priced_lambda()]
        mr = monthly_request_count(cr)
        dur, mem = 300.0, 1024.0
        qty = mr * (dur / 1000.0) * (mem / 1024.0)
        est = UsageEstimate(
            component_name="Compute Function",
            dimension="gb_second",
            unit="GB-Seconds",
            low_quantity=qty,
            typical_quantity=qty,
            high_quantity=qty,
            provenance=UsageProvenance.ESTIMATOR_ASSUMPTION,
            confidence=EstimationConfidence.MEDIUM,
        )
        result = CostCalculator().calculate(cr, sel, priced, "us-east-1", usage_estimates=[est])
        self.assertEqual(len(result.estimated_lines), 1)
        self.assertEqual(result.completeness, CostCompleteness.ESTIMATED_WITH_ASSUMPTIONS)
        self.assertIsNotNone(result.best_estimate_total_usd_per_month)
        self.assertAlmostEqual(
            result.best_estimate_total_usd_per_month,
            result.totals_usd_per_month.get("on_demand", 0) + result.estimated_totals_usd_per_month.get("on_demand", 0),
            places=6,
        )
        self.assertEqual(result.estimated_range_usd_per_month.low_usd, result.estimated_range_usd_per_month.high_usd)

    def test_no_estimates_preserves_e1(self):
        cr = _capacity_result()
        sel = _selection()
        priced = [_priced_lambda()]
        result = CostCalculator().calculate(cr, sel, priced, "us-east-1")
        # no priced lines for gb_second (no derivation) -> usage_unknown
        self.assertEqual(result.completeness, CostCompleteness.PARTIALLY_UNRESOLVED)
        self.assertIsNone(result.best_estimate_total_usd_per_month)

    def test_known_fact_not_overwritten(self):
        cr = _capacity_result()
        sel = _selection()
        priced = [_priced_lambda()]
        # estimate for requests dimension which is priced -> ignored
        est = UsageEstimate(
            component_name="Compute Function",
            dimension="requests",
            unit="Requests",
            low_quantity=1.0,
            typical_quantity=1.0,
            high_quantity=1.0,
            provenance=UsageProvenance.ESTIMATOR_ASSUMPTION,
            confidence=EstimationConfidence.MEDIUM,
        )
        result = CostCalculator().calculate(cr, sel, priced, "us-east-1", usage_estimates=[est])
        self.assertEqual(len(result.estimated_lines), 0)
        self.assertTrue(any("already priced deterministically" in w for w in result.warnings))

    def test_unit_mismatch_rejected(self):
        cr = _capacity_result()
        sel = _selection()
        priced = [_priced_lambda()]
        est = UsageEstimate(
            component_name="Compute Function",
            dimension="gb_second",
            unit="WrongUnit",
            low_quantity=1.0,
            typical_quantity=1.0,
            high_quantity=1.0,
            provenance=UsageProvenance.ESTIMATOR_ASSUMPTION,
            confidence=EstimationConfidence.MEDIUM,
        )
        result = CostCalculator().calculate(cr, sel, priced, "us-east-1", usage_estimates=[est])
        self.assertEqual(len(result.estimated_lines), 0)
        self.assertTrue(any("unit mismatch" in w for w in result.warnings))

    def test_residual_unknowns_publishes(self):
        cr = _capacity_result()
        sel = _selection_lambda_and_logs()
        priced = [_priced_lambda(), _priced_cloudwatch()]
        mr = monthly_request_count(cr)
        dur, mem = 300.0, 1024.0
        qty = mr * (dur / 1000.0) * (mem / 1024.0)
        gb_est = UsageEstimate(
            component_name="Compute Function",
            dimension="gb_second",
            unit="GB-Seconds",
            low_quantity=qty,
            typical_quantity=qty,
            high_quantity=qty,
            provenance=UsageProvenance.ESTIMATOR_ASSUMPTION,
            confidence=EstimationConfidence.MEDIUM,
        )
        ingested_gb = mr * 512.0 / 1_000_000_000.0
        ingested_est = UsageEstimate(
            component_name="Log Store",
            dimension="log_data_ingested_gb",
            unit="GB",
            low_quantity=ingested_gb * 0.5,
            typical_quantity=ingested_gb,
            high_quantity=ingested_gb * 2.0,
            provenance=UsageProvenance.ESTIMATOR_ASSUMPTION,
            confidence=EstimationConfidence.MEDIUM,
        )
        storage_est = UsageEstimate(
            component_name="Log Store",
            dimension="log_storage_gbmo",
            unit="GB-Mo",
            low_quantity=ingested_gb * 0.5,
            typical_quantity=ingested_gb,
            high_quantity=ingested_gb * 2.0,
            provenance=UsageProvenance.ESTIMATOR_ASSUMPTION,
            confidence=EstimationConfidence.MEDIUM,
        )
        result = CostCalculator().calculate(
            cr, sel, priced, "us-east-1",
            usage_estimates=[gb_est, ingested_est, storage_est])
        # custom_metrics and alarms stay unsupported residuals; every
        # log dimension was estimated, so the best-estimate total is
        # still published.
        self.assertEqual(
            result.completeness,
            CostCompleteness.ESTIMATED_WITH_RESIDUAL_UNKNOWNS)
        residual_dims = {
            entry.dimension for entry in result.coverage
            if entry.status == "unsupported"
        }
        self.assertEqual(residual_dims, {"alarms", "custom_metrics"})
        self.assertIsNotNone(result.best_estimate_total_usd_per_month)
        estimated_dims = {e.dimension for e in result.estimates}
        self.assertEqual(
            estimated_dims,
            {"gb_second", "log_data_ingested_gb", "log_storage_gbmo"})

    def test_partially_unresolved_suppresses(self):
        cr = _capacity_result()
        sel = _selection()
        priced = [_priced_lambda()]
        # no estimate for gb_second -> usage_unknown remains unresolvable
        result = CostCalculator().calculate(cr, sel, priced, "us-east-1")
        self.assertEqual(result.completeness, CostCompleteness.PARTIALLY_UNRESOLVED)
        self.assertIsNone(result.best_estimate_total_usd_per_month)

    def test_leftover_estimate_warned(self):
        cr = _capacity_result()
        sel = _selection()
        priced = [_priced_lambda()]
        est = UsageEstimate(
            component_name="Ghost Component",
            dimension="gb_second",
            unit="GB-Seconds",
            low_quantity=1.0,
            typical_quantity=1.0,
            high_quantity=1.0,
            provenance=UsageProvenance.ESTIMATOR_ASSUMPTION,
            confidence=EstimationConfidence.MEDIUM,
        )
        result = CostCalculator().calculate(cr, sel, priced, "us-east-1", usage_estimates=[est])
        self.assertTrue(any("not part of the selection" in w for w in result.warnings))

    def test_estimates_sorted_in_result(self):
        cr = _capacity_result()
        sel = _selection()
        priced = [_priced_lambda()]
        mr = monthly_request_count(cr)
        qty = mr * 0.3
        est = UsageEstimate(
            component_name="Compute Function",
            dimension="gb_second",
            unit="GB-Seconds",
            low_quantity=qty,
            typical_quantity=qty,
            high_quantity=qty,
            provenance=UsageProvenance.ESTIMATOR_ASSUMPTION,
            confidence=EstimationConfidence.MEDIUM,
        )
        result = CostCalculator().calculate(cr, sel, priced, "us-east-1", usage_estimates=[est])
        self.assertEqual([e.component_name for e in result.estimates], ["Compute Function"])


class TestCheckpointRoundtrip(unittest.TestCase):
    def test_msgpack_roundtrip(self):
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        from ai_architect.infrastructure.checkpoint.sqlite import _MSGPACK_ALLOWLISTED_TYPES
        serde = JsonPlusSerializer(allowed_msgpack_modules=list(_MSGPACK_ALLOWLISTED_TYPES))
        est = UsageEstimate(
            component_name="Fn",
            dimension="gb_second",
            unit="GB-Seconds",
            low_quantity=1.0,
            typical_quantity=2.0,
            high_quantity=3.0,
            provenance=UsageProvenance.ESTIMATOR_ASSUMPTION,
            confidence=EstimationConfidence.MEDIUM,
        )
        result = CostEstimationResult(estimates=[est])
        blob = serde.dumps_typed(result)
        restored = serde.loads_typed(blob)
        self.assertEqual(restored.estimates[0].typical_quantity, 2.0)


class TestModelsValidation(unittest.TestCase):
    def test_usage_estimate_ordering(self):
        with self.assertRaises(Exception):
            UsageEstimate(
                component_name="Fn",
                dimension="gb_second",
                unit="GB-Seconds",
                low_quantity=5.0,
                typical_quantity=2.0,
                high_quantity=3.0,
                provenance=UsageProvenance.ESTIMATOR_ASSUMPTION,
                confidence=EstimationConfidence.MEDIUM,
            )

    def test_estimated_usage_proposal_forbids_price(self):
        from ai_architect.agents.cost_estimation.models import EstimatedUsageProposal
        with self.assertRaises(Exception):
            EstimatedUsageProposal(
                component_name="Fn",
                dimension="gb_second",
                low_quantity=1.0,
                typical_quantity=1.0,
                high_quantity=1.0,
                confidence="medium",
                price_usd=5.0,
            )

    def test_cost_completeness_new_value(self):
        self.assertEqual(CostCompleteness.ESTIMATED_WITH_RESIDUAL_UNKNOWNS.value, "estimated_with_residual_unknowns")


if __name__ == "__main__":
    unittest.main()
