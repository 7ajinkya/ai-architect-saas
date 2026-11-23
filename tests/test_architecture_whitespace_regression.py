"""Focused regression tests for Architecture Composer decision-service
whitespace normalization.

Covers the confirmed defect: Component Selection can return component
names containing Unicode whitespace such as U+202F NARROW NO-BREAK
SPACE (e.g. "Amazon Route\u202f53"), while Composer decision prose uses
regular U+0020 spaces. The validator's known-component set previously
used comp.name.strip().lower() without normalization, so the same
component was flagged as unknown.

All tests are deterministic: constructed artifacts only, no live LLM
calls.
"""
import unittest

from ai_architect.agents.component_selection.models import (
    ComponentCandidate,
    ComponentSelectionResult,
)
from ai_architect.domain.architecture.models import ArchitectureInputsRef, ArchitectureResult
from ai_architect.domain.capacity.models import CapacityModelResult
from ai_architect.domain.cost.models import CostEstimationResult
from ai_architect.domain.requirements.models import RequirementsExtraction
from ai_architect.services.architecture.core import hash_upstream_inputs
from ai_architect.services.architecture.validator import validate_architecture
from ai_architect.agents.architecture.run import (
    ArchitectureLLMOutput,
    LLMArchitectureDecision,
    LLMArchitectureEdge,
    LLMArchitectureNode,
    normalize_architecture,
)

GEN = __import__("datetime").datetime(2026, 10, 16, 13, 20, 0)
REGION = "us-east-1"
PROVIDER = "aws"
REQ_SESSION = "req-session"
DESIGN_SESSION = "design-session"

DNS_COMPONENT_REGULAR = "Amazon Route 53"
DNS_COMPONENT_NBSP = "Amazon Route\u202f53"


def _extraction() -> RequirementsExtraction:
    return RequirementsExtraction(
        application_description="URL shortener for 1000 daily users",
        cloud_provider="aws",
        region="us-east-1",
        target_users_dau="1000",
        budget_constraints="under $50 per month",
    )


def _capacity() -> CapacityModelResult:
    return CapacityModelResult(
        peak_write_qps=0.07,
        peak_read_qps=0.7,
        concurrent_connections=100.0,
        storage_growth_bytes_per_day=400000.0,
        hot_working_set_bytes=36000000.0,
        egress_bytes_per_second=138.89,
        formula_version="v1",
        calculation_trace={},
    )


def _cost() -> CostEstimationResult:
    return CostEstimationResult(
        lines=[],
        unpriced=[],
        totals_usd_per_month={"on_demand": 1.5365},
        generated_at=GEN,
        pricing_as_of=GEN,
    )


def _selection_with(dns_name: str) -> ComponentSelectionResult:
    return ComponentSelectionResult(
        selected_components=[
            ComponentCandidate(
                name=dns_name,
                provider="aws",
                category="dns",
                role="provide DNS routing",
                rationale="managed DNS service",
                evidence_refs=[],
                confidence="low",
                evidence_support="insufficient",
            )
        ],
        rejected_candidates=[],
        unresolved_tradeoffs=[],
        evidence_references=[],
        generated_at=GEN,
    )


def _inputs_ref(selection, cost) -> ArchitectureInputsRef:
    return ArchitectureInputsRef(
        requirements_session_id=REQ_SESSION,
        capacity_formula_version="v1",
        selection_generated_at=selection.generated_at,
        cost_generated_at=cost.generated_at,
        cost_pricing_as_of=cost.pricing_as_of,
        region_code=REGION,
        upstream_inputs_hash=hash_upstream_inputs(
            _extraction(), _capacity(), selection, cost, REGION
        ),
    )


def _result_with_decision(
    decision_text: str, selection, cost, inputs_ref
) -> ArchitectureResult:
    dns_name = selection.selected_components[0].name
    node_id = "amazon-route-53"
    return ArchitectureResult(
        artifact_id="artifact-1",
        session_id=DESIGN_SESSION,
        version=1,
        nodes=[
            {
                "node_id": node_id,
                "component_name": dns_name,
                "category": "dns",
                "role_in_system": "provide DNS routing",
                "capacity": [
                    {
                        "metric": "peak_read_qps",
                        "value": 0.7,
                        "unit": "requests/s",
                    }
                ],
                "scaling_note": "",
                "availability_note": "",
                "evidence_refs": [],
            }
        ],
        edges=[
            {
                "edge_id": "e1",
                "from_node": "client",
                "to_node": node_id,
                "kind": "request",
                "protocol": "https",
                "description": "client sends shorten requests",
                "load": None,
                "evidence_refs": [],
            }
        ],
        entry_points=[node_id],
        data_store_node_ids=[],
        boundaries=[f"region:{REGION}"],
        cost_links=[
            {
                "component_name": dns_name,
                "priced": False,
                "pricing_model": None,
                "unit": None,
                "monthly_cost_usd": None,
                "pricing_source": None,
                "unpriced_reason": "No pricing lines were produced for this component.",
            }
        ],
        cost_totals_snapshot={"on_demand": 1.5365},
        decisions=[
            {
                "decision": decision_text,
                "rationale": "managed DNS for routing",
                "alternatives_considered": [],
                "evidence_refs": [],
            }
        ],
        assumptions=[],
        warnings=[],
        unresolved_tradeoffs=[],
        evidence_references=[],
        inputs_ref=inputs_ref,
        generated_at=GEN,
    )


def _errors_for(dns_name: str, decision_text: str) -> list[str]:
    selection = _selection_with(dns_name)
    cost = _cost()
    inputs_ref = _inputs_ref(selection, cost)
    return validate_architecture(
        _result_with_decision(decision_text, selection, cost, inputs_ref),
        selection=selection,
        capacity=_capacity(),
        cost=cost,
        extraction=_extraction(),
        requirements_session_id=REQ_SESSION,
        design_session_id=DESIGN_SESSION,
        provider=PROVIDER,
        region_code=REGION,
    )


class NbspWhitespaceRegressionTests(unittest.TestCase):
    def test_nbsp_component_with_regular_space_prose_is_accepted(self):
        # Confirmed defect: Component Selection emits "Amazon Route\u202f53"
        # (narrow no-break space) while decision prose uses "amazon route 53"
        # (regular space). Previously flagged as unknown service.
        errors = _errors_for(DNS_COMPONENT_NBSP, "amazon route 53 for DNS")
        decision_errors = [e for e in errors if "decision" in e.lower()]
        self.assertEqual(
            decision_errors,
            [],
            msg=f"unexpected decision errors for NBSP component: {'; '.join(decision_errors)}",
        )

    def test_regular_space_component_with_regular_space_prose_still_passed(
        self,
    ):
        # Regression guard: the regular-space path must keep passing.
        errors = _errors_for(DNS_COMPONENT_REGULAR, "amazon route 53 for DNS")
        decision_errors = [e for e in errors if "decision" in e.lower()]
        self.assertEqual(
            decision_errors,
            [],
            msg=f"unexpected decision errors for regular-space component: {'; '.join(decision_errors)}",
        )

    def test_unknown_service_still_rejected(self):
        # Unknown-service validation must not be weakened: "amazon bedrock"
        # absent from selected/rejected components must still be rejected.
        errors = _errors_for(DNS_COMPONENT_REGULAR, "amazon bedrock for messaging")
        self.assertTrue(
            any("amazon bedrock" in e for e in errors),
            msg=f"expected unknown-service rejection, got: {'; '.join(errors)}",
        )


class AboutComponentRefNbspTests(unittest.TestCase):
    """Test the about_component_ref resolution path with NBSP names.

    about_component_ref is resolved in normalize_architecture() via
    _lookup_vocab_name(): exact selected/rejected name match first, then
    slug lookup. This path is tested separately; it is not changed unless a
    concrete defect is demonstrated under the existing resolution contract.
    """

    def _normalize_with_ref(self, about_ref: str):
        selection = _selection_with(DNS_COMPONENT_NBSP)
        cost = _cost()
        inputs_ref = _inputs_ref(selection, cost)
        llm_output = ArchitectureLLMOutput(
            nodes=[
                LLMArchitectureNode(
                    component_ref="amazon-route-53",
                    component_name=DNS_COMPONENT_NBSP,
                    role_in_system="DNS routing",
                    capacity_keys=["peak_read_qps"],
                )
            ],
            edges=[
                LLMArchitectureEdge(
                    from_component="client",
                    to_component="amazon-route-53",
                    kind="request",
                    protocol="https",
                    description="client sends shorten requests",
                )
            ],
            entry_points=[DNS_COMPONENT_NBSP],
            data_stores=[],
            boundaries=[f"region:{REGION}"],
            decisions=[
                LLMArchitectureDecision(
                    about_component_ref=about_ref,
                    decision="use managed DNS",
                    rationale="routing needs a domain",
                    alternatives_considered=[],
                    evidence_refs=[],
                )
            ],
            assumptions=[],
            warnings=[],
            new_tradeoffs=[],
        )
        return normalize_architecture(
            llm_output,
            extraction=_extraction(),
            capacity=_capacity(),
            selection=selection,
            cost=cost,
            region_code=REGION,
            requirements_session_id=REQ_SESSION,
            design_session_id=DESIGN_SESSION,
            provider=PROVIDER,
        )

    def test_about_component_ref_exact_nbsp_match_resolves(self):
        # about_component_ref equal to the stored NBSP name matches by
        # exact name; no unknown-service defect here.
        result = self._normalize_with_ref(DNS_COMPONENT_NBSP)
        self.assertEqual(len(result.decisions), 1)
        self.assertIn(f"[About: {DNS_COMPONENT_NBSP}]", result.decisions[0].decision)

    def test_about_component_ref_regular_space_matches_nbsp_name_via_normalization(
        self,
    ):
        # Normalization now resolves "Amazon Route 53" (regular space)
        # against the stored "Amazon Route\u202f53" (narrow no-break
        # space). Both normalize to "amazon route 53" and resolve to the
        # same canonical selected component name.
        result = self._normalize_with_ref("Amazon Route 53")
        self.assertEqual(len(result.decisions), 1)
        self.assertIn(f"[About: {DNS_COMPONENT_NBSP}]", result.decisions[0].decision)


if __name__ == "__main__":
    unittest.main()

