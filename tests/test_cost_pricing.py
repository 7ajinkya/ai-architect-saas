"""Pricing service tests: normalization, staleness, invalid data, mapping.

All AWS calls are faked; no network, no LLM.
Run: poetry run python -m unittest discover tests
"""
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from ai_architect.domain.cost.models import PricingModel
from ai_architect.services.cost import pricing_service as ps
from ai_architect.services.cost.pricing_service import PricingService


def _doc(attrs, terms):
    return json.dumps({"product": {"attributes": attrs}, "terms": terms})


def _sqs_page():
    return {
        "PriceList": [
            _doc(
                {"servicename": "Amazon Simple Queue Service",
                 "usagetype": "APS3-Requests-Tier1", "regionCode": "ap-south-1",
                 "group": "SQS-APIRequest-Tier1", "queueType": "Standard"},
                {"OnDemand": {"sku.1": {"priceDimensions": {
                    "sku.1.rate": {
                        "unit": "Requests", "beginRange": "0", "endRange": "Inf",
                        "description": "$0.40 per million",
                        "pricePerUnit": {"USD": "0.0000004000"}}}}}},
            )
        ]
    }


def _ddb_reserved_page():
    return {
        "PriceList": [
            _doc(
                {"servicename": "Amazon DynamoDB", "usagetype": "APS3-WriteCapacityUnit-Hrs",
                 "regionCode": "ap-south-1", "group": "DDB-WriteUnits",
                 "operation": "CommittedThroughput"},
                {"Reserved": {
                    "term.a": {
                        "termAttributes": {
                            "LeaseContractLength": "1yr",
                            "OfferingClass": "standard",
                            "PurchaseOption": "No Upfront",
                        },
                        "priceDimensions": {
                            "term.a.hourly": {
                                "unit": "WriteCapacityUnit-Hrs", "beginRange": "0",
                                "endRange": "Inf", "description": "reserved WCU",
                                "pricePerUnit": {"USD": "0.0001460000"}},
                        },
                    },
                    "term.b": {
                        "termAttributes": {
                            "LeaseContractLength": "3yr",
                            "OfferingClass": "standard",
                            "PurchaseOption": "Partial Upfront",
                        },
                        "priceDimensions": {
                            "term.b.up": {
                                "unit": "Quantity", "description": "Upfront Fee",
                                "pricePerUnit": {"USD": "12.00"}},
                            "term.b.hr": {
                                "unit": "WriteCapacityUnit-Hrs", "beginRange": "0",
                                "endRange": "Inf", "description": "reserved WCU",
                                "pricePerUnit": {"USD": "0.0001"}},
                        },
                    },
                }},
            )
        ]
    }


def _ddb_on_demand_page(
    *, usagetype, group, unit, price, description,
    operation="PayPerRequestThroughput",
):
    return {
        "PriceList": [
            _doc(
                {
                    "servicename": "Amazon DynamoDB",
                    "usagetype": usagetype,
                    "regionCode": "us-east-1",
                    "group": group,
                    "operation": operation,
                },
                {
                    "OnDemand": {
                        "sku.1": {
                            "priceDimensions": {
                                "sku.1.rate": {
                                    "unit": unit,
                                    "beginRange": "0",
                                    "endRange": "Inf",
                                    "description": description,
                                    "pricePerUnit": {"USD": price},
                                }
                            }
                        }
                    }
                },
            )
        ]
    }


def _ddb_write_page():
    return _ddb_on_demand_page(
        usagetype="WriteRequestUnits",
        group="DDB-WriteUnits",
        unit="WriteRequestUnits",
        price="0.0000006250",
        description="$0.625 per million write request units",
    )


def _ddb_read_page():
    return _ddb_on_demand_page(
        usagetype="ReadRequestUnits",
        group="DDB-ReadUnits",
        unit="ReadRequestUnits",
        price="0.0000001250",
        description="$0.125 per million read request units",
    )


def _ddb_committed_write_page():
    return _ddb_on_demand_page(
        usagetype="WriteCapacityUnit-Hrs",
        group="DDB-WriteUnits",
        unit="WriteCapacityUnit-Hrs",
        price="0.0001460000",
        description="provisioned write capacity (committed throughput)",
        operation="CommittedThroughput",
    )


def _ddb_committed_read_page():
    return _ddb_on_demand_page(
        usagetype="ReadCapacityUnit-Hrs",
        group="DDB-ReadUnits",
        unit="ReadCapacityUnit-Hrs",
        price="0.0000292000",
        description="provisioned read capacity (committed throughput)",
        operation="CommittedThroughput",
    )


def _ddb_storage_page():
    # Storage is model-independent: no operation attribute.
    return {
        "PriceList": [
            _doc(
                {
                    "servicename": "Amazon DynamoDB",
                    "usagetype": "TimedStorage-ByteHrs",
                    "regionCode": "us-east-1",
                    "group": "DDB-Storage",
                },
                {
                    "OnDemand": {
                        "sku.1": {
                            "priceDimensions": {
                                "sku.1.rate": {
                                    "unit": "GB-Mo",
                                    "beginRange": "0",
                                    "endRange": "Inf",
                                    "description": "$0.25 per GB-month",
                                    "pricePerUnit": {"USD": "0.2500000000"},
                                }
                            }
                        }
                    }
                },
            )
        ]
    }


class FakePricingClient:
    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = []

    def get_products(self, **kwargs):
        self.calls.append(kwargs)
        return self._pages.pop(0) if self._pages else {"PriceList": []}


class NormalizeTests(unittest.TestCase):
    def test_on_demand_tiers_normalized_with_provenance(self):
        svc = PricingService(client=FakePricingClient([_sqs_page()]))
        facts, warnings = svc.get_price_facts(
            "AWSQueueService", "ap-south-1",
            {"group": "SQS-APIRequest-Tier1", "regionCode": "ap-south-1"})
        self.assertEqual(warnings, [])
        self.assertEqual(len(facts), 1)
        fact = facts[0]
        self.assertEqual(fact.pricing_model, PricingModel.ON_DEMAND)
        self.assertEqual(fact.unit, "Requests")
        self.assertEqual(len(fact.tiers), 1)
        self.assertAlmostEqual(fact.tiers[0].unit_price_usd, 0.0000004)
        self.assertEqual(fact.source, "aws-price-list-api")
        self.assertEqual(fact.region_code, "ap-south-1")
        self.assertIsInstance(fact.retrieved_at, datetime)

    def test_reserved_terms_parsed_with_upfront_and_preference(self):
        svc = PricingService(client=FakePricingClient([_ddb_reserved_page()]))
        facts, _ = svc.get_price_facts("AmazonDynamoDB", "ap-south-1", {})
        by_model = {f.pricing_model: f for f in facts}
        self.assertIn(PricingModel.RESERVED_1YR, by_model)
        self.assertIn(PricingModel.RESERVED_3YR, by_model)
        one = by_model[PricingModel.RESERVED_1YR]
        self.assertEqual(one.purchase_option, "No Upfront")
        self.assertEqual(one.upfront_usd, 0.0)
        self.assertAlmostEqual(one.tiers[0].unit_price_usd, 0.000146)
        three = by_model[PricingModel.RESERVED_3YR]
        self.assertEqual(three.upfront_usd, 12.0)
        self.assertEqual(three.lease_length, "3yr")

    def test_empty_results_are_not_an_error(self):
        svc = PricingService(client=FakePricingClient([{"PriceList": []}]))
        facts, warnings = svc.get_price_facts("Nope", "ap-south-1", {})
        self.assertEqual(facts, [])
        self.assertEqual(warnings, [])

    def test_invalid_sku_skipped_with_warning(self):
        dim = {
            "unit": "Requests",
            "beginRange": "0",
            "endRange": "Inf",
            "description": "d",
            "pricePerUnit": {},
        }
        sku_doc = {
            "product": {"attributes": {"usagetype": "X"}},
            "terms": {"OnDemand": {"t": {"priceDimensions": {"t.r": dim}}}},
        }
        page = {"PriceList": [json.dumps(sku_doc)]}
        svc = PricingService(client=FakePricingClient([page]))
        facts, warnings = svc.get_price_facts("S", "ap-south-1", {})
        self.assertEqual(facts, [])
        self.assertTrue(any("no usable USD tiers" in w for w in warnings))

    def test_stale_pricing_rejected(self):
        svc = PricingService(client=FakePricingClient([]))
        facts, _ = PricingService(
            client=FakePricingClient([_sqs_page()])).get_price_facts(
                "AWSQueueService", "ap-south-1", {})
        facts[0].retrieved_at = datetime.utcnow() - timedelta(hours=25)
        with self.assertRaises(ValueError) as ctx:
            svc.assert_fresh(facts)
        self.assertIn("Stale pricing", str(ctx.exception))

    def test_fresh_pricing_accepted(self):
        svc = PricingService(client=FakePricingClient([]))
        facts, _ = PricingService(
            client=FakePricingClient([_sqs_page()])).get_price_facts(
                "AWSQueueService", "ap-south-1", {})
        svc.assert_fresh(facts)  # must not raise


class MappingTests(unittest.TestCase):
    def test_region_aliases(self):
        self.assertEqual(ps.normalize_region("Mumbai region"), "ap-south-1")
        self.assertEqual(ps.normalize_region("ap-south-1"), "ap-south-1")
        with self.assertRaises(ValueError):
            ps.normalize_region("Atlantis")

    def test_region_prefix_table_covers_supported_regions(self):
        for code in ("ap-south-1", "us-east-1"):
            self.assertIn("usage", ps.REGION_USAGE_PREFIXES[code])

    def test_known_component_maps_to_spec(self):
        from ai_architect.agents.component_selection.models import ComponentCandidate
        comp = ComponentCandidate(
            name="Amazon Simple Queue Service (SQS)", provider="aws",
            category="messaging", role="q", rationale="r",
            confidence="high", evidence_support="strong")
        svc = PricingService(client=FakePricingClient([_sqs_page()]))
        components, warnings = svc.price_components(
            _selection([comp]), "ap-south-1")
        self.assertEqual(len(components), 1)
        self.assertIsNone(components[0].unavailable_reason)
        self.assertEqual(components[0].service_code, "AWSQueueService")
        self.assertTrue(components[0].facts)

    def test_unknown_component_explicitly_unavailable(self):
        from ai_architect.agents.component_selection.models import ComponentCandidate
        comp = ComponentCandidate(
            name="Some Hypothetical Service", provider="aws",
            category="compute", role="q", rationale="r",
            confidence="high", evidence_support="strong")
        svc = PricingService(client=FakePricingClient([]))
        components, _ = svc.price_components(_selection([comp]), "ap-south-1")
        self.assertIsNotNone(components[0].unavailable_reason)
        self.assertEqual(components[0].facts, [])


def _lambda_requests_page(*, usagetype="Request", region_code="us-east-1"):
    """Lambda request SKU. us-east-1 uses the bare "Request" form."""
    return {
        "PriceList": [
            _doc(
                {"servicename": "AWS Lambda",
                 "usagetype": usagetype,
                 "regionCode": region_code,
                 "group": "AWS-Lambda-Requests"},
                {"OnDemand": {"sku.lam.req": {"priceDimensions": {
                    "sku.lam.req.rate": {
                        "unit": "Requests",
                        "beginRange": "0",
                        "endRange": "Inf",
                        "description": "$0.20 per 1 million requests",
                        "pricePerUnit": {"USD": "0.0000002000"}}}}}},
            )
        ]
    }


def _lambda_gb_second_page(*, usagetype="Lambda-GB-Second",
                           region_code="us-east-1"):
    """Lambda GB-second SKU. us-east-1 uses the bare form."""
    return {
        "PriceList": [
            _doc(
                {"servicename": "AWS Lambda",
                 "usagetype": usagetype,
                 "regionCode": region_code},
                {"OnDemand": {"sku.lam.gbs": {"priceDimensions": {
                    "sku.lam.gbs.rate": {
                        "unit": "GB-second",
                        "beginRange": "0",
                        "endRange": "Inf",
                        "description": "$0.0000166667 per GB-second",
                        "pricePerUnit": {"USD": "0.0000166667"}}}}}},
            )
        ]
    }


def _sns_page():
    """SNS request SKU (live-verified group SNS-Requests-Tier1)."""
    return {
        "PriceList": [
            _doc(
                {"servicename": "Amazon Simple Notification Service",
                 "usagetype": "Requests-Tier1",
                 "regionCode": "us-east-1",
                 "group": "SNS-Requests-Tier1"},
                {"OnDemand": {"sku.sns": {"priceDimensions": {
                    "sku.sns.rate": {
                        "unit": "Requests",
                        "beginRange": "0",
                        "endRange": "Inf",
                        "description": "$0.50 per 1 million requests",
                        "pricePerUnit": {"USD": "0.0000005000"}}}}}},
            )
        ]
    }


class SnsPricingTests(unittest.TestCase):
    """Amazon SNS prices via the live-verified requests SKU; the
    component resolves through the "sns" spec ("amazon sns" prefix
    strip). No other dimension is claimed."""

    def test_sns_maps_to_requests_sku(self):
        from ai_architect.agents.component_selection.models import ComponentCandidate
        comp = ComponentCandidate(
            name="Amazon SNS", provider="aws",
            category="messaging", role="fan-out notifications to subscribers",
            rationale="r", confidence="high", evidence_support="strong")
        client = FakePricingClient([_sns_page()])
        svc = PricingService(client=client)
        components, _ = svc.price_components(
            _selection([comp]), "us-east-1")
        self.assertEqual(len(components), 1)
        self.assertIsNone(components[0].unavailable_reason)
        self.assertEqual(components[0].service_code, "AmazonSNS")
        self.assertEqual(
            [f.usage_key for f in components[0].facts], ["requests"])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            _filter_value(client.calls[0], "group"), "SNS-Requests-Tier1")
        self.assertIsNone(_filter_value(client.calls[0], "usagetype"))


class LambdaPricingTests(unittest.TestCase):
    """Live-verified Lambda usagetype forms.

    us-east-1 uses the bare "Request" and "Lambda-GB-Second" forms;
    ap-south-1 uses "APS3-Request" and "APS3-Lambda-GB-Second". The
    previous "{prefix}-Lambda-Requests" template matched nothing live
    (us-east-1) and left Lambda explicitly unpriced.
    """

    def _comp(self):
        from ai_architect.agents.component_selection.models import ComponentCandidate
        return ComponentCandidate(
            name="AWS Lambda", provider="aws",
            category="compute", role="serverless backend logic",
            rationale="r", confidence="high", evidence_support="strong")

    def test_lambda_spec_is_recognized_and_priced(self):
        client = FakePricingClient([
            _lambda_requests_page(), _lambda_gb_second_page()])
        svc = PricingService(client=client)
        components, warnings = svc.price_components(
            _selection([self._comp()]), "us-east-1")
        self.assertEqual(len(components), 1)
        self.assertIsNone(components[0].unavailable_reason)
        self.assertEqual(components[0].service_code, "AWSLambda")
        self.assertEqual(
            sorted(f.usage_key for f in components[0].facts),
            ["gb_second", "requests"])
        # Exact live-verified filter forms for us-east-1.
        self.assertEqual(
            _filter_value(client.calls[0], "usagetype"), "Request")
        self.assertEqual(
            _filter_value(client.calls[1], "usagetype"),
            "Lambda-GB-Second")
        self.assertEqual(
            _filter_value(client.calls[0], "regionCode"), "us-east-1")

    def test_lambda_apsouth1_uses_prefixed_forms(self):
        client = FakePricingClient([
            _lambda_requests_page(
                usagetype="APS3-Request", region_code="ap-south-1"),
            _lambda_gb_second_page(
                usagetype="APS3-Lambda-GB-Second",
                region_code="ap-south-1"),
        ])
        svc = PricingService(client=client)
        components, _ = svc.price_components(
            _selection([self._comp()]), "ap-south-1")
        self.assertIsNone(components[0].unavailable_reason)
        self.assertEqual(
            _filter_value(client.calls[0], "usagetype"), "APS3-Request")
        self.assertEqual(
            _filter_value(client.calls[1], "usagetype"),
            "APS3-Lambda-GB-Second")


def _selection(comps):
    from ai_architect.agents.component_selection.models import (
        ComponentSelectionResult,
    )
    return ComponentSelectionResult(selected_components=comps)


class DynamoDbOnDemandRegressionTests(unittest.TestCase):
    def test_price_list_rates_are_used_without_correction(self):
        """DynamoDB on-demand results must use the literal Price List rates.

        The Price List dimensions below match the actual us-east-1 response:
        USD 0.0000006250 per WriteRequestUnit and USD 0.0000001250 per
        ReadRequestUnit. The calculator must not multiply either rate.
        """
        from ai_architect.agents.capacity.models import CapacityModelProposal
        from ai_architect.agents.capacity.result import CapacityModelingResult
        from ai_architect.agents.component_selection.models import ComponentCandidate
        from ai_architect.domain.capacity.models import CapacityModelResult
        from ai_architect.domain.cost.models import PricedComponent
        from ai_architect.services.cost.calculator import CostCalculator

        service = PricingService(
            client=FakePricingClient([_ddb_write_page(), _ddb_read_page()])
        )
        write_facts, _ = service.get_price_facts(
            "AmazonDynamoDB",
            "us-east-1",
            {
                "group": "DDB-WriteUnits",
                "operation": "PayPerRequestThroughput",
            },
        )
        read_facts, _ = service.get_price_facts(
            "AmazonDynamoDB",
            "us-east-1",
            {
                "group": "DDB-ReadUnits",
                "operation": "PayPerRequestThroughput",
            },
        )
        write_fact = write_facts[0].model_copy(update={"usage_key": "wru"})
        read_fact = read_facts[0].model_copy(update={"usage_key": "rru"})

        capacity = CapacityModelingResult(
            proposal=CapacityModelProposal(
                daily_active_users=1000,
                messages_per_user_per_day=10.0,
                peak_factor=2.0,
                read_write_ratio=0.9,
                average_message_size_bytes=150,
                connection_factor=1.25,
                storage_retention_days=365,
                rationale={},
            ),
            calculation=CapacityModelResult(
                peak_write_qps=2000.0,
                peak_read_qps=2000.0,
                concurrent_connections=2500.0,
                storage_growth_bytes_per_day=1500000.0,
                hot_working_set_bytes=547500000.0,
                egress_bytes_per_second=300000.0,
                formula_version="v1",
                calculation_trace={},
            ),
        )
        selection = _selection([
            ComponentCandidate(
                name="Amazon DynamoDB",
                provider="aws",
                category="database",
                role="primary key-value store",
                rationale="r",
                confidence="high",
                evidence_support="strong",
            )
        ])
        priced = [
            PricedComponent(
                component_name="Amazon DynamoDB",
                category="database",
                service_code="AmazonDynamoDB",
                facts=[write_fact, read_fact],
            )
        ]

        result = CostCalculator().calculate(
            capacity, selection, priced, "us-east-1"
        )

        self.assertEqual(len(result.lines), 2)
        by_unit = {line.unit: line for line in result.lines}

        write_line = by_unit["WriteRequestUnits"]
        self.assertAlmostEqual(write_line.monthly_usage, 2592000000.0)
        self.assertAlmostEqual(write_line.unit_price_usd, 0.000000625)
        # 2,592,000,000 request units at the literal Price List rate.
        self.assertAlmostEqual(write_line.monthly_cost_usd, 1620.0)

        read_line = by_unit["ReadRequestUnits"]
        self.assertAlmostEqual(read_line.monthly_usage, 2592000 * 1000.0)
        self.assertAlmostEqual(read_line.unit_price_usd, 0.000000125)
        # 2,592,000,000 request units at the literal Price List rate.
        self.assertAlmostEqual(read_line.monthly_cost_usd, 324.0)

        self.assertAlmostEqual(
            result.totals_usd_per_month["on_demand"], 1620.0 + 324.0
        )
        self.assertFalse(
            any(
                "correction applied" in assumption.lower()
                for assumption in result.assumptions
            )
        )


class DynamoDbBillingModelTests(unittest.TestCase):
    """Exactly one DynamoDB billing model's SKUs is queried per component.

    Fetching committed-throughput SKUs as on-demand facts produced
    phantom wcu_reserved/rcu_reserved unpriced dimensions on every
    on-demand design (and forced fully_priced=False for DynamoDB).
    """

    def _comp(self, name, role="primary key-value store"):
        from ai_architect.agents.component_selection.models import ComponentCandidate
        return ComponentCandidate(
            name=name, provider="aws", category="database", role=role,
            rationale="r", confidence="high", evidence_support="strong")

    def _capacity(self):
        from ai_architect.agents.capacity.models import CapacityModelProposal
        from ai_architect.agents.capacity.result import CapacityModelingResult
        from ai_architect.domain.capacity.models import CapacityModelResult
        return CapacityModelingResult(
            proposal=CapacityModelProposal(
                daily_active_users=1000,
                messages_per_user_per_day=10.0,
                peak_factor=2.0,
                read_write_ratio=0.9,
                average_message_size_bytes=150,
                connection_factor=1.25,
                storage_retention_days=365,
                rationale={},
            ),
            calculation=CapacityModelResult(
                peak_write_qps=2000.0,
                peak_read_qps=2000.0,
                concurrent_connections=2500.0,
                storage_growth_bytes_per_day=1500000.0,
                hot_working_set_bytes=547500000.0,
                egress_bytes_per_second=300000.0,
                formula_version="v1",
                calculation_trace={},
            ),
        )

    def test_on_demand_name_queries_only_request_billing_skus(self):
        # U+2011 non-breaking hyphen in the name, exactly as recorded
        # live: the collapsed-text picker must still see "on-demand".
        comp = self._comp("Amazon DynamoDB (On\u2011Demand)")
        client = FakePricingClient([
            _ddb_write_page(), _ddb_read_page(), _ddb_storage_page()])
        svc = PricingService(client=client)
        components, warnings = svc.price_components(
            _selection([comp]), "us-east-1")
        self.assertEqual(len(client.calls), 3)
        for call in client.calls[:2]:
            self.assertEqual(
                _filter_value(call, "operation"), "PayPerRequestThroughput")
        self.assertEqual(
            sorted(f.usage_key for f in components[0].facts),
            ["rru", "storage_gbmo", "wru"])
        # Storage is queried independently of the request billing
        # model: no operation filter.
        self.assertIsNone(_filter_value(client.calls[2], "operation"))
        self.assertEqual(
            _filter_value(client.calls[2], "group"), "DDB-Storage")
        # Explicit on-demand signal: no default-model warning.
        self.assertFalse(any("default" in w for w in warnings))

    def test_provisioned_name_queries_only_committed_skus(self):
        comp = self._comp("Amazon DynamoDB (Provisioned Capacity)")
        client = FakePricingClient([
            _ddb_committed_write_page(), _ddb_committed_read_page(),
            _ddb_storage_page()])
        svc = PricingService(client=client)
        components, warnings = svc.price_components(
            _selection([comp]), "us-east-1")
        self.assertEqual(len(client.calls), 3)
        for call in client.calls[:2]:
            self.assertEqual(
                _filter_value(call, "operation"), "CommittedThroughput")
        self.assertEqual(
            sorted(f.usage_key for f in components[0].facts),
            ["rcu_reserved", "storage_gbmo", "wcu_reserved"])
        self.assertEqual(
            _filter_value(client.calls[2], "group"), "DDB-Storage")
        # Storage is model-independent and prices through the growth
        # rule; provisioned WCU/RCU-hour volumes stay explicitly
        # unresolved because the request-based capacity model cannot
        # derive them.
        from ai_architect.services.cost.calculator import CostCalculator
        result = CostCalculator().calculate(
            self._capacity(), _selection([comp]), components, "us-east-1")
        self.assertEqual(len(result.lines), 1)
        self.assertEqual(result.lines[0].unit, "GB-Mo")
        self.assertFalse(result.fully_priced)
        self.assertEqual(len(result.unpriced), 2)
        self.assertTrue(all(
            "No usage derivation rule" in u.reason
            for u in result.unpriced))

    def test_no_signal_defaults_to_on_demand_with_warning(self):
        comp = self._comp("Amazon DynamoDB")
        client = FakePricingClient([
            _ddb_write_page(), _ddb_read_page(), _ddb_storage_page()])
        svc = PricingService(client=client)
        components, warnings = svc.price_components(
            _selection([comp]), "us-east-1")
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(
            sorted(f.usage_key for f in components[0].facts),
            ["rru", "storage_gbmo", "wru"])
        self.assertTrue(
            any("on-demand" in w and "default" in w for w in warnings),
            msg=f"expected default-model warning, got: {warnings}")


def _s3_storage_page(*, usagetype, region_code):
    """Single-tier S3 standard-storage SKU doc mirroring the live shape."""
    return {
        "PriceList": [
            _doc(
                {"servicename": "Amazon Simple Storage Service",
                 "usagetype": usagetype, "regionCode": region_code},
                {"OnDemand": {"sku.s3": {"priceDimensions": {
                    "sku.s3.rate": {
                        "unit": "GB-Mo", "beginRange": "0", "endRange": "51200",
                        "description": "$0.023 per GB - first 50 TB",
                        "pricePerUnit": {"USD": "0.023"}}}}}},
            )
        ]
    }


def _s3_request_page(*, group, price):
    tier = "Requests-Tier1" if group.endswith("Tier1") else "Requests-Tier2"
    return {
        "PriceList": [
            _doc(
                {"servicename": "Amazon Simple Storage Service",
                 "usagetype": tier, "regionCode": "us-east-1",
                 "productFamily": "API Request", "group": group},
                {"OnDemand": {"sku.s3.req": {"priceDimensions": {
                    "sku.s3.req.rate": {
                        "unit": "Requests", "beginRange": "0",
                        "endRange": "Inf",
                        "description": "request tier",
                        "pricePerUnit": {"USD": price}}}}}},
            )
        ]
    }


def _cloudwatch_page(*, group, unit, price):
    return {
        "PriceList": [
            _doc(
                {"servicename": "Amazon CloudWatch",
                 "usagetype": group, "regionCode": "us-east-1",
                 "group": group},
                {"OnDemand": {"sku.cw": {"priceDimensions": {
                    "sku.cw.rate": {
                        "unit": unit, "beginRange": "0",
                        "endRange": "Inf",
                        "description": "cloudwatch log sku",
                        "pricePerUnit": {"USD": price}}}}}},
            )
        ]
    }


def _apigw_page(*, operation, usagetype, description,
                 t1="0.0000035000", t2="0.0000028000"):
    """Two-tier API Gateway request SKU doc mirroring the live shape."""
    return {
        "PriceList": [
            _doc(
                {"servicename": "Amazon API Gateway",
                 "usagetype": usagetype, "regionCode": "us-east-1",
                 "operation": operation, "description": description},
                {"OnDemand": {"sku.agw": {"priceDimensions": {
                    "sku.agw.t1": {
                        "unit": "Requests", "beginRange": "0",
                        "endRange": "333000000",
                        "description": "first 333 million requests",
                        "pricePerUnit": {"USD": t1}},
                    "sku.agw.t2": {
                        "unit": "Requests", "beginRange": "333000000",
                        "endRange": "1000000000",
                        "description": "next 667 million requests",
                        "pricePerUnit": {"USD": t2}}}}}},
            )
        ]
    }


def _apigw_rest_page():
    return _apigw_page(
        operation="ApiGatewayRequest",
        usagetype="USE1-ApiGatewayRequest",
        description="API calls received",
    )


def _apigw_http_page():
    return _apigw_page(
        operation="ApiGatewayHttpApi",
        usagetype="USE1-ApiGatewayHttpRequest",
        description="HTTP API Requests",
        t1="0.0000010000",
        t2="0.0000009000",
    )


def _filter_value(call, field):
    for f in call.get("Filters", []):
        if f.get("Field") == field:
            return f.get("Value")
    return None


class LookupFilterRegressionTests(unittest.TestCase):
    """S3/APIGW Query-API filters, live-verified during the forensic audit.

    us-east-1 S3 uses bare "TimedStorage-ByteHrs" (the USE1- prefixed form
    matches nothing); ap-south-1 uses the prefixed form. API Gateway REST
    vs HTTP is discriminated by operation (ApiGatewayRequest vs
    ApiGatewayHttpApi) — the old description="REST API" filter matched
    nothing because the REST SKU's description is "API calls received".
    """

    def test_s3_useast1_uses_bare_usagetype(self):
        from ai_architect.agents.component_selection.models import ComponentCandidate
        comp = ComponentCandidate(
            name="Amazon S3", provider="aws",
            category="storage", role="r", rationale="r",
            confidence="high", evidence_support="strong")
        client = FakePricingClient([_s3_storage_page(
            usagetype="TimedStorage-ByteHrs", region_code="us-east-1")])
        svc = PricingService(client=client)
        components, _ = svc.price_components(
            _selection([comp]), "us-east-1")
        self.assertEqual(len(components), 1)
        self.assertIsNone(components[0].unavailable_reason)
        self.assertEqual(
            [f.usage_key for f in components[0].facts], ["storage_gbmo"])
        # Storage first, then the two request classes. Only the storage
        # page was served, so the request classes yield no facts here.
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(
            _filter_value(client.calls[0], "usagetype"), "TimedStorage-ByteHrs")
        self.assertEqual(
            _filter_value(client.calls[0], "regionCode"), "us-east-1")
        self.assertEqual(
            _filter_value(client.calls[1], "productFamily"), "API Request")
        self.assertEqual(
            _filter_value(client.calls[1], "group"), "S3-API-Tier1")
        self.assertEqual(
            _filter_value(client.calls[2], "group"), "S3-API-Tier2")

    def test_s3_apsouth1_keeps_prefixed_usagetype(self):
        from ai_architect.agents.component_selection.models import ComponentCandidate
        comp = ComponentCandidate(
            name="Amazon Simple Storage Service", provider="aws",
            category="storage", role="r", rationale="r",
            confidence="high", evidence_support="strong")
        client = FakePricingClient([_s3_storage_page(
            usagetype="APS3-TimedStorage-ByteHrs",
            region_code="ap-south-1")])
        svc = PricingService(client=client)
        components, _ = svc.price_components(
            _selection([comp]), "ap-south-1")
        self.assertEqual(len(components), 1)
        self.assertIsNone(components[0].unavailable_reason)
        self.assertEqual(
            [f.usage_key for f in components[0].facts], ["storage_gbmo"])
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(
            _filter_value(client.calls[0], "usagetype"),
            "APS3-TimedStorage-ByteHrs")
        self.assertEqual(
            _filter_value(client.calls[0], "regionCode"), "ap-south-1")

    def test_apigw_rest_selected_by_operation_without_description(self):
        from ai_architect.agents.component_selection.models import ComponentCandidate
        comp = ComponentCandidate(
            name="Amazon API Gateway", provider="aws",
            category="networking", role="REST API front door",
            rationale="r",
            confidence="high", evidence_support="strong")
        client = FakePricingClient([_apigw_rest_page()])
        svc = PricingService(client=client)
        components, warnings = svc.price_components(
            _selection([comp]), "us-east-1")
        self.assertEqual(len(components), 1)
        self.assertIsNone(components[0].unavailable_reason)
        self.assertEqual(
            [f.usage_key for f in components[0].facts], ["rest_requests"])
        # Exactly one lookup (mutually exclusive variant); it must not
        # filter by description, which is what zeroed the old queries.
        self.assertEqual(len(client.calls), 1)
        self.assertIsNone(_filter_value(client.calls[0], "description"))
        self.assertEqual(
            _filter_value(client.calls[0], "operation"), "ApiGatewayRequest")
        self.assertFalse(any("REST" in w and "default" in w for w in warnings))
        # Tiered REST pricing survives normalization untouched.
        tiers = components[0].facts[0].tiers
        self.assertEqual(len(tiers), 2)
        self.assertAlmostEqual(tiers[0].unit_price_usd, 0.0000035)
        self.assertAlmostEqual(tiers[1].unit_price_usd, 0.0000028)

    def test_apigw_http_selected_by_operation(self):
        from ai_architect.agents.component_selection.models import ComponentCandidate
        comp = ComponentCandidate(
            name="Amazon API Gateway", provider="aws",
            category="networking", role="front-door HTTP API",
            rationale="r",
            confidence="high", evidence_support="strong")
        client = FakePricingClient([_apigw_http_page()])
        svc = PricingService(client=client)
        components, _ = svc.price_components(
            _selection([comp]), "us-east-1")
        self.assertEqual(len(components), 1)
        self.assertIsNone(components[0].unavailable_reason)
        self.assertEqual(
            [f.usage_key for f in components[0].facts], ["http_requests"])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            _filter_value(client.calls[0], "operation"), "ApiGatewayHttpApi")

    def test_apigw_empty_results_stay_unpriced(self):
        from ai_architect.agents.component_selection.models import ComponentCandidate
        comp = ComponentCandidate(
            name="Amazon API Gateway", provider="aws",
            category="networking", role="REST API front door",
            rationale="r",
            confidence="high", evidence_support="strong")
        svc = PricingService(
            client=FakePricingClient([{"PriceList": []}]))
        components, _ = svc.price_components(
            _selection([comp]), "us-east-1")
        self.assertEqual(len(components), 1)
        self.assertEqual(components[0].facts, [])
        self.assertIsNotNone(components[0].unavailable_reason)
        self.assertIn("no usable SKU", components[0].unavailable_reason)


class NewDimensionSkuMappingTests(unittest.TestCase):
    """S3 request classes and CloudWatch log SKUs map to distinct facts."""

    def test_s3_request_classes_are_distinct_skus(self):
        from ai_architect.agents.component_selection.models import ComponentCandidate
        comp = ComponentCandidate(
            name="Amazon S3", provider="aws",
            category="storage", role="object store", rationale="r",
            confidence="high", evidence_support="strong")
        client = FakePricingClient([
            _s3_storage_page(
                usagetype="TimedStorage-ByteHrs", region_code="us-east-1"),
            _s3_request_page(group="S3-API-Tier1", price="0.0000050000"),
            _s3_request_page(group="S3-API-Tier2", price="0.0000004000"),
        ])
        svc = PricingService(client=client)
        components, warnings = svc.price_components(
            _selection([comp]), "us-east-1")
        self.assertEqual(warnings, [])
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(
            _filter_value(client.calls[1], "group"), "S3-API-Tier1")
        self.assertEqual(
            _filter_value(client.calls[2], "group"), "S3-API-Tier2")
        facts = {f.usage_key: f for f in components[0].facts}
        self.assertEqual(
            sorted(facts),
            ["s3_get_requests", "s3_put_requests", "storage_gbmo"])
        self.assertAlmostEqual(
            facts["s3_put_requests"].tiers[0].unit_price_usd,
            0.000005, places=12)
        self.assertAlmostEqual(
            facts["s3_get_requests"].tiers[0].unit_price_usd,
            0.0000004, places=12)
        self.assertEqual(facts["s3_put_requests"].tiers[0].unit, "Requests")

    def test_cloudwatch_log_skus_map_ingestion_and_storage(self):
        from ai_architect.agents.component_selection.models import ComponentCandidate
        comp = ComponentCandidate(
            name="Amazon CloudWatch", provider="aws",
            category="monitoring", role="logs and metrics", rationale="r",
            confidence="high", evidence_support="strong")
        client = FakePricingClient([
            _cloudwatch_page(
                group="CW:DataProcessing", unit="GB", price="0.5000000000"),
            _cloudwatch_page(
                group="CW:Storage", unit="GB-Mo", price="0.0300000000"),
        ])
        svc = PricingService(client=client)
        components, warnings = svc.price_components(
            _selection([comp]), "us-east-1")
        self.assertEqual(warnings, [])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(
            _filter_value(client.calls[0], "group"), "CW:DataProcessing")
        self.assertEqual(
            _filter_value(client.calls[1], "group"), "CW:Storage")
        self.assertEqual(
            sorted(f.usage_key for f in components[0].facts),
            ["log_data_ingested_gb", "log_storage_gbmo"])

    def test_cloudwatch_without_retrieved_skus_stays_unpriced(self):
        from ai_architect.agents.component_selection.models import ComponentCandidate
        comp = ComponentCandidate(
            name="Amazon CloudWatch", provider="aws",
            category="monitoring", role="logs and metrics", rationale="r",
            confidence="high", evidence_support="strong")
        svc = PricingService(client=FakePricingClient([]))
        components, _ = svc.price_components(
            _selection([comp]), "us-east-1")
        self.assertEqual(components[0].facts, [])
        self.assertIsNotNone(components[0].unavailable_reason)
        self.assertIn("no usable SKU", components[0].unavailable_reason)


def _apigw_comp(role, name="Amazon API Gateway"):
    from ai_architect.agents.component_selection.models import ComponentCandidate
    return ComponentCandidate(
        name=name, provider="aws",
        category="networking", role=role, rationale="r",
        confidence="high", evidence_support="strong")


class ApigatewayVariantTests(unittest.TestCase):
    """REST vs HTTP are mutually exclusive: one component prices exactly
    one variant. Picker reads name + role text only (SQS precedent)."""

    def test_ambiguous_role_defaults_to_rest_with_warning(self):
        client = FakePricingClient([_apigw_rest_page()])
        svc = PricingService(client=client)
        components, warnings = svc.price_components(
            _selection([_apigw_comp("front door for short URLs")]),
            "us-east-1")
        self.assertIsNone(components[0].unavailable_reason)
        self.assertEqual(
            [f.usage_key for f in components[0].facts], ["rest_requests"])
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(
            any("REST" in w and "default" in w for w in warnings),
            msg=f"expected REST-default warning, got: {warnings}")

    def test_https_wording_does_not_classify_as_http_api(self):
        # "https" is a protocol word, not an API-type signal.
        client = FakePricingClient([_apigw_rest_page()])
        svc = PricingService(client=client)
        components, warnings = svc.price_components(
            _selection([_apigw_comp("front-door HTTPS endpoint")]),
            "us-east-1")
        self.assertIsNone(components[0].unavailable_reason)
        self.assertEqual(
            [f.usage_key for f in components[0].facts], ["rest_requests"])
        self.assertEqual(
            _filter_value(client.calls[0], "operation"), "ApiGatewayRequest")
        self.assertTrue(
            any("default" in w for w in warnings),
            msg=f"expected default warning, got: {warnings}")

    def test_websocket_role_is_unsupported_not_folded(self):
        client = FakePricingClient([])
        svc = PricingService(client=client)
        components, _ = svc.price_components(
            _selection([_apigw_comp("WebSocket API for realtime updates")]),
            "us-east-1")
        self.assertEqual(components[0].facts, [])
        self.assertIsNotNone(components[0].unavailable_reason)
        self.assertIn("WebSocket", components[0].unavailable_reason)
        # Short-circuits before any Price List query.
        self.assertEqual(client.calls, [])

    def test_websocket_name_is_unsupported_not_folded(self):
        # An unknown websocket-flavoured name matches no spec at all, so
        # it stays generically unpriced: still never folded into REST/HTTP.
        client = FakePricingClient([])
        svc = PricingService(client=client)
        components, _ = svc.price_components(
            _selection([_apigw_comp("realtime fan-out",
                                   name="API Gateway WebSocket")]),
            "us-east-1")
        self.assertEqual(components[0].facts, [])
        self.assertIsNotNone(components[0].unavailable_reason)
        self.assertEqual(client.calls, [])

    def test_session_role_prices_http_variant(self):
        # Exact role shape from the URL-shortener smoke session.
        client = FakePricingClient([_apigw_http_page()])
        svc = PricingService(client=client)
        components, warnings = svc.price_components(
            _selection([_apigw_comp(
                "front-door HTTP API for creating short URLs")]),
            "us-east-1")
        self.assertIsNone(components[0].unavailable_reason)
        self.assertEqual(
            [f.usage_key for f in components[0].facts], ["http_requests"])
        self.assertFalse(any("default" in w for w in warnings))

    def test_single_line_per_component_with_unchanged_quantity(self):
        from ai_architect.agents.capacity.models import CapacityModelProposal
        from ai_architect.agents.capacity.result import CapacityModelingResult
        from ai_architect.domain.capacity.models import CapacityModelResult
        from ai_architect.domain.cost.models import PricedComponent
        from ai_architect.services.cost.calculator import (
            CostCalculator,
            SECONDS_PER_MONTH,
        )

        capacity = CapacityModelingResult(
            proposal=CapacityModelProposal(
                daily_active_users=1000,
                messages_per_user_per_day=10.0,
                peak_factor=2.0,
                read_write_ratio=0.9,
                average_message_size_bytes=150,
                connection_factor=1.25,
                storage_retention_days=365,
                rationale={},
            ),
            calculation=CapacityModelResult(
                peak_write_qps=2000.0,
                peak_read_qps=2000.0,
                concurrent_connections=2500.0,
                storage_growth_bytes_per_day=1500000.0,
                hot_working_set_bytes=547500000.0,
                egress_bytes_per_second=300000.0,
                formula_version="v1",
                calculation_trace={},
            ),
        )
        rest_comps, _ = PricingService(
            client=FakePricingClient([_apigw_rest_page()])
        ).price_components(
            _selection([_apigw_comp("REST API front door")]), "us-east-1")
        http_comps, _ = PricingService(
            client=FakePricingClient([_apigw_http_page()])
        ).price_components(
            _selection([_apigw_comp("front-door HTTP API")]), "us-east-1")

        calc = CostCalculator()
        rest_result = calc.calculate(
            capacity, _selection([_apigw_comp("REST API front door")]),
            [PricedComponent(
                component_name="Amazon API Gateway",
                category="networking",
                service_code="AmazonApiGateway",
                facts=rest_comps[0].facts,
            )],
            "us-east-1",
        )
        http_result = calc.calculate(
            capacity, _selection([_apigw_comp("front-door HTTP API")]),
            [PricedComponent(
                component_name="Amazon API Gateway",
                category="networking",
                service_code="AmazonApiGateway",
                facts=http_comps[0].facts,
            )],
            "us-east-1",
        )
        # Exactly one line each; only the variant (rate) changes.
        self.assertEqual(len(rest_result.lines), 1)
        self.assertEqual(len(http_result.lines), 1)
        expected_usage = (1000.0 + 1000.0) * SECONDS_PER_MONTH
        self.assertAlmostEqual(
            rest_result.lines[0].monthly_usage, expected_usage)
        self.assertAlmostEqual(
            http_result.lines[0].monthly_usage, expected_usage)
        # 5.184B requests land in tier 2 of each fixture's tiers, so
        # the displayed price is the matched tier's rate per variant.
        self.assertAlmostEqual(rest_result.lines[0].unit_price_usd, 0.0000028)
        self.assertAlmostEqual(http_result.lines[0].unit_price_usd, 0.0000009)

    def test_dynamodb_and_cloudfront_specs_unchanged(self):
        # Guard: the variant rework must not reshape dimension-based specs.
        self.assertNotIn("variants", ps.SERVICE_SPECS["amazon dynamodb"])
        self.assertNotIn("variants", ps.SERVICE_SPECS["amazon cloudfront"])
        self.assertIn("variants", ps.SERVICE_SPECS["amazon api gateway"])
        self.assertEqual(
            ps.SERVICE_SPECS["amazon api gateway"]["default_variant"],
            "rest")

    def test_sqs_variant_behavior_preserved(self):
        # Deliberate contract update (cost-accounting milestone): SQS
        # facts are now stamped with the spec-level usage key
        # "requests". Previously the raw usagetype ("APS3-Requests-
        # Tier1") leaked into the usage-key slot, so the calculator had
        # no derivation rule and SQS stayed permanently unpriced even
        # though its SKU was fetched fine. The variant machinery itself
        # (group + queueType filters, single query) is unchanged.
        from ai_architect.agents.component_selection.models import ComponentCandidate
        comp = ComponentCandidate(
            name="Amazon Simple Queue Service (SQS)", provider="aws",
            category="messaging", role="q", rationale="r",
            confidence="high", evidence_support="strong")
        client = FakePricingClient([_sqs_page()])
        svc = PricingService(client=client)
        components, _ = svc.price_components(
            _selection([comp]), "ap-south-1")
        self.assertIsNone(components[0].unavailable_reason)
        self.assertEqual(len(components[0].facts), 1)
        self.assertEqual(components[0].facts[0].usage_key, "requests")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            _filter_value(client.calls[0], "group"), "SQS-APIRequest-Tier1")
        self.assertEqual(
            _filter_value(client.calls[0], "queueType"), "Standard")

    def test_sqs_alias_name_maps_to_spec(self):
        # "Amazon SQS (Standard)" (the live-selection shape) normalizes
        # to "amazon sqs", which the "sqs" alias key resolves via the
        # "amazon " prefix strip in _match_spec.
        from ai_architect.agents.component_selection.models import ComponentCandidate
        comp = ComponentCandidate(
            name="Amazon SQS (Standard)", provider="aws",
            category="messaging", role="queued delivery", rationale="r",
            confidence="high", evidence_support="strong")
        client = FakePricingClient([_sqs_page()])
        svc = PricingService(client=client)
        components, _ = svc.price_components(
            _selection([comp]), "us-east-1")
        self.assertIsNone(components[0].unavailable_reason)
        self.assertEqual(components[0].service_code, "AWSQueueService")
        self.assertEqual(
            [f.usage_key for f in components[0].facts], ["requests"])


class DescriptiveLiveNameResolutionTests(unittest.TestCase):
    """The recorded live selection's descriptive names resolve exactly.

    Every name below comes from the audited five-component design run.
    Four services must price; the IAM execution role has no Price List
    mapping and must stay explicitly unpriced rather than inventing a
    cost.
    """

    def _components(self):
        from ai_architect.agents.component_selection.models import (
            ComponentCandidate,
        )
        return [
            ComponentCandidate(
                name="Amazon API Gateway HTTP API", provider="aws",
                category="networking", role="HTTP ingress", rationale="r",
                confidence="high", evidence_support="strong"),
            ComponentCandidate(
                name="AWS Lambda Function (Python\u202f3.12)", provider="aws",
                category="compute", role="business logic", rationale="r",
                confidence="high", evidence_support="strong"),
            ComponentCandidate(
                name=(
                    "Amazon DynamoDB Table "
                    "(On\u2011Demand billing)"
                ),
                provider="aws", category="database", role="notes store",
                rationale="r", confidence="high",
                evidence_support="strong"),
            ComponentCandidate(
                name=(
                    "Amazon CloudWatch Log Group "
                    "(for Lambda & API Gateway)"
                ),
                provider="aws", category="monitoring", role="logs",
                rationale="r", confidence="high",
                evidence_support="strong"),
            ComponentCandidate(
                name="IAM Role for Lambda Execution", provider="aws",
                category="security", role="execution role", rationale="r",
                confidence="high", evidence_support="strong"),
        ]

    def test_all_descriptive_service_names_resolve_and_iam_stays_unpriced(
        self,
    ):
        client = FakePricingClient([
            _apigw_http_page(),
            _lambda_requests_page(), _lambda_gb_second_page(),
            _ddb_write_page(), _ddb_read_page(), _ddb_storage_page(),
            _cloudwatch_page(
                group="CW:DataProcessing", unit="GB",
                price="0.5000000000"),
            _cloudwatch_page(
                group="CW:Storage", unit="GB-Mo",
                price="0.0300000000"),
        ])
        svc = PricingService(client=client)
        components, _ = svc.price_components(
            _selection(self._components()), "us-east-1")
        by_name = {c.component_name: c for c in components}

        api = by_name["Amazon API Gateway HTTP API"]
        self.assertIsNone(api.unavailable_reason)
        self.assertEqual(api.service_code, "AmazonApiGateway")
        self.assertEqual(
            [f.usage_key for f in api.facts], ["http_requests"])

        lam = by_name["AWS Lambda Function (Python\u202f3.12)"]
        self.assertIsNone(lam.unavailable_reason)
        self.assertEqual(lam.service_code, "AWSLambda")
        self.assertEqual(
            sorted(f.usage_key for f in lam.facts),
            ["gb_second", "requests"])

        table = by_name[
            "Amazon DynamoDB Table (On\u2011Demand billing)"
        ]
        self.assertIsNone(table.unavailable_reason)
        self.assertEqual(table.service_code, "AmazonDynamoDB")
        self.assertEqual(
            sorted(f.usage_key for f in table.facts),
            ["rru", "storage_gbmo", "wru"])

        logs = by_name[
            "Amazon CloudWatch Log Group (for Lambda & API Gateway)"
        ]
        self.assertIsNone(logs.unavailable_reason)
        self.assertEqual(logs.service_code, "AmazonCloudWatch")
        self.assertEqual(
            sorted(f.usage_key for f in logs.facts),
            ["log_data_ingested_gb", "log_storage_gbmo"])

        iam = by_name["IAM Role for Lambda Execution"]
        self.assertIsNotNone(iam.unavailable_reason)
        self.assertEqual(iam.facts, [])
        self.assertEqual(len(client.calls), 8)


if __name__ == "__main__":
    unittest.main()
