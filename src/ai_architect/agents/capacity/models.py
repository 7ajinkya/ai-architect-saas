from pydantic import BaseModel

from ai_architect.domain.capacity.models import CapacityModelInput


class CapacityModelProposal(CapacityModelInput):
    """
    Structured output produced by the Capacity Modeling Agent.

    The agent proposes calculator parameters.
    The deterministic CapacityCalculator performs the arithmetic.
    """

    rationale: dict[str, str]