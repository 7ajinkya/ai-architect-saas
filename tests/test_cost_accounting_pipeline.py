"""Cross-module cost-accounting seam tests (messaging acceptance scenario).

Regression lock for the cost-accounting milestone: exercises the real
PricingService specs/prefix logic, CostCalculator derivations,
build_cost_links aggregation, and evaluate_quality_gate verdicts in one
deterministic pass, with the AWS Price List boundary faked at
get_products (no network, no LLM, no retries). Every recorded query
filter is asserted, so a regression in any module moves pricing from
"priced" to "unknown" (or back) visibly instead of silently.

Scenario: the five-component messaging architecture from the audited
live design run, us-east-1:

  Amazon API Gateway (WebSocket/API)  WebSocket: stays explicitly unpriced
  AWS Lambda                          requests priced; gb_second explicitly unpriced
  Amazon SQS (Standard)               requests priced (usage key stamped "requests")
  Amazon SNS                          requests priced (usage key stamped "requests")
  Amazon DynamoDB (On-Demand)         wru/rru/storage priced; no phantom
                                      provisioned entries

Recorded pre-fix red state these tests locked down before the slice
fixes landed: Lambda queried unverifiable usagetype forms and ended
unpriced; "amazon sqs" matched no spec; SNS had no spec; DynamoDB
fetched committed-throughput SKUs as phantom wcu_reserved/rcu_reserved
dimensions; and evaluate_quality_gate could not see mixed priced/
unpriced dimensions (Lambda) because it read only cost links.

Run: poetry run python -m unittest discover tests
"""
import json
import unittest
from types import SimpleNamespace

from ai_architect.agents.capacity.models import CapacityModelProposal
from ai_architect.agents.capacity.result import CapacityModelingResult
from ai_architect.agents.component_selection.models import (
    ComponentCandidate,
    ComponentSelectionResult,
)
from ai_architect.domain.architecture.critique import (
    DesignCritique,
    QualityGateOutcome,
)
from ai_architect.services.architecture.core import build_cost_links
from ai_architect.services.architecture.quality_gate import (
    evaluate_quality_gate,
)
from ai_architect.services.capacity.calculator import (
    CapacityCalculator as CapCalc,
)
from ai_architect.services.cost.calculator import CostCalculator
from ai_architect.services.cost.pricing_service import PricingService

REGION = "us-east-1"


def _doc(attrs, terms):
    return json.dumps({"product": {"attributes": attrs}, "terms": terms})


def _sku_doc(attrs, *, unit, price, description):
    return _doc(
        attrs,
        {"OnDemand": {"sku.1": {"priceDimensions": {"sku.1.rate": {
            "unit": unit,
            "beginRange": "0",
            "endRange": "Inf",
            "description": description,
            "pricePerUnit": {"USD": price},
        }}}}},
    )


def _lambda_request_sku():
    # Live-verified us-east-1 form: bare "Request" (the old
    # "{prefix}-Lambda-Requests" template matched nothing).
    return _sku_doc(
        {
            "servicename": "AWS Lambda",
            "usagetype": "Request",
            "regionCode": REGION,
            "group": "AWS-Lambda-Requests",
        },
        unit="Requests",
        price="0.0000002000",
        description="$0.20 per 1 million requests",
    )


def _lambda_gb_second_sku():
    # Live-verified us-east-1 form: bare "Lambda-GB-Second".
    return _sku_doc(
        {
            "servicename": "AWS Lambda",
            "usagetype": "Lambda-GB-Second",
            "regionCode": REGION,
        },
        unit="GB-second",
        price="0.0000166667",
        description="$0.0000166667 per GB-second",
    )


def _sqs_sku():
    # Live-verified: group SQS-APIRequest-Tier1 + queueType Standard
    # resolves to usagetype Requests-RBP in us-east-1.
    return _sku_doc(
        {
            "servicename": "Amazon Simple Queue Service",
            "usagetype": "Requests-RBP",
            "regionCode": REGION,
            "group": "SQS-APIRequest-Tier1",
            "queueType": "Standard",
        },
        unit="Requests",
        price="0.0000004000",
        description="$0.40 per million requests",
    )


def _sns_sku():
    # Live-verified: group SNS-Requests-Tier1 (usagetype Requests-Tier1).
    return _sku_doc(
        {
            "servicename": "Amazon Simple Notification Service",
            "usagetype": "Requests-Tier1",
            "regionCode": REGION,
            "group": "SNS-Requests-Tier1",
        },
        unit="Requests",
        price="0.0000005000",
        description="$0.50 per million requests",
    )


def _ddb_on_demand_sku(*, usagetype, group, unit, price):
    return _sku_doc(
        {
            "servicename": "Amazon DynamoDB",
            "usagetype": usagetype,
            "regionCode": REGION,
            "group": group,
            "operation": "PayPerRequestThroughput",
        },
        unit=unit,
        price=price,
        description="on-demand request unit",
    )


def _ddb_committed_sku(*, usagetype, group, unit, price):
    return _sku_doc(
        {
            "servicename": "Amazon DynamoDB",
            "usagetype": usagetype,
            "regionCode": REGION,
            "group": group,
            "operation": "CommittedThroughput",
        },
        unit=unit,
        price=price,
        description="provisioned committed throughput",
    )


def _ddb_storage_sku():
    # Table storage is billed independently of the request capacity
    # model: no operation attribute, group DDB-Storage.
    return _sku_doc(
        {
            "servicename": "Amazon DynamoDB",
            "usagetype": "TimedStorage-ByteHrs",
            "regionCode": REGION,
            "group": "DDB-Storage",
        },
        unit="GB-Mo",
        price="0.2500000000",
        description="$0.25 per GB-month",
    )


class FakePriceListClient:
    """boto3-shape fake at the get_products boundary.

    Records every (ServiceCode, Filters) call and answers only the
    live-verified query forms. A regression to a wrong filter therefore
    yields an empty page and fails the assertions below instead of
    being silently accepted (the filters under test are never bypassed).
    """

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def get_products(self, **kwargs):
        filters = {
            f.get("Field"): f.get("Value")
            for f in kwargs.get("Filters", [])
        }
        service = kwargs.get("ServiceCode", "")
        self.calls.append((service, filters))
        if service == "AWSLambda" and filters.get("regionCode") == REGION:
            if filters.get("usagetype") == "Request":
                return {"PriceList": [_lambda_request_sku()]}
            if filters.get("usagetype") == "Lambda-GB-Second":
                return {"PriceList": [_lambda_gb_second_sku()]}
        if (
            service == "AWSQueueService"
            and filters.get("group") == "SQS-APIRequest-Tier1"
            and filters.get("queueType") == "Standard"
        ):
            return {"PriceList": [_sqs_sku()]}
        if (
            service == "AmazonSNS"
            and filters.get("group") == "SNS-Requests-Tier1"
        ):
            return {"PriceList": [_sns_sku()]}
        if service == "AmazonDynamoDB":
            if filters.get("group") == "DDB-Storage":
                return {"PriceList": [_ddb_storage_sku()]}
            operation = filters.get("operation")
            if operation == "PayPerRequestThroughput":
                if filters.get("group") == "DDB-WriteUnits":
                    return {"PriceList": [_ddb_on_demand_sku(
                        usagetype="WriteRequestUnits",
                        group="DDB-WriteUnits",
                        unit="WriteRequestUnits",
                        price="0.0000006250",
                    )]}
                if filters.get("group") == "DDB-ReadUnits":
                    return {"PriceList": [_ddb_on_demand_sku(
                        usagetype="ReadRequestUnits",
                        group="DDB-ReadUnits",
                        unit="ReadRequestUnits",
                        price="0.0000001250",
                    )]}
            if operation == "CommittedThroughput":
                # Served faithfully so a regression that fetches these
                # SKUs as on-demand still reproduces the phantom
                # wcu_reserved/rcu_reserved dimensions.
                if filters.get("group") == "DDB-WriteUnits":
                    return {"PriceList": [_ddb_committed_sku(
                        usagetype="WriteCapacityUnit-Hrs",
                        group="DDB-WriteUnits",
                        unit="WriteCapacityUnit-Hrs",
                        price="0.0001460000",
                    )]}
                if filters.get("group") == "DDB-ReadUnits":
                    return {"PriceList": [_ddb_committed_sku(
                        usagetype="ReadCapacityUnit-Hrs",
                        group="DDB-ReadUnits",
                        unit="ReadCapacityUnit-Hrs",
                        price="0.0000292000",
                    )]}
        return {"PriceList": []}


def _candidate(name: str, category: str, role: str) -> ComponentCandidate:
    return ComponentCandidate(
        name=name, provider="aws", category=category, role=role,
        rationale="r", confidence="high", evidence_support="strong")


def _selection() -> ComponentSelectionResult:
    # Exact component names/categories from the audited live design run.
    # The DynamoDB name carries a U+2011 non-breaking hyphen, as recorded.
    return ComponentSelectionResult(selected_components=[
        _candidate(
            "Amazon API Gateway (WebSocket/API)", "hosting",
            "exposes HTTP & WebSocket endpoints for client apps"),
        _candidate("AWS Lambda", "compute", "serverless backend logic"),
        _candidate(
            "Amazon SQS (Standard)", "messaging",
            "queue for asynchronous message processing"),
        _candidate(
            "Amazon SNS", "messaging",
            "fan-out notifications to subscribers"),
        _candidate(
            "Amazon DynamoDB (On\u2011Demand)", "database",
            "key-value store for application state and sessions"),
    ])


def _capacity() -> CapacityModelingResult:
    proposal = CapacityModelProposal(
        daily_active_users=10000,
        messages_per_user_per_day=40.0,
        peak_factor=2.0,
        read_write_ratio=4.0,
        average_message_size_bytes=1024,
        connection_factor=0.15,
        storage_retention_days=30,
        rationale={},
    )
    return CapacityModelingResult(
        proposal=proposal, calculation=CapCalc().calculate(proposal))


def _critique(reviewed: str = "artifact-1") -> DesignCritique:
    return DesignCritique(
        artifact_id="critique-1",
        session_id="design-s1",
        reviewed_artifact_id=reviewed,
        reviewed_version=1,
        findings=[],
        summary="summary",
    )


def _pipeline():
    """One deterministic pass through the whole seam, fake Price List."""
    client = FakePriceListClient()
    selection = _selection()
    priced, _warnings = PricingService(client=client).price_components(
        selection, REGION)
    result = CostCalculator().calculate(
        _capacity(), selection, priced, REGION)
    return client, selection, priced, result


def _arch(selection, result) -> SimpleNamespace:
    return SimpleNamespace(
        artifact_id="artifact-1",
        cost_totals_snapshot=result.totals_usd_per_month,
        cost_links=build_cost_links(selection, result),
    )


class CostAccountingPipelineTests(unittest.TestCase):
    def test_capacity_anchor_matches_audited_run(self):
        calc = _capacity().calculation
        self.assertAlmostEqual(calc.peak_write_qps, 9.259259, places=4)
        self.assertAlmostEqual(calc.peak_read_qps, 37.037037, places=4)
        self.assertAlmostEqual(calc.concurrent_connections, 1500.0)

    def test_price_list_queries_use_live_verified_filter_forms(self):
        client, _, _, _ = _pipeline()

        def filters_for(service_code):
            return [
                filters for service, filters in client.calls
                if service == service_code
            ]

        lambda_filters = filters_for("AWSLambda")
        self.assertEqual(len(lambda_filters), 2)
        self.assertEqual(
            sorted(f.get("usagetype") for f in lambda_filters),
            ["Lambda-GB-Second", "Request"],
        )
        for filters in lambda_filters:
            self.assertEqual(filters.get("regionCode"), "us-east-1")

        sqs_filters = filters_for("AWSQueueService")
        self.assertEqual(len(sqs_filters), 1)
        self.assertEqual(sqs_filters[0], {
            "group": "SQS-APIRequest-Tier1",
            "queueType": "Standard",
            "regionCode": "us-east-1",
        })

        sns_filters = filters_for("AmazonSNS")
        self.assertEqual(len(sns_filters), 1)
        self.assertEqual(sns_filters[0], {
            "group": "SNS-Requests-Tier1",
            "regionCode": "us-east-1",
        })

        ddb_filters = filters_for("AmazonDynamoDB")
        self.assertEqual(len(ddb_filters), 3)
        by_group = {f.get("group"): f for f in ddb_filters}
        self.assertEqual(
            sorted(by_group),
            ["DDB-ReadUnits", "DDB-Storage", "DDB-WriteUnits"],
        )
        for group in ("DDB-ReadUnits", "DDB-WriteUnits"):
            self.assertEqual(
                by_group[group].get("operation"),
                "PayPerRequestThroughput",
            )
            self.assertEqual(
                by_group[group].get("regionCode"), "us-east-1")
        # Storage is model-independent: queried for a pay-per-request
        # table too, with no operation filter.
        self.assertNotIn("operation", by_group["DDB-Storage"])

        # The WebSocket component must not trigger any Price List lookup.
        self.assertEqual(filters_for("AmazonApiGateway"), [])
        self.assertEqual(len(client.calls), 7)

    def test_lambda_facts_carry_both_live_usagetype_dimensions(self):
        _, _, priced, _ = _pipeline()
        lam = next(p for p in priced if p.component_name == "AWS Lambda")
        self.assertIsNone(lam.unavailable_reason)
        self.assertEqual(
            sorted(f.usage_key for f in lam.facts),
            ["gb_second", "requests"],
        )

    def test_sqs_and_sns_facts_are_stamped_with_the_requests_usage_key(self):
        _, _, priced, _ = _pipeline()
        by_name = {p.component_name: p for p in priced}
        sqs = by_name["Amazon SQS (Standard)"]
        self.assertIsNone(sqs.unavailable_reason)
        self.assertEqual([f.usage_key for f in sqs.facts], ["requests"])
        sns = by_name["Amazon SNS"]
        self.assertIsNone(sns.unavailable_reason)
        self.assertEqual([f.usage_key for f in sns.facts], ["requests"])

    def test_dynamodb_on_demand_facts_have_no_phantom_provisioned_dimensions(
        self,
    ):
        _, _, priced, _ = _pipeline()
        ddb = next(
            p for p in priced if p.component_name.startswith("Amazon DynamoDB")
        )
        self.assertEqual(
            sorted(f.usage_key for f in ddb.facts),
            ["rru", "storage_gbmo", "wru"],
        )

    def test_calculator_lines_totals_and_unpriced_entries(self):
        _, _, _, result = _pipeline()
        by_component: dict[str, list] = {}
        for line in result.lines:
            by_component.setdefault(line.component_name, []).append(line)
        self.assertEqual(len(result.lines), 6)
        self.assertEqual(len(by_component["AWS Lambda"]), 1)
        self.assertEqual(by_component["AWS Lambda"][0].unit, "Requests")
        self.assertEqual(len(by_component["Amazon SQS (Standard)"]), 1)
        self.assertEqual(len(by_component["Amazon SNS"]), 1)
        self.assertEqual(
            len(by_component["Amazon DynamoDB (On\u2011Demand)"]), 3)
        self.assertAlmostEqual(
            result.totals_usd_per_month["on_demand"], 82.572, places=3)
        self.assertFalse(result.fully_priced)

        self.assertEqual(len(result.unpriced), 2)
        self.assertEqual(
            sorted(u.component_name for u in result.unpriced),
            ["AWS Lambda", "Amazon API Gateway (WebSocket/API)"],
        )
        lambda_entry = next(
            u for u in result.unpriced if u.component_name == "AWS Lambda")
        self.assertIn("gb_second", lambda_entry.reason)
        apigw_entry = next(
            u for u in result.unpriced
            if u.component_name.startswith("Amazon API Gateway"))
        self.assertIn("WebSocket", apigw_entry.reason)

    def test_dynamodb_storage_is_priced_from_retrieved_sku(self):
        """DDB table storage is priced from the independent storage SKU
        through the existing deterministic growth rule."""
        _, _, priced, result = _pipeline()
        ddb_name = "Amazon DynamoDB (On\u2011Demand)"
        ddb = next(p for p in priced if p.component_name == ddb_name)
        self.assertIn(
            "storage_gbmo", {f.usage_key for f in ddb.facts})
        entries = [
            e for e in result.coverage if e.component_name == ddb_name
        ]
        by_dimension = {e.dimension: e for e in entries}
        self.assertEqual(
            sorted(by_dimension),
            ["rcu_reserved", "rru", "storage_gbmo", "wcu_reserved", "wru"],
        )
        self.assertEqual(by_dimension["wru"].status, "priced")
        self.assertEqual(by_dimension["rru"].status, "priced")
        self.assertEqual(by_dimension["storage_gbmo"].status, "priced")
        storage_line = next(
            line for line in result.lines
            if line.component_name == ddb_name and line.unit == "GB-Mo"
        )
        expected_gb = (
            _capacity().calculation.storage_growth_bytes_per_day
            * 30 / 1_000_000_000
        )
        self.assertAlmostEqual(
            storage_line.monthly_usage, expected_gb, places=6)
        self.assertAlmostEqual(
            storage_line.monthly_cost_usd, expected_gb * 0.25, places=6)
        # pay_per_request billing is resolved, so the provisioned
        # reserved-capacity dimensions are disclosed as not applicable
        # rather than silently absent.
        self.assertEqual(
            by_dimension["rcu_reserved"].status, "not_applicable")
        self.assertEqual(
            by_dimension["wcu_reserved"].status, "not_applicable")
        # Disclosure only: totals still equal the sum of priced lines.
        self.assertAlmostEqual(
            result.totals_usd_per_month["on_demand"],
            sum(line.monthly_cost_usd for line in result.lines),
            places=9,
        )

    def test_lambda_duration_gap_has_one_unpriced_and_one_coverage_row(self):
        """A priceable-but-underivable dimension keeps its unpriced entry
        and gains one matching usage_unknown coverage row (1:1)."""
        _, _, _, result = _pipeline()
        lam_reasons = [
            u.reason for u in result.unpriced
            if u.component_name == "AWS Lambda"
        ]
        self.assertEqual(len(lam_reasons), 1)
        entry = next(
            e for e in result.coverage
            if e.component_name == "AWS Lambda" and e.dimension == "gb_second"
        )
        self.assertEqual(entry.status, "usage_unknown")
        self.assertEqual(entry.reason, lam_reasons[0])

    def test_cost_links_mark_component_level_pricing(self):
        _, selection, _, result = _pipeline()
        links = {
            link.component_name: link
            for link in build_cost_links(selection, result)
        }
        apigw = links["Amazon API Gateway (WebSocket/API)"]
        self.assertFalse(apigw.priced)
        self.assertIn("WebSocket", apigw.unpriced_reason)

        lam = links["AWS Lambda"]
        self.assertTrue(lam.priced)
        self.assertAlmostEqual(lam.monthly_cost_usd, 12.0, places=2)

        sqs = links["Amazon SQS (Standard)"]
        self.assertTrue(sqs.priced)
        self.assertAlmostEqual(sqs.monthly_cost_usd, 24.0, places=2)

        sns = links["Amazon SNS"]
        self.assertTrue(sns.priced)
        self.assertAlmostEqual(sns.monthly_cost_usd, 30.0, places=2)

        ddb = links["Amazon DynamoDB (On\u2011Demand)"]
        self.assertTrue(ddb.priced)
        self.assertAlmostEqual(ddb.monthly_cost_usd, 16.572, places=2)

    def test_gate_names_mixed_line_components_from_cost_unpriced(self):
        _, selection, _, result = _pipeline()
        gate = evaluate_quality_gate(
            architecture=_arch(selection, result),
            critique=_critique(),
            revision_count=0,
            max_cycles=2,
            budget_constraints="$500/mo ceiling",
            cost_unpriced=result.unpriced,
        )
        self.assertEqual(
            gate.outcome, QualityGateOutcome.APPROVED_FOR_REVIEW)
        self.assertNotIn(
            "Priced totals stay within the $500.00/mo ceiling.",
            gate.reasons,
            msg="complete-assurance wording must not be used while "
                "pricing is incomplete",
        )
        self.assertTrue(
            any("Priced lines stay within" in r
                and "cannot be asserted" in r
                for r in gate.reasons),
            msg=f"expected conditional ceiling-assurance reason, "
                f"got: {gate.reasons}",
        )
        incomplete = [
            r for r in gate.reasons if r.startswith("Pricing incomplete")
        ]
        self.assertEqual(len(incomplete), 1)
        self.assertIn(
            "Pricing incomplete for 2 component(s): "
            "AWS Lambda, Amazon API Gateway (WebSocket/API).",
            incomplete[0],
        )
        self.assertNotIn("DynamoDB", incomplete[0])

    def test_gate_links_only_cannot_see_mixed_dimensions(self):
        """Pins why the gate must also consume cost_result.unpriced.

        Lambda has one priced line (requests) and one unknown dimension
        (gb_second), so its link reports priced=True and the links-only
        comparison cannot flag it. The union with cost_result.unpriced
        is what keeps the mixed-dimension gap visible.
        """
        _, selection, _, result = _pipeline()
        links = build_cost_links(selection, result)
        lam_link = next(
            link for link in links if link.component_name == "AWS Lambda")
        self.assertTrue(lam_link.priced)

        gate = evaluate_quality_gate(
            architecture=_arch(selection, result),
            critique=_critique(),
            revision_count=0,
            max_cycles=2,
            budget_constraints="$500/mo ceiling",
        )
        incomplete = [
            r for r in gate.reasons if r.startswith("Pricing incomplete")
        ]
        self.assertEqual(len(incomplete), 1)
        self.assertIn(
            "Pricing incomplete for 1 component(s): "
            "Amazon API Gateway (WebSocket/API).",
            incomplete[0],
        )
        self.assertNotIn("AWS Lambda", incomplete[0])

    def test_gate_breach_dominates_incomplete_estimate(self):
        _, selection, _, result = _pipeline()
        gate = evaluate_quality_gate(
            architecture=_arch(selection, result),
            critique=_critique(),
            revision_count=0,
            max_cycles=2,
            budget_constraints="monthly infrastructure cost below $50 per month",
            cost_unpriced=result.unpriced,
        )
        self.assertEqual(gate.outcome, QualityGateOutcome.BLOCKED)
        joined = "; ".join(gate.reasons)
        self.assertIn("exceeds the $50.00/mo ceiling", joined)


if __name__ == "__main__":
    unittest.main()
