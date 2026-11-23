"""Critique/revision loop tests: routing, briefs, nodes, wiring.

LLM workers are faked at the graph-module boundary; routing helpers
are exercised directly. No network, no Ollama calls.

The loop is bounded by MAX_CRITIQUE_CYCLES, separate from the
composer correction retry: these tests pin both the bound and the
distinction.

Run: poetry run python -m unittest discover tests
"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ai_architect.agents.design import graph as graph_module
from ai_architect.agents.design.graph import (
    _critique_route,
    _revision_brief,
    build_design_graph,
    composer_revision_stage,
    design_critic_stage,
    quality_gate_stage,
    route_after_critic,
)
from ai_architect.domain.architecture.critique import (
    MAX_CRITIQUE_CYCLES,
    CritiqueFinding,
    DesignCritique,
    QualityGateOutcome,
)


def _finding(severity="critical", route="composer_revision",
             subject="Amazon S3"):
    return CritiqueFinding(
        category="spof",
        severity=severity,
        statement="Single store with no failover",
        rationale="Store failure takes the system down",
        about_component_ref=subject,
        evidence_refs=[],
        recommendation="Replicate across zones",
        suggested_route=route,
    )


def _critique(findings, reviewed="artifact-1"):
    return DesignCritique(
        artifact_id="critique-1",
        session_id="design-s1",
        reviewed_artifact_id=reviewed,
        reviewed_version=1,
        findings=findings,
        summary="summary",
    )


def _state(**overrides):
    state = {
        "session_id": "design-s1",
        "user_request": "build it",
        "requirements_state": SimpleNamespace(
            extraction=SimpleNamespace(budget_constraints=""),
            session_id="req-s1",
        ),
        "capacity_result": SimpleNamespace(calculation=object()),
        "component_selection": object(),
        "cost_result": object(),
        "architecture": SimpleNamespace(version=1, artifact_id="a1"),
        "region_code": "us-east-1",
        "status": "running",
        "error": None,
        "warnings": [],
        "critique": None,
        "revision_count": 0,
        "critic_route": "",
        "quality_gate": None,
    }
    state.update(overrides)
    return state


def _run(coro):
    return asyncio.run(coro)


class CritiqueRoutingTests(unittest.TestCase):
    def test_no_criticals_goes_to_gate_without_spending_budget(self):
        critique = _critique([_finding("warning"), _finding("info")])
        self.assertEqual(_critique_route(critique, 0), "quality_gate")

    def test_criticals_revise_while_budget_remains(self):
        critique = _critique([_finding("critical")])
        self.assertEqual(_critique_route(critique, 0), "composer_revision")
        self.assertEqual(_critique_route(critique, 1), "composer_revision")

    def test_exhausted_budget_goes_to_gate(self):
        critique = _critique([_finding("critical")])
        self.assertEqual(
            _critique_route(critique, MAX_CRITIQUE_CYCLES), "quality_gate"
        )

    def test_reselection_route_for_component_faults(self):
        critique = _critique(
            [_finding("critical", route="component_reselection")]
        )
        self.assertEqual(
            _critique_route(critique, 0), "component_selection"
        )

    def test_revision_brief_renders_criticals_verbatim(self):
        critique = _critique([
            _finding("critical"),
            _finding("warning"),
        ])
        brief = _revision_brief(critique)
        self.assertIn("Single store with no failover", brief)
        self.assertNotIn("warning", brief.lower().split("recommendation")[0])
        # Warning findings are not revision drivers.
        self.assertEqual(brief.count("Recommendation:"), 1)


class CriticNodeTests(unittest.TestCase):
    def test_critic_stores_critique_and_routes(self):
        critique = _critique([_finding("critical")])
        with patch.object(
            graph_module, "run_design_critic", return_value=critique
        ):
            result = _run(design_critic_stage(_state()))
        self.assertIs(result["critique"], critique)
        self.assertEqual(result["critic_route"], "composer_revision")
        self.assertEqual(result["revision_count"], 1)
        self.assertEqual(result["status"], "running")

    def test_clean_critique_spends_no_budget(self):
        critique = _critique([])
        with patch.object(
            graph_module, "run_design_critic", return_value=critique
        ):
            result = _run(design_critic_stage(_state()))
        self.assertEqual(result["critic_route"], "quality_gate")
        self.assertEqual(result["revision_count"], 0)

    def test_critic_failure_fails_loudly(self):
        with patch.object(
            graph_module, "run_design_critic",
            side_effect=ValueError("boom"),
        ):
            result = _run(design_critic_stage(_state()))
        self.assertEqual(result["status"], "failed")
        self.assertIn("critic", result["error"].lower())

    def test_revision_chains_version_and_brief(self):
        critique = _critique([_finding("critical")])
        seen = {}

        def fake_composer(*args, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(version=2, artifact_id="a2")

        with patch.object(
            graph_module, "run_architecture_composer",
            side_effect=fake_composer,
        ):
            result = _run(composer_revision_stage(
                _state(critique=critique)
            ))
        self.assertEqual(seen["version"], 2)
        self.assertEqual(seen["supersedes_artifact_id"], "a1")
        self.assertIn("Single store", seen["revision_brief"])
        self.assertEqual(result["architecture"].artifact_id, "a2")

    def test_revision_without_criticals_refuses(self):
        with patch.object(
            graph_module, "run_architecture_composer",
            return_value=SimpleNamespace(version=2, artifact_id="a2"),
        ):
            result = _run(composer_revision_stage(
                _state(critique=_critique([]))
            ))
        self.assertEqual(result["status"], "failed")


class QualityGateNodeTests(unittest.TestCase):
    def _arch(self, artifact_id="a1"):
        return SimpleNamespace(
            artifact_id=artifact_id, cost_totals_snapshot={}
        )

    def test_approved_gate_stays_ready(self):
        result = _run(quality_gate_stage(_state(
            architecture=self._arch(),
            critique=_critique([], reviewed="a1"),
        )))
        self.assertEqual(result["status"], "ready")
        self.assertEqual(
            str(result["quality_gate"].outcome),
            QualityGateOutcome.APPROVED_FOR_REVIEW.value,
        )

    def test_blocked_gate_fails_with_reason(self):
        result = _run(quality_gate_stage(_state(
            architecture=self._arch(),
            critique=_critique([], reviewed="other"),
        )))
        self.assertEqual(result["status"], "failed")
        self.assertIn("stale", result["error"].lower())

    def test_route_after_critic_honors_channels(self):
        from langgraph.graph import END

        self.assertEqual(
            route_after_critic(_state(critic_route="composer_revision")),
            "composer_revision",
        )
        self.assertIs(
            route_after_critic(_state(critic_route="bogus")), END
        )


class GraphWiringTests(unittest.TestCase):
    def test_graph_contains_critic_loop_nodes(self):
        from langgraph.checkpoint.memory import MemorySaver

        graph = build_design_graph(MemorySaver())
        nodes = set(graph.get_graph().nodes)
        for expected in (
            "requirements",
            "capacity",
            "component_selection",
            "cost_estimation",
            "architecture_composer",
            "design_critic",
            "composer_revision",
            "quality_gate",
        ):
            self.assertIn(expected, nodes)


if __name__ == "__main__":
    unittest.main()
