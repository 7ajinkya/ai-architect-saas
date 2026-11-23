"""Design Critic tests: LLM boundary + normalization + closed world.

All LLM calls are faked; the run path is exercised through the same
deterministic normalization as production. No network, no Ollama calls.

The Critic reviews; it never mutates: no test here alters the input
architecture, and every test asserts the reviewed artifact linkage.

Run: poetry run python -m unittest discover tests
"""
import unittest
from datetime import datetime
from unittest.mock import patch

from langchain_core.messages import AIMessage

from ai_architect.agents.component_selection.models import (
    ComponentCandidate,
    ComponentSelectionResult,
)
from ai_architect.agents.critic import run as run_module
from ai_architect.agents.critic.run import (
    CriticLLMOutput,
    normalize_critique,
)
from ai_architect.domain.architecture.critique import (
    DesignCritique,
    FindingCategory,
    FindingSeverity,
    SuggestedRoute,
)
from ai_architect.domain.architecture.models import (
    ArchitectureInputsRef,
    ArchitectureResult,
)
from ai_architect.domain.capacity.models import CapacityModelResult
from ai_architect.domain.cost.models import (
    CostEstimationResult,
    UnpricedComponent,
)
from ai_architect.domain.requirements.models import RequirementsExtraction

GEN = datetime(2026, 10, 4, 12, 0, 0)
REQ_SESSION = "req-s1"
DESIGN_SESSION = "design-s1"
REGION = "us-east-1"

COMP_A = "Amazon API Gateway"
COMP_B = "Amazon DynamoDB"
REJECTED_C = "Amazon RDS"


def _extraction() -> RequirementsExtraction:
    return RequirementsExtraction(
        application_description="URL shortener",
        cloud_provider="aws",
        region="us-east-1",
        availability="",
        latency="low",
    )


def _capacity() -> CapacityModelResult:
    return CapacityModelResult(
        peak_write_qps=10.0,
        peak_read_qps=40.0,
        concurrent_connections=100.0,
        storage_growth_bytes_per_day=1000.0,
        hot_working_set_bytes=2000.0,
        egress_bytes_per_second=3000.0,
        formula_version="v1",
        calculation_trace={},
    )


def _candidate(name, category="compute"):
    return ComponentCandidate(
        name=name,
        provider="aws",
        category=category,
        role="role",
        rationale="rationale",
        confidence="high",
        evidence_support="insufficient",
    )


def _selection() -> ComponentSelectionResult:
    return ComponentSelectionResult(
        selected_components=[
            _candidate(COMP_A, "networking"),
            _candidate(COMP_B, "database"),
        ],
        rejected_candidates=[_candidate(REJECTED_C, "database")],
        evidence_references=["ev-1"],
    )


def _cost() -> CostEstimationResult:
    return CostEstimationResult(
        lines=[],
        totals_usd_per_month={},
        warnings=[],
        unpriced=[],
        generated_at=GEN,
        pricing_as_of=GEN,
    )


def _architecture() -> ArchitectureResult:
    return ArchitectureResult(
        artifact_id="artifact-1",
        session_id=DESIGN_SESSION,
        version=1,
        nodes=[],
        edges=[],
        entry_points=[],
        data_store_node_ids=[],
        boundaries=[],
        cost_links=[],
        cost_totals_snapshot={},
        decisions=[],
        assumptions=[],
        warnings=[],
        unresolved_tradeoffs=[],
        evidence_references=[],
        inputs_ref=ArchitectureInputsRef(
            requirements_session_id=REQ_SESSION,
            capacity_formula_version="v1",
            selection_generated_at=GEN,
            cost_generated_at=GEN,
            cost_pricing_as_of=GEN,
            region_code=REGION,
            upstream_inputs_hash="hash-1",
        ),
        generated_at=GEN,
    )


def _upstream():
    return {
        "extraction": _extraction(),
        "capacity": _capacity(),
        "selection": _selection(),
        "cost": _cost(),
        "architecture": _architecture(),
    }


def _good_finding(**overrides):
    payload = {
        "category": "spof",
        "severity": "critical",
        "statement": "Single gateway with no failover story",
        "rationale": "Gateway failure takes the system down",
        "about_component_ref": COMP_A,
        "evidence_refs": ["ev-1"],
        "recommendation": "Add a second availability zone entry",
        "suggested_route": "composer_revision",
    }
    payload.update(overrides)
    return payload


def _good_shape():
    return {"findings": [_good_finding()]}


class _FakeBoundAgent:
    def __init__(self, outcome):
        self._outcome = outcome
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        return self._outcome


class _FakeRawAgent:
    def __init__(self, content="should never be used"):
        self._content = content
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        from langchain_core.messages import AIMessage as _AIMessage

        return _AIMessage(content=self._content)


def _run_with(parsed=None, raw_text="", parsing_error=None):
    bound = _FakeBoundAgent(
        {
            "raw": AIMessage(content=raw_text),
            "parsed": parsed,
            "parsing_error": parsing_error,
        }
    )
    raw = _FakeRawAgent()
    up = _upstream()
    with (
        patch.object(
            run_module, "create_design_critic_agent_with_raw",
            return_value=bound,
        ),
        patch.object(
            run_module, "create_design_critic_agent_raw",
            return_value=raw,
        ),
    ):
        result = run_module.run_design_critic(
            up["extraction"], up["capacity"], up["selection"],
            up["cost"], up["architecture"], REGION, DESIGN_SESSION,
            requirements_session_id=REQ_SESSION,
        )
    return result, bound, raw


class CriticContractTests(unittest.TestCase):
    def test_01_valid_critique_links_reviewed_artifact(self):
        result, _, _ = _run_with(
            parsed=CriticLLMOutput.model_validate(_good_shape())
        )
        self.assertIsInstance(result, DesignCritique)
        self.assertEqual(result.reviewed_artifact_id, "artifact-1")
        self.assertEqual(result.reviewed_version, 1)
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(len(result.criticals), 1)
        self.assertFalse(result.needs_reselection)
        self.assertIn("1 critical", result.summary)

    def test_02_empty_findings_is_valid_silence(self):
        result, _, _ = _run_with(
            parsed=CriticLLMOutput.model_validate({"findings": []})
        )
        self.assertEqual(result.findings, [])
        self.assertEqual(result.criticals, [])
        self.assertIn("0 critical", result.summary)

    def test_03_unknown_subject_rejected(self):
        bad = _good_shape()
        bad["findings"][0]["about_component_ref"] = "Amazon Neptune"
        with self.assertRaises(ValueError) as ctx:
            _run_with(parsed=CriticLLMOutput.model_validate(bad))
        self.assertIn("unknown component", str(ctx.exception).lower())

    def test_04_invented_evidence_rejected(self):
        bad = _good_shape()
        bad["findings"][0]["evidence_refs"] = ["ev-forged"]
        with self.assertRaises(ValueError) as ctx:
            _run_with(parsed=CriticLLMOutput.model_validate(bad))
        self.assertIn("ev-forged", str(ctx.exception))

    def test_05_absolute_numbers_rejected_ratios_allowed(self):
        bad = _good_shape()
        bad["findings"][0]["statement"] = "Handles 347 QPS today"
        with self.assertRaises(ValueError) as ctx:
            _run_with(parsed=CriticLLMOutput.model_validate(bad))
        self.assertIn("throughput", str(ctx.exception).lower())

        ok = _good_shape()
        ok["findings"][0]["statement"] = (
            "Runs above roughly 70% of peak and cliffs at roughly 3x load"
        )
        result, _, _ = _run_with(
            parsed=CriticLLMOutput.model_validate(ok)
        )
        self.assertEqual(len(result.findings), 1)

    def test_06_empty_statement_rejected(self):
        bad = _good_shape()
        bad["findings"][0]["statement"] = "  "
        with self.assertRaises(ValueError) as ctx:
            _run_with(parsed=CriticLLMOutput.model_validate(bad))
        self.assertIn("statement", str(ctx.exception).lower())

    def test_07_reselection_route_detected(self):
        shape = _good_shape()
        shape["findings"][0]["about_component_ref"] = REJECTED_C
        shape["findings"][0]["suggested_route"] = "component_reselection"
        result, _, _ = _run_with(
            parsed=CriticLLMOutput.model_validate(shape)
        )
        self.assertTrue(result.needs_reselection)

    def test_08_rejected_subject_allowed_but_never_invented(self):
        up = _upstream()
        parsed = CriticLLMOutput.model_validate(_good_shape())
        parsed.findings[0].about_component_ref = REJECTED_C
        result = normalize_critique(
            parsed,
            selection=up["selection"],
            architecture=up["architecture"],
            session_id=DESIGN_SESSION,
        )
        self.assertEqual(
            result.findings[0].about_component_ref, REJECTED_C
        )

    def test_09_category_and_severity_enums_enforced(self):
        with self.assertRaises(Exception):
            CriticLLMOutput.model_validate({
                "findings": [_good_finding(category="vibes")],
            })
        with self.assertRaises(Exception):
            CriticLLMOutput.model_validate({
                "findings": [_good_finding(severity="urgent")],
            })
        self.assertIn("spof", {c.value for c in FindingCategory})
        self.assertIn("critical", {s.value for s in FindingSeverity})
        self.assertIn(
            "component_reselection", {r.value for r in SuggestedRoute}
        )

    def test_10_critique_never_mutates_architecture(self):
        up = _upstream()
        before = up["architecture"].model_dump(mode="json")
        _run_with(parsed=CriticLLMOutput.model_validate(_good_shape()))
        self.assertEqual(up["architecture"].model_dump(mode="json"), before)

    def test_11_price_in_statement_rejected(self):
        bad = _good_shape()
        bad["findings"][0]["statement"] = (
            "Priced total exceeds the $100/month budget"
        )
        with self.assertRaises(ValueError) as ctx:
            _run_with(parsed=CriticLLMOutput.model_validate(bad))
        self.assertIn("price", str(ctx.exception).lower())

    def test_12_price_in_rationale_rejected(self):
        bad = _good_shape()
        bad["findings"][0]["rationale"] = (
            "The overrun matters because the ceiling is $100"
        )
        with self.assertRaises(ValueError) as ctx:
            _run_with(parsed=CriticLLMOutput.model_validate(bad))
        self.assertIn("price", str(ctx.exception).lower())

    def test_13_qualitative_budget_breach_accepted(self):
        shape = {"findings": [_good_finding(
            category="cost_ceiling",
            severity="warning",
            statement=(
                "Priced total exceeds the stated monthly budget ceiling"
            ),
            rationale=(
                "The largest priced line carries the workload at this "
                "scale; the ceiling leaves no headroom for growth"
            ),
            about_component_ref=COMP_B,
            recommendation=(
                "Revisit the capacity model or request a ceiling change"
            ),
        )]}
        result, _, _ = _run_with(
            parsed=CriticLLMOutput.model_validate(shape)
        )
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(
            str(result.findings[0].category), "cost_ceiling"
        )

    def test_14_budget_status_reaches_the_prompt(self):
        from ai_architect.agents.critic.prompt import (
            DESIGN_CRITIC_SYSTEM_PROMPT,
        )

        self.assertIn("dollar amount", DESIGN_CRITIC_SYSTEM_PROMPT)
        self.assertIn(
            "stated monthly budget ceiling", DESIGN_CRITIC_SYSTEM_PROMPT
        )

        seen = {}

        class _CaptureAgent:
            def __init__(self, outcome):
                self._outcome = outcome

            def invoke(self, messages):
                seen["human"] = str(messages[1].content)
                return self._outcome

        up = _upstream()
        over_extraction = up["extraction"].model_copy(
            update={"budget_constraints": "monthly AWS budget under $100"}
        )
        over_cost = up["cost"].model_copy(
            update={"totals_usd_per_month": {"on_demand": 150.0}}
        )
        parsed = CriticLLMOutput.model_validate(_good_shape())
        with (
            patch.object(
                run_module, "create_design_critic_agent_with_raw",
                return_value=_CaptureAgent({
                    "raw": AIMessage(content=""),
                    "parsed": parsed,
                    "parsing_error": None,
                }),
            ),
            patch.object(
                run_module, "create_design_critic_agent_raw",
                return_value=_FakeRawAgent(),
            ),
        ):
            run_module.run_design_critic(
                over_extraction, up["capacity"], up["selection"],
                over_cost, up["architecture"], REGION, DESIGN_SESSION,
                requirements_session_id=REQ_SESSION,
            )
        self.assertIn("OVER", seen["human"])

        under_cost = up["cost"].model_copy(
            update={"totals_usd_per_month": {"on_demand": 50.0}}
        )
        with (
            patch.object(
                run_module, "create_design_critic_agent_with_raw",
                return_value=_CaptureAgent({
                    "raw": AIMessage(content=""),
                    "parsed": parsed,
                    "parsing_error": None,
                }),
            ),
            patch.object(
                run_module, "create_design_critic_agent_raw",
                return_value=_FakeRawAgent(),
            ),
        ):
            run_module.run_design_critic(
                over_extraction, up["capacity"], up["selection"],
                under_cost, up["architecture"], REGION, DESIGN_SESSION,
                requirements_session_id=REQ_SESSION,
            )
        self.assertIn("WITHIN", seen["human"])
        assessment = seen["human"].split(
            "## Budget Assessment"
        )[1].split("## Capacity Model")[0]
        self.assertNotIn("$", assessment)

    def test_15_correction_retry_recovers_on_compliance(self):
        bad = CriticLLMOutput.model_validate({"findings": [_good_finding(
            statement="Priced total exceeds the $100/month budget"
        )]})
        good = CriticLLMOutput.model_validate({"findings": [_good_finding(
            statement=(
                "Priced total exceeds the stated monthly budget ceiling"
            ),
            rationale="Ceiling leaves no headroom for growth",
        )]})
        calls = []

        class _SequencingAgent:
            def invoke(self, messages):
                calls.append(str(messages[-1].content)[:80])
                return {
                    "raw": AIMessage(content=""),
                    "parsed": bad if len(calls) == 1 else good,
                    "parsing_error": None,
                }

        # Drive run_design_critic directly with the sequencing agent:
        # first attempt carries a price (rejected), the correction
        # complies, so the bounded retry recovers instead of failing.
        up = _upstream()
        with (
            patch.object(
                run_module, "create_design_critic_agent_with_raw",
                return_value=_SequencingAgent(),
            ),
            patch.object(
                run_module, "create_design_critic_agent_raw",
                return_value=_FakeRawAgent(),
            ),
        ):
            result = run_module.run_design_critic(
                up["extraction"], up["capacity"], up["selection"],
                up["cost"], up["architecture"], REGION, DESIGN_SESSION,
                requirements_session_id=REQ_SESSION,
            )
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(len(calls), 2)

    def test_16_offending_excerpt_quotes_rejected_sentence(self):
        from ai_architect.agents.critic.run import _offending_excerpt

        parsed = CriticLLMOutput.model_validate({"findings": [_good_finding(
            statement="Priced total exceeds the $100/month budget"
        )]})
        excerpt = _offending_excerpt(
            ValueError(
                "Design Critic finding [0] statement states a price; "
                "cost facts live only in cost links."
            ),
            parsed,
        )
        self.assertIn("$100", excerpt)
        self.assertLessEqual(len(excerpt), 200)
        self.assertEqual(
            _offending_excerpt(ValueError("unrelated boom"), parsed), ""
        )

    def test_17_schema_fields_carry_no_price_rule(self):
        from ai_architect.agents.critic.run import LLMCritiqueFinding

        for field in ("statement", "rationale", "recommendation"):
            description = LLMCritiqueFinding.model_fields[field].description
            self.assertIn("dollar", description or "")


class BudgetStatusTests(unittest.TestCase):
    """Ceiling posture is never asserted beyond what pricing proves:
    WITHIN only for a complete estimate, PARTIAL for an incomplete one,
    UNKNOWN when there is no comparison basis."""

    def _extraction_with_ceiling(self) -> RequirementsExtraction:
        return _extraction().model_copy(
            update={"budget_constraints": "monthly AWS budget under $100"}
        )

    def test_no_ceiling_unchanged(self):
        status = run_module._budget_status(_extraction(), _cost())
        self.assertTrue(status.startswith("NO_CEILING"))

    def test_over_unchanged(self):
        cost = _cost().model_copy(
            update={"totals_usd_per_month": {"on_demand": 150.0}}
        )
        status = run_module._budget_status(
            self._extraction_with_ceiling(), cost
        )
        self.assertTrue(status.startswith("OVER"))

    def test_fully_priced_within_is_within(self):
        cost = _cost().model_copy(
            update={"totals_usd_per_month": {"on_demand": 50.0}}
        )
        status = run_module._budget_status(
            self._extraction_with_ceiling(), cost
        )
        self.assertTrue(status.startswith("WITHIN"))

    def test_no_totals_with_ceiling_is_unknown(self):
        status = run_module._budget_status(
            self._extraction_with_ceiling(), _cost()
        )
        self.assertTrue(status.startswith("UNKNOWN"))
        self.assertNotIn("WITHIN", status)
        self.assertIn("cannot be assessed", status)

    def test_no_totals_unknown_names_unpriced_count(self):
        cost = _cost().model_copy(update={
            "unpriced": [UnpricedComponent(
                component_name="AWS Lambda", reason="no pricing facts")],
            "fully_priced": False,
        })
        status = run_module._budget_status(
            self._extraction_with_ceiling(), cost
        )
        self.assertTrue(status.startswith("UNKNOWN"))
        self.assertIn("1 selected component(s)", status)

    def test_unpriced_component_downgrades_within_to_partial(self):
        cost = _cost().model_copy(update={
            "totals_usd_per_month": {"on_demand": 50.0},
            "unpriced": [UnpricedComponent(
                component_name="AWS Lambda", reason="no pricing facts")],
            "fully_priced": False,
        })
        status = run_module._budget_status(
            self._extraction_with_ceiling(), cost
        )
        self.assertTrue(status.startswith("PARTIAL"))
        self.assertNotIn("WITHIN:", status)
        self.assertIn("cannot be asserted", status)

    def test_inconsistent_fully_priced_flag_still_partial(self):
        cost = _cost().model_copy(update={
            "totals_usd_per_month": {"on_demand": 50.0},
            "unpriced": [UnpricedComponent(
                component_name="AWS Lambda", reason="no pricing facts")],
            "fully_priced": True,
        })
        status = run_module._budget_status(
            self._extraction_with_ceiling(), cost
        )
        self.assertTrue(status.startswith("PARTIAL"))

    def test_uncomparable_totals_unknown(self):
        cost = _cost().model_copy(
            update={"totals_usd_per_month": {"on_demand": "abc"}}
        )
        status = run_module._budget_status(
            self._extraction_with_ceiling(), cost
        )
        self.assertTrue(status.startswith("UNKNOWN"))


if __name__ == "__main__":
    unittest.main()
