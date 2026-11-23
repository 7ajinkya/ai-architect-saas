"""Start a design run over the existing orchestration.

This module owns no pipeline. It validates that the requirements stage
is already complete, then calls the SAME
``agents.design.run.run_design_workflow`` entry point the CLI uses, so
there is exactly one design engine.

Session identity is unchanged from Slice 3: the design session id is the
design graph's own checkpoint thread id, created here and nested
requirements state keeps its own id. No second graph, no second
checkpoint database, no re-implemented stage.

Requirements HITL is not bypassed: a run may only start once the
requirements state is READY, which is exactly the state produced by the
existing ``/design/sessions`` + ``/messages`` endpoints.
"""

import asyncio
import uuid
from typing import Awaitable, Callable, Optional

from ai_architect.agents.design.run import run_design_workflow
from ai_architect.agents.design.state import DesignStatus
from ai_architect.domain.requirements.state import (
    RequirementsState,
    SessionStatus,
)
from ai_architect.infrastructure.checkpoint.design import save_design_run_marker

# Strong references to in-flight background tasks. asyncio only keeps a
# weak reference, so without this a running design could be garbage
# collected mid-flight.
_RUNNING_TASKS: set[asyncio.Task] = set()

# Requirements sessions with an in-flight design run. A second start for
# the same requirements session is refused with run_in_progress instead
# of racing two runs (duplicate LLM spend, competing design sessions).
# Different requirements sessions never serialize against each other.
_ACTIVE_REQUIREMENTS: set[str] = set()
_ACTIVE_LOCK = asyncio.Lock()


def _release_requirements(session_id: str) -> None:
    """Release the single-flight reservation for a requirements session.

    Idempotent; safe to call from either the run's task or the starter.
    """
    if session_id:
        _ACTIVE_REQUIREMENTS.discard(session_id)

Executor = Callable[[str, str, RequirementsState], Awaitable[None]]


class DesignRunError(Exception):
    """Recoverable refusal to start a design run."""

    def __init__(self, detail: str, code: str = "error", status_code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.status_code = status_code


async def _execute(
    design_session_id: str,
    user_request: str,
    requirements_state: RequirementsState,
) -> None:
    """Run the shared workflow, then leave a truthful final marker.

    Stage failures are recorded inside graph state, so the normal path
    simply returns. This guard exists only for an exception that escapes
    the graph (for example a refused model gate or a checkpoint write
    failure), so a caller never polls a session stuck on RUNNING.
    """
    try:
        await run_design_workflow(
            user_request=user_request,
            session_id=design_session_id,
            requirements_state=requirements_state,
        )
    except asyncio.CancelledError:
        await save_design_run_marker(
            design_session_id,
            status=DesignStatus.FAILED.value,
            user_request=user_request,
            requirements_state=requirements_state,
            error="Design run was cancelled.",
            after_graph=True,
        )
        raise
    except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
        await save_design_run_marker(
            design_session_id,
            status=DesignStatus.FAILED.value,
            user_request=user_request,
            requirements_state=requirements_state,
            error=f"{type(exc).__name__}: {exc}",
            after_graph=True,
        )
    finally:
        # The run is over (success, recorded failure, or cancellation):
        # the requirements session may start another run.
        _release_requirements(
            str(getattr(requirements_state, "session_id", "") or "")
        )


async def _launch_in_background(
    design_session_id: str,
    user_request: str,
    requirements_state: RequirementsState,
) -> None:
    task = asyncio.create_task(
        _execute(design_session_id, user_request, requirements_state)
    )
    _RUNNING_TASKS.add(task)
    task.add_done_callback(_RUNNING_TASKS.discard)


async def start_design_run(
    requirements_state: Optional[RequirementsState],
    *,
    executor: Optional[Executor] = None,
) -> tuple[str, str]:
    """Start one design run and return (design_session_id, status).

    ``executor`` exists so tests can drive the run deterministically
    instead of racing a background task. Production callers use the
    default background launcher.
    """
    if requirements_state is None:
        raise DesignRunError(
            "Requirements session not found.", code="session_not_found",
            status_code=404,
        )

    if requirements_state.extraction is None:
        raise DesignRunError(
            "Session has no requirements extraction yet.",
            code="state_invalid",
            status_code=409,
        )

    if requirements_state.status is not SessionStatus.READY:
        raise DesignRunError(
            "Requirements are not ready; answer the outstanding "
            "clarification before starting a design run.",
            code="clarification_required",
            status_code=409,
        )

    requirements_session_id = str(
        getattr(requirements_state, "session_id", "") or ""
    )
    if requirements_session_id:
        async with _ACTIVE_LOCK:
            if requirements_session_id in _ACTIVE_REQUIREMENTS:
                raise DesignRunError(
                    "A design run is already in progress for this "
                    "requirements session.",
                    code="run_in_progress",
                    status_code=409,
                )
            _ACTIVE_REQUIREMENTS.add(requirements_session_id)

    try:
        design_session_id = str(uuid.uuid4())
        user_request = requirements_state.original_request

        # Persist a truthful RUNNING marker before any LLM call, so polling
        # clients see the run instead of a missing session.
        await save_design_run_marker(
            design_session_id,
            status=DesignStatus.RUNNING.value,
            user_request=user_request,
            requirements_state=requirements_state,
        )

        launch = executor or _launch_in_background
        if executor is not None:
            # An inline executor completes the whole run before returning,
            # so the reservation is released here. The background launcher
            # hands release off to the run's own task instead.
            try:
                await launch(design_session_id, user_request, requirements_state)
            finally:
                _release_requirements(requirements_session_id)
        else:
            await launch(design_session_id, user_request, requirements_state)
    except BaseException:
        # Marker or hand-off failure: never leave the session reserved.
        _release_requirements(requirements_session_id)
        raise

    return design_session_id, DesignStatus.RUNNING.value
