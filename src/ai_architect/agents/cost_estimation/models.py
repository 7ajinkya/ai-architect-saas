from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_architect.domain.cost.models import EstimationConfidence


class EstimatedFactorScenario(BaseModel):
    """Low/typical/high factor values for the quantity scenarios.

    Every scenario must name exactly the same factors; a scenario
    naming a different factor set is rejected here rather than
    silently ignored. Values are finite and non-negative at the model
    level; each rule's hard bounds are enforced by the estimator.
    """

    low: dict[str, float]
    typical: dict[str, float]
    high: dict[str, float]

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    @model_validator(mode="after")
    def _same_factor_names(self) -> "EstimatedFactorScenario":
        if set(self.low) != set(self.typical) or set(self.low) != set(
            self.high
        ):
            raise ValueError(
                "All three factor scenarios must name the same factors; "
                f"got low={sorted(self.low)}, typical={sorted(self.typical)}, "
                f"high={sorted(self.high)}"
            )
        for name, value in {
            **{f"low.{k}": v for k, v in self.low.items()},
            **{f"typical.{k}": v for k, v in self.typical.items()},
            **{f"high.{k}": v for k, v in self.high.items()},
        }.items():
            if value < 0:
                raise ValueError(
                    f"Factor '{name}' must be non-negative; got {value}"
                )
        return self


class EstimatedUsageProposal(BaseModel):
    """One proposed usage estimate; quantities and factors only.

    Structurally price-free: extra fields (including anything named
    like a price or rate) are rejected by ``extra="forbid"``. The
    declared quantities must be reproducible from the declared factor
    scenarios (checked per dimension rule by the estimator), and the
    low/typical/high ordering is enforced here. Provenance is assigned
    by the application; the proposal cannot claim one.
    """

    component_name: str
    dimension: str
    low_quantity: float = Field(ge=0, description="Low scenario monthly usage")
    typical_quantity: float = Field(ge=0, description="Typical scenario monthly usage")
    high_quantity: float = Field(ge=0, description="High scenario monthly usage")
    confidence: EstimationConfidence
    rationale: str = ""
    assumptions: list[str] = Field(default_factory=list)
    facts_used: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    factors: EstimatedFactorScenario | None = Field(
        default=None,
        description="Factor scenarios; required for factor-parameterized "
        "dimensions, forbidden otherwise.",
    )

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    @model_validator(mode="after")
    def _ordering(self) -> "EstimatedUsageProposal":
        if not (
            self.low_quantity <= self.typical_quantity <= self.high_quantity
        ):
            raise ValueError(
                "Proposed quantities must satisfy low <= typical <= high; "
                f"got {self.low_quantity} / {self.typical_quantity} / "
                f"{self.high_quantity} for "
                f"{self.component_name}/{self.dimension}"
            )
        return self


class CostUsageEstimationProposal(BaseModel):
    """Envelope returned by the Cost Usage Estimation Agent."""

    estimates: list[EstimatedUsageProposal] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")
