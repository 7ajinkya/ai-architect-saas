"""Expected billing-dimension coverage registry (E1 foundation).

Declares, per canonical AWS service, the billable dimensions this
pipeline expects for a component and what it can currently do with
each one:

- PRICEABLE: a Price List SKU mapping exists (SERVICE_SPECS) *and* the
  calculator has a deterministic usage derivation for the key.
- ESTIMABLE: no deterministic derivation exists; usage can only come
  from the estimator's bounded factor scenarios when a price fact was
  retrieved. A SKU mapping may or may not exist.
- UNSUPPORTED: billed by the service but audited as outside this
  pipeline's pricing scope.

Priceable dimensions and their SKU queries are derived from
SERVICE_SPECS so the two can never drift; a consistency test locks
the derivation rules. Declared dimensions (estimable/unsupported) are
listed explicitly with their audited reasons -- only audited, known
omissions belong here, and nothing here ever carries or changes a
dollar amount.

Configuration gates: a dimension can belong to a mutually exclusive
variant (API Gateway REST vs HTTP) or billing model (DynamoDB
pay-per-request vs provisioned). ``coverage_for`` splits the expected
dimensions into applicable and not-applicable lists for a resolved
configuration. An unresolved gate keeps the dimension applicable
(conservative: an unproven exclusion must never fake completeness).
"""

from dataclasses import dataclass

from ai_architect.domain.cost.models import (
    DimensionDisposition,
    PricedComponent,
)
from ai_architect.services.cost.dimensions import label_for_dimension
from ai_architect.services.cost.pricing_service import (
    SERVICE_SPECS,
    canonical_component_name,
)

# Usage keys the calculator has a deterministic monthly derivation
# rule for (see CostCalculator._price_on_demand). Kept here so the
# registry can classify spec-mapped dimensions; a test prices a fact
# for every PRICEABLE dimension end-to-end, so a rule removal or a
# registry/spec drift fails loudly.
DERIVATION_SUPPORTED_DIMENSIONS: frozenset[str] = frozenset(
    {
        "requests",
        "rest_requests",
        "http_requests",
        "storage_gbmo",
        "wru",
        "rru",
        "transfer_gb",
        "queries",
        "zones",
    }
)

# Usage facts each dimension needs to be calculated. For PRICEABLE
# dimensions these are the modeled inputs the deterministic derivation
# consumes (names as in the capacity/proposal artifacts); for
# ESTIMABLE dimensions they are the assumption inputs a later
# estimation slice must supply. Never used to invent a value here.
REQUIRED_USAGE_FACTS: dict[str, tuple[str, ...]] = {
    "requests": ("peak_write_qps", "peak_read_qps", "peak_factor"),
    "rest_requests": ("peak_write_qps", "peak_read_qps", "peak_factor"),
    "http_requests": ("peak_write_qps", "peak_read_qps", "peak_factor"),
    "storage_gbmo": ("storage_growth_bytes_per_day",),
    "wru": ("peak_write_qps", "average_message_size_bytes", "peak_factor"),
    "rru": ("peak_read_qps", "average_message_size_bytes", "peak_factor"),
    "transfer_gb": ("egress_bytes_per_second", "peak_factor"),
    "queries": ("peak_write_qps", "peak_read_qps", "peak_factor"),
    "zones": (),
    "gb_second": (
        "requests",
        "average_invocation_duration_ms",
        "memory_mb",
    ),
    "wcu_reserved": ("peak_write_qps", "average_message_size_bytes"),
    "rcu_reserved": ("peak_read_qps", "average_message_size_bytes"),
    "s3_put_requests": (
        "requests",
        "s3_calls_per_request",
        "tier1_share",
    ),
    "s3_get_requests": (
        "requests",
        "s3_calls_per_request",
        "tier1_share",
    ),
    "log_data_ingested_gb": ("requests", "log_bytes_per_request"),
    "log_storage_gbmo": (
        "requests",
        "log_bytes_per_request",
        "log_retention_days",
    ),
    "custom_metrics": (),
    "alarms": (),
}

_ESTIMABLE_FROM_SPEC_REASON = (
    "has a Price List SKU mapping but no deterministic usage derivation: "
    "usage can only come from the estimator's bounded low/typical/high "
    "factor scenarios for the requested request volume. Without a "
    "validated proposal it stays explicitly unresolved, never invented."
)


@dataclass(frozen=True)
class DimensionSpec:
    """One expected billable dimension of a service."""

    dimension: str
    label: str
    disposition: DimensionDisposition
    required_usage_facts: tuple[str, ...] = ()
    reason: str = ""
    variant: str | None = None
    billing_model: str | None = None
    catalog_lookup: str = ""


@dataclass(frozen=True)
class ResolutionHint:
    """Configuration values inferred from retrieved fact usage keys."""

    variant: str | None = None
    billing_model: str | None = None


@dataclass(frozen=True)
class ServiceCoverage:
    """Expected billing dimensions for one canonical service."""

    service_code: str
    canonical_name: str
    dimensions: tuple[DimensionSpec, ...]


def _make_spec_dimension(
    key: str,
    *,
    variant: str | None = None,
    billing_model: str | None = None,
    catalog_lookup: str = "",
) -> DimensionSpec:
    if key in DERIVATION_SUPPORTED_DIMENSIONS:
        disposition = DimensionDisposition.PRICEABLE
        reason = (
            "Price List SKU mapping and a deterministic usage derivation "
            "both exist; priced whenever a matching fact is retrieved."
        )
    else:
        disposition = DimensionDisposition.ESTIMABLE
        reason = f"'{key}' {_ESTIMABLE_FROM_SPEC_REASON}"
    return DimensionSpec(
        dimension=key,
        label=label_for_dimension(key),
        disposition=disposition,
        required_usage_facts=REQUIRED_USAGE_FACTS.get(key, ()),
        reason=reason,
        variant=variant,
        billing_model=billing_model,
        catalog_lookup=catalog_lookup,
    )


def _spec_dimensions(spec: dict) -> list[DimensionSpec]:
    """Derive expected dimensions from one SERVICE_SPECS entry."""
    dims: dict[str, DimensionSpec] = {}
    variants = spec.get("variants")
    if variants:
        variant_count = len(variants)
        by_key: dict[str, list[str]] = {}
        for variant_name, variant_def in variants.items():
            key = variant_def.get("usage_key") or spec.get("usage_key")
            if key:
                by_key.setdefault(key, []).append(variant_name)
        for key in sorted(by_key):
            names = by_key[key]
            # A key shared by every variant (SQS) applies to all of
            # them; a key owned by exactly one of several mutually
            # exclusive variants (API Gateway) is gated on that
            # variant. A partial overlap is not expressed today and
            # would need a multi-value gate; it falls back to ungated
            # (conservative) rather than silently excluding.
            gate = (
                names[0]
                if len(names) == 1 and variant_count > 1
                else None
            )
            dims.setdefault(
                key,
                _make_spec_dimension(
                    key,
                    variant=gate,
                    catalog_lookup=f"SERVICE_SPECS variant '{gate or '*'}'",
                ),
            )
    else:
        for sku in spec.get("skus") or []:
            key = sku["usage_key"]
            attributes = sku.get("attributes") or {}
            lookup = ", ".join(
                f"{field}={value}" for field, value in sorted(attributes.items())
            )
            dims.setdefault(
                key,
                _make_spec_dimension(
                    key,
                    billing_model=sku.get("billing_model"),
                    catalog_lookup=lookup,
                ),
            )
    return [dims[key] for key in sorted(dims)]


# Audited dimensions a billed service expects but this pipeline cannot
# price or derive today. Only known, material omissions belong here.
# Storage, request, and log dimensions with a SERVICE_SPECS entry are
# derived from those specs instead (see _spec_dimensions).
_DECLARED_DIMENSIONS: dict[str, list[DimensionSpec]] = {
    "AmazonCloudWatch": [
        DimensionSpec(
            dimension="custom_metrics",
            label="custom metrics",
            disposition=DimensionDisposition.UNSUPPORTED,
            reason=(
                "Custom metric charges depend on application-level "
                "instrumentation that is not derivable from the "
                "architecture; outside this pipeline's pricing scope."
            ),
        ),
        DimensionSpec(
            dimension="alarms",
            label="alarms",
            disposition=DimensionDisposition.UNSUPPORTED,
            reason=(
                "Alarm counts are a deployment choice, not derivable "
                "from the architecture; outside this pipeline's pricing "
                "scope."
            ),
        ),
    ],
}

# Canonical display names per Price List service code.
_CANONICAL_NAMES: dict[str, str] = {
    "AWSQueueService": "Amazon SQS",
    "AmazonSNS": "Amazon SNS",
    "AmazonS3": "Amazon S3",
    "AmazonDynamoDB": "Amazon DynamoDB",
    "AmazonApiGateway": "Amazon API Gateway",
    "AWSLambda": "AWS Lambda",
    "AmazonCloudFront": "Amazon CloudFront",
    "AmazonRoute53": "Amazon Route 53",
    "AmazonCloudWatch": "Amazon CloudWatch",
}

# Normalized component-name aliases for services with no SERVICE_SPECS
# entry (their names cannot be discovered from the specs).
_DECLARED_NAME_ALIASES: dict[str, str] = {
    "amazon cloudwatch": "AmazonCloudWatch",
    "cloudwatch": "AmazonCloudWatch",
    "aws cloudwatch": "AmazonCloudWatch",
}


def _build_registry() -> tuple[
    dict[str, ServiceCoverage], dict[str, str]
]:
    dims_by_service: dict[str, dict[str, DimensionSpec]] = {}
    aliases: dict[str, str] = {}
    for name, spec in SERVICE_SPECS.items():
        code = spec["service_code"]
        aliases[name] = code
        bucket = dims_by_service.setdefault(code, {})
        for dim in _spec_dimensions(spec):
            bucket.setdefault(dim.dimension, dim)
    for code, declared in _DECLARED_DIMENSIONS.items():
        bucket = dims_by_service.setdefault(code, {})
        for dim in declared:
            if dim.dimension in bucket:
                raise ValueError(
                    f"Registry conflict: {code}/{dim.dimension} is both "
                    "spec-derived and declared."
                )
            bucket[dim.dimension] = dim
    aliases.update(_DECLARED_NAME_ALIASES)
    registry = {
        code: ServiceCoverage(
            service_code=code,
            canonical_name=_CANONICAL_NAMES.get(code, code),
            dimensions=tuple(
                bucket[key] for key in sorted(bucket)
            ),
        )
        for code, bucket in dims_by_service.items()
    }
    return registry, aliases


_REGISTRY, _ALIAS_TO_SERVICE = _build_registry()


def resolve_service(
    component_name: str, service_code: str = ""
) -> ServiceCoverage | None:
    """Canonical service coverage for a component, or None when unknown.

    A resolved Price List service code wins; otherwise the component
    name is normalized and matched against the same alias rules the
    pricing service uses (explicit descriptive aliases, exact match,
    then an 'amazon '/'aws ' prefix strip). No fuzzy or invented
    matching.
    """
    if service_code and service_code in _REGISTRY:
        return _REGISTRY[service_code]
    normalized = canonical_component_name(component_name)
    code = _ALIAS_TO_SERVICE.get(normalized)
    if code is None:
        for prefix in ("amazon ", "aws "):
            if normalized.startswith(prefix):
                code = _ALIAS_TO_SERVICE.get(normalized[len(prefix):])
                if code is not None:
                    break
    return _REGISTRY.get(code) if code else None


def resolve_configuration(
    facts_holder: PricedComponent,
) -> dict[str, str | None]:
    """Resolved variant/billing model for one priced component.

    Stamps written during pricing win; otherwise the values are
    inferred from the retrieved fact usage keys (which proves which
    gated dimension was fetched). Conflicting or absent evidence stays
    unresolved, which keeps every gated dimension applicable. Shared
    by the calculator and the usage estimator so both classify a
    component's dimensions identically.
    """
    variant = facts_holder.resolved_variant
    billing_model = facts_holder.resolved_billing_model
    if variant is None or billing_model is None:
        hint = infer_resolution(
            facts_holder.service_code,
            [fact.usage_key for fact in facts_holder.facts],
        )
        if variant is None:
            variant = hint.variant
        if billing_model is None:
            billing_model = hint.billing_model
    return {"variant": variant, "billing_model": billing_model}


def infer_resolution(
    service_code: str, fact_usage_keys
) -> ResolutionHint:
    """Infer variant/billing model from retrieved fact usage keys.

    A retrieved fact for a gated dimension proves which variant or
    billing model was resolved during pricing, which keeps hand-built
    fact sets and previously persisted pricing data classifiable
    without stamps. Conflicting evidence stays unresolved (None), and
    never excludes a dimension.
    """
    coverage = _REGISTRY.get(service_code)
    if coverage is None:
        return ResolutionHint()
    variants: set[str] = set()
    models: set[str] = set()
    for key in fact_usage_keys:
        dim = _dimension_spec(coverage, key)
        if dim is None:
            continue
        if dim.variant:
            variants.add(dim.variant)
        if dim.billing_model:
            models.add(dim.billing_model)
    return ResolutionHint(
        variant=next(iter(variants)) if len(variants) == 1 else None,
        billing_model=next(iter(models)) if len(models) == 1 else None,
    )


def _dimension_spec(
    coverage: ServiceCoverage, dimension: str
) -> DimensionSpec | None:
    for dim in coverage.dimensions:
        if dim.dimension == dimension:
            return dim
    return None


def coverage_for(
    service_code: str,
    *,
    variant: str | None = None,
    billing_model: str | None = None,
) -> tuple[list[DimensionSpec], list[DimensionSpec]]:
    """Split expected dimensions into (applicable, not_applicable).

    A gated dimension is not applicable only when the resolved
    configuration is known and differs; an unresolved gate keeps the
    dimension applicable so an unproven exclusion can never fake
    completeness.
    """
    coverage = _REGISTRY.get(service_code)
    if coverage is None:
        return [], []
    applicable: list[DimensionSpec] = []
    excluded: list[DimensionSpec] = []
    for dim in coverage.dimensions:
        if (
            dim.variant is not None
            and variant is not None
            and dim.variant != variant
        ):
            excluded.append(dim)
            continue
        if (
            dim.billing_model is not None
            and billing_model is not None
            and dim.billing_model != billing_model
        ):
            excluded.append(dim)
            continue
        applicable.append(dim)
    return applicable, excluded


def not_applicable_reason(
    dim: DimensionSpec,
    *,
    variant: str | None = None,
    billing_model: str | None = None,
) -> str:
    """Deterministic reason for a not-applicable dimension."""
    if dim.variant is not None and variant is not None and dim.variant != variant:
        return (
            f"Not applicable: the component resolved to the "
            f"'{variant}' API type; this dimension belongs to the "
            f"'{dim.variant}' variant."
        )
    if (
        dim.billing_model is not None
        and billing_model is not None
        and dim.billing_model != billing_model
    ):
        return (
            f"Not applicable: the component resolved to the "
            f"'{billing_model}' billing model; this dimension belongs "
            f"to the '{dim.billing_model}' model."
        )
    return "Not applicable to this component's configuration."


def estimable_reason(
    dim: DimensionSpec, *, price_fact_retrieved: bool
) -> str:
    """Why an estimable dimension is not priced in this slice."""
    facts = ", ".join(dim.required_usage_facts) or (
        "assumption inputs the pipeline does not model"
    )
    reason = (
        f"Usage for '{dim.dimension}' ({dim.label}) cannot be "
        f"deterministically derived: it needs {facts}. "
        "Assumption-based estimation runs only when the estimator "
        "proposes a bounded factor scenario; otherwise it is not "
        "priced rather than invented."
    )
    if not price_fact_retrieved:
        reason += " No retrieved price fact covers it either."
    return reason


def price_unavailable_reason(dim: DimensionSpec, region_code: str) -> str:
    """Why a derivable dimension has no catalog price in the result."""
    return (
        f"No price fact was retrieved for the expected "
        f"'{dim.dimension}' dimension ({dim.label}) in {region_code}; "
        "monthly usage is derivable but no catalog price is available, "
        "so no rate is invented."
    )
