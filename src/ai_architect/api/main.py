"""FastAPI application exposing the design session lifecycle.

Product-level endpoints only; LangGraph state is never returned raw.

This layer is an adapter. It reads the same checkpoints the existing
workflow already writes and re-serializes existing domain artifacts. It
contains no capacity maths, component selection, pricing, composition,
validation or LLM calls, and it never recomputes a domain value.
"""

import asyncio
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from ai_architect.api.design_run import DesignRunError, start_design_run
from ai_architect.api.design_state import design_stages
from ai_architect.api.models import (
    ApprovalView,
    ApproveDesignRequest,
    ApproveProvisioningRequest,
    ClarificationView,
    CreateSessionRequest,
    DesignRunView,
    DesignStages,
    ErrorView,
    InputSuggestionView,
    InputSuggestionsView,
    OptimizationRecommendationView,
    OptimizationRecommendationsView,
    ProvisionRequest,
    ProvisioningResultView,
    SessionMessageRequest,
    SessionSummary,
    SessionView,
    SkippedInputView,
    TerraformPlanView,
    TerraformRequest,
)
from ai_architect.api.sessions import (
    SessionError,
    create_session,
    pending_candidate,
    submit_message,
)
from ai_architect.api.security import cors_origins, require_api_token
from ai_architect.domain.architecture.critique import (
    DesignCritique,
    QualityGateResult,
)
from ai_architect.domain.architecture.models import ArchitectureResult
from ai_architect.domain.cost.models import CostEstimationResult
from ai_architect.domain.requirements.state import (
    RequirementsState,
    SessionStatus,
)
from ai_architect.infrastructure.checkpoint.design import (
    DesignSessionSnapshot,
    PersistedStateError,
    list_design_sessions,
    load_design_snapshot,
    save_session_record,
)
from ai_architect.services.provisioning.executor import (
    ExecutionRefused,
    build_pending_apply_record,
    build_provisioning_approval,
    build_provisioning_result,
    consume_provisioning_approval,
    invalidate_provisioning_approval,
    require_destroy_plan_review,
    require_provisioning_approval,
    require_provisioning_review,
    run_terraform_apply,
    run_terraform_destroy,
    run_terraform_destroy_plan,
    run_terraform_drift_check,
    run_terraform_plan,
    workspace_fingerprint,
    workspace_path,
)
from ai_architect.services.requirements.assumptions import evaluate_deferral

API_TITLE = "AI Architect Design API"
API_VERSION = "0.2.0"


def _clarification_view(state: RequirementsState) -> ClarificationView | None:
    item = (state.pending_clarifications() or [None])[0]
    if item is None:
        return None
    candidate = pending_candidate(state)
    # Default to not-assumable: the view must never advertise a
    # delegation that submission would then refuse.
    assumable = (
        evaluate_deferral(candidate, state.extraction).deferrable
        if candidate is not None and state.extraction is not None
        else False
    )
    return ClarificationView(
        requirement_key=item.requirement_key,
        requirement=item.requirement,
        question=item.question,
        reason=item.reason,
        impact=str(item.impact),
        assumable=assumable,
    )


def _session_view(
    state: RequirementsState | None,
    snapshot: DesignSessionSnapshot | None = None,
) -> SessionView:
    extraction = state.extraction if state is not None else None
    # The addressed design session id wins over the nested requirements
    # session id: a completed design run runs the requirements stage under
    # its own thread, so echoing the nested id would contradict the URL.
    if snapshot is not None and snapshot.session_id:
        session_id = snapshot.session_id
    else:
        session_id = state.session_id if state is not None else ""
    requirements_session_id = (
        state.session_id
        if state is not None and state.session_id != session_id
        else ""
    )
    return SessionView(
        session_id=session_id,
        requirements_session_id=requirements_session_id,
        status=state.status.value if state is not None else "",
        user_request=state.original_request if state is not None else "",
        requirements=(
            extraction.model_dump(mode="json") if extraction is not None else None
        ),
        assumptions=[
            a.model_dump(mode="json")
            for a in (extraction.assumptions if extraction is not None else [])
        ],
        pending_clarification=(
            _clarification_view(state) if state is not None else None
        ),
        warnings=list(snapshot.warnings) if snapshot is not None else [],
        error=snapshot.error if snapshot is not None else None,
        stages=(
            design_stages(snapshot) if snapshot is not None else DesignStages()
        ),
    )


def _persisted_state_error(exc: PersistedStateError) -> HTTPException:
    """Surface corrupt stored state as a clean, actionable API error."""
    return HTTPException(
        status_code=409,
        detail={
            "detail": str(exc),
            "code": "persisted_state_invalid",
        },
    )


def _server_aws_env() -> dict:
    """Explicit AWS entries from the server operator's own environment.

    The UI never sends credentials: execution can only use what the
    operator placed in the server process environment (the same trust
    as the CLI's --pass-aws-env). Absent entries mean an honest
    downstream failure, never invented credentials.
    """
    import os

    from ai_architect.services.provisioning.executor import (
        FORWARDABLE_AWS_ENV,
    )

    return {
        key: os.environ[key]
        for key in FORWARDABLE_AWS_ENV
        if os.environ.get(key)
    }


# One in-process lock per session: provisions on the same session
# serialize so a second apply can never start while the first is
# mid-flight. Cross-process safety comes from the consumed approval
# and the stored apply_running marker, which are durable.
_PROVISION_LOCKS: dict[str, asyncio.Lock] = {}


def _provision_lock(session_id: str) -> asyncio.Lock:
    lock = _PROVISION_LOCKS.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _PROVISION_LOCKS[session_id] = lock
    return lock


def _plan_architecture_mismatch(snapshot, plan: dict) -> str | None:
    """Reason the stored plan no longer targets the current architecture.

    A new design run leaves the old plan and approval records behind.
    Execution is refused unless the plan was built from the session's
    current architecture artifact; regenerating the plan is the only
    way forward.
    """
    architecture = getattr(snapshot, "architecture", None)
    artifact_id = str(getattr(architecture, "artifact_id", "") or "")
    if not artifact_id:
        return (
            "Provisioning is not available: the session has no current "
            "architecture artifact."
        )
    if plan.get("artifact_id") != artifact_id:
        return (
            "Provisioning is not available: the stored Terraform plan "
            "does not match the session's current architecture artifact. "
            "Regenerate the plan first."
        )
    return None


async def _invalidate_stale_provisioning_approval(
    session_id: str, snapshot
) -> None:
    """Force any prior positive approval to authorize nothing.

    Called after a fresh plan is generated: the human verdict covered
    the previous artifact, so it must not silently cover the new one.
    The record is kept with an explicit superseded reason for audit.
    """
    approval = getattr(snapshot, "provisioning_approval", None)
    if not isinstance(approval, dict) or not approval.get("approved"):
        return
    try:
        await save_session_record(
            session_id,
            field="provisioning_approval",
            record=invalidate_provisioning_approval(
                approval,
                "A new Terraform plan was generated; re-approve the "
                "current plan explicitly.",
            ),
        )
    except PersistedStateError as exc:
        raise _persisted_state_error(exc) from exc


async def _snapshot_or_404(session_id: str) -> DesignSessionSnapshot:
    try:
        snapshot = await load_design_snapshot(session_id)
    except PersistedStateError as exc:
        raise _persisted_state_error(exc) from exc
    if snapshot is None or snapshot.requirements_state is None:
        raise HTTPException(
            status_code=404,
            detail={
                "detail": f"Unknown session: {session_id}",
                "code": "session_not_found",
            },
        )
    return snapshot


def create_app() -> FastAPI:
    app = FastAPI(title=API_TITLE, version=API_VERSION)
    # CORS is a browser policy, not authentication: the allowlist only
    # bounds which pages may call the API. Mutating endpoints are
    # token-guarded independently (see api/security.py).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins(),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.post(
        "/design/sessions",
        response_model=SessionView,
        responses={400: {"model": ErrorView}},
        dependencies=[Depends(require_api_token)],
    )
    async def post_session(payload: CreateSessionRequest) -> SessionView:
        try:
            state = await create_session(payload.user_request)
        except SessionError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"detail": exc.detail, "code": exc.code},
            ) from exc
        except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
            raise HTTPException(
                status_code=502,
                detail={
                    "detail": f"Requirements extraction failed: {exc}",
                    "code": "requirements_failed",
                },
            ) from exc
        try:
            snapshot = await load_design_snapshot(state.session_id)
        except PersistedStateError as exc:
            raise _persisted_state_error(exc) from exc
        return _session_view(state, snapshot)

    @app.get(
        "/design/sessions",
        response_model=list[SessionSummary],
    )
    async def list_sessions(
        query: str | None = Query(default=None, max_length=200),
        limit: int = Query(default=10, ge=1, le=50),
        offset: int = Query(default=0, ge=0),
    ) -> list[SessionSummary]:
        """List recent design sessions, most recently active first.

        Server-side search + pagination: ``query`` matches the persisted
        user request or the session id (case-insensitive substring);
        ``limit``/``offset`` bound the page so the browser never receives
        the full history. Read-only over existing checkpoints: one summary
        row per persisted thread that still loads. Ordering is best-effort
        recency; no timestamps are stored, so none are reported.
        """
        summaries: list[SessionSummary] = []
        for snapshot in await list_design_sessions(
            limit=limit, offset=offset, query=query
        ):
            requirements_status = (
                snapshot.requirements_state.status.value
                if snapshot.requirements_state is not None
                else ""
            )
            summaries.append(
                SessionSummary(
                    session_id=snapshot.session_id,
                    user_request=snapshot.user_request,
                    requirements_status=requirements_status,
                    design_status=snapshot.status,
                    has_design_run=snapshot.has_design_run,
                    has_error=snapshot.error is not None,
                )
            )
        return summaries

    @app.get(
        "/design/sessions/{session_id}",
        response_model=SessionView,
        responses={404: {"model": ErrorView}, 409: {"model": ErrorView}},
    )
    async def read_session(session_id: str) -> SessionView:
        snapshot = await _snapshot_or_404(session_id)
        return _session_view(snapshot.requirements_state, snapshot)

    @app.post(
        "/design/sessions/{session_id}/run",
        response_model=DesignRunView,
        responses={404: {"model": ErrorView}, 409: {"model": ErrorView}},
        dependencies=[Depends(require_api_token)],
    )
    async def post_run(session_id: str) -> DesignRunView:
        """Start one design run for an already-clarified session.

        Delegates to the same ``run_design_workflow`` entry point the CLI
        uses; the API performs no capacity, selection, pricing or
        composition work itself. Requirements must already be READY, so
        the existing HITL flow is preserved rather than bypassed.
        """
        snapshot = await _snapshot_or_404(session_id)
        try:
            design_session_id, status = await start_design_run(
                snapshot.requirements_state
            )
        except DesignRunError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"detail": exc.detail, "code": exc.code},
            ) from exc
        return DesignRunView(
            session_id=design_session_id,
            requirements_session_id=session_id,
            status=status,
        )

    @app.get(
        "/design/sessions/{session_id}/architecture",
        response_model=ArchitectureResult,
        responses={404: {"model": ErrorView}, 409: {"model": ErrorView}},
    )
    async def read_architecture(session_id: str) -> ArchitectureResult:
        """Return the persisted architecture artifact verbatim.

        Node ids, edges and cost links are re-serialized as stored, so
        the deterministic ids produced upstream reach the UI unchanged.
        """
        snapshot = await _snapshot_or_404(session_id)
        if snapshot.architecture is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "detail": (
                        "Session has no architecture artifact yet: "
                        f"status={snapshot.status or 'unknown'}"
                    ),
                    "code": "architecture_not_available",
                },
            )
        return snapshot.architecture

    @app.get(
        "/design/sessions/{session_id}/cost",
        response_model=CostEstimationResult,
        responses={404: {"model": ErrorView}, 409: {"model": ErrorView}},
    )
    async def read_cost(session_id: str) -> CostEstimationResult:
        """Return the persisted cost artifact verbatim.

        Unpriced components keep their reasons; totals cover priced
        lines only, exactly as the backend computed them.
        """
        snapshot = await _snapshot_or_404(session_id)
        if snapshot.cost_result is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "detail": "Session has no cost artifact yet.",
                    "code": "cost_not_available",
                },
            )
        return snapshot.cost_result

    @app.get(
        "/design/sessions/{session_id}/critique",
        response_model=DesignCritique,
        responses={404: {"model": ErrorView}, 409: {"model": ErrorView}},
    )
    async def read_critique(session_id: str) -> DesignCritique:
        """Return the persisted design critique verbatim.

        Findings, severities and recommendations are re-serialized as
        stored; the critic reviews but never mutates the architecture.
        """
        snapshot = await _snapshot_or_404(session_id)
        if snapshot.critique is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "detail": (
                        "Session has no critique artifact yet: "
                        f"status={snapshot.status or 'unknown'}"
                    ),
                    "code": "critique_not_available",
                },
            )
        return snapshot.critique

    @app.get(
        "/design/sessions/{session_id}/quality-gate",
        response_model=QualityGateResult,
        responses={404: {"model": ErrorView}, 409: {"model": ErrorView}},
    )
    async def read_quality_gate(session_id: str) -> QualityGateResult:
        """Return the persisted quality-gate verdict verbatim.

        READY does not mean GOOD ENOUGH TO PROVISION: this verdict is
        the explicit boundary between them.
        """
        snapshot = await _snapshot_or_404(session_id)
        if snapshot.quality_gate is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "detail": (
                        "Session has no quality-gate verdict yet: "
                        f"status={snapshot.status or 'unknown'}"
                    ),
                    "code": "quality_gate_not_available",
                },
            )
        return snapshot.quality_gate

    @app.get(
        "/design/sessions/{session_id}/optimization-recommendations",
        response_model=OptimizationRecommendationsView,
        responses={404: {"model": ErrorView}},
    )
    async def read_optimization_recommendations(
        session_id: str,
    ) -> OptimizationRecommendationsView:
        """Return unevaluated diagnostic recommendations for a blocked gate.

        Recommendations are generated deterministically from the stored
        quality-gate verdict and, where applicable, the design critique
        and cost lines. They are not priced, composed, or gated, and none
        represents a feasible, affordable, or budget-compliant
        architecture. Under the current quality gate a BLOCKED verdict
        arises only from a budget-ceiling breach (blocks caused by a
        missing or stale artifact are not remediable and yield no
        recommendations), so in production recommendations are
        budget-breach driven.

        Generation is deterministic and repeat GETs are idempotent: when
        the stored record already matches the regenerated recommendations
        and their source artifact, nothing new is written to the
        checkpoint thread.
        """
        from ai_architect.domain.architecture.recommendation import (
            generate_recommendations,
        )

        snapshot = await _snapshot_or_404(session_id)
        recommendations = generate_recommendations(
            snapshot.architecture,
            snapshot.quality_gate,
            snapshot.critique,
            snapshot.cost_result,
        )
        derived_from = ""
        architecture = snapshot.architecture
        if architecture is not None:
            derived_from = str(architecture.artifact_id or "")

        stored = snapshot.optimization_recommendations
        already_current = (
            isinstance(stored, dict)
            and stored.get("recommendations") == recommendations
            and str(stored.get("derived_from_artifact_id") or "") == derived_from
        )
        if not already_current:
            record = {
                "recommendations": recommendations,
                "derived_from_artifact_id": derived_from,
                "generated_at": datetime.utcnow().isoformat(),
            }
            try:
                await save_session_record(
                    session_id,
                    field="optimization_recommendations",
                    record=record,
                )
            except PersistedStateError as exc:
                raise _persisted_state_error(exc) from exc

        return OptimizationRecommendationsView(
            session_id=session_id,
            derived_from_artifact_id=derived_from,
            recommendations=[
                OptimizationRecommendationView(**rec) for rec in recommendations
            ],
        )

    @app.post(
        "/design/sessions/{session_id}/approve-design",
        response_model=ApprovalView,
        responses={404: {"model": ErrorView}, 409: {"model": ErrorView}},
        dependencies=[Depends(require_api_token)],
    )
    async def approve_design(
        session_id: str, payload: ApproveDesignRequest
    ) -> ApprovalView:
        """Record an explicit human verdict on the gated design.

        Allowed only when the stored gate verdict is approved_for_review
        or requires_user_decision (the latter is exactly the verdict
        that asks the user to accept remaining findings as trade-offs).
        Approval alone provisions nothing; it only unlocks the
        Terraform generation step, which re-validates it.
        """
        from ai_architect.services.approvals import (
            build_design_approval,
            design_approval_allowed,
        )

        snapshot = await _snapshot_or_404(session_id)
        allowed, gate_outcome = design_approval_allowed(snapshot)
        if not allowed:
            raise HTTPException(
                status_code=409,
                detail={
                    "detail": (
                        "Design approval is not available: the session "
                        "has no passing quality-gate verdict "
                        f"(gate={gate_outcome})."
                    ),
                    "code": "approval_not_allowed",
                },
            )
        record = build_design_approval(
            approved=payload.approved,
            note=payload.note,
            artifact_id=snapshot.architecture.artifact_id,
            gate_outcome=gate_outcome,
        )
        try:
            await save_session_record(
                session_id, field="design_approval", record=record
            )
        except PersistedStateError as exc:
            raise _persisted_state_error(exc) from exc
        return ApprovalView(**record)

    @app.post(
        "/design/sessions/{session_id}/terraform",
        response_model=TerraformPlanView,
        responses={404: {"model": ErrorView}, 409: {"model": ErrorView}},
        dependencies=[Depends(require_api_token)],
    )
    async def build_terraform(
        session_id: str, payload: TerraformRequest
    ) -> TerraformPlanView:
        """Generate the deterministic Terraform plan for an approval.

        Requires a positive design approval for the session's current
        architecture artifact; anything else is refused before any
        Terraform is produced. The stored plan artifact is returned, so
        repeated calls are idempotent for identical inputs.
        """
        from ai_architect.services.provisioning.plan_builder import (
            build_provisioning_plan,
        )

        snapshot = await _snapshot_or_404(session_id)
        if (
            snapshot.requirements_state is None
            or snapshot.requirements_state.extraction is None
            or snapshot.architecture is None
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "detail": (
                        "Terraform generation is not available: the "
                        "session has no approved architecture artifact."
                    ),
                    "code": "terraform_not_allowed",
                },
            )
        # Raw provider string only: build_infra_spec enforces the
        # single-cloud contract deterministically, so the API layer
        # never imports a pipeline stage runner (see boundary tests).
        provider = (
            snapshot.requirements_state.extraction.cloud_provider or ""
        )
        requirements_state = snapshot.requirements_state
        requirements = (
            requirements_state.extraction
            if requirements_state is not None
            else None
        )
        stored_plan = getattr(snapshot, "terraform_artifact", None)
        prior_inputs = (
            stored_plan.get("inputs")
            if isinstance(stored_plan, dict)
            and isinstance(stored_plan.get("inputs"), dict)
            else None
        )
        try:
            plan = build_provisioning_plan(
                architecture=snapshot.architecture,
                provider=provider,
                approval=snapshot.design_approval,
                inputs=dict(payload.inputs),
                prior_inputs=prior_inputs,
                requirements=requirements,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "detail": f"Terraform generation refused: {exc}",
                    "code": "terraform_not_allowed",
                },
            ) from exc
        try:
            await save_session_record(
                session_id, field="terraform_artifact", record=plan
            )
        except PersistedStateError as exc:
            raise _persisted_state_error(exc) from exc
        await _invalidate_stale_provisioning_approval(session_id, snapshot)
        return TerraformPlanView(**plan)

    @app.get(
        "/design/sessions/{session_id}/terraform",
        response_model=TerraformPlanView,
        responses={404: {"model": ErrorView}, 409: {"model": ErrorView}},
    )
    async def read_terraform(session_id: str) -> TerraformPlanView:
        """Return the stored Terraform plan artifact verbatim.

        Generation stays on the POST endpoint (approval-gated); this
        read path only serves what was already generated and stored.
        """
        snapshot = await _snapshot_or_404(session_id)
        if not isinstance(snapshot.terraform_artifact, dict):
            raise HTTPException(
                status_code=404,
                detail={
                    "detail": (
                        "Session has no Terraform plan artifact yet."
                    ),
                    "code": "terraform_not_available",
                },
            )
        try:
            return TerraformPlanView(**snapshot.terraform_artifact)
        except Exception as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "detail": (
                        "Stored Terraform plan is invalid and cannot "
                        f"be reconstructed: {exc}"
                    ),
                    "code": "persisted_state_invalid",
                },
            ) from exc

    @app.post(
        "/design/sessions/{session_id}/provisioning/suggest-inputs",
        response_model=InputSuggestionsView,
        responses={404: {"model": ErrorView}, 409: {"model": ErrorView}},
        dependencies=[Depends(require_api_token)],
    )
    async def suggest_provisioning_inputs(
        session_id: str,
    ) -> InputSuggestionsView:
        """Propose values for the pending human decisions of a stored plan.

        The values are advisory: this endpoint writes nothing to the plan,
        the checkpoint or AWS. It returns a starting point the operator
        reviews, edits and submits through the normal input path, where
        the same deterministic contract validation runs again.

        The model is asked for the policy values only. Values that encode
        a fact the platform owns come from the platform's own artifact
        contract, and values only the operator can decide (a globally
        unique bucket name) are never proposed - they are reported as
        skipped, with the reason.
        """
        from ai_architect.services.provisioning.input_suggestions import (
            PendingInput,
            SuggestionError,
            suggest_pending_inputs,
        )
        from ai_architect.services.provisioning.spec_builder import (
            demo_lambda_artifact,
        )

        snapshot = await _snapshot_or_404(session_id)
        plan = snapshot.terraform_artifact
        if not isinstance(plan, dict):
            raise HTTPException(
                status_code=409,
                detail={
                    "detail": (
                        "Input suggestions are not available: the session "
                        "has no stored Terraform plan. Generate the plan "
                        "first."
                    ),
                    "code": "terraform_not_available",
                },
            )

        pending: list[PendingInput] = []
        for resource in plan.get("resources") or []:
            if not isinstance(resource, dict):
                continue
            for item in resource.get("pending_inputs") or []:
                if not isinstance(item, dict):
                    continue
                pending.append(PendingInput(
                    resource_id=str(resource.get("resource_id") or ""),
                    kind=str(resource.get("kind") or ""),
                    component_name=str(resource.get("component_name") or ""),
                    name=str(item.get("name") or ""),
                    reason=str(item.get("reason") or ""),
                ))
        if not pending:
            return InputSuggestionsView(
                suggestions=[],
                skipped=[],
                notes=["This plan has no pending human decisions."],
            )

        extraction = (
            snapshot.requirements_state.extraction
            if snapshot.requirements_state is not None
            else None
        )
        requirements = (
            extraction.model_dump(mode="json")
            if extraction is not None
            else {}
        )
        capacity = (
            snapshot.capacity_result.model_dump(mode="json")
            if snapshot.capacity_result is not None
            else {}
        )
        architecture = snapshot.architecture
        components = [
            {
                "component": node.component_name,
                "role": getattr(node, "role_in_system", "") or "",
            }
            for node in (getattr(architecture, "nodes", None) or [])
        ]
        context = {
            "user_request": snapshot.user_request,
            "requirements": requirements,
            "capacity": capacity,
            "components": components,
        }

        try:
            result = await asyncio.to_thread(
                suggest_pending_inputs,
                pending=pending,
                context=context,
                artifact_available=demo_lambda_artifact() is not None,
            )
        except SuggestionError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "detail": f"Input suggestions failed: {exc}",
                    "code": "input_suggestions_unavailable",
                },
            ) from exc

        payload = result.as_payload()
        return InputSuggestionsView(
            suggestions=[
                InputSuggestionView(**item) for item in payload["suggestions"]
            ],
            skipped=[SkippedInputView(**item) for item in payload["skipped"]],
            notes=list(payload["notes"]),
        )

    @app.post(
        "/design/sessions/{session_id}/approve-provisioning",
        response_model=ApprovalView,
        responses={404: {"model": ErrorView}, 409: {"model": ErrorView}},
        dependencies=[Depends(require_api_token)],
    )
    async def approve_provisioning(
        session_id: str, payload: ApproveProvisioningRequest
    ) -> ApprovalView:
        """Record an explicit human verdict on the stored Terraform plan.

        Requires a stored plan built from an approved design and, for a
        positive verdict, the stored successful plan review for that
        exact tree: the approval binds the reviewed saved-plan file and
        the workspace fingerprint. Recording approval never executes
        anything.
        """
        snapshot = await _snapshot_or_404(session_id)
        plan = snapshot.terraform_artifact
        if (
            not isinstance(plan, dict)
            or not plan.get("tree_hash")
            or not plan.get("spec_id")
            or not plan.get("artifact_id")
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "detail": (
                        "Provisioning approval is not available: the "
                        "session has no stored Terraform plan. Generate "
                        "it first."
                    ),
                    "code": "provisioning_not_allowed",
                },
            )
        mismatch = _plan_architecture_mismatch(snapshot, plan)
        if mismatch is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "detail": mismatch,
                    "code": "provisioning_not_allowed",
                },
            )
        plan_file = None
        workspace_hash = ""
        if payload.approved:
            try:
                review = require_provisioning_review(
                    snapshot.provisioning_result, plan
                )
            except ExecutionRefused as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "detail": str(exc),
                        "code": "plan_review_required",
                    },
                ) from exc
            plan_file = dict(review["plan_file"])
            workspace_hash = workspace_fingerprint(
                dict(plan.get("files") or {}),
                plan.get("artifacts"),
            )
        record = build_provisioning_approval(
            approved=payload.approved,
            note=payload.note,
            tree_hash=plan["tree_hash"],
            spec_id=plan["spec_id"],
            artifact_id=plan["artifact_id"],
            artifacts=plan.get("artifacts"),
            session_id=session_id,
            plan_file=plan_file,
            workspace_hash=workspace_hash,
        )
        try:
            await save_session_record(
                session_id, field="provisioning_approval", record=record
            )
        except PersistedStateError as exc:
            raise _persisted_state_error(exc) from exc
        return ApprovalView(
            approved=record["approved"],
            note=record["note"],
            artifact_id=record["artifact_id"],
            gate_outcome="",
            recorded_at=record["recorded_at"],
        )

    @app.post(
        "/design/sessions/{session_id}/provision",
        response_model=ProvisioningResultView,
        responses={404: {"model": ErrorView}, 409: {"model": ErrorView}},
        dependencies=[Depends(require_api_token)],
    )
    async def provision(
        session_id: str, payload: ProvisionRequest
    ) -> ProvisioningResultView:
        """Execute the stored plan: preview, apply, destroy, or drift.

        Plan mode is a read-only preview and needs NO provisioning
        approval: the review is what the human authorizes next. It runs
        ``terraform init`` + ``plan`` in a durable workspace under
        var/terraform, saves the exact plan file there, and stores the
        reviewable result (including the saved plan's {name, sha256}
        binding). Apply mode requires confirm to equal "APPLY", a stored
        plan review for the same tree (plan mode run first), and a
        provisioning approval bound to that review's saved plan and
        workspace fingerprint. Apply executes that exact saved plan
        file — never a freshly computed plan — consumes the approval
        (single use), and records an apply_running marker before
        anything runs, so a duplicate or interrupted apply is an
        explicit state, never a silent retry.

        Destroy is its own reviewed operation: destroy-plan saves a
        distinct ``terraform plan -destroy`` file, and destroy requires
        confirm to equal "DESTROY" plus that stored review bound to the
        destroy plan's sha256. Drift runs a refresh-only plan and
        reports no_drift, drift, or drift_error without mutating state.
        """
        if payload.mode not in (
            "plan", "apply", "destroy-plan", "destroy", "drift",
        ):
            raise HTTPException(
                status_code=400,
                detail={
                    "detail": (
                        f"Unknown provision mode {payload.mode!r}: "
                        'use "plan", "apply", "destroy-plan", '
                        '"destroy", or "drift".'
                    ),
                    "code": "error",
                },
            )
        if payload.mode == "apply" and payload.confirm != "APPLY":
            raise HTTPException(
                status_code=409,
                detail={
                    "detail": (
                        "Terraform apply refused: explicit confirmation "
                        "'APPLY' was not supplied."
                    ),
                    "code": "provisioning_refused",
                },
            )
        if payload.mode == "destroy" and payload.confirm != "DESTROY":
            raise HTTPException(
                status_code=409,
                detail={
                    "detail": (
                        "Terraform destroy refused: explicit confirmation "
                        "'DESTROY' was not supplied."
                    ),
                    "code": "provisioning_refused",
                },
            )

        async with _provision_lock(session_id):
            snapshot = await _snapshot_or_404(session_id)
            plan = snapshot.terraform_artifact
            if not isinstance(plan, dict) or not plan.get("files"):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "detail": (
                            "Provisioning is not available: the session "
                            "has no stored Terraform plan with files."
                        ),
                        "code": "provisioning_not_allowed",
                    },
                )
            mismatch = _plan_architecture_mismatch(snapshot, plan)
            if mismatch is not None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "detail": mismatch,
                        "code": "provisioning_not_allowed",
                    },
                )

            review = snapshot.provisioning_result
            if payload.mode == "apply":
                try:
                    require_provisioning_approval(
                        snapshot.provisioning_approval, plan
                    )
                except ExecutionRefused as exc:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "detail": str(exc),
                            "code": "provisioning_refused",
                        },
                    ) from exc
                if (
                    isinstance(review, dict)
                    and review.get("mode") == "apply"
                    and review.get("status") == "apply_running"
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "detail": (
                                "Terraform apply refused: a previous "
                                "apply did not finish. Inspect the "
                                "workspace under var/terraform (the "
                                "terraform state is preserved there), "
                                "then run plan mode again to produce a "
                                "fresh review before applying."
                            ),
                            "code": "apply_interrupted",
                        },
                    )
                try:
                    review = require_provisioning_review(review, plan)
                except ExecutionRefused as exc:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "detail": str(exc),
                            "code": "plan_review_required",
                        },
                    ) from exc
                try:
                    require_provisioning_approval(
                        snapshot.provisioning_approval,
                        plan,
                        session_id=session_id,
                        review=review,
                    )
                except ExecutionRefused as exc:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "detail": str(exc),
                            "code": "provisioning_refused",
                        },
                    ) from exc
            elif payload.mode == "destroy":
                if (
                    isinstance(review, dict)
                    and review.get("mode") == "destroy"
                    and review.get("status") == "destroy_running"
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "detail": (
                                "Terraform destroy refused: a previous "
                                "destroy did not finish. Inspect the "
                                "workspace under var/terraform (the "
                                "terraform state is preserved there), "
                                "then run destroy-plan mode again to "
                                "produce a fresh review before "
                                "destroying."
                            ),
                            "code": "destroy_interrupted",
                        },
                    )
                try:
                    review = require_destroy_plan_review(review, plan)
                except ExecutionRefused as exc:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "detail": str(exc),
                            "code": "destroy_plan_review_required",
                        },
                    ) from exc

            try:
                workspace = workspace_path(
                    session_id, plan.get("tree_hash", "")
                )
            except ExecutionRefused as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "detail": str(exc), "code": "provisioning_refused"
                    },
                ) from exc

            if payload.mode == "apply":
                try:
                    await save_session_record(
                        session_id,
                        field="provisioning_result",
                        record=build_pending_apply_record(plan),
                    )
                    await save_session_record(
                        session_id,
                        field="provisioning_approval",
                        record=consume_provisioning_approval(
                            snapshot.provisioning_approval
                        ),
                    )
                except PersistedStateError as exc:
                    raise _persisted_state_error(exc) from exc
            elif payload.mode == "destroy":
                try:
                    await save_session_record(
                        session_id,
                        field="provisioning_result",
                        record=build_provisioning_result(
                            plan, "destroy", {"status": "destroy_running"}
                        ),
                    )
                except PersistedStateError as exc:
                    raise _persisted_state_error(exc) from exc

            try:
                manifest = plan.get("artifacts")
                if payload.mode == "apply":
                    # The exact reviewed saved plan: its hash is the
                    # binding recorded by the plan review and is
                    # re-verified against the workspace file by the
                    # executor before anything runs.
                    outcome = run_terraform_apply(
                        dict(plan["files"]), confirm=payload.confirm,
                        extra_env=_server_aws_env(),
                        artifact_manifest=manifest,
                        workspace_dir=workspace,
                        saved_plan_sha256=review["plan_file"]["sha256"],
                    )
                elif payload.mode == "destroy-plan":
                    outcome = run_terraform_destroy_plan(
                        dict(plan["files"]), extra_env=_server_aws_env(),
                        artifact_manifest=manifest,
                        workspace_dir=workspace,
                    )
                elif payload.mode == "destroy":
                    outcome = run_terraform_destroy(
                        dict(plan["files"]), confirm=payload.confirm,
                        extra_env=_server_aws_env(),
                        artifact_manifest=manifest,
                        workspace_dir=workspace,
                        destroy_plan_sha256=review["plan_file"]["sha256"],
                    )
                elif payload.mode == "drift":
                    outcome = run_terraform_drift_check(
                        dict(plan["files"]), extra_env=_server_aws_env(),
                        artifact_manifest=manifest,
                        workspace_dir=workspace,
                    )
                else:
                    # A regenerated plan after a failed apply starts in a
                    # new workspace (the tree hash changed). Carry the
                    # failed apply's Terraform state forward so the review
                    # reconciles resources that already exist instead of
                    # proposing to create them again.
                    seed_state_from = None
                    previous = snapshot.provisioning_result
                    current_hash = plan.get("tree_hash", "")
                    if (
                        isinstance(previous, dict)
                        and previous.get("status") == "apply_failed"
                        and isinstance(previous.get("tree_hash"), str)
                        and previous["tree_hash"]
                        and previous["tree_hash"] != current_hash
                    ):
                        try:
                            prior_workspace = workspace_path(
                                session_id, previous["tree_hash"]
                            )
                        except ExecutionRefused:
                            prior_workspace = None
                        if (
                            prior_workspace is not None
                            and (
                                prior_workspace / "terraform.tfstate"
                            ).is_file()
                        ):
                            seed_state_from = prior_workspace
                    outcome = run_terraform_plan(
                        dict(plan["files"]), extra_env=_server_aws_env(),
                        artifact_manifest=manifest,
                        workspace_dir=workspace,
                        seed_state_from=seed_state_from,
                    )
            except ExecutionRefused as exc:
                if payload.mode == "apply":
                    # Nothing ran: restore the reviewed plan and the
                    # unconsumed approval so the operator can retry
                    # after fixing the environment.
                    try:
                        await save_session_record(
                            session_id,
                            field="provisioning_result",
                            record=dict(review),
                        )
                        await save_session_record(
                            session_id,
                            field="provisioning_approval",
                            record=dict(snapshot.provisioning_approval),
                        )
                    except PersistedStateError as persist_exc:
                        raise _persisted_state_error(persist_exc) from exc
                elif payload.mode == "destroy":
                    try:
                        await save_session_record(
                            session_id,
                            field="provisioning_result",
                            record=dict(review),
                        )
                    except PersistedStateError as persist_exc:
                        raise _persisted_state_error(persist_exc) from exc
                raise HTTPException(
                    status_code=409,
                    detail={
                        "detail": str(exc), "code": "provisioning_refused"
                    },
                ) from exc

            record_mode = (
                "destroy_plan"
                if payload.mode == "destroy-plan"
                else payload.mode
            )
            result = build_provisioning_result(plan, record_mode, outcome)
            try:
                await save_session_record(
                    session_id, field="provisioning_result", record=result
                )
            except PersistedStateError as exc:
                raise _persisted_state_error(exc) from exc
            return ProvisioningResultView(**result)

    @app.post(
        "/design/sessions/{session_id}/messages",
        response_model=SessionView,
        responses={400: {"model": ErrorView},
                   404: {"model": ErrorView},
                   409: {"model": ErrorView}},
        dependencies=[Depends(require_api_token)],
    )
    async def post_message(
        session_id: str, payload: SessionMessageRequest
    ) -> SessionView:
        try:
            state = await submit_message(session_id, payload.text)
        except SessionError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"detail": exc.detail, "code": exc.code},
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "detail": f"Clarification could not be applied: {exc}",
                    "code": "clarification_invalid",
                },
            ) from exc
        try:
            snapshot = await load_design_snapshot(session_id)
        except PersistedStateError as exc:
            raise _persisted_state_error(exc) from exc
        return _session_view(state, snapshot)

    return app


app = create_app()
