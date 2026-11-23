"""Slice 3.5 tests: the API live design-run boundary.

These tests pin the boundary itself, not the pipeline: the API must
delegate to the single existing ``run_design_workflow`` entry point,
preserve requirements HITL, persist a truthful RUNNING state, and never
duplicate orchestration. No LLM, Qdrant or pricing call is made here;
the real pipeline is covered by one end-to-end smoke.

Run: poetry run python -m unittest discover tests
"""
import asyncio
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

from tests.api_auth import api_client

from ai_architect.agents.design.state import DesignStatus
from ai_architect.api import design_run as api_design_run
from ai_architect.api import sessions as api_sessions_mod
from ai_architect.api.main import create_app
from ai_architect.domain.requirements.models import (
    ClarificationCandidate,
    RequirementImpact,
    RequirementsExtraction,
)
from ai_architect.domain.requirements.state import (
    RequirementsState,
    SessionStatus,
)
from ai_architect.infrastructure.checkpoint import sqlite as ckpt_sqlite
from ai_architect.infrastructure.checkpoint.design import load_design_snapshot
from ai_architect.services.requirements.validator import RequirementsValidator


def uuid6_from_langgraph():
    """The id generator the design graph itself uses."""
    from langgraph.checkpoint.base.id import uuid6 as lg_uuid6

    return lg_uuid6()


def _extraction(**overrides) -> RequirementsExtraction:
    payload = {
        "application_description": "a generic event ingestion pipeline",
        "cloud_provider": "aws",
        "region": "us-east-1",
        "target_users_dau": "10000",
        "storage_characteristics": "durable object storage",
        "retention": "30 days",
        "availability": "99.9%",
    }
    payload.update(overrides)
    return RequirementsExtraction(**payload)


def _extraction_with_question() -> RequirementsExtraction:
    return _extraction(
        region="",
        clarification_candidates=[
            ClarificationCandidate(
                requirement_key="region",
                requirement="geographic deployment region",
                question="Which region should this deploy to?",
                reason="Region changes cost and latency.",
                impact=RequirementImpact.ARCHITECTURE,
            )
        ],
    )


def _state(extraction: RequirementsExtraction, session_id: str) -> RequirementsState:
    validation = RequirementsValidator().validate(extraction)
    return RequirementsState.from_extraction(
        session_id=session_id,
        original_request="design a generic pipeline",
        extraction=extraction,
        validation_result=validation,
    )


def _ready_state(session_id: str) -> RequirementsState:
    return _state(_extraction(), session_id)


class DesignRunBoundaryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = ckpt_sqlite.CHECKPOINT_DB_PATH
        ckpt_sqlite.CHECKPOINT_DB_PATH = os.path.join(
            self._tmp.name, "checkpoints.sqlite"
        )
        self._ckpt_sqlite = ckpt_sqlite
        self.client = api_client(create_app())
        self.calls: list[tuple] = []

    def tearDown(self):
        self._ckpt_sqlite.CHECKPOINT_DB_PATH = self._orig_path
        self._tmp.cleanup()

    # --- helpers ---------------------------------------------------
    def _record_executor(self, outcome: str = "ready", error: str | None = None):
        """Executor that records delegation without running any stage."""

        async def executor(design_session_id, user_request, requirements_state):
            self.calls.append((design_session_id, user_request, requirements_state))
            if outcome == "failed":
                await api_design_run.save_design_run_marker(
                    design_session_id,
                    status=DesignStatus.FAILED.value,
                    user_request=user_request,
                    requirements_state=requirements_state,
                    error=error or "stage failed",
                    after_graph=True,
                )

        return executor

    def _create_ready_session(self) -> str:
        with patch.object(
            api_sessions_mod,
            "build_initial_requirements_state",
            side_effect=lambda request, session_id=None: _ready_state(
                session_id or str(uuid.uuid4())
            ),
        ):
            response = self.client.post(
                "/design/sessions", json={"user_request": "design a generic pipeline"}
            )
        self.assertEqual(response.status_code, 200)
        return response.json()["session_id"]

    def _create_session_needing_clarification(self) -> str:
        with patch.object(
            api_sessions_mod,
            "build_initial_requirements_state",
            side_effect=lambda request, session_id=None: _state(
                _extraction_with_question(), session_id or str(uuid.uuid4())
            ),
        ):
            response = self.client.post(
                "/design/sessions", json={"user_request": "design a generic pipeline"}
            )
        self.assertEqual(response.status_code, 200)
        return response.json()["session_id"]

    def _run(self, session_id: str, executor) -> dict:
        try:
            with patch.object(api_design_run, "_launch_in_background", executor):
                response = self.client.post(f"/design/sessions/{session_id}/run")
        finally:
            # The fake launcher completes inline and never owns the
            # background task, so the real launcher's release hand-off
            # has no task to run; release here to mirror "run finished".
            api_design_run._release_requirements(session_id)
        return {"status": response.status_code, "body": response.json()}

    # --- starting a run --------------------------------------------
    def test_01_ready_session_starts_a_run(self):
        session_id = self._create_ready_session()
        result = self._run(session_id, self._record_executor())

        self.assertEqual(result["status"], 200)
        body = result["body"]
        self.assertEqual(body["status"], DesignStatus.RUNNING.value)
        self.assertTrue(body["session_id"])
        # The design session id is distinct from the requirements thread.
        self.assertNotEqual(body["session_id"], session_id)
        self.assertEqual(body["requirements_session_id"], session_id)

    def test_02_run_delegates_to_the_shared_entrypoint(self):
        session_id = self._create_ready_session()
        self._run(session_id, self._record_executor())

        self.assertEqual(len(self.calls), 1)
        design_session_id, user_request, requirements_state = self.calls[0]
        self.assertEqual(user_request, "design a generic pipeline")
        # The resolved requirements are handed to the shared workflow so
        # the requirements stage does not re-run or read stdin.
        self.assertIsInstance(requirements_state, RequirementsState)
        self.assertEqual(requirements_state.status, SessionStatus.READY)
        self.assertIsNotNone(requirements_state.extraction)

    def test_03_running_state_is_persisted_and_readable(self):
        session_id = self._create_ready_session()
        result = self._run(session_id, self._record_executor())
        design_session_id = result["body"]["session_id"]

        response = self.client.get(f"/design/sessions/{design_session_id}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["session_id"], design_session_id)
        self.assertEqual(body["requirements_session_id"], session_id)
        self.assertEqual(body["stages"]["design_status"], DesignStatus.RUNNING.value)
        self.assertFalse(body["stages"]["has_design_run"])
        # Requirements are already visible while the run proceeds.
        self.assertEqual(body["requirements"]["region"], "us-east-1")

    def test_04_architecture_endpoint_reports_not_yet_available(self):
        session_id = self._create_ready_session()
        result = self._run(session_id, self._record_executor())
        design_session_id = result["body"]["session_id"]

        architecture = self.client.get(
            f"/design/sessions/{design_session_id}/architecture"
        )
        cost = self.client.get(f"/design/sessions/{design_session_id}/cost")
        self.assertEqual(architecture.status_code, 404)
        self.assertEqual(
            architecture.json()["detail"]["code"], "architecture_not_available"
        )
        self.assertEqual(cost.status_code, 404)
        self.assertEqual(
            cost.json()["detail"]["code"], "cost_not_available"
        )

    def test_05_failed_run_is_recorded(self):
        session_id = self._create_ready_session()
        result = self._run(
            session_id,
            self._record_executor(outcome="failed", error="composer exploded"),
        )
        design_session_id = result["body"]["session_id"]

        body = self.client.get(f"/design/sessions/{design_session_id}").json()
        self.assertEqual(body["stages"]["design_status"], DesignStatus.FAILED.value)
        self.assertEqual(body["error"], "composer exploded")

    def test_06_escaping_exception_leaves_a_failed_marker(self):
        """An exception outside graph state must not strand RUNNING."""
        session_id = self._create_ready_session()

        async def executor(design_session_id, user_request, requirements_state):
            with patch.object(
                api_design_run,
                "run_design_workflow",
                side_effect=RuntimeError("model gate refused"),
            ):
                await api_design_run._execute(
                    design_session_id, user_request, requirements_state
                )

        result = self._run(session_id, executor)
        design_session_id = result["body"]["session_id"]

        snapshot = asyncio.run(load_design_snapshot(design_session_id))
        self.assertEqual(snapshot.status, DesignStatus.FAILED.value)
        self.assertIn("model gate refused", snapshot.error)

    # --- requirements HITL is preserved ----------------------------
    def test_07_run_refused_while_clarification_outstanding(self):
        session_id = self._create_session_needing_clarification()
        result = self._run(session_id, self._record_executor())

        self.assertEqual(result["status"], 409)
        self.assertEqual(result["body"]["detail"]["code"], "clarification_required")
        self.assertEqual(self.calls, [])

    def test_08_run_allowed_after_answering_clarification(self):
        session_id = self._create_session_needing_clarification()

        answered = self.client.post(
            f"/design/sessions/{session_id}/messages",
            json={"text": "Mumbai region"},
        )
        self.assertEqual(answered.status_code, 200)
        self.assertEqual(answered.json()["status"], SessionStatus.READY.value)

        result = self._run(session_id, self._record_executor())
        self.assertEqual(result["status"], 200)
        self.assertEqual(len(self.calls), 1)

    def test_09_you_decide_still_records_an_assumption(self):
        session_id = self._create_session_needing_clarification()

        deferred = self.client.post(
            f"/design/sessions/{session_id}/messages",
            json={"text": "you decide"},
        )
        self.assertEqual(deferred.status_code, 409)
        self.assertEqual(
            deferred.json()["detail"]["code"], "clarification_required"
        )

    def test_10_you_decide_records_assumption_when_deferrable(self):
        with patch.object(
            api_sessions_mod,
            "build_initial_requirements_state",
            side_effect=lambda request, session_id=None: _state(
                _extraction(storage_characteristics=""),
                session_id or str(uuid.uuid4()),
            ).model_copy(
                update={
                    "clarification_items": [
                        {
                            "id": "storage_characteristics",
                            "requirement_key": "storage_characteristics",
                            "requirement": "storage characteristics",
                            "question": "What storage requirements apply?",
                            "reason": "Changes the architecture.",
                            "impact": RequirementImpact.ARCHITECTURE,
                            "status": "pending",
                        }
                    ],
                    "status": SessionStatus.NEEDS_CLARIFICATION,
                }
            ),
        ):
            created = self.client.post(
                "/design/sessions", json={"user_request": "design a generic pipeline"}
            )
        session_id = created.json()["session_id"]

        deferred = self.client.post(
            f"/design/sessions/{session_id}/messages", json={"text": "you decide"}
        )
        self.assertEqual(deferred.status_code, 200)
        body = deferred.json()
        self.assertEqual(len(body["assumptions"]), 1)
        self.assertEqual(body["assumptions"][0]["source"], "architect_default")

        result = self._run(session_id, self._record_executor())
        self.assertEqual(result["status"], 200)

    # --- refusals --------------------------------------------------
    def test_11_unknown_session_run_returns_404(self):
        result = self._run("no-such-session", self._record_executor())
        self.assertEqual(result["status"], 404)
        self.assertEqual(result["body"]["detail"]["code"], "session_not_found")

    def test_12_run_does_not_mutate_the_requirements_session(self):
        session_id = self._create_ready_session()
        before = self.client.get(f"/design/sessions/{session_id}").json()
        result = self._run(session_id, self._record_executor())
        after = self.client.get(f"/design/sessions/{session_id}").json()

        self.assertEqual(result["status"], 200)
        self.assertEqual(before["requirements"], after["requirements"])
        self.assertFalse(after["stages"]["has_design_run"])
        self.assertEqual(after["stages"]["design_status"], "")

    def test_16_advertised_assumable_matches_enforcement(self):
        """A question the backend will refuse must not claim assumable.

        Regression: the session view used to default ``assumable`` to True
        whenever it could not find the candidate in the extraction, so a
        material question such as cloud_provider was advertised as
        delegatable and then refused with 409.
        """
        session_id = self._create_session_needing_clarification()
        advertised = self.client.get(
            f"/design/sessions/{session_id}"
        ).json()["pending_clarification"]
        self.assertFalse(advertised["assumable"])

        refused = self.client.post(
            f"/design/sessions/{session_id}/messages", json={"text": "you decide"}
        )
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(
            refused.json()["detail"]["code"], "clarification_required"
        )

    def test_17_deferrable_question_is_advertised_as_assumable(self):
        """The flag must stay accurate in the permissive direction too."""
        deferrable = _extraction(storage_characteristics="")
        with patch.object(
            api_sessions_mod,
            "build_initial_requirements_state",
            side_effect=lambda request, session_id=None: _state(
                deferrable,
                session_id or str(uuid.uuid4()),
            ).model_copy(
                update={
                    "clarification_items": [
                        {
                            "id": "storage_characteristics",
                            "requirement_key": "storage_characteristics",
                            "requirement": "storage characteristics",
                            "question": "What storage requirements apply?",
                            "reason": "Changes the architecture.",
                            "impact": RequirementImpact.ARCHITECTURE,
                            "status": "pending",
                        }
                    ],
                    "status": SessionStatus.NEEDS_CLARIFICATION,
                }
            ),
        ):
            created = self.client.post(
                "/design/sessions", json={"user_request": "design something"}
            )

        advertised = created.json()["pending_clarification"]
        self.assertTrue(advertised["assumable"])
        accepted = self.client.post(
            f"/design/sessions/{created.json()['session_id']}/messages",
            json={"text": "you decide"},
        )
        self.assertEqual(accepted.status_code, 200)

    # --- one engine, checkpoint ordering ---------------------------
    def test_13_api_uses_the_same_entrypoint_as_the_cli(self):
        """CLI and API must both call run_design_workflow."""
        import inspect

        import ai_architect.cli.design as cli_design
        from ai_architect.agents.design import run as design_run

        api_source = inspect.getsource(api_design_run)
        self.assertIn("run_design_workflow(", api_source)
        self.assertIn("from ai_architect.agents.design.run import run_design_workflow", api_source)
        # The CLI still uses that same function.
        self.assertIn("run_design_workflow", inspect.getsource(cli_design))
        # And the API never calls the stage runners itself.
        for stage in (
            "run_capacity_modeling",
            "run_component_selection",
            "run_architecture_composer",
            "CostCalculator",
            "PricingService",
        ):
            self.assertNotIn(stage, api_source)

    def test_14_marker_ids_sort_around_graph_checkpoints(self):
        """A RUNNING marker must lose to real graph checkpoints.

        LangGraph orders checkpoints as text and mints time-ordered
        UUIDv6 ids, so the marker must be a valid UUIDv6 carrying an
        earlier timestamp than the graph will use (and a later one when
        recording a terminal failure).
        """
        from ai_architect.infrastructure.checkpoint.design import (
            marker_checkpoint_id,
        )

        before = marker_checkpoint_id(after_graph=False)
        after = marker_checkpoint_id(after_graph=True)
        graph_id = str(uuid6_from_langgraph())

        # Valid UUIDv6 shape, which a prefixed string is not.
        parsed = uuid.UUID(before)
        self.assertEqual(parsed.version, 6)

        self.assertLess(before, graph_id)
        self.assertGreater(after, graph_id)

    def test_15_graph_checkpoints_win_over_running_marker(self):
        session_id = self._create_ready_session()
        result = self._run(session_id, self._record_executor())
        design_session_id = result["body"]["session_id"]

        # Simulate the graph writing its own newer checkpoint.
        asyncio.run(self._write_graph_ready_checkpoint(design_session_id))

        body = self.client.get(f"/design/sessions/{design_session_id}").json()
        self.assertEqual(body["stages"]["design_status"], DesignStatus.READY.value)

    async def _write_graph_ready_checkpoint(self, session_id: str) -> None:
        from ai_architect.infrastructure.checkpoint.sqlite import (
            create_checkpointer,
        )

        channels = {
            "session_id": session_id,
            "user_request": "design a generic pipeline",
            "requirements_state": _ready_state(
                "nested-requirements"
            ).model_dump(mode="json"),
            "capacity_result": None,
            "component_selection": None,
            "cost_result": None,
            "architecture": None,
            "status": DesignStatus.READY.value,
            "error": None,
            "warnings": [],
        }
        checkpoint = {
            "v": 4,
            # Minted the way LangGraph mints checkpoint ids.
            "id": str(uuid6_from_langgraph()),
            "ts": "",
            "channel_values": channels,
            "channel_versions": {},
            "versions_seen": {},
            "updated_channels": sorted(channels),
        }
        async with create_checkpointer() as checkpointer:
            await checkpointer.aput(
                {"configurable": {"thread_id": session_id, "checkpoint_ns": ""}},
                checkpoint,
                {"source": "update", "step": 0, "writes": {}},
                {},
            )


class DesignRunSingleFlightTests(unittest.TestCase):
    """At most one design run may be in flight per requirements session.

    Two concurrent starts would race two full pipelines (duplicate LLM
    spend, two competing design sessions), so the second start is refused
    with run_in_progress until the first finishes; a finished, failed or
    cancelled run releases the session again.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = ckpt_sqlite.CHECKPOINT_DB_PATH
        ckpt_sqlite.CHECKPOINT_DB_PATH = os.path.join(
            self._tmp.name, "checkpoints.sqlite"
        )
        self._ckpt_sqlite = ckpt_sqlite

    def tearDown(self):
        self._ckpt_sqlite.CHECKPOINT_DB_PATH = self._orig_path
        self._tmp.cleanup()
        api_design_run._ACTIVE_REQUIREMENTS.clear()

    def test_01_second_start_while_in_flight_is_refused(self):
        session_id = "req-single-flight"
        state = _ready_state(session_id)

        async def scenario():
            gate = asyncio.Event()

            async def slow_executor(
                design_session_id, user_request, requirements_state
            ):
                await gate.wait()

            first = asyncio.create_task(
                api_design_run.start_design_run(state, executor=slow_executor)
            )
            for _ in range(200):
                if session_id in api_design_run._ACTIVE_REQUIREMENTS:
                    break
                await asyncio.sleep(0)
            else:
                self.fail("reservation was never taken")

            with self.assertRaises(api_design_run.DesignRunError) as ctx:
                await api_design_run.start_design_run(
                    state, executor=slow_executor
                )
            self.assertEqual(ctx.exception.code, "run_in_progress")
            self.assertEqual(ctx.exception.status_code, 409)

            gate.set()
            design_session_id, status = await first
            self.assertTrue(design_session_id)
            self.assertEqual(status, DesignStatus.RUNNING.value)

        asyncio.run(scenario())
        self.assertNotIn(session_id, api_design_run._ACTIVE_REQUIREMENTS)

    def test_02_completion_releases_the_session_for_a_new_run(self):
        session_id = "req-single-flight-2"
        state = _ready_state(session_id)

        async def scenario():
            async def quick_executor(
                design_session_id, user_request, requirements_state
            ):
                return None

            first_id, _ = await api_design_run.start_design_run(
                state, executor=quick_executor
            )
            self.assertNotIn(session_id, api_design_run._ACTIVE_REQUIREMENTS)

            second_id, _ = await api_design_run.start_design_run(
                state, executor=quick_executor
            )
            self.assertNotEqual(first_id, second_id)

        asyncio.run(scenario())
        self.assertNotIn(session_id, api_design_run._ACTIVE_REQUIREMENTS)

    def test_03_failed_start_releases_the_session(self):
        session_id = "req-single-flight-3"
        state = _ready_state(session_id)

        async def scenario():
            async def exploding_executor(
                design_session_id, user_request, requirements_state
            ):
                raise RuntimeError("boom")

            with self.assertRaises(RuntimeError):
                await api_design_run.start_design_run(
                    state, executor=exploding_executor
                )
            self.assertNotIn(session_id, api_design_run._ACTIVE_REQUIREMENTS)

        asyncio.run(scenario())
        self.assertNotIn(session_id, api_design_run._ACTIVE_REQUIREMENTS)

    def test_04_different_requirements_sessions_do_not_serialize(self):
        first_state = _ready_state("req-single-flight-a")
        second_state = _ready_state("req-single-flight-b")

        async def scenario():
            gate = asyncio.Event()
            entered = asyncio.Event()

            async def slow_executor(
                design_session_id, user_request, requirements_state
            ):
                entered.set()
                await gate.wait()

            first = asyncio.create_task(
                api_design_run.start_design_run(first_state, executor=slow_executor)
            )

            # Wait until the first run is genuinely in flight, not merely
            # reserved: the active-session reservation is taken before the
            # initial RUNNING marker write, so starting the second run at
            # that point would race the second start's marker write against
            # the first one (two SQLite connections entering setup()) and
            # flake with "database is locked". The executor is only entered
            # after the first marker write has completed and its connection
            # closed, so waiting for entry keeps the writes ordered.
            entered_wait = asyncio.create_task(entered.wait())
            done, _ = await asyncio.wait(
                {first, entered_wait},
                timeout=10,
                return_when=asyncio.FIRST_COMPLETED,
            )
            entered_wait.cancel()
            if first in done:
                # The first run died before reaching the executor; surface
                # that error instead of masking it as a timeout.
                await first
            self.assertTrue(entered.is_set(), "executor was never entered")
            self.assertIn(
                "req-single-flight-a", api_design_run._ACTIVE_REQUIREMENTS
            )

            # A different requirements session may still start.
            async def quick_executor(
                design_session_id, user_request, requirements_state
            ):
                return None

            second_id, _ = await api_design_run.start_design_run(
                second_state, executor=quick_executor
            )

            gate.set()
            first_id, _ = await first
            self.assertNotEqual(first_id, second_id)

        asyncio.run(scenario())


class ProviderIndependenceRunTests(unittest.TestCase):
    """Starting a run must not leak provider knowledge into the API."""

    def test_api_run_module_has_no_model_or_provider_names(self):
        import pathlib

        source = (
            pathlib.Path(__file__).resolve().parents[1]
            / "src"
            / "ai_architect"
            / "api"
            / "design_run.py"
        ).read_text(encoding="utf-8")
        for token in (
            "ollama",
            "Ollama",
            "opencode",
            "OpenCode",
            "space-bunny",
            "gpt-oss",
            "ChatOpenAI",
            "ChatOllama",
            "active_provider",
            "configured_model",
        ):
            self.assertNotIn(token, source, f"API leaked {token!r}")


if __name__ == "__main__":
    unittest.main()
