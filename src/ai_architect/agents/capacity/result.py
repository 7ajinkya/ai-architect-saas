from pydantic import BaseModel

from ai_architect.agents.capacity.models import CapacityModelProposal
from ai_architect.domain.capacity.models import CapacityModelResult


class CapacityModelingResult(BaseModel):
    """
    Complete output of the Capacity Modeling stage.

    Contains both:
    - the agent's proposed modeling parameters
    - the deterministic calculator result
    """

    proposal: CapacityModelProposal
    calculation: CapacityModelResult