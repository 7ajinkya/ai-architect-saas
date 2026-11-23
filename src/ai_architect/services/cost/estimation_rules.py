"""Deterministic estimation rules for estimable billing dimensions.

Rules apply ONLY to dimensions the coverage registry declares
ESTIMABLE and for which the pricing service retrieved an on-demand
fact (a price path exists; nothing is estimated without one). Each
rule is one of two kinds:

- Fully deterministic: low/typical/high quantities are computed
  arithmetically from facts already in the capacity artifacts with no
  model involvement (DynamoDB provisioned capacity sized to the
  modeled peak).
- Factor-parameterized: the estimator proposes low/typical/high factor
  scenarios within hard bounds, and the quantity scenario is
  recomputed deterministically from those factors plus the known
  request volume (Lambda GB-seconds, S3 request classes, CloudWatch
  Logs ingestion/storage); a declared quantity that disagrees with its
  own factors beyond tolerance is rejected.

Nothing here invents a price, a rate, or a fact. Quantities produced
here are paired with a retrieved catalog fact by the calculator, which
alone converts them to dollars.
"""

from dataclasses import dataclass, field
import math

from ai_architect.agents.capacity.result import CapacityModelingResult

SECONDS_PER_MONTH = 2_592_000  # 30 days
HOURS_PER_MONTH = 730
BYTES_PER_GB = 1_000_000_000
DAYS_PER_MONTH = 30

# Dimensions this estimator can supply usage for. Kept explicit so an
# estimable dimension the registry declares but the estimator has no
# rule for stays unresolved (disclosed), never guessed.
SUPPORTED_ESTIMATION_DIMENSIONS: frozenset[str] = frozenset(
    {
        "gb_second",
        "wcu_reserved",
        "rcu_reserved",
        "s3_put_requests",
        "s3_get_requests",
        "log_data_ingested_gb",
        "log_storage_gbmo",
    }
)

# Relative tolerance when checking a declared quantity against the
# quantity recomputed from its own declared factors; absolute floor of
# 1.0 unit absorbs rounding in the model's arithmetic.
RECOMPUTE_RELATIVE_TOLERANCE = 0.01
RECOMPUTE_ABSOLUTE_TOLERANCE = 1.0


@dataclass(frozen=True)
class FactorSpec:
    """One allowed factor of a factor-parameterized estimation rule."""

    name: str
    unit: str
    minimum: float
    maximum: float
    minimum_exclusive: bool = False
    description: str = ""


GB_SECOND_FACTOR_SPECS: tuple[FactorSpec, ...] = (
    FactorSpec(
        name="average_invocation_duration_ms",
        unit="ms",
        minimum=0.0,
        maximum=900_000.0,
        minimum_exclusive=True,
        description="Average function invocation duration in milliseconds; "
        "bounded by the Lambda 15-minute maximum.",
    ),
    FactorSpec(
        name="memory_mb",
        unit="MB",
        minimum=128.0,
        maximum=10_240.0,
        description="Configured function memory in MB within Lambda's "
        "supported range.",
    ),
)

# S3 request classes share one modeled call volume: Tier1 (PUT/COPY/
# POST/LIST) and Tier2 (GET/SELECT) are disjoint shares of the same
# calls, so the pair is estimated from the same factor scenario and
# cross-checked by the estimator to prevent double counting.
S3_REQUEST_FACTOR_SPECS: tuple[FactorSpec, ...] = (
    FactorSpec(
        name="s3_calls_per_request",
        unit="S3 calls/request",
        minimum=0.0,
        maximum=100.0,
        minimum_exclusive=True,
        description="S3 API calls per modeled application request; "
        "expresses fan-out such as listing then fetching objects.",
    ),
    FactorSpec(
        name="tier1_share",
        unit="fraction",
        minimum=0.0,
        maximum=1.0,
        description="Share of modeled S3 calls in Tier1 (PUT/COPY/POST/"
        "LIST); Tier2 (GET/SELECT) receives 1 - tier1_share.",
    ),
)

LOG_INGESTION_FACTOR_SPECS: tuple[FactorSpec, ...] = (
    FactorSpec(
        name="log_bytes_per_request",
        unit="bytes",
        minimum=0.0,
        maximum=1_048_576.0,
        minimum_exclusive=True,
        description="Log bytes emitted per modeled application request; "
        "bounded by the CloudWatch Logs 1 MiB PutLogEvents request "
        "limit.",
    ),
)

LOG_STORAGE_FACTOR_SPECS: tuple[FactorSpec, ...] = (
    *LOG_INGESTION_FACTOR_SPECS,
    FactorSpec(
        name="log_retention_days",
        unit="days",
        minimum=1.0,
        maximum=3653.0,
        description="Log retention in days; bounded by the CloudWatch "
        "Logs 10-year retention maximum.",
    ),
)

FACTOR_SPECS_BY_DIMENSION: dict[str, tuple[FactorSpec, ...]] = {
    "gb_second": GB_SECOND_FACTOR_SPECS,
    "s3_put_requests": S3_REQUEST_FACTOR_SPECS,
    "s3_get_requests": S3_REQUEST_FACTOR_SPECS,
    "log_data_ingested_gb": LOG_INGESTION_FACTOR_SPECS,
    "log_storage_gbmo": LOG_STORAGE_FACTOR_SPECS,
}


@dataclass(frozen=True)
class DeterministicEstimate:
    """A fully deterministic scenario for one estimable dimension."""

    low_quantity: float
    typical_quantity: float
    high_quantity: float
    formula: str
    assumptions: list[str] = field(default_factory=list)
    facts_used: list[str] = field(default_factory=list)


def monthly_request_count(capacity: CapacityModelingResult) -> float:
    """Known monthly request volume shared by priced request-based SKUs.

    Identical derivation to the calculator's requests rule, so a GB-
    second estimate is consistent with the same component's priced
    request line.
    """
    calc = capacity.calculation
    proposal = capacity.proposal
    avg_total_qps = (
        calc.peak_write_qps + calc.peak_read_qps
    ) / proposal.peak_factor
    return avg_total_qps * SECONDS_PER_MONTH


def derive_gb_second(
    monthly_requests: float, duration_ms: float, memory_mb: float
) -> float:
    """GB-seconds for one scenario: requests * seconds * memory-GB."""
    return (
        monthly_requests
        * (duration_ms / 1000.0)
        * (memory_mb / 1024.0)
    )


def check_gb_second_consistency(
    monthly_requests: float,
    duration_ms: float,
    memory_mb: float,
    declared_quantity: float,
    scenario: str,
) -> None:
    """Reject a declared GB-seconds quantity that contradicts its factors.

    The quantity must equal requests * (duration/1000) * (memory/1024)
    within max(1% of expected, 1.0). A model that names factors but
    reports an unrelated quantity is rejected loudly rather than
    trusted.
    """
    expected = derive_gb_second(monthly_requests, duration_ms, memory_mb)
    tolerance = max(
        expected * RECOMPUTE_RELATIVE_TOLERANCE,
        RECOMPUTE_ABSOLUTE_TOLERANCE,
    )
    if abs(declared_quantity - expected) > tolerance:
        raise ValueError(
            f"Declared {scenario} gb_second quantity "
            f"{declared_quantity:.3f} does not match the quantity "
            f"recomputed from its own factors "
            f"(requests {monthly_requests:.3f} * "
            f"{duration_ms:.3f} ms / 1000 * {memory_mb:.3f} MB / 1024 "
            f"= {expected:.3f}; tolerance {tolerance:.3f})."
        )


def _check_recomputed(
    dimension: str,
    expected: float,
    declared_quantity: float,
    scenario: str,
    formula: str,
) -> None:
    tolerance = max(
        expected * RECOMPUTE_RELATIVE_TOLERANCE,
        RECOMPUTE_ABSOLUTE_TOLERANCE,
    )
    if abs(declared_quantity - expected) > tolerance:
        raise ValueError(
            f"Declared {scenario} {dimension} quantity "
            f"{declared_quantity:.3f} does not match the quantity "
            f"recomputed from its own factors ({formula} = "
            f"{expected:.3f}; tolerance {tolerance:.3f})."
        )


def derive_s3_request_quantity(
    monthly_requests: float,
    calls_per_request: float,
    share: float,
) -> float:
    """One S3 request class: modeled requests * calls * class share."""
    return monthly_requests * calls_per_request * share


def check_s3_request_consistency(
    dimension: str,
    monthly_requests: float,
    calls_per_request: float,
    tier1_share: float,
    declared_quantity: float,
    scenario: str,
) -> None:
    """Reject a declared S3 request quantity that contradicts its factors.

    Tier1 uses tier1_share and Tier2 uses 1 - tier1_share: the two
    dimensions split one call volume rather than each assuming the
    whole volume. The estimator additionally requires both dimensions
    of one component to declare identical factors per scenario.
    """
    share = tier1_share if dimension == "s3_put_requests" else (
        1.0 - tier1_share
    )
    expected = derive_s3_request_quantity(
        monthly_requests, calls_per_request, share
    )
    _check_recomputed(
        dimension,
        expected,
        declared_quantity,
        scenario,
        (
            f"requests {monthly_requests:.3f} * "
            f"{calls_per_request:.3f} S3 calls/request * "
            f"{share:.6f} class share"
        ),
    )


def derive_log_data_ingested_gb(
    monthly_requests: float, bytes_per_request: float
) -> float:
    """CloudWatch Logs monthly ingestion: requests * bytes / 1e9."""
    return monthly_requests * bytes_per_request / BYTES_PER_GB


def derive_log_storage_gbmo(
    monthly_requests: float,
    bytes_per_request: float,
    retention_days: float,
) -> float:
    """CloudWatch Logs retained volume: ingestion * retention/30.

    Steady-state retained GB-months, not ingestion added on top of
    storage: every ingested byte is retained for the configured window.
    """
    return (
        derive_log_data_ingested_gb(monthly_requests, bytes_per_request)
        * retention_days
        / DAYS_PER_MONTH
    )


def check_log_ingestion_consistency(
    monthly_requests: float,
    bytes_per_request: float,
    declared_quantity: float,
    scenario: str,
) -> None:
    expected = derive_log_data_ingested_gb(
        monthly_requests, bytes_per_request
    )
    _check_recomputed(
        "log_data_ingested_gb",
        expected,
        declared_quantity,
        scenario,
        (
            f"requests {monthly_requests:.3f} * "
            f"{bytes_per_request:.3f} B/request / 1e9 B/GB"
        ),
    )


def check_log_storage_consistency(
    monthly_requests: float,
    bytes_per_request: float,
    retention_days: float,
    declared_quantity: float,
    scenario: str,
) -> None:
    expected = derive_log_storage_gbmo(
        monthly_requests, bytes_per_request, retention_days
    )
    _check_recomputed(
        "log_storage_gbmo",
        expected,
        declared_quantity,
        scenario,
        (
            f"requests {monthly_requests:.3f} * "
            f"{bytes_per_request:.3f} B/request / 1e9 B/GB * "
            f"{retention_days:.3f} days / 30"
        ),
    )


def validate_factor_scenario(
    dimension: str, factors: dict[str, float], scenario: str
) -> dict[str, float]:
    """Validate one declared factor scenario against its spec bounds.

    Returns a copy with every spec factor required and bounded; unknown
    factors are rejected (the proposal model forbids extras) and a
    missing factor is an explicit error, never defaulted.
    """
    specs = FACTOR_SPECS_BY_DIMENSION.get(dimension, ())
    validated: dict[str, float] = {}
    for spec in specs:
        if spec.name not in factors:
            raise ValueError(
                f"{dimension}: {scenario} factors are missing required "
                f"factor '{spec.name}' ({spec.description})"
            )
        value = float(factors[spec.name])
        if not math.isfinite(value):
            raise ValueError(
                f"{dimension}: {scenario} factor '{spec.name}' is not a "
                "finite number"
            )
        below = (
            value <= spec.minimum
            if spec.minimum_exclusive
            else value < spec.minimum
        )
        if below or value > spec.maximum:
            raise ValueError(
                f"{dimension}: {scenario} factor '{spec.name}'={value} is "
                f"outside the supported range "
                f"({spec.minimum}, {spec.maximum}] {spec.unit}"
            )
        validated[spec.name] = value
    return validated


def _write_capacity(peak_qps: float, avg_msg_bytes: float) -> tuple[int, int]:
    per_request = max(1, math.ceil(avg_msg_bytes / 1024.0))
    return math.ceil(peak_qps * per_request), per_request


def _read_capacity(peak_qps: float, avg_msg_bytes: float) -> tuple[int, int]:
    per_request = max(1, math.ceil(avg_msg_bytes / 4096.0))
    return math.ceil(peak_qps * per_request / 4.0), per_request


def deterministic_estimate(
    dimension: str, capacity: CapacityModelingResult
) -> DeterministicEstimate | None:
    """Fully deterministic scenario for a dimension, or None.

    Only dimensions whose monthly quantity is computable purely from
    the capacity artifacts have a rule here; gb_second needs
    invocation-shape factors and is handled by the factor rule
    instead.
    """
    calc = capacity.calculation
    proposal = capacity.proposal
    avg_msg_bytes = proposal.average_message_size_bytes
    if dimension == "wcu_reserved":
        units, per_request = _write_capacity(
            calc.peak_write_qps, avg_msg_bytes
        )
        monthly = units * HOURS_PER_MONTH
        formula = (
            f"ceil(peak_write_qps {calc.peak_write_qps:.3f} * "
            f"{per_request} write request unit(s)/request of "
            f"{avg_msg_bytes:.0f} B average, 1 KB rounding) "
            f"= {units} WCU * {HOURS_PER_MONTH} h/month"
        )
        return DeterministicEstimate(
            low_quantity=float(monthly),
            typical_quantity=float(monthly),
            high_quantity=float(monthly),
            formula=formula,
            assumptions=[
                "Provisioned write capacity is sized to exactly the "
                "modeled peak duration, with no headroom, burst capacity, "
                "or auto-scaling margin modeled; any headroom would raise "
                "the monthly WCU-hours.",
            ],
            facts_used=[
                f"peak_write_qps={calc.peak_write_qps}",
                f"average_message_size_bytes={avg_msg_bytes}",
            ],
        )
    if dimension == "rcu_reserved":
        units, per_request = _read_capacity(
            calc.peak_read_qps, avg_msg_bytes
        )
        monthly = units * HOURS_PER_MONTH
        formula = (
            f"ceil(peak_read_qps {calc.peak_read_qps:.3f} * "
            f"{per_request} read request unit(s)/request of "
            f"{avg_msg_bytes:.0f} B average, 4 KB rounding, strongly "
            f"consistent reads / 4) = {units} RCU * "
            f"{HOURS_PER_MONTH} h/month"
        )
        return DeterministicEstimate(
            low_quantity=float(monthly),
            typical_quantity=float(monthly),
            high_quantity=float(monthly),
            formula=formula,
            assumptions=[
                "Provisioned read capacity is sized to exactly the "
                "modeled peak duration with strongly consistent reads and "
                "no headroom, burst capacity, or auto-scaling margin "
                "modeled; any headroom would raise the monthly RCU-hours.",
            ],
            facts_used=[
                f"peak_read_qps={calc.peak_read_qps}",
                f"average_message_size_bytes={avg_msg_bytes}",
            ],
        )
    return None
