"""Composer closed-world regression: capacity-in-assumption, unknown
evidence, OOV decision service.

All LLM calls are faked; no Ollama, Qdrant, AWS, or network access.
For each violation:
  1. first response invalid
  2. correction response valid
  3. correction response still invalid
  4. bounded failure behavior (exactly 2 calls, fail-closed)

Validators are never weakened; recovery stays bounded at one retry.
"""
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage

from ai_architect.agents.architecture import run as run_module
from ai_architect.agents.architecture.run import ArchitectureLLMOutput
from ai_architect.agents.component_selection.models import (
    ComponentCandidate,
    ComponentSelectionResult,
)
from ai_architect.domain.architecture.models import ArchitectureResult
from ai_architect.domain.capacity.models import CapacityModelResult
from ai_architect.domain.cost.models import (
    CostEstimationResult,
    UnpricedComponent,
)
from ai_architect.domain.requirements.models import RequirementsExtraction
from ai_architect.services.architecture.validator import validate_architecture

REGION = "us-east-1"
PROVIDER = "aws"
REQ_SESSION = "req-session"
DESIGN_SESSION = "design-session"

COMP_A = "AWS Lambda"
COMP_B = "Amazon Simple Queue Service"
REJECTED_C = "Amazon ElastiCache"
EV_A = "ev-a"
EV_B = "ev-b"


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


def _selection():
    return ComponentSelectionResult(
        selected_components=[
            _candidate(COMP_A, "compute"),
            _candidate(COMP_B, "messaging"),
        ],
        rejected_candidates=[_candidate(REJECTED_C, "caching")],
        evidence_references=[EV_A, EV_B],
    )


def _capacity():
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


def _cost(selection):
    return CostEstimationResult(
        lines=[],
        totals_usd_per_month={},
        unpriced=[
            UnpricedComponent(component_name=c.name, reason="no mapping")
            for c in selection.selected_components
        ],
    )


def _extraction():
    return RequirementsExtraction(
        application_description="url shortener",
        cloud_provider="aws",
        availability="high",
        latency="low",
    )


def _upstream():
    selection = _selection()
    return {
        "extraction": _extraction(),
        "capacity": _capacity(),
        "selection": selection,
        "cost": _cost(selection),
    }


def _good_shape():
    return {
        "nodes": [
            {
                "component_name": COMP_A,
                "role_in_system": "Serve ingress traffic",
                "capacity_keys": ["peak_write_qps"],
                "scaling_note": "",
                "availability_note": "",
                "evidence_refs": [],
            },
            {
                "component_name": COMP_B,
                "role_in_system": "Buffer fan-out load",
                "capacity_keys": ["peak_write_qps"],
                "scaling_note": "",
                "availability_note": "",
                "evidence_refs": [EV_A],
            },
        ],
        "edges": [
            {
                "from_component": "client",
                "to_component": COMP_A,
                "kind": "request",
                "protocol": "",
                "description": "Accepts client traffic",
                "evidence_refs": [],
            },
            {
                "from_component": COMP_A,
                "to_component": COMP_B,
                "kind": "request",
                "protocol": "",
                "description": "Routes work to the managed broker",
                "evidence_refs": [],
            },
        ],
        "entry_points": [COMP_A],
        "data_stores": [],
        "boundaries": ["region:us-east-1"],
        "decisions": [
            {
                "about_component_ref": "",
                "decision": "Use a managed broker for fan-out",
                "rationale": "Fits peak write load",
                "alternatives_considered": [],
                "evidence_refs": [EV_A],
            },
        ],
        "assumptions": ["Retain message history within the hot working set"],
        "warnings": [],
        "new_tradeoffs": [],
    }


class _ScriptedBoundAgent:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0
        self.seen_messages = []

    def invoke(self, messages):
        self.calls += 1
        self.seen_messages.append(messages)
        parsed = self._outputs[min(self.calls - 1, len(self._outputs) - 1)]
        return {"raw": AIMessage(content=""), "parsed": parsed,
                "parsing_error": None}


class _FakeRawAgent:
    def invoke(self, messages):
        raise AssertionError("unbound retry must not trigger")


def _run_with(bound, up=None):
    up = up if up is not None else _upstream()
    with (
        patch.object(
            run_module, "create_architecture_composer_agent_with_raw",
            return_value=bound,
        ),
        patch.object(
            run_module, "create_architecture_composer_agent_raw",
            return_value=_FakeRawAgent(),
        ),
    ):
        return run_module.run_architecture_composer(
            up["extraction"], up["capacity"], up["selection"],
            up["cost"], REGION, DESIGN_SESSION,
            requirements_session_id=REQ_SESSION,
        ), bound


def _check_valid(up, result):
    return validate_architecture(
        result,
        selection=up["selection"],
        capacity=up["capacity"],
        cost=up["cost"],
        extraction=up["extraction"],
        requirements_session_id=REQ_SESSION,
        design_session_id=DESIGN_SESSION,
        provider=PROVIDER,
        region_code=REGION,
    )


class CapacityAssumptionTests(unittest.TestCase):
    def _bad(self):
        shape = _good_shape()
        shape["assumptions"] = ["Retain 500 GB of history for 30 days"]
        return ArchitectureLLMOutput.model_validate(shape)

    def test_first_invalid_then_correction_valid(self):
        bad = self._bad()
        good = ArchitectureLLMOutput.model_validate(_good_shape())
        bound = _ScriptedBoundAgent([bad, good])
        up = _upstream()
        result, agent = _run_with(bound, up)
        self.assertIsInstance(result, ArchitectureResult)
        self.assertEqual(agent.calls, 2)
        self.assertEqual(_check_valid(up, result), [])

    def test_correction_still_invalid_fails_bounded(self):
        bad = self._bad()
        bound = _ScriptedBoundAgent([bad, bad])
        up = _upstream()
        with self.assertRaises(run_module.ArchitectureCompositionError) as ctx:
            _run_with(bound, up)[0]
        self.assertEqual(bound.calls, 2)
        self.assertIn("after one correction retry", str(ctx.exception))
        self.assertIn("storage size", str(ctx.exception).lower())
        self.assertEqual(len(ctx.exception.diagnostics), 2)


class UnknownEvidenceTests(unittest.TestCase):
    def _bad(self):
        shape = _good_shape()
        shape["nodes"][1]["evidence_refs"] = ["qdrant:aws-well-architected-labs0"]
        return ArchitectureLLMOutput.model_validate(shape)

    def test_first_invalid_then_correction_valid(self):
        bad = self._bad()
        good = ArchitectureLLMOutput.model_validate(_good_shape())
        bound = _ScriptedBoundAgent([bad, good])
        up = _upstream()
        result, agent = _run_with(bound, up)
        self.assertEqual(agent.calls, 2)
        self.assertEqual(_check_valid(up, result), [])
        retry_prompt = agent.seen_messages[1][-1].content
        self.assertIn("ONLY use evidence IDs", retry_prompt)
        self.assertIn(EV_A, retry_prompt)

    def test_correction_still_invalid_fails_bounded(self):
        bad = self._bad()
        bound = _ScriptedBoundAgent([bad, bad])
        with self.assertRaises(run_module.ArchitectureCompositionError) as ctx:
            _run_with(bound)[0]
        self.assertEqual(bound.calls, 2)
        self.assertIn("qdrant:aws-well-architected-labs0", str(ctx.exception))
        self.assertEqual(len(ctx.exception.diagnostics), 2)


class OovDecisionServiceTests(unittest.TestCase):
    def _bad(self):
        shape = _good_shape()
        shape["decisions"] = [
            {
                "about_component_ref": "amazon route 53",
                "decision": "Use managed DNS for routing",
                "rationale": "Needs DNS",
                "alternatives_considered": [],
                "evidence_refs": [],
            },
        ]
        return ArchitectureLLMOutput.model_validate(shape)

    def test_first_invalid_then_correction_valid(self):
        bad = self._bad()
        good = ArchitectureLLMOutput.model_validate(_good_shape())
        bound = _ScriptedBoundAgent([bad, good])
        up = _upstream()
        result, agent = _run_with(bound, up)
        self.assertEqual(agent.calls, 2)
        self.assertEqual(_check_valid(up, result), [])

    def test_correction_still_invalid_fails_bounded(self):
        bad = self._bad()
        bound = _ScriptedBoundAgent([bad, bad])
        with self.assertRaises(run_module.ArchitectureCompositionError) as ctx:
            _run_with(bound)[0]
        self.assertEqual(bound.calls, 2)
        self.assertIn("route 53", str(ctx.exception).lower())
        self.assertEqual(len(ctx.exception.diagnostics), 2)


class FirstAttemptPromptTests(unittest.TestCase):
    def test_initial_prompt_lists_evidence_and_rejected(self):
        good = ArchitectureLLMOutput.model_validate(_good_shape())
        bound = _ScriptedBoundAgent([good])
        up = _upstream()
        _run_with(bound, up)
        initial = bound.seen_messages[0][1].content
        self.assertIn("Authoritative evidence IDs", initial)
        self.assertIn(EV_A, initial)
        self.assertIn("Rejected components", initial)
        self.assertIn(REJECTED_C, initial)


if __name__ == "__main__":
    unittest.main()
