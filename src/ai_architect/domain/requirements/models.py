from pydantic import BaseModel, ConfigDict, Field


class ExtractedRequirements(BaseModel):
    """
    LLM-facing schema.

    All fields are required strings because this schema is used for
    tool/function calling. An empty string means the user did not provide
    that information.
    """

    application_description: str
    cloud_provider: str
    region: str
    target_users_dau: str
    availability_target: str
    latency_requirements: str
    traffic_characteristics: str
    read_write_characteristics: str
    storage_characteristics: str
    retention: str
    compliance_requirements: str
    budget_constraints: str
    managed_service_preference: str

    model_config = ConfigDict(extra="forbid")


class RequirementValue(BaseModel):
    value: str | None = None
    source: str = "unknown"
    evidence: str | None = None

    model_config = ConfigDict(extra="forbid")


class RequirementsSpec(BaseModel):
    """
    Application/domain representation of extracted requirements.
    """

    application_description: str

    cloud_provider: RequirementValue = Field(
        default_factory=RequirementValue
    )

    region: RequirementValue = Field(
        default_factory=RequirementValue
    )

    target_users_dau: RequirementValue = Field(
        default_factory=RequirementValue
    )

    availability_target: RequirementValue = Field(
        default_factory=RequirementValue
    )

    latency_requirements: RequirementValue = Field(
        default_factory=RequirementValue
    )

    traffic_characteristics: RequirementValue = Field(
        default_factory=RequirementValue
    )

    read_write_characteristics: RequirementValue = Field(
        default_factory=RequirementValue
    )

    storage_characteristics: RequirementValue = Field(
        default_factory=RequirementValue
    )

    retention: RequirementValue = Field(
        default_factory=RequirementValue
    )

    compliance_requirements: list[str] = Field(
        default_factory=list
    )

    budget_constraints: RequirementValue = Field(
        default_factory=RequirementValue
    )

    managed_service_preference: RequirementValue = Field(
        default_factory=RequirementValue
    )

    model_config = ConfigDict(extra="forbid")
