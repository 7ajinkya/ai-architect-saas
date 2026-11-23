"""Capacity determinism chain: proposal inputs -> formulas -> cost usage.

Locks the authoritative derivation path inspected during the IoT
smoke review: every capacity value is a pure function of the proposed
workload inputs (documented in calculation_trace), and cost storage
usage derives from the capacity growth figure -- never from request
rate alone. Service-specific sizing prose (shard counts, instance
counts) appears nowhere in this chain: it is non-authoritative
model free text consumed by no deterministic code.

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
    FORMULA_VERSION,
    CapacityCalculator,
)
from ai_architect.services.cost.calculator import (
    BYTES_PER_GB,
    CostCalculator,
)


def _proposal() -> CapacityModelProposal:
    return CapacityModelProposal(
        daily_active_users=200000,
        messages_per_user_per_day=50.0,
        peak_factor=5.0,
        read_write_ratio=10.0,
        average_message_size_bytes=2048,
        connection_factor=0.1,
        storage_retention_days=365,
        rationale={},
    )


class CapacityChainTests(unittest.TestCase):
    def test_formulas_apply_inputs_without_shortcuts(self):
        proposal = _proposal()
        calc = CapacityCalculator().calculate(proposal)

        daily = 200000 * 50.0
        avg_write = daily / 86_400
        self.assertAlmostEqual(
            calc.peak_write_qps, avg_write * 5.0, places=6
        )
        self.assertAlmostEqual(
            calc.peak_read_qps, calc.peak_write_qps * 10.0, places=6
        )
        self.assertAlmostEqual(calc.concurrent_connections, 20000.0)
        growth = daily * 2048
        self.assertAlmostEqual(
            calc.storage_growth_bytes_per_day, growth
        )
        # Hot working set is defined as full-retention accumulation:
        # a coarse modeling semantic (hot != entire history), but the
        # arithmetic itself is exact and traceable, not invented.
        self.assertAlmostEqual(
            calc.hot_working_set_bytes, growth * 365
        )
        self.assertAlmostEqual(
            calc.egress_bytes_per_second,
            calc.peak_read_qps * 2048,
        )
        self.assertEqual(calc.formula_version, FORMULA_VERSION)
        for key in (
            "peak_write_qps",
            "peak_read_qps",
            "concurrent_connections",
            "storage_growth_bytes_per_day",
            "hot_working_set_bytes",
            "egress_bytes_per_second",
        ):
            self.assertIn(key, calc.calculation_trace)

    def test_cost_storage_derives_from_capacity_growth(self):
        proposal = _proposal()
        modeling = CapacityModelingResult(
            proposal=proposal,
            calculation=CapacityCalculator().calculate(proposal),
        )
        store = ComponentCandidate(
            name="Amazon Simple Storage Service", provider="aws",
            category="storage", role="r", rationale="r",
            confidence="high", evidence_support="strong")
        priced = [PricedComponent(
            component_name="Amazon Simple Storage Service",
            category="storage", service_code="AmazonS3",
            facts=[PricingFact(
                service_code="AmazonS3", region_code="ap-south-1",
                pricing_model=PricingModel.ON_DEMAND,
                usage_key="storage_gbmo", unit="GB-Mo",
                tiers=[PriceTier(begin=0.0, end=None, unit="GB-Mo",
                                 unit_price_usd=0.023)],
                source="aws-price-list-api",
                retrieved_at=datetime.utcnow(),
            )])]
        result = CostCalculator().calculate(
            modeling,
            ComponentSelectionResult(selected_components=[store]),
            priced, "ap-south-1")
        self.assertEqual(len(result.lines), 1)
        line = result.lines[0]
        # Storage usage is capacity growth x 30 days -- never request
        # rate alone; the average-message-size assumption is explicit.
        self.assertAlmostEqual(
            line.monthly_usage,
            modeling.calculation.storage_growth_bytes_per_day
            * 30 / BYTES_PER_GB,
        )
        self.assertIn("storage_growth_bytes_per_day", line.usage_formula)
        self.assertTrue(
            any("one month of accumulated" in a
                for a in result.assumptions)
        )


if __name__ == "__main__":
    unittest.main()
