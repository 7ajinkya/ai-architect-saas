import json

from rich.console import Console

from ai_architect.agents.capacity.result import CapacityModelingResult
from ai_architect.agents.component_selection.models import (
    ComponentSelectionResult,
)
from ai_architect.agents.cost_estimation.agent import (
    create_usage_estimation_agent,
    create_usage_estimation_agent_raw,
)
from ai_architect.agents.cost_estimation.models import (
    CostUsageEstimationProposal,
)
from ai_architect.agents.cost_estimation.prompt import (
    COST_USAGE_ESTIMATION_SYSTEM_PROMPT,
)
from ai_architect.domain.cost.models import PricedComponent
from ai_architect.services.cost.usage_estimator import (
    EstimationJob,
    UsageEstimationOutcome,
    UsageEstimator,
    build_llm_request_payload,
)

console = Console()


def _parse_text_envelope(content: str) -> CostUsageEstimationProposal:
    """Parse a text-envelope model response against the proposal schema.

    Used ONLY when the model answered in plain JSON text instead of
    emitting the requested function call. The text must still validate
    against the same CostUsageEstimationProposal contract; anything
    else raises loudly. No defaults are invented.
    """
    text = (content or "").strip()
    if not text:
        raise ValueError(
            "Usage Estimation agent returned no function call and empty "
            "text; nothing to parse."
        )
    try:
        return CostUsageEstimationProposal.model_validate_json(text)
    except Exception:
        pass
    try:
        start = text.index("{")
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
        return CostUsageEstimationProposal.model_validate(obj)
    except Exception as e:
        raise ValueError(
            "Usage Estimation agent did not emit a function call and its "
            "text output is not valid CostUsageEstimationProposal JSON: "
            f"{e}. Preview: {text[:300]!r}"
        ) from e


def request_usage_estimation_proposal(
    jobs: list[EstimationJob],
    capacity_result: CapacityModelingResult,
    context: dict | None = None,
) -> CostUsageEstimationProposal:
    """One bounded LLM call producing factor-scenario estimates.

    Raises on model or validation failure; the UsageEstimator catches
    that and degrades to an explicit warning, leaving the affected
    dimensions unresolved rather than fabricating quantities.
    """
    payload = build_llm_request_payload(jobs, capacity_result, context)

    agent = create_usage_estimation_agent()
    messages = [
        ("system", COST_USAGE_ESTIMATION_SYSTEM_PROMPT),
        ("human", json.dumps(payload, indent=2)),
    ]

    with console.status("Estimating usage quantities..."):
        proposal = agent.invoke(messages)

    if proposal is None:
        raw_agent = create_usage_estimation_agent_raw()
        with console.status("Estimating usage quantities..."):
            retry_msg = raw_agent.invoke(messages)
        content = getattr(retry_msg, "content", "") or ""
        proposal = _parse_text_envelope(content)

    if proposal is None:
        raise ValueError(
            "Usage Estimation agent produced no usable proposal."
        )

    return proposal


def run_usage_estimation(
    capacity_result: CapacityModelingResult,
    selection: ComponentSelectionResult,
    priced: list[PricedComponent],
    *,
    context: dict | None = None,
    proposal_fn=None,
) -> UsageEstimationOutcome:
    """Estimate missing usage quantities for the priced selection.

    Deterministic rules always run; the LLM-backed proposal call runs
    only for the remaining factor-parameterized dimensions and a
    failure there degrades to explicit warnings (never a stage
    failure, never a fabricated quantity). ``proposal_fn`` is
    injectable for tests.
    """
    return UsageEstimator(proposal_fn=proposal_fn).estimate(
        capacity_result,
        selection,
        priced,
        context=context,
    )
