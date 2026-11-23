from pydantic import BaseModel

from ai_architect.domain.capacity.models import CapacityModelInput


class CapacityModelProposal(CapacityModelInput):
    """
    Structured output produced by the Capacity Modeling Agent.

    The agent proposes calculator parameters.
    The deterministic CapacityCalculator performs the arithmetic.

    messages_per_user_per_day is WRITE-only: it counts new
    messages/writes per user per day. Reads are derived by the
    calculator via read_write_ratio and must not be folded into
    this field.
    """

    rationale: dict[str, str]