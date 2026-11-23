"""Top-level design workflow graph: Requirements -> Capacity ->
Component Selection -> Cost Estimation -> Architecture Composer ->
Design Critic -> Quality Gate, with a bounded critique/revision loop.

Node boundaries (the orchestrator only coordinates):
- requirements node -> existing requirements workflow
  (build_initial_requirements_state + run_requirements_flow, which owns
  interrupt/resume HITL internally; no interrupt crosses this boundary)
- capacity node -> run_capacity_modeling (agent proposal + deterministic
  CapacityCalculator; no arithmetic here)
- component selection node -> existing run path
  (resolve_provider + retrieve_evidence_for_selection +
  run_component_selection incl. deterministic validation; no retrieval,
  validation, or prompting here)
- cost estimation node -> deterministic PricingService + bounded usage
  estimation (deterministic rules first, one model call for
  factor-parameterized dimensions) + deterministic CostCalculator
  pricing of the estimated quantities; estimation failure degrades to
  warnings, never a fabricated number
- architecture composer node -> run_architecture_composer (LLM keys +
  deterministic normalize/validate; no retrieval, arithmetic, or
  pricing here)
- design critic node -> run_design_critic (adversarial review only;
  never mutates the architecture)
- composer revision node -> run_architecture_composer with a
  deterministic revision brief and a version bump, same closed-world
  contract as first composition
- quality gate node -> deterministic evaluate_quality_gate over the
  finished artifacts (no LLM here)

Critique loop (bounded by MAX_CRITIQUE_CYCLES, separate from the
composer correction retry): critic findings route to composer revision
(topology fix within the selected set) or, when a finding faults the
component set itself, back through component selection -> cost ->
composer. After the budget is spent, remaining criticals surface as
explicit user decisions via the gate. No unbounded agent loop.

Failure boundary: a stage exception is recorded in state.error with
status FAILED and routed straight to END. No silent continuation, no
partial results invented.
"""

import asyncio

from langgraph.graph import END, START, StateGraph

from ai_architect.agents.architecture.run import (
    ArchitectureCompositionError,
    run_architecture_composer,
)
from ai_architect.agents.capacity.run import run_capacity_modeling
from ai_architect.agents.component_selection.run import (
    resolve_provider,
    retrieve_evidence_for_selection,
    run_component_selection,
)
from ai_architect.agents.cost_estimation.run import run_usage_estimation
from ai_architect.agents.critic.run import (
    DesignCritiqueError,
    run_design_critic,
)
from ai_architect.agents.design.state import DesignGraphState, DesignStatus
from ai_architect.agents.requirements.run import (
    build_initial_requirements_state,
    run_requirements_flow,
)
from ai_architect.domain.architecture.critique import (
    MAX_CRITIQUE_CYCLES,
    QualityGateOutcome,
)
from ai_architect.domain.requirements.state import SessionStatus
from ai_architect.infrastructure.checkpoint.requirements import (
    save_requirements_state,
)
from ai_architect.services.architecture.quality_gate import (
    evaluate_quality_gate,
)
from ai_architect.services.cost.calculator import CostCalculator
from ai_architect.services.cost.pricing_service import PricingService
from ai_architect.services.region.normalizer import (
    resolve_effective_region as resolve_region,
)


async def requirements_stage(state: DesignGraphState) -> dict:
    try:
        requirements_state = state.get("requirements_state")

        if requirements_state is None:
            requirements_state = await asyncio.to_thread(
                build_initial_requirements_state,
                state["user_request"],
            )

        final_state = await run_requirements_flow(requirements_state)
        await save_requirements_state(final_state)

        if (
            final_state.status != SessionStatus.READY
            or final_state.extraction is None
        ):
            return {
                "requirements_state": final_state,
                "status": DesignStatus.FAILED.value,
                "error": (
                    "Requirements did not reach READY status "
                    f"(status={final_state.status.value})."
                ),
            }

        return {
            "requirements_state": final_state,
            "status": DesignStatus.RUNNING.value,
        }
    except Exception as e:
        return {
            "status": DesignStatus.FAILED.value,
            "error": f"Requirements stage failed: {e}",
        }


async def capacity_stage(state: DesignGraphState) -> dict:
    try:
        requirements_state = state.get("requirements_state")

        if requirements_state is None or requirements_state.extraction is None:
            raise ValueError(
                "Capacity requires canonical requirements extraction."
            )

        result = await asyncio.to_thread(
            run_capacity_modeling,
            requirements_state,
        )

        return {
            "capacity_result": result,
            "status": DesignStatus.RUNNING.value,
        }
    except Exception as e:
        return {
            "status": DesignStatus.FAILED.value,
            "error": f"Capacity stage failed: {e}",
        }


async def component_selection_stage(state: DesignGraphState) -> dict:
    try:
        requirements_state = state.get("requirements_state")
        capacity_result = state.get("capacity_result")

        if requirements_state is None or requirements_state.extraction is None:
            raise ValueError(
                "Component selection requires canonical requirements extraction."
            )
        if capacity_result is None:
            raise ValueError(
                "Component selection requires a deterministic capacity result."
            )

        extraction = requirements_state.extraction
        provider = resolve_provider(extraction)
        bundle = await retrieve_evidence_for_selection(
            extraction,
            provider,
        )
        result = await asyncio.to_thread(
            run_component_selection,
            extraction,
            capacity_result.calculation,
            bundle,
            provider,
        )

        return {
            "component_selection": result,
            "status": DesignStatus.RUNNING.value,
        }
    except Exception as e:
        return {
            "status": DesignStatus.FAILED.value,
            "error": f"Component selection stage failed: {e}",
        }


async def cost_estimation_stage(state: DesignGraphState) -> dict:
    """Deterministic cost stage with bounded usage estimation.

    Live pricing facts are fetched deterministically; missing usage
    quantities are then estimated (deterministic rules first, one
    bounded model call for factor-parameterized dimensions), validated,
    and priced deterministically at the retrieved catalog rates. An
    estimation failure degrades to explicit warnings; it never fails
    the stage and never fabricates a quantity or a price.
    """
    try:
        requirements_state = state.get("requirements_state")
        capacity_result = state.get("capacity_result")
        selection = state.get("component_selection")

        if requirements_state is None or requirements_state.extraction is None:
            raise ValueError(
                "Cost estimation requires canonical requirements extraction."
            )
        if capacity_result is None:
            raise ValueError(
                "Cost estimation requires a deterministic capacity result."
            )
        if selection is None:
            raise ValueError(
                "Cost estimation requires a component selection result."
            )

        # Effective region: explicit canonical region first, else a
        # single valid region assumption, else fail closed. Cost
        # Estimation and the Composer resolve identically so the two
        # stages cannot disagree about the deployment region.
        region_code = resolve_region(
            requirements_state.extraction,
            provider=resolve_provider(requirements_state.extraction),
        )

        def _run() -> dict:
            service = PricingService()
            priced, pricing_warnings = service.price_components(
                selection, region_code
            )
            estimation = run_usage_estimation(
                capacity_result,
                selection,
                priced,
                context={
                    "application_description": (
                        requirements_state.extraction.application_description
                    )
                },
            )
            result = CostCalculator().calculate(
                capacity_result,
                selection,
                priced,
                region_code,
                usage_estimates=estimation.estimates,
            )
            result.warnings.extend(
                w for w in pricing_warnings if w not in result.warnings
            )
            result.warnings.extend(
                w for w in estimation.warnings if w not in result.warnings
            )
            return {
                # Resolved once here so Cost Estimation and the Composer
                # cannot disagree about the deployment region.
                "region_code": region_code,
                "cost_result": result,
                "status": DesignStatus.RUNNING.value,
            }

        return await asyncio.to_thread(_run)
    except Exception as e:
        return {
            "status": DesignStatus.FAILED.value,
            "error": f"Cost estimation stage failed: {e}",
        }


async def architecture_composer_stage(state: DesignGraphState) -> dict:
    """Composer stage: LLM keys + deterministic normalize/validate.

    Consumes the already-produced upstream artifacts from state; the
    frozen run_architecture_composer() owns prompting, normalization,
    and validation. No retrieval, no arithmetic, no pricing here.
    On failure nothing is stored: no partial architecture artifact.
    """
    try:
        requirements_state = state.get("requirements_state")
        capacity_result = state.get("capacity_result")
        selection = state.get("component_selection")
        cost_result = state.get("cost_result")

        if requirements_state is None or requirements_state.extraction is None:
            raise ValueError(
                "Architecture composition requires canonical requirements "
                "extraction."
            )
        if capacity_result is None:
            raise ValueError(
                "Architecture composition requires a deterministic "
                "capacity result."
            )
        if selection is None:
            raise ValueError(
                "Architecture composition requires a component selection "
                "result."
            )
        if cost_result is None:
            raise ValueError(
                "Architecture composition requires a cost estimation result."
            )

        extraction = requirements_state.extraction
        # Reuse the region resolved by Cost Estimation. Re-resolving here
        # would let the two stages disagree; recompute only if a caller
        # reached the Composer without a cost stage.
        region_code = state.get("region_code") or ""
        if not region_code:
            region_code = resolve_region(
                extraction,
                provider=resolve_provider(extraction),
            )

        # Revision cycles chain versions onto the critiqued artifact.
        # First composition has no predecessor, so it stays version 1
        # with no supersedes link, exactly as before.
        previous = state.get("architecture")
        version = 1
        supersedes_artifact_id = None
        if previous is not None:
            try:
                version = int(getattr(previous, "version", 0) or 0) + 1
                supersedes_artifact_id = (
                    getattr(previous, "artifact_id", None) or None
                )
            except (TypeError, ValueError):
                version = 1
                supersedes_artifact_id = None

        def _run():
            return run_architecture_composer(
                extraction,
                capacity_result.calculation,
                selection,
                cost_result,
                region_code,
                state["session_id"],
                requirements_session_id=requirements_state.session_id,
                version=version,
                supersedes_artifact_id=supersedes_artifact_id,
            )

        result = await asyncio.to_thread(_run)

        return {
            "architecture": result,
            "status": DesignStatus.RUNNING.value,
        }
    except ArchitectureCompositionError as e:
        # Failure diagnostics ride the internal checkpoint channel only;
        # the public error string is unchanged and the snapshot loader
        # keeps ignoring the diagnostics channel.
        return {
            "status": DesignStatus.FAILED.value,
            "error": f"Architecture composer stage failed: {e}",
            "composer_diagnostics": list(e.diagnostics),
        }
    except Exception as e:
        return {
            "status": DesignStatus.FAILED.value,
            "error": f"Architecture composer stage failed: {e}",
        }


def _revision_brief(critique) -> str:
    """Render critical findings deterministically for a revision cycle.

    The composer receives finding text verbatim; nothing is
    reinterpreted, softened, or invented here.
    """
    lines = []
    for finding in critique.criticals:
        subject = getattr(finding, "about_component_ref", "") or (
            "cross-cutting"
        )
        lines.append(
            f"[{finding.category}/{finding.severity}] "
            f"{finding.statement} "
            f"Recommendation: {finding.recommendation} "
            f"(about: {subject})"
        )
    return "\n".join(lines)


def _critique_route(critique, revision_count: int) -> str:
    """Route after a finished critique. Pure routing, no side effects.

    Returns one of "quality_gate", "composer_revision",
    "component_selection". Revision paths are only offered while the
    cycle budget remains; otherwise the gate records the user decision.
    """
    if not critique.criticals:
        return "quality_gate"
    if revision_count >= MAX_CRITIQUE_CYCLES:
        return "quality_gate"
    if critique.needs_reselection:
        return "component_selection"
    return "composer_revision"


async def design_critic_stage(state: DesignGraphState) -> dict:
    """Critic stage: adversarial review only, never a mutation.

    Runs the critic over the current architecture, stores the typed
    critique, decides the single next step, and consumes one cycle of
    the bounded budget exactly when a revision path is taken.
    """
    try:
        requirements_state = state.get("requirements_state")
        capacity_result = state.get("capacity_result")
        selection = state.get("component_selection")
        cost_result = state.get("cost_result")
        architecture = state.get("architecture")

        if requirements_state is None or requirements_state.extraction is None:
            raise ValueError(
                "Design critique requires canonical requirements "
                "extraction."
            )
        if capacity_result is None:
            raise ValueError(
                "Design critique requires a deterministic capacity result."
            )
        if selection is None:
            raise ValueError(
                "Design critique requires a component selection result."
            )
        if cost_result is None:
            raise ValueError(
                "Design critique requires a cost estimation result."
            )
        if architecture is None:
            raise ValueError(
                "Design critique requires a composed architecture."
            )

        extraction = requirements_state.extraction
        region_code = state.get("region_code") or ""
        if not region_code:
            region_code = resolve_region(
                extraction,
                provider=resolve_provider(extraction),
            )

        def _run():
            return run_design_critic(
                extraction,
                capacity_result.calculation,
                selection,
                cost_result,
                architecture,
                region_code,
                state["session_id"],
                requirements_session_id=requirements_state.session_id,
            )

        critique = await asyncio.to_thread(_run)

        revision_count = int(state.get("revision_count") or 0)
        route = _critique_route(critique, revision_count)
        if route != "quality_gate":
            revision_count += 1

        return {
            "critique": critique,
            "critic_route": route,
            "revision_count": revision_count,
            "status": DesignStatus.RUNNING.value,
        }
    except DesignCritiqueError as e:
        return {
            "status": DesignStatus.FAILED.value,
            "error": f"Design critic stage failed: {e}",
        }
    except Exception as e:
        return {
            "status": DesignStatus.FAILED.value,
            "error": f"Design critic stage failed: {e}",
        }


async def composer_revision_stage(state: DesignGraphState) -> dict:
    """Revision stage: composer re-run with a deterministic brief.

    Same closed-world contract as first composition; the brief carries
    critic findings verbatim. Versions chain onto the critiqued
    artifact. A finding that faults the component set itself never
    reaches here: the critic routes those to component selection.
    """
    try:
        requirements_state = state.get("requirements_state")
        capacity_result = state.get("capacity_result")
        selection = state.get("component_selection")
        cost_result = state.get("cost_result")
        architecture = state.get("architecture")
        critique = state.get("critique")

        if requirements_state is None or requirements_state.extraction is None:
            raise ValueError(
                "Architecture revision requires canonical requirements "
                "extraction."
            )
        if capacity_result is None or selection is None:
            raise ValueError(
                "Architecture revision requires capacity and selection "
                "results."
            )
        if cost_result is None:
            raise ValueError(
                "Architecture revision requires a cost estimation result."
            )
        if architecture is None or critique is None:
            raise ValueError(
                "Architecture revision requires a critiqued architecture."
            )

        extraction = requirements_state.extraction
        region_code = state.get("region_code") or ""
        if not region_code:
            region_code = resolve_region(
                extraction,
                provider=resolve_provider(extraction),
            )
        brief = _revision_brief(critique)
        if not brief.strip():
            raise ValueError(
                "Architecture revision requested with no critical "
                "findings to address."
            )
        version = int(getattr(architecture, "version", 0) or 0) + 1

        def _run():
            return run_architecture_composer(
                extraction,
                capacity_result.calculation,
                selection,
                cost_result,
                region_code,
                state["session_id"],
                requirements_session_id=requirements_state.session_id,
                revision_brief=brief,
                version=version,
                supersedes_artifact_id=architecture.artifact_id,
            )

        result = await asyncio.to_thread(_run)

        return {
            "architecture": result,
            "status": DesignStatus.RUNNING.value,
        }
    except ArchitectureCompositionError as e:
        return {
            "status": DesignStatus.FAILED.value,
            "error": f"Architecture revision stage failed: {e}",
        }
    except Exception as e:
        return {
            "status": DesignStatus.FAILED.value,
            "error": f"Architecture revision stage failed: {e}",
        }


async def quality_gate_stage(state: DesignGraphState) -> dict:
    """Gate stage: deterministic verdict over the finished design.

    No LLM here. BLOCKED fails the run loudly; every other outcome
    ends the run successfully with the verdict stored for review,
    approval, and provisioning decisions downstream.
    """
    try:
        requirements_state = state.get("requirements_state")
        architecture = state.get("architecture")
        critique = state.get("critique")
        cost_result = state.get("cost_result")
        revision_count = int(state.get("revision_count") or 0)
        budget = ""
        if (
            requirements_state is not None
            and requirements_state.extraction is not None
        ):
            budget = (
                requirements_state.extraction.budget_constraints or ""
            )

        gate = evaluate_quality_gate(
            architecture=architecture,
            critique=critique,
            revision_count=revision_count,
            max_cycles=MAX_CRITIQUE_CYCLES,
            budget_constraints=budget,
            cost_unpriced=getattr(cost_result, "unpriced", None),
        )
        if str(gate.outcome) == QualityGateOutcome.BLOCKED.value:
            return {
                "quality_gate": gate,
                "status": DesignStatus.FAILED.value,
                "error": "Design quality gate blocked: "
                + "; ".join(gate.reasons),
            }
        return {
            "quality_gate": gate,
            "status": DesignStatus.READY.value,
        }
    except Exception as e:
        return {
            "status": DesignStatus.FAILED.value,
            "error": f"Design quality gate failed: {e}",
        }


def route_after_critic(state: DesignGraphState) -> str:
    if state.get("error"):
        return END
    if state.get("status") == DesignStatus.FAILED.value:
        return END
    route = state.get("critic_route") or "quality_gate"
    if route not in (
        "quality_gate",
        "composer_revision",
        "component_selection",
    ):
        return END
    return route


def route_on_error(state: DesignGraphState) -> str:
    if state.get("error"):
        return END
    if state.get("status") == DesignStatus.FAILED.value:
        return END
    return "continue"


def build_design_graph(checkpointer):
    builder = StateGraph(DesignGraphState)

    builder.add_node("requirements", requirements_stage)
    builder.add_node("capacity", capacity_stage)
    builder.add_node("component_selection", component_selection_stage)
    builder.add_node("cost_estimation", cost_estimation_stage)
    builder.add_node("architecture_composer", architecture_composer_stage)
    builder.add_node("design_critic", design_critic_stage)
    builder.add_node("composer_revision", composer_revision_stage)
    builder.add_node("quality_gate", quality_gate_stage)

    builder.add_edge(START, "requirements")

    builder.add_conditional_edges(
        "requirements",
        route_on_error,
        {"continue": "capacity", END: END},
    )
    builder.add_conditional_edges(
        "capacity",
        route_on_error,
        {"continue": "component_selection", END: END},
    )

    builder.add_conditional_edges(
        "component_selection",
        route_on_error,
        {"continue": "cost_estimation", END: END},
    )

    builder.add_conditional_edges(
        "cost_estimation",
        route_on_error,
        {"continue": "architecture_composer", END: END},
    )

    builder.add_conditional_edges(
        "architecture_composer",
        route_on_error,
        {"continue": "design_critic", END: END},
    )

    builder.add_conditional_edges(
        "design_critic",
        route_after_critic,
        {
            "quality_gate": "quality_gate",
            "composer_revision": "composer_revision",
            "component_selection": "component_selection",
            END: END,
        },
    )

    builder.add_edge("composer_revision", "design_critic")
    builder.add_edge("quality_gate", END)

    return builder.compile(checkpointer=checkpointer)
