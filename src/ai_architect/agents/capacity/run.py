from ai_architect.agents.capacity.agent import (
    create_capacity_modeling_agent,
)
from ai_architect.agents.capacity.models import CapacityModelProposal
from ai_architect.agents.capacity.prompt import (
    CAPACITY_MODELING_SYSTEM_PROMPT,
)
from ai_architect.agents.capacity.result import CapacityModelingResult
from ai_architect.domain.requirements.state import RequirementsState
from ai_architect.services.capacity.calculator import CapacityCalculator


def run_capacity_modeling(
    state: RequirementsState,
) -> CapacityModelingResult:
    if state.extraction is None:
        raise ValueError(
            "Requirements extraction is missing from session state."
        )

    agent = create_capacity_modeling_agent()

    proposal: CapacityModelProposal = agent.invoke(
        [
            ("system", CAPACITY_MODELING_SYSTEM_PROMPT),
            (
                "human",
                state.extraction.model_dump_json(indent=2),
            ),
        ]
    )

    calculator = CapacityCalculator()

    calculation = calculator.calculate(proposal)

    return CapacityModelingResult(
        proposal=proposal,
        calculation=calculation,
    )