"""Coverage registry tests (E1): every expected dimension is classified.

Pins the deterministic skeleton the rest of the cost pipeline hangs on:

- the registry resolves services by Price List code and by the same
  name aliases the pricing service uses, and never by fuzzy matching;
- priceable dimensions stay derived from SERVICE_SPECS, so a spec
  change cannot silently orphan a calculator derivation rule;
- configuration gates (DynamoDB billing model, API Gateway variant)
  exclude the other configuration's dimensions, while an unresolved
  gate keeps everything applicable (an unproven exclusion can never
  fake completeness);
- the completeness verdict gates the best-estimate total: published
  only for FULLY_PRICED, withheld with an explicit count while any
  applicable dimension is unresolved;
- artifacts persisted before E1 derive completeness from the legacy
  fully_priced flag without inventing an estimation status.

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
    CostCompleteness,
    CostEstimationResult,
    DimensionDisposition,
    PricedComponent,
    PriceTier,
    PricingFact,
    PricingModel,
)
from ai_architect.services.capacity.calculator import CapacityCalculator
from ai_architect.services.cost import coverage_registry
from ai_architect.services.cost.calculator import CostCalculator
from ai_architect.services.cost.pricing_service import SERVICE_SPECS


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


def _candidate(name: str, category: str = "other") -> ComponentCandidate:
    return ComponentCandidate(
        name=name, provider="aws", category=category, role="r",
        rationale="r", confidence="high", evidence_support="strong")


def _selection(comps):
    return ComponentSelectionResult(selected_components=comps)


def _fact(service_code: str, usage_key: str, unit: str,
          price: float = 0.000001) -> PricingFact:
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
        component_name=name, category="other",
        service_code=service_code, facts=facts)


class RegistryResolutionTests(unittest.TestCase):
    def test_service_code_wins_over_name(self):
        coverage = coverage_registry.resolve_service(
            "Some Unmatched Name", "AWSQueueService")
        self.assertIsNotNone(coverage)
        self.assertEqual(coverage.service_code, "AWSQueueService")

    def test_declared_name_alias_resolves_cloudwatch(self):
        for name in ("Amazon CloudWatch", "CloudWatch", "AWS CloudWatch"):
            with self.subTest(name=name):
                coverage = coverage_registry.resolve_service(name, "")
                self.assertIsNotNone(coverage)
                self.assertEqual(coverage.service_code, "AmazonCloudWatch")

    def test_prefix_stripped_name_alias_resolves(self):
        for name in ("Amazon SQS", "AWS SNS"):
            with self.subTest(name=name):
                coverage = coverage_registry.resolve_service(name, "")
                self.assertIsNotNone(coverage)
                self.assertTrue(
                    coverage.service_code in ("AWSQueueService", "AmazonSNS"))

    def test_unknown_service_is_none_not_guessed(self):
        self.assertIsNone(
            coverage_registry.resolve_service(
                "Amazon Elastic Kubernetes Service (EKS)", "")
        )
        self.assertIsNone(coverage_registry.resolve_service("mystery", ""))

    def test_priceable_dimensions_all_have_derivation_rules(self):
        codes = sorted({
            spec["service_code"] for spec in SERVICE_SPECS.values()
        })
        checked = 0
        for code in codes:
            coverage = coverage_registry.resolve_service("", code)
            self.assertIsNotNone(coverage, msg=code)
            for dim in coverage.dimensions:
                if dim.disposition == DimensionDisposition.PRICEABLE:
                    self.assertIn(
                        dim.dimension,
                        coverage_registry.DERIVATION_SUPPORTED_DIMENSIONS,
                        msg=f"{code}/{dim.dimension}",
                    )
                    checked += 1
        self.assertGreater(checked, 0)

    def test_every_spec_usage_key_is_registered(self):
        for name, spec in SERVICE_SPECS.items():
            with self.subTest(spec=name):
                coverage = coverage_registry.resolve_service(
                    "", spec["service_code"])
                self.assertIsNotNone(coverage)
                keys = {dim.dimension for dim in coverage.dimensions}
                expected: set[str] = set()
                if "variants" in spec:
                    for vdef in spec["variants"].values():
                        key = vdef.get("usage_key") or spec.get("usage_key")
                        if key:
                            expected.add(key)
                for sku in spec.get("skus") or []:
                    expected.add(sku["usage_key"])
                self.assertTrue(
                    expected <= keys,
                    msg=f"{name}: missing {sorted(expected - keys)}",
                )

    def test_every_derivation_rule_is_registered_somewhere(self):
        registered: set[str] = set()
        codes = sorted({
            spec["service_code"] for spec in SERVICE_SPECS.values()
        })
        codes.append("AmazonCloudWatch")
        for code in codes:
            coverage = coverage_registry.resolve_service("", code)
            registered |= {dim.dimension for dim in coverage.dimensions}
        self.assertTrue(
            coverage_registry.DERIVATION_SUPPORTED_DIMENSIONS <= registered,
            msg=sorted(
                coverage_registry.DERIVATION_SUPPORTED_DIMENSIONS
                - registered
            ),
        )

    def test_new_dimensions_are_classified_from_specs(self):
        ddb = coverage_registry.resolve_service("", "AmazonDynamoDB")
        by_dim = {dim.dimension: dim for dim in ddb.dimensions}
        self.assertEqual(
            by_dim["storage_gbmo"].disposition,
            DimensionDisposition.PRICEABLE)
        self.assertIn("DDB-Storage", by_dim["storage_gbmo"].catalog_lookup)

        s3 = coverage_registry.resolve_service("", "AmazonS3")
        by_dim = {dim.dimension: dim for dim in s3.dimensions}
        for dimension in ("s3_put_requests", "s3_get_requests"):
            self.assertEqual(
                by_dim[dimension].disposition,
                DimensionDisposition.ESTIMABLE)
            self.assertIn("S3-API-Tier", by_dim[dimension].catalog_lookup)

        cw = coverage_registry.resolve_service("", "AmazonCloudWatch")
        by_dim = {dim.dimension: dim for dim in cw.dimensions}
        for dimension in ("log_data_ingested_gb", "log_storage_gbmo"):
            self.assertEqual(
                by_dim[dimension].disposition,
                DimensionDisposition.ESTIMABLE)
            self.assertIn("CW:", by_dim[dimension].catalog_lookup)
        for dimension in ("custom_metrics", "alarms"):
            self.assertEqual(
                by_dim[dimension].disposition,
                DimensionDisposition.UNSUPPORTED)


class CoverageGatingTests(unittest.TestCase):
    def test_unresolved_gate_keeps_dimensions_applicable(self):
        applicable, excluded = coverage_registry.coverage_for(
            "AmazonDynamoDB")
        self.assertEqual(excluded, [])
        self.assertEqual(
            sorted(dim.dimension for dim in applicable),
            ["rcu_reserved", "rru", "storage_gbmo", "wcu_reserved", "wru"],
        )

    def test_conflicting_evidence_keeps_every_gate_open(self):
        hint = coverage_registry.infer_resolution(
            "AmazonDynamoDB",
            ["wru", "rru", "wcu_reserved", "rcu_reserved"],
        )
        self.assertIsNone(hint.billing_model)
        applicable, excluded = coverage_registry.coverage_for(
            "AmazonDynamoDB", billing_model=hint.billing_model)
        self.assertEqual(excluded, [])
        self.assertEqual(len(applicable), 5)

    def test_resolved_billing_model_excludes_only_the_other_side(self):
        applicable, excluded = coverage_registry.coverage_for(
            "AmazonDynamoDB", billing_model="pay_per_request")
        self.assertEqual(
            {dim.dimension for dim in excluded},
            {"wcu_reserved", "rcu_reserved"},
        )
        self.assertEqual(
            {dim.dimension for dim in applicable},
            {"rru", "storage_gbmo", "wru"},
        )

    def test_resolved_variant_excludes_only_the_other_api_type(self):
        applicable, excluded = coverage_registry.coverage_for(
            "AmazonApiGateway", variant="http")
        self.assertEqual(
            {dim.dimension for dim in excluded}, {"rest_requests"})
        self.assertEqual(
            {dim.dimension for dim in applicable}, {"http_requests"})

    def test_inferred_resolution_from_facts(self):
        self.assertEqual(
            coverage_registry.infer_resolution(
                "AmazonApiGateway", ["http_requests"]).variant,
            "http",
        )
        self.assertEqual(
            coverage_registry.infer_resolution(
                "AmazonDynamoDB", ["wru", "rru"]).billing_model,
            "pay_per_request",
        )

    def test_unavailable_cloudwatch_gets_declared_dimension_rows(self):
        cw = PricedComponent(
            component_name="Amazon CloudWatch", category="monitoring",
            service_code="AmazonCloudWatch",
            unavailable_reason=(
                "Cannot price: no Price List mapping and metric and log "
                "volumes are not determined."),
        )
        result = CostCalculator().calculate(
            _capacity(), _selection([_candidate("Amazon CloudWatch")]),
            [cw], "us-east-1")
        # Rows only: the component-level unpriced entry is the gap signal.
        self.assertEqual(len(result.unpriced), 1)
        rows = {entry.dimension: entry.status for entry in result.coverage}
        self.assertEqual(rows, {
            "alarms": "unsupported",
            "custom_metrics": "unsupported",
            "log_data_ingested_gb": "usage_unknown",
            "log_storage_gbmo": "usage_unknown",
        })
        self.assertEqual(result.unresolved_dimension_count, 4)


class CompletenessTests(unittest.TestCase):
    def test_fully_priced_publishes_best_estimate_and_range(self):
        priced = [_priced(
            "Amazon Simple Queue Service (SQS)", "AWSQueueService",
            [_fact("AWSQueueService", "requests", "Requests", 4e-07)])]
        result = CostCalculator().calculate(
            _capacity(),
            _selection([_candidate(
                "Amazon Simple Queue Service (SQS)", "messaging")]),
            priced, "us-east-1")
        self.assertEqual(result.completeness, CostCompleteness.FULLY_PRICED)
        self.assertTrue(result.fully_priced)
        subtotal = result.totals_usd_per_month["on_demand"]
        self.assertAlmostEqual(
            result.best_estimate_total_usd_per_month, subtotal, places=9)
        self.assertEqual(result.unresolved_dimension_count, 0)
        self.assertAlmostEqual(
            result.estimated_range_usd_per_month.low_usd, subtotal,
            places=9)
        self.assertAlmostEqual(
            result.estimated_range_usd_per_month.high_usd, subtotal,
            places=9)

    def test_partial_estimate_withholds_total_and_counts_dimensions(self):
        priced = [_priced(
            "Amazon DynamoDB", "AmazonDynamoDB",
            [_fact("AmazonDynamoDB", "storage_gbmo", "GB-Mo", 0.25)])]
        result = CostCalculator().calculate(
            _capacity(), _selection([_candidate("Amazon DynamoDB")]),
            priced, "us-east-1")
        self.assertEqual(
            result.completeness, CostCompleteness.PARTIALLY_UNRESOLVED)
        self.assertFalse(result.fully_priced)
        self.assertIsNone(result.best_estimate_total_usd_per_month)
        self.assertIsNone(result.estimated_range_usd_per_month)
        self.assertEqual(result.unresolved_dimension_count, 4)

    def test_unsupported_when_nothing_can_be_priced(self):
        priced = [PricedComponent(
            component_name="Amazon Elastic Kubernetes Service (EKS)",
            category="compute",
            unavailable_reason="Cannot price: no Price List mapping.")]
        result = CostCalculator().calculate(
            _capacity(),
            _selection([_candidate(
                "Amazon Elastic Kubernetes Service (EKS)", "compute")]),
            priced, "us-east-1")
        self.assertEqual(result.lines, [])
        self.assertEqual(result.completeness, CostCompleteness.UNSUPPORTED)
        self.assertIsNone(result.best_estimate_total_usd_per_month)

    def test_legacy_artifact_completeness_is_derived_not_invented(self):
        legacy_partial = CostEstimationResult(fully_priced=False)
        self.assertEqual(
            legacy_partial.completeness,
            CostCompleteness.PARTIALLY_UNRESOLVED,
        )
        legacy_full = CostEstimationResult(fully_priced=True)
        self.assertEqual(
            legacy_full.completeness, CostCompleteness.FULLY_PRICED)


class UsageProvenanceTests(unittest.TestCase):
    def test_stated_assumption_provenance_for_zones(self):
        priced = [_priced(
            "Amazon Route 53", "AmazonRoute53",
            [_fact("AmazonRoute53", "zones", "HostedZone", 0.5)])]
        result = CostCalculator().calculate(
            _capacity(), _selection([_candidate("Amazon Route 53", "dns")]),
            priced, "us-east-1")
        zones_line = next(
            line for line in result.lines if line.unit == "HostedZone")
        self.assertEqual(
            zones_line.usage_provenance, "stated_assumption")
        # The missing 'queries' fact stays an explicit price gap.
        rows = {entry.dimension: entry.status for entry in result.coverage}
        self.assertEqual(rows["queries"], "price_unavailable")
        self.assertEqual(rows["zones"], "priced")

    def test_derived_provenance_for_requests(self):
        priced = [_priced(
            "Amazon Simple Queue Service (SQS)", "AWSQueueService",
            [_fact("AWSQueueService", "requests", "Requests", 4e-07)])]
        result = CostCalculator().calculate(
            _capacity(),
            _selection([_candidate(
                "Amazon Simple Queue Service (SQS)", "messaging")]),
            priced, "us-east-1")
        self.assertEqual(
            result.lines[0].usage_provenance, "deterministic_derivation")


if __name__ == "__main__":
    unittest.main()
