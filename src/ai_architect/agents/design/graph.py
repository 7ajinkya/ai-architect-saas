"""Top-level design workflow graph: Requirements -> Capacity ->
Component Selection -> Cost Estimation -> Architecture Composer.

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
- cost estimation node -> deterministic PricingService +
  CostCalculator (no LLM here)
- architecture composer node -> run_architecture_composer (LLM keys +
  deterministic normalize/validate; no retrieval, arithmetic, or
  pricing here)

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
from ai_architect.agents.design.state import DesignGraphState, DesignStatus
from ai_architect.agents.requirements.run import (
    build_initial_requirements_state,
    run_requirements_flow,
)
from ai_architect.domain.requirements.state import SessionStatus
from ai_architect.infrastructure.checkpoint.requirements import (
    save_requirements_state,
)
from ai_architect.services.cost.calculator import CostCalculator
from ai_architect.services.cost.pricing_service import PricingService
from ai_architect.services.region.normalizer import normalize_region as resolve_region


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
            "status": DesignStatus.READY.value,
        }
    except Exception as e:
        return {
            "status": DesignStatus.FAILED.value,
            "error": f"Component selection stage failed: {e}",
        }


async def cost_estimation_stage(state: DesignGraphState) -> dict:
    """Deterministic cost stage: live pricing facts + pure arithmetic.

    No LLM is invoked here; the Cost Estimation Agent is a later slice.
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

        region_code = resolve_region(
            requirements_state.extraction.region,
            provider=resolve_provider(requirements_state.extraction),
        )

        def _run() -> dict:
            service = PricingService()
            priced, pricing_warnings = service.price_components(
                selection, region_code
            )
            result = CostCalculator().calculate(
                capacity_result, selection, priced, region_code
            )
            result.warnings.extend(
                w for w in pricing_warnings if w not in result.warnings
            )
            return {
                # Resolved once here so Cost Estimation and the Composer
                # cannot disagree about the deployment region.
                "region_code": region_code,
                "cost_result": result,
                "status": DesignStatus.READY.value,
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
                extraction.region,
                provider=resolve_provider(extraction),
            )

        def _run():
            return run_architecture_composer(
                extraction,
                capacity_result.calculation,
                selection,
                cost_result,
                region_code,
                state["session_id"],
                requirements_session_id=requirements_state.session_id,
            )

        result = await asyncio.to_thread(_run)

        return {
            "architecture": result,
            "status": DesignStatus.READY.value,
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

    builder.add_edge("architecture_composer", END)

    return builder.compile(checkpointer=checkpointer)
