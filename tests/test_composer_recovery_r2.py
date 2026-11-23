"""R2 regression tests: labeled correction vocabulary + OOV-subject guidance.

All LLM calls are faked; no Ollama, Qdrant, AWS, or network access.

Covers, with deterministic scripted model responses:
1. labeled vocabulary enables OOV-subject recovery (messaging failure
   shape: numeric storage prose + `amazon mq` mention -> corrected output
   with about_component_ref "" and generic prose succeeds);
2. selected/rejected semantics remain enforced (rejected usable as
   alternative, rejected-as-node still fails, validator untouched);
3. multi-error recovery (storage prose + OOV mention corrected together
   with no new validation error);
4. unrecoverable OOV remains fail-closed with bounded diagnostics and no
   third attempt (plus no `amazon mq` alias was added);
5. valid first attempt uses exactly one call and never sees the
   correction path;
6. retry remains bounded (locked via the exhaustion test's call count).

Run: python -m unittest discover -s tests
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
from ai_architect.services.architecture.validator import (
    _COMPONENT_ALIASES,
    validate_architecture,
)

REGION = "us-east-1"
PROVIDER = "aws"
REQ_SESSION = "req-session"
DESIGN_SESSION = "design-session"

COMP_A = "AWS Lambda"
COMP_B = "Amazon Simple Queue Service"
REJECTED_C = "Amazon ElastiCache"


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
        application_description="messaging",
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
                "evidence_refs": [],
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
                "evidence_refs": [],
            },
        ],
        "assumptions": ["Retain message history within the hot working set"],
        "warnings": [],
        "new_tradeoffs": [],
    }


def _bad_shape():
    shape = _good_shape()
    shape["assumptions"] = ["Retain 500 GB of history for 30 days"]
    shape["decisions"] = [
        {
            "about_component_ref": "",
            "decision": "Use Amazon MQ as the message broker",
            "rationale": "Managed service",
            "alternatives_considered": [],
            "evidence_refs": [],
        },
    ]
    return shape


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


class LabeledVocabularyRecoveryTests(unittest.TestCase):
    def test_oov_subject_recovers_with_labeled_guidance(self):
        bad = ArchitectureLLMOutput.model_validate(_bad_shape())
        good = ArchitectureLLMOutput.model_validate(_good_shape())
        bound = _ScriptedBoundAgent([bad, good])
        up = _upstream()
        result, agent = _run_with(bound, up)
        self.assertIsInstance(result, ArchitectureResult)
        self.assertEqual(agent.calls, 2)
        retry_prompt = agent.seen_messages[1][-1].content
        # Labeled membership sections, not an unlabeled merge.
        self.assertIn(
            "Selected components (may be nodes, edges, subjects, "
            "alternatives):",
            retry_prompt,
        )
        self.assertIn(
            "Rejected components (subjects and alternatives only, "
            "never nodes):",
            retry_prompt,
        )
        self.assertIn("aws-lambda -> AWS Lambda", retry_prompt)
        self.assertIn(
            "amazon-elasticache -> Amazon ElastiCache", retry_prompt
        )
        # Selected and rejected entries are not intermingled: every
        # selected line sits under the selected header.
        selected_block = retry_prompt.split("Selected components")[1].split(
            "Rejected components")[0]
        rejected_block = retry_prompt.split("Rejected components")[1].split(
            "Rejection reasons:")[0]
        self.assertIn(COMP_A, selected_block)
        self.assertNotIn(REJECTED_C, selected_block)
        self.assertIn(REJECTED_C, rejected_block)
        # Explicit OOV-subject replacement guidance.
        self.assertIn("NEITHER list", retry_prompt)
        self.assertIn('about_component_ref', retry_prompt)
        self.assertIn("provider-free generic words", retry_prompt)
        # The recovered artifact round-trips validation cleanly.
        self.assertEqual(_check_valid(up, result), [])

    def test_multi_error_recovery_introduces_no_new_error(self):
        # Mirrors the messaging failure exactly: storage-size prose plus
        # an OOV service mention, corrected together in one retry.
        bad = ArchitectureLLMOutput.model_validate(_bad_shape())
        good = ArchitectureLLMOutput.model_validate(_good_shape())
        bound = _ScriptedBoundAgent([bad, good])
        up = _upstream()
        result, agent = _run_with(bound, up)
        self.assertEqual(agent.calls, 2)
        self.assertEqual(_check_valid(up, result), [])
        self.assertEqual(
            [n.component_name for n in result.nodes], [COMP_A, COMP_B]
        )
        self.assertEqual(len(result.decisions), 1)


class SelectedRejectedSemanticsTests(unittest.TestCase):
    def test_rejected_usable_as_alternative_not_as_node(self):
        shape = _good_shape()
        shape["decisions"][0]["alternatives_considered"] = [
            {"component_ref": REJECTED_C,
             "note": "managed cache could cut read load"},
        ]
        parsed = ArchitectureLLMOutput.model_validate(shape)
        bound = _ScriptedBoundAgent([parsed])
        up = _upstream()
        result, agent = _run_with(bound, up)
        self.assertEqual(agent.calls, 1)
        self.assertIn(
            f"{REJECTED_C}: managed cache could cut read load",
            result.decisions[0].alternatives_considered,
        )
        self.assertEqual(_check_valid(up, result), [])

    def test_rejected_as_node_still_fails(self):
        shape = _good_shape()
        shape["nodes"] = [
            {
                "component_name": REJECTED_C,
                "role_in_system": "Cache reads",
                "capacity_keys": ["peak_read_qps"],
                "scaling_note": "",
                "availability_note": "",
                "evidence_refs": [],
            },
        ]
        shape["edges"] = []
        shape["entry_points"] = []
        parsed = ArchitectureLLMOutput.model_validate(shape)
        up = _upstream()
        with self.assertRaises(ValueError) as ctx:
            run_module.normalize_architecture(
                parsed,
                extraction=up["extraction"],
                capacity=up["capacity"],
                selection=up["selection"],
                cost=up["cost"],
                region_code=REGION,
                requirements_session_id=REQ_SESSION,
                design_session_id=DESIGN_SESSION,
                provider=PROVIDER,
            )
        self.assertIn("unknown component", str(ctx.exception).lower())


class ExhaustionTests(unittest.TestCase):
    def test_persisted_oov_still_fails_closed_and_bounded(self):
        self.assertNotIn("mq", _COMPONENT_ALIASES)
        self.assertNotIn("amazon mq", _COMPONENT_ALIASES)
        bad = ArchitectureLLMOutput.model_validate(_bad_shape())
        bound = _ScriptedBoundAgent([bad, bad])
        up = _upstream()
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
            with self.assertRaises(run_module.ArchitectureCompositionError) as ctx:
                run_module.run_architecture_composer(
                    up["extraction"], up["capacity"], up["selection"],
                    up["cost"], REGION, DESIGN_SESSION,
                    requirements_session_id=REQ_SESSION,
                )
        self.assertEqual(bound.calls, 2)
        message = str(ctx.exception)
        self.assertIn("after one correction retry", message)
        self.assertIn("amazon mq as the", message)
        diagnostics = ctx.exception.diagnostics
        self.assertEqual(len(diagnostics), 2)


class FirstAttemptSuccessTests(unittest.TestCase):
    def test_valid_first_attempt_never_sees_correction(self):
        good = ArchitectureLLMOutput.model_validate(_good_shape())
        bound = _ScriptedBoundAgent([good])
        up = _upstream()
        result, agent = _run_with(bound, up)
        self.assertEqual(agent.calls, 1)
        self.assertEqual(len(agent.seen_messages[0]), 2)
        for messages in agent.seen_messages:
            for message in messages:
                self.assertNotIn("REJECTED", getattr(message, "content", ""))
        self.assertEqual(_check_valid(up, result), [])


if __name__ == "__main__":
    unittest.main()

