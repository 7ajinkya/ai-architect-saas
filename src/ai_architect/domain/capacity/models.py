from pydantic import BaseModel, Field


class CapacityModelInput(BaseModel):
    daily_active_users: int = Field(gt=0)
    messages_per_user_per_day: float = Field(
        gt=0,
        description=(
            "WRITE-only quantity: new messages/writes per user per day. "
            "Reads are derived separately via read_write_ratio and must "
            "not be included here."
        ),
    )

    peak_factor: float = Field(gt=0)
    read_write_ratio: float = Field(
        gt=0,
        description=(
            "Reads per write (e.g. 9.0 means nine reads for each write)."
        ),
    )

    average_message_size_bytes: int = Field(gt=0)

    connection_factor: float = Field(gt=0)

    storage_retention_days: int = Field(gt=0)


class CapacityModelResult(BaseModel):
    peak_write_qps: float
    peak_read_qps: float

    concurrent_connections: float

    storage_growth_bytes_per_day: float
    hot_working_set_bytes: float

    egress_bytes_per_second: float

    formula_version: str

    calculation_trace: dict[str, str]