"""Integration regression: one canonical region through the design pipeline.

The first manual end-to-end smoke test failed at Cost Estimation because
the requirements artifact held the literal "US East (N. Virginia)" while
the design stages required an AWS region code. These tests pin the fixed
behaviour end to end without any LLM call or network request:

- the region is resolved ONCE, at the design-graph boundary;
- Cost Estimation receives the canonical code;
- the Composer receives that same code (and does not re-resolve it);
- the resulting ArchitectureResult carries the canonical code in
  inputs_ref and in its boundaries.

Run: poetry run python -m unittest discover tests
"""
import unittest
import uuid
from unittest.mock import patch

from ai_architect.agents.architecture import run as composer_run
from ai_architect.agents.design import graph as design_graph
from ai_architect.agents.design import run as design_run

from ai_architect.domain.requirements.models import RequirementsExtraction
from ai_architect.domain.requirements.state import RequirementsState
from ai_architect.services.requirements.validator import RequirementsValidator

from tests.test_architecture_composer import (
    DESIGN_SESSION,
    PROVIDER,
    REQ_SESSION,
    _FakeBoundAgent,
    _FakeRawAgent,
    _llm_output,
    _upstream,
)

CANONICAL = "us-east-1"
USER_TEXT = "US East (N. Virginia)"


def _requirements_state(region: str = USER_TEXT) -> RequirementsState:
    extraction = RequirementsExtraction(
        application_description="document upload service",
        cloud_provider="aws",
        region=region,
        target_users_dau="2000",
        storage_characteristics="object storage",
        retention="90 days",
        availability="99.9%",
    )
    validation = RequirementsValidator().validate(extraction)
    return RequirementsState.from_extraction(
        session_id=REQ_SESSION,
        original_request="Design an online service.",
        extraction=extraction,
        validation_result=validation,
    )


def _design_state(region: str = USER_TEXT) -> dict:
    return {
        "session_id": DESIGN_SESSION,
        "user_request": "Design an online service.",
        "requirements_state": _requirements_state(region),
        "capacity_result": None,
        "component_selection": None,
        "cost_result": None,
        "architecture": None,
        "region_code": "",
        "status": "running",
        "error": None,
        "warnings": [],
    }


class _RecordingPricingService:
    """Captures the region Cost Estimation asks pricing to use."""

    regions: list = []

    def price_components(self, selection, region_code):
        _RecordingPricingService.regions.append(region_code)
        return [], []


class _RecordingCostCalculator:
    regions: list = []

    def calculate(
        self, capacity, selection, priced, region_code, usage_estimates=None
    ):
        _RecordingCostCalculator.regions.append(region_code)
        from ai_architect.domain.cost.models import CostEstimationResult

        return CostEstimationResult()


def _empty_estimation(*args, **kwargs):
    from ai_architect.services.cost.usage_estimator import (
        UsageEstimationOutcome,
    )

    return UsageEstimationOutcome()


class CostStageRegionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _RecordingPricingService.regions = []
        _RecordingCostCalculator.regions = []

    async def test_cost_stage_receives_canonical_region(self):
        state = _design_state()
        state["capacity_result"] = object()
        state["component_selection"] = object()

        with (
            patch.object(design_graph, "PricingService", _RecordingPricingService),
            patch.object(design_graph, "CostCalculator", _RecordingCostCalculator),
            patch.object(design_graph, "run_usage_estimation", _empty_estimation),
        ):
            update = await design_graph.cost_estimation_stage(state)

        self.assertEqual(_RecordingPricingService.regions, [CANONICAL])
        self.assertEqual(_RecordingCostCalculator.regions, [CANONICAL])

    async def test_cost_stage_publishes_resolved_region(self):
        state = _design_state()
        state["capacity_result"] = object()
        state["component_selection"] = object()

        with (
            patch.object(design_graph, "PricingService", _RecordingPricingService),
            patch.object(design_graph, "CostCalculator", _RecordingCostCalculator),
            patch.object(design_graph, "run_usage_estimation", _empty_estimation),
        ):
            update = await design_graph.cost_estimation_stage(state)

        self.assertEqual(update["region_code"], CANONICAL)

    async def test_canonical_code_input_still_works(self):
        state = _design_state(region=CANONICAL)
        state["capacity_result"] = object()
        state["component_selection"] = object()

        with (
            patch.object(design_graph, "PricingService", _RecordingPricingService),
            patch.object(design_graph, "CostCalculator", _RecordingCostCalculator),
            patch.object(design_graph, "run_usage_estimation", _empty_estimation),
        ):
            update = await design_graph.cost_estimation_stage(state)

        self.assertEqual(update["region_code"], CANONICAL)
        self.assertEqual(_RecordingPricingService.regions, [CANONICAL])

    async def test_unresolvable_region_fails_the_stage_clearly(self):
        state = _design_state(region="Atlantis")
        state["capacity_result"] = object()
        state["component_selection"] = object()

        update = await design_graph.cost_estimation_stage(state)

        self.assertEqual(update["status"], "failed")
        self.assertIn("Atlantis", update["error"])


class ComposerStageRegionTests(unittest.IsolatedAsyncioTestCase):
    """The Composer must consume the resolved region, not re-resolve it."""

    def _composer_state(self, *, region_code: str, extraction_region: str) -> dict:
        upstream = _upstream()
        state = _design_state(region=extraction_region)
        state["requirements_state"] = _requirements_state(extraction_region)
        state["capacity_result"] = type("C", (), {"calculation": upstream["capacity"]})()
        state["component_selection"] = upstream["selection"]
        state["cost_result"] = upstream["cost"]
        state["region_code"] = region_code
        return state

    async def test_composer_receives_the_resolved_region(self):
        state = self._composer_state(
            region_code=CANONICAL, extraction_region=USER_TEXT
        )
        captured = {}

        def _capture(extraction, capacity, selection, cost, region_code, session_id, **kw):
            captured["region_code"] = region_code
            raise RuntimeError("stop after capture")

        with patch.object(design_graph, "run_architecture_composer", _capture):
            await design_graph.architecture_composer_stage(state)

        self.assertEqual(captured["region_code"], CANONICAL)

    async def test_composer_does_not_re_resolve_an_unresolvable_region(self):
        """Proves the region is resolved once, not per stage.

        The extraction holds a region that could never be normalized. The
        Composer still runs, because it reuses the code Cost Estimation
        already resolved instead of normalizing again.
        """
        state = self._composer_state(
            region_code=CANONICAL, extraction_region="Atlantis"
        )
        captured = {}

        def _capture(extraction, capacity, selection, cost, region_code, session_id, **kw):
            captured["region_code"] = region_code
            raise RuntimeError("stop after capture")

        with patch.object(design_graph, "run_architecture_composer", _capture):
            await design_graph.architecture_composer_stage(state)

        self.assertEqual(captured["region_code"], CANONICAL)

    async def test_composer_falls_back_to_normalizing_when_region_unset(self):
        """A caller reaching the Composer without cost still works."""
        state = self._composer_state(region_code="", extraction_region=USER_TEXT)
        captured = {}

        def _capture(extraction, capacity, selection, cost, region_code, session_id, **kw):
            captured["region_code"] = region_code
            raise RuntimeError("stop after capture")

        with patch.object(design_graph, "run_architecture_composer", _capture):
            await design_graph.architecture_composer_stage(state)

        self.assertEqual(captured["region_code"], CANONICAL)


class ArchitectureArtifactRegionTests(unittest.TestCase):
    """The composed artifact must carry the canonical region."""

    def _compose(self):
        upstream = _upstream()
        # Same upstream artifacts, expressed in the canonical region.
        extraction = upstream["extraction"].model_copy(
            update={"region": USER_TEXT}
        )
        cost = upstream["cost"].model_copy(
            update={
                "lines": [
                    line.model_copy(update={"region_code": CANONICAL})
                    for line in upstream["cost"].lines
                ]
            }
        )
        llm_output = _llm_output().model_copy(
            update={
                "boundaries": [f"region:{CANONICAL}", "single-region"],
            }
        )
        from langchain_core.messages import AIMessage

        bound = _FakeBoundAgent({"raw": AIMessage(content=""), "parsed": llm_output})
        raw = _FakeRawAgent()

        with (
            patch.object(
                composer_run,
                "create_architecture_composer_agent_with_raw",
                return_value=bound,
            ),
            patch.object(
                composer_run,
                "create_architecture_composer_agent_raw",
                return_value=raw,
            ),
        ):
            result = composer_run.run_architecture_composer(
                extraction,
                upstream["capacity"],
                upstream["selection"],
                cost,
                CANONICAL,
                DESIGN_SESSION,
                requirements_session_id=REQ_SESSION,
            )
        return result, upstream, cost


    def test_artifact_records_canonical_region(self):
        result, _up, _cost = self._compose()

        self.assertEqual(result.inputs_ref.region_code, CANONICAL)
        self.assertIn(f"region:{CANONICAL}", result.boundaries)

    def test_artifact_passes_deterministic_validation(self):
        from ai_architect.services.architecture.validator import validate_architecture

        result, upstream, cost = self._compose()
        errors = validate_architecture(
            result,
            selection=upstream["selection"],
            capacity=upstream["capacity"],
            cost=cost,
            extraction=upstream["extraction"].model_copy(
                update={"region": USER_TEXT}
            ),
            requirements_session_id=REQ_SESSION,
            design_session_id=DESIGN_SESSION,
            provider=PROVIDER,
            region_code=CANONICAL,
        )
        self.assertEqual(errors, [])


class DesignRunInitialStateTests(unittest.TestCase):
    def test_initial_state_declares_the_region_channel(self):
        """The graph state must carry region_code so it can be resolved once."""
        import inspect

        source = inspect.getsource(design_run.run_design_workflow)
        self.assertIn('"region_code": ""', source)


if __name__ == "__main__":
    unittest.main()



