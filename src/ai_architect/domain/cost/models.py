from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PricingModel(str, Enum):
    """Pricing model for a single normalized price fact."""

    ON_DEMAND = "on_demand"
    RESERVED_1YR = "reserved_1yr"
    RESERVED_3YR = "reserved_3yr"


class UsageProvenance(str, Enum):
    """Where one monthly usage quantity came from.

    ``USER_SUPPLIED`` is reserved for user-provided workload facts.
    ``DETERMINISTIC_DERIVATION`` covers values computed arithmetically
    from modeled capacity inputs or stated factor assumptions;
    ``STATED_ASSUMPTION`` covers values resting on an explicit pipeline
    assumption (e.g. 1 hosted zone); ``ESTIMATOR_ASSUMPTION`` covers
    quantities produced by the estimation pipeline's judgment. No
    amount is ever priced from a rate that was invented.
    """

    USER_SUPPLIED = "user_supplied"
    DETERMINISTIC_DERIVATION = "deterministic_derivation"
    STATED_ASSUMPTION = "stated_assumption"
    ESTIMATOR_ASSUMPTION = "estimator_assumption"


class DimensionDisposition(str, Enum):
    """Registry classification of one billable dimension.

    Independent of the runtime coverage status: it states what the
    pipeline can currently do with the dimension for a given
    configuration.

    PRICEABLE: a catalog SKU mapping and a deterministic usage
    derivation both exist today.
    ESTIMABLE: no deterministic usage derivation exists; usage comes
    from the estimation pipeline (deterministic factor arithmetic or
    explicit estimator assumptions) when a price fact is available.
    UNSUPPORTED: billed by the service, audited as outside this
    pipeline's pricing scope.
    """

    PRICEABLE = "priceable"
    ESTIMABLE = "estimable"
    UNSUPPORTED = "unsupported"


class CostCompleteness(str, Enum):
    """How complete the monthly cost result is (see CostEstimationResult).

    FULLY_PRICED: every applicable billing dimension of every selected
    component has a deterministically priced line; no estimation is
    needed and the best-estimate total equals the priced total.
    ESTIMATED_WITH_ASSUMPTIONS: every applicable dimension is priced,
    estimated, or explicitly unsupported-by-scope, and at least one
    estimated line exists; the best-estimate total and the low/high
    range are published.
    ESTIMATED_WITH_RESIDUAL_UNKNOWNS: at least one estimated line
    exists and the only remaining gaps are dimensions this pipeline
    audits as outside its pricing scope (unsupported / not_modeled);
    the best-estimate total is still published, with the residuals
    disclosed as potentially cost-bearing.
    PARTIALLY_UNRESOLVED: some components or applicable dimensions
    remain unresolved (usage unknown or no retrieved price), so no
    total is published.
    UNSUPPORTED: nothing defensible could be priced or estimated at
    all (e.g. every selected component lacks a pricing path).
    """

    FULLY_PRICED = "fully_priced"
    ESTIMATED_WITH_ASSUMPTIONS = "estimated_with_assumptions"
    ESTIMATED_WITH_RESIDUAL_UNKNOWNS = "estimated_with_residual_unknowns"
    PARTIALLY_UNRESOLVED = "partially_unresolved"
    UNSUPPORTED = "unsupported"


class EstimationConfidence(str, Enum):
    """Confidence the estimator attaches to one usage estimate."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PriceTier(BaseModel):
    """One begin/end range slice of a Price List price dimension."""

    begin: float = Field(description="Inclusive lower bound of usage for this tier")
    end: float | None = Field(
        default=None,
        description="Exclusive upper bound of usage; None means unbounded",
    )
    unit: str = Field(description="Billing unit, e.g. Requests, GB-Mo")
    unit_price_usd: float = Field(ge=0, description="Price per unit in USD")
    description: str = Field(default="", description="AWS description of the tier")
    rate_code: str = Field(default="", description="AWS rate code for traceability")

    model_config = ConfigDict(extra="forbid")


class PricingFact(BaseModel):
    """One normalized, live pricing fact from the AWS Price List API.

    Never fabricated: every fact originates from a GetProducts response.
    """

    service_code: str = Field(description="Price List service code, e.g. AWSQueueService")
    service_name: str = Field(default="", description="Human-readable AWS service name")
    region_code: str = Field(description="AWS region code, e.g. ap-south-1")
    pricing_model: PricingModel = Field(description="On-demand or reserved term")
    usage_key: str = Field(
        description="What the price applies to, e.g. requests, storage_gbmo"
    )
    unit: str = Field(description="Billing unit shared by all tiers of this fact")
    tiers: list[PriceTier] = Field(
        default_factory=list,
        description="Ordered usage tiers; empty means no usable tier was found",
    )
    upfront_usd: float = Field(
        default=0.0, ge=0,
        description="One-time upfront fee for reserved terms, else 0",
    )
    purchase_option: str | None = Field(
        default=None, description="Reserved purchase option, if applicable"
    )
    lease_length: str | None = Field(
        default=None, description="Reserved lease length as reported, if applicable"
    )
    offering_class: str | None = Field(
        default=None, description="Reserved offering class, if applicable"
    )
    currency: str = Field(default="USD", description="Price currency")
    source: str = Field(
        default="aws-price-list-api",
        description="Provenance: always the live Price List API",
    )
    retrieved_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="When this price was retrieved (freshness boundary)",
    )

    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class PricedComponent(BaseModel):
    """Normalized pricing for one selected component, or an explicit reason."""

    component_name: str = Field(description="Component name from ComponentSelectionResult")
    category: str = Field(default="", description="Component category, if known")
    service_code: str = Field(
        default="", description="Resolved Price List service code, if mappable"
    )
    facts: list[PricingFact] = Field(
        default_factory=list, description="Live facts retrieved for this component"
    )
    unavailable_reason: str | None = Field(
        default=None,
        description="Set when pricing cannot be determined; facts must then be empty",
    )
    resolved_variant: str | None = Field(
        default=None,
        description=(
            "Mutually exclusive service variant selected during pricing "
            "(e.g. API Gateway 'rest'/'http', SQS 'standard'/'fifo'), or "
            "None when the service has no variants or none was resolved. "
            "Never a guess: set only by the pricing service's pickers."
        ),
    )
    resolved_billing_model: str | None = Field(
        default=None,
        description=(
            "Mutually exclusive billing model selected during pricing "
            "(e.g. DynamoDB 'pay_per_request'/'provisioned'), or None when "
            "not applicable or unresolved."
        ),
    )

    model_config = ConfigDict(extra="forbid")


class CostLineItem(BaseModel):
    """One deterministic monthly cost line."""

    component_name: str
    pricing_model: PricingModel
    unit: str
    monthly_usage: float = Field(ge=0)
    unit_price_usd: float = Field(ge=0)
    monthly_cost_usd: float = Field(ge=0)
    usage_formula: str = Field(
        description="Exact deterministic derivation of monthly_usage"
    )
    pricing_source: str = Field(description="Always aws-price-list-api for priced lines")
    pricing_retrieved_at: datetime = Field(description="Freshness of the price used")
    region_code: str
    usage_provenance: UsageProvenance = Field(
        default=UsageProvenance.DETERMINISTIC_DERIVATION,
        description=(
            "Where monthly_usage came from. DETERMINISTIC_DERIVATION for "
            "values computed from modeled capacity inputs; "
            "STATED_ASSUMPTION for values resting on an explicit pipeline "
            "assumption (e.g. 1 hosted zone); ESTIMATOR_ASSUMPTION is "
            "reserved for the later estimation slice and never appears in "
            "this slice's priced lines."
        ),
    )

    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class UnpricedComponent(BaseModel):
    """A component that could not be priced, with an explicit reason."""

    component_name: str
    reason: str = Field(
        description="Why no price exists; never a silent omission"
    )

    model_config = ConfigDict(extra="forbid")


class CoverageStatus(str, Enum):
    """Runtime pricing-coverage state of one component-dimension."""

    PRICED = "priced"
    ESTIMATED = "estimated"
    USAGE_UNKNOWN = "usage_unknown"
    PRICE_UNAVAILABLE = "price_unavailable"
    UNSUPPORTED = "unsupported"
    NOT_APPLICABLE = "not_applicable"
    # Legacy value from before the E1 coverage registry: equivalent to
    # UNSUPPORTED. Retained so previously persisted artifacts load and
    # are served verbatim; this slice never emits it.
    NOT_MODELED = "not_modeled"


class CoverageEntry(BaseModel):
    """One component-dimension of the estimate's pricing coverage report.

    Disclosure only: makes what is inside and outside the priced
    subtotal auditable. PRICED dimensions have a priced line in the
    total; ESTIMATED dimensions have an estimated line priced from an
    estimated usage quantity (inside the best estimate and range, not
    the priced subtotal); USAGE_UNKNOWN dimensions have no derivable or
    estimated monthly usage; PRICE_UNAVAILABLE dimensions have derivable
    usage but no retrieved price fact; UNSUPPORTED dimensions are billed
    by the service but outside this pipeline's pricing scope;
    NOT_APPLICABLE dimensions do not apply to the component's resolved
    configuration and are not gaps. An entry never carries or changes a
    dollar amount.
    """

    component_name: str
    dimension: str = Field(
        description="Usage key, e.g. requests, storage_gbmo, gb_second"
    )
    label: str = Field(description="Human-readable name of the dimension")
    status: CoverageStatus
    reason: str = Field(
        default="",
        description="Why this dimension is or is not in the estimated total",
    )
    disposition: DimensionDisposition | None = Field(
        default=None,
        description=(
            "Registry classification of the dimension (priceable / "
            "estimable / unsupported). None for entries loaded from "
            "artifacts produced before the coverage registry existed."
        ),
    )

    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class EstimatedCostRange(BaseModel):
    """A lower/upper monthly bound, when one is defensible."""

    low_usd: float = Field(ge=0)
    high_usd: float = Field(ge=0)

    model_config = ConfigDict(extra="forbid")


class UsageEstimate(BaseModel):
    """One estimated monthly usage quantity for a component-dimension.

    Produced by the estimation pipeline for dimensions that have a
    price path but no deterministic usage derivation in this pipeline.
    An estimate is a quantity with an explicit scenario
    (low/typical/high), a provenance (deterministically derived from
    stated factor assumptions, or an estimator assumption), a
    confidence, and the rationale/assumptions it rests on. It never
    carries a price or a dollar amount; pricing stays a deterministic
    catalog lookup.
    """

    component_name: str = Field(description="Component the estimate applies to")
    dimension: str = Field(
        description="Billing dimension / usage key, e.g. gb_second"
    )
    unit: str = Field(description="Billing unit, e.g. GB-Seconds")
    low_quantity: float = Field(ge=0, description="Low scenario monthly usage")
    typical_quantity: float = Field(
        ge=0, description="Typical (best) scenario monthly usage"
    )
    high_quantity: float = Field(ge=0, description="High scenario monthly usage")
    provenance: UsageProvenance = Field(
        description="DETERMINISTIC_DERIVATION when computed from stated "
        "factor assumptions arithmetically; ESTIMATOR_ASSUMPTION when the "
        "quantity itself is the estimator's judgment. Never USER_SUPPLIED "
        "or STATED_ASSUMPTION in this slice."
    )
    confidence: EstimationConfidence = Field(
        description="Estimator-reported confidence in this quantity"
    )
    rationale: str = Field(
        default="", description="Why this estimate and scenario are defensible"
    )
    assumptions: list[str] = Field(
        default_factory=list,
        description="Explicit assumptions the estimate rests on",
    )
    facts_used: list[str] = Field(
        default_factory=list,
        description="Known pipeline facts this estimate was derived from",
    )
    unresolved_questions: list[str] = Field(
        default_factory=list,
        description="What would tighten this estimate if answered",
    )

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    @model_validator(mode="after")
    def _ordering(self) -> "UsageEstimate":
        if not (
            self.low_quantity <= self.typical_quantity <= self.high_quantity
        ):
            raise ValueError(
                "UsageEstimate quantities must satisfy "
                "low <= typical <= high; got "
                f"{self.low_quantity} / {self.typical_quantity} / "
                f"{self.high_quantity} for "
                f"{self.component_name}/{self.dimension}"
            )
        return self


class CostEstimationResult(BaseModel):
    """Cost artifact: deterministic totals plus full coverage accounting.

    ``totals_usd_per_month`` and ``lines`` are the deterministic priced
    subtotal only; they never include estimated, unresolved, or
    unsupported amounts. ``estimated_lines`` are the typical-scenario
    lines priced deterministically from estimated usage quantities
    (never estimated prices); ``estimates`` carries the underlying
    quantity scenarios. ``best_estimate_total_usd_per_month`` is
    published only when every applicable dimension is accounted for
    (priced, estimated, or — for ESTIMATED_WITH_RESIDUAL_UNKNOWNS — an
    audited out-of-scope residual); otherwise it is None and the
    unresolved exposure is named by ``unpriced``, ``coverage``, and
    ``unresolved_dimension_count`` -- an incomplete subtotal is never
    relabeled as a total. ``estimated_range_usd_per_month`` is the
    low/high scenario bound over the same priced subtotal
    (illustrative, not a contractual bound).
    """

    lines: list[CostLineItem] = Field(default_factory=list)
    totals_usd_per_month: dict[str, float] = Field(
        default_factory=dict,
        description="Total per pricing model present; absent models have no lines",
    )
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    unpriced: list[UnpricedComponent] = Field(default_factory=list)
    coverage: list[CoverageEntry] = Field(
        default_factory=list,
        description="Per-dimension pricing coverage: priced, estimated, "
        "usage-unknown, price-unavailable, unsupported and not-applicable "
        "dimensions. Disclosure only; never affects the priced totals.",
    )
    fully_priced: bool = Field(
        default=True,
        description="True only when completeness is FULLY_PRICED: every "
        "selected component has every applicable billing dimension "
        "deterministically priced, with no unpriced entries and no "
        "coverage gaps. False means the priced subtotal alone is not the "
        "full cost; see completeness, unpriced, and coverage.",
    )
    completeness: CostCompleteness = Field(
        default=CostCompleteness.FULLY_PRICED,
        description="FULLY_PRICED | ESTIMATED_WITH_ASSUMPTIONS | "
        "ESTIMATED_WITH_RESIDUAL_UNKNOWNS | PARTIALLY_UNRESOLVED | "
        "UNSUPPORTED. Deterministic function of the coverage report, "
        "unpriced entries, and estimated lines.",
    )
    estimated_lines: list[CostLineItem] = Field(
        default_factory=list,
        description="Lines priced deterministically from estimated usage "
        "(typical scenario); each carries usage_provenance="
        "deterministic_derivation or estimator_assumption. The paired "
        "usage scenarios live in ``estimates``.",
    )
    estimates: list[UsageEstimate] = Field(
        default_factory=list,
        description="Estimated usage quantities (low/typical/high) behind "
        "``estimated_lines``; the only place the scenario data lives.",
    )
    estimated_totals_usd_per_month: dict[str, float] = Field(
        default_factory=dict,
        description="Total of estimated lines per pricing model, using the "
        "typical scenario; empty when nothing is estimated.",
    )
    best_estimate_total_usd_per_month: float | None = Field(
        default=None,
        description="Best-estimate monthly total: priced totals plus "
        "typical-scenario estimated totals when every applicable dimension "
        "is accounted for. None while any applicable dimension is "
        "unresolved, so no partial subtotal is ever published as a total.",
    )
    estimated_range_usd_per_month: EstimatedCostRange | None = Field(
        default=None,
        description="Low/high scenario bound: priced subtotal plus the "
        "low/high estimated costs. Illustrative given the stated "
        "assumptions, not a contractual bound. For a FULLY_PRICED result "
        "it collapses to low == high == the deterministic total.",
    )
    unresolved_dimension_count: int = Field(
        default=0, ge=0,
        description="Applicable coverage dimensions with status "
        "usage_unknown, price_unavailable, or unsupported (legacy "
        "not_modeled counts as unsupported).",
    )
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    pricing_as_of: datetime = Field(
        default_factory=datetime.utcnow,
        description="Newest retrieved_at across facts used; freshness anchor",
    )

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _completeness_legacy_sync(cls, data):
        """Derive completeness for artifacts persisted before this field.

        Old artifacts only carry ``fully_priced``; the deterministic
        pre-E1 rule flipped it False on any unpriced entry or coverage
        gap, so it maps to PARTIALLY_UNRESOLVED without inventing an
        estimation status that never existed.
        """
        if isinstance(data, dict) and data.get("completeness") is None:
            data = dict(data)
            data["completeness"] = (
                CostCompleteness.FULLY_PRICED.value
                if data.get("fully_priced", True)
                else CostCompleteness.PARTIALLY_UNRESOLVED.value
            )
        return data
