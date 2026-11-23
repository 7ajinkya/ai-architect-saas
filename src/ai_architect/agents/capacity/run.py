from rich.console import Console

from ai_architect.agents.capacity.agent import (
    create_capacity_modeling_agent,
    create_capacity_modeling_agent_raw,
)
from ai_architect.agents.capacity.models import CapacityModelProposal
from ai_architect.agents.capacity.prompt import (
    CAPACITY_MODELING_SYSTEM_PROMPT,
)
from ai_architect.agents.capacity.result import CapacityModelingResult
from ai_architect.domain.requirements.state import RequirementsState
from ai_architect.services.capacity.calculator import CapacityCalculator
from ai_architect.services.capacity.consistency import (
    check_workload_consistency,
)


console = Console()


def _parse_text_envelope(content: str) -> CapacityModelProposal:
    """Parse a text-envelope model response against the proposal schema.

    Used ONLY when the model answered in plain JSON text instead of emitting
    the requested function call. The text must still validate against the
    same CapacityModelProposal contract; anything else raises loudly.
    No defaults are invented: every required input must be present.
    """
    import json as _json

    text = (content or "").strip()
    if not text:
        raise ValueError(
            "Capacity Modeling agent returned no function call and empty text; "
            "nothing to parse."
        )
    try:
        return CapacityModelProposal.model_validate_json(text)
    except Exception:
        pass
    try:
        start = text.index("{")
        obj, _ = _json.JSONDecoder().raw_decode(text[start:])
        return CapacityModelProposal.model_validate(obj)
    except Exception as e:
        raise ValueError(
            "Capacity Modeling agent did not emit a function call and its "
            f"text output is not valid CapacityModelProposal JSON: {e}. "
            f"Preview: {text[:300]!r}"
        ) from e


def run_capacity_modeling(
    state: RequirementsState,
) -> CapacityModelingResult:
    if state.extraction is None:
        raise ValueError(
            "Requirements extraction is missing from session state."
        )

    agent = create_capacity_modeling_agent()

    messages = [
        ("system", CAPACITY_MODELING_SYSTEM_PROMPT),
        (
            "human",
            state.extraction.model_dump_json(indent=2),
        ),
    ]

    with console.status("Modeling capacity..."):
        proposal: CapacityModelProposal | None = agent.invoke(messages)

    if proposal is None:
        # Objectively empty response with tools bound: the model aborted
        # tool calling without producing anything. Retry the identical
        # prompt WITHOUT tools and parse the text through the same
        # proposal contract. Generic: keyed on the empty response only,
        # never on model identity.
        raw_agent = create_capacity_modeling_agent_raw()
        with console.status("Modeling capacity..."):
            retry_msg = raw_agent.invoke(messages)
        content = getattr(retry_msg, "content", "") or ""
        proposal = _parse_text_envelope(content)

    if proposal is None:
        raise ValueError(
            "Capacity Modeling agent produced no usable proposal."
        )

    # Deterministic guardrail: a proposal whose implied average
    # throughput materially exceeds the user's stated request rate
    # (e.g. total traffic filed as writes, then multiplied again by
    # read_write_ratio) fails here with a diagnostic, before any
    # downstream sizing or pricing consumes it.
    check_workload_consistency(state.extraction, proposal)

    calculator = CapacityCalculator()

    calculation = calculator.calculate(proposal)

    return CapacityModelingResult(
        proposal=proposal,
        calculation=calculation,
    )