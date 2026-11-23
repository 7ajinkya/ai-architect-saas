"""Deterministic AWS Price List pricing service.

Obtains live pricing from the AWS Price List Query API (boto3) and
normalizes it into typed PricingFact records with source and retrieval
timestamp preserved. Never fabricates or remembers prices: SKUs with no
usable USD price are skipped with an explicit warning, and missing data
surfaces as unavailable pricing downstream.
"""

from datetime import datetime, timedelta
import json as _json
import re

from ai_architect.domain.cost.models import (
    PriceTier,
    PricedComponent,
    PricingFact,
    PricingModel,
)
from ai_architect.services.region.normalizer import (
    LEGACY_REGION_ALIASES,
    RegionNormalizationError,
    normalize_region as region_normalize_region,
)

PRICING_SOURCE = "aws-price-list-api"
PRICING_QUERY_REGION = "us-east-1"
MAX_FRESHNESS_HOURS = 24

# Free-text region mentions to Price List region codes. Unknown values
# raise loudly; nothing is defaulted.
# Region normalization is a cross-cutting concern, not a pricing one. The
# canonical implementation lives in services.region and is re-exported
# here so existing pricing callers and tests keep working unchanged.
# Canonical codes and AWS display names come from botocore; these curated
# aliases remain for legacy and informal forms.
REGION_ALIASES = LEGACY_REGION_ALIASES

# Region-code prefixes used inside Price List usage-type codes. Only
# services whose SKU templates actually reference a prefix need one; a
# component whose template needs a prefix that is missing here becomes
# explicitly unpriced rather than aborting the pipeline.
REGION_USAGE_PREFIXES = {
    "ap-south-1": {"usage": "APS3", "cf_geo": "IN"},
    "us-east-1": {"usage": "USE1", "cf_geo": "US"},
}

# Placeholders in a SKU's attribute templates that require a
# region-specific prefix. Derived from the spec itself, so a new spec
# never silently requires an unlisted prefix.
_PREFIX_PLACEHOLDERS = ("{usage_prefix}", "{cf_geo}")

# S3 standard-storage usagetype form is region-dependent, live-verified:
# us-east-1 uses the bare form "TimedStorage-ByteHrs" (the
# "{usage_prefix}-TimedStorage-ByteHrs" template matches nothing there),
# while ap-south-1 uses the prefixed "APS3-TimedStorage-ByteHrs" form.
# Only live-verified exceptions belong here; every other region keeps
# the templated prefixed form. Never guessed, only recorded.
S3_STORAGE_USAGETYPE_OVERRIDES = {
    "us-east-1": "TimedStorage-ByteHrs",
}

# Component-name (normalized) to Price List SKU queries. Each SKU query is
# {usage_key, attributes} where attribute values support {region_code},
# {usage_prefix} and {cf_geo} templating. Usage-based SKUs are listed
# when the calculator can derive their consumption deterministically or
# the usage estimator has a bounded rule for them.
# SKUs may carry a "billing_model" tag; when a spec tags its SKUs, only
# the model selected for the component (see _pick_billing_model) is
# queried, so another model's SKUs can never masquerade as the chosen
# model's facts. Untagged SKUs (e.g. DynamoDB storage) are billed under
# both models and are queried for either.
SERVICE_SPECS: dict[str, dict] = {
    "amazon simple queue service": {
        "service_code": "AWSQueueService",
        "variants": {
            "standard": {"group": "SQS-APIRequest-Tier1", "queueType": "Standard"},
            "fifo": {"group": "SQS-APIRequest-Tier1", "queueType": "FIFO (first-in, first-out)"},
            "fair": {"group": "SQS-APIRequest-Tier1", "queueType": "Fair"},
        },
        "default_variant": "standard",
        "usage_key": "requests",
    },
    # Alias entry: live selections name this component "Amazon SQS
    # (Standard)", which normalizes to "amazon sqs"; _match_spec strips
    # the "amazon " prefix to the short form "sqs" defined here.
    "sqs": {
        "service_code": "AWSQueueService",
        "variants": {
            "standard": {"group": "SQS-APIRequest-Tier1", "queueType": "Standard"},
            "fifo": {"group": "SQS-APIRequest-Tier1", "queueType": "FIFO (first-in, first-out)"},
            "fair": {"group": "SQS-APIRequest-Tier1", "queueType": "Fair"},
        },
        "default_variant": "standard",
        "usage_key": "requests",
    },
    # Amazon SNS: live-verified request SKU is discriminated by
    # group=SNS-Requests-Tier1 (usagetype Requests-Tier1); the group
    # filter is the stable selector. Named "Amazon SNS" in live
    # selections, which normalizes to "amazon sns" and resolves here
    # via the "amazon " prefix strip. Only the requests dimension is
    # derivable; nothing else is invented.
    "sns": {
        "service_code": "AmazonSNS",
        "skus": [
            {
                "usage_key": "requests",
                "attributes": {"group": "SNS-Requests-Tier1"},
            },
        ],
    },
    "amazon s3": {
        "service_code": "AmazonS3",
        "skus": [
            {
                "usage_key": "storage_gbmo",
                "attributes": {"usagetype": "{usage_prefix}-TimedStorage-ByteHrs"},
            },
            # S3 request classes are separate SKUs: Tier1 covers
            # PUT/COPY/POST/LIST, Tier2 covers GET/SELECT and other
            # read-class calls. productFamily + group is the stable
            # selector; the two tiers are disjoint, so estimating them
            # from one shared call volume never double-counts traffic.
            {
                "usage_key": "s3_put_requests",
                "attributes": {
                    "productFamily": "API Request",
                    "group": "S3-API-Tier1",
                },
            },
            {
                "usage_key": "s3_get_requests",
                "attributes": {
                    "productFamily": "API Request",
                    "group": "S3-API-Tier2",
                },
            },
        ],
    },
    "amazon simple storage service": {
        "service_code": "AmazonS3",
        "skus": [
            {
                "usage_key": "storage_gbmo",
                "attributes": {"usagetype": "{usage_prefix}-TimedStorage-ByteHrs"},
            },
            {
                "usage_key": "s3_put_requests",
                "attributes": {
                    "productFamily": "API Request",
                    "group": "S3-API-Tier1",
                },
            },
            {
                "usage_key": "s3_get_requests",
                "attributes": {
                    "productFamily": "API Request",
                    "group": "S3-API-Tier2",
                },
            },
        ],
    },
    # Billing-model-gated spec: the request billing model and the
    # provisioned (committed-throughput) model are mutually exclusive
    # per table, so exactly one model's SKUs is queried per component
    # (see _pick_billing_model). Fetching both made the provisioned
    # SKUs surface as phantom wcu_reserved/rcu_reserved dimensions on
    # every on-demand design.
    "amazon dynamodb": {
        "service_code": "AmazonDynamoDB",
        "skus": [
            {
                "usage_key": "wru",
                "billing_model": "pay_per_request",
                "attributes": {
                    "group": "DDB-WriteUnits",
                    "operation": "PayPerRequestThroughput",
                },
            },
            {
                "usage_key": "rru",
                "billing_model": "pay_per_request",
                "attributes": {
                    "group": "DDB-ReadUnits",
                    "operation": "PayPerRequestThroughput",
                },
            },
            {
                "usage_key": "wcu_reserved",
                "billing_model": "provisioned",
                "attributes": {
                    "group": "DDB-WriteUnits",
                    "operation": "CommittedThroughput",
                },
            },
            {
                "usage_key": "rcu_reserved",
                "billing_model": "provisioned",
                "attributes": {
                    "group": "DDB-ReadUnits",
                    "operation": "CommittedThroughput",
                },
            },
            # Table storage is billed per GB-month independently of the
            # read/write capacity model, so this SKU intentionally has
            # no billing_model tag: it applies to on-demand and
            # provisioned tables alike.
            {
                "usage_key": "storage_gbmo",
                "attributes": {"group": "DDB-Storage"},
            },
        ],
    },
    "amazon api gateway": {
        "service_code": "AmazonApiGateway",
        # REST and HTTP are mutually exclusive API types: exactly one
        # variant is priced per component (see _pick_apigateway_variant).
        # They must never be listed as independent skus, which would
        # price the same traffic twice.
        "variants": {
            # REST APIs are discriminated by operation alone:
            # ApiGatewayRequest SKUs carry description
            # "API calls received", never "REST API", so a
            # description filter matches nothing.
            "rest": {
                "operation": "ApiGatewayRequest",
                "usage_key": "rest_requests",
            },
            # HTTP APIs are a separate operation, not a
            # description variant of ApiGatewayRequest.
            "http": {
                "operation": "ApiGatewayHttpApi",
                "usage_key": "http_requests",
            },
        },
        "default_variant": "rest",
    },
    "aws lambda": {
        "service_code": "AWSLambda",
        "skus": [
            {
                "usage_key": "requests",
                # Live-verified: ap-south-1 uses "APS3-Request";
                # us-east-1 uses the bare "Request" form (the old
                # "{prefix}-Lambda-Requests" template matched nothing
                # live and left Lambda explicitly unpriced).
                "attributes": {"usagetype": "{usage_prefix}-Request"},
                "usagetype_overrides": {"us-east-1": "Request"},
            },
            {
                "usage_key": "gb_second",
                "attributes": {"usagetype": "{usage_prefix}-Lambda-GB-Second"},
                # Live-verified: us-east-1 uses the bare form.
                "usagetype_overrides": {"us-east-1": "Lambda-GB-Second"},
            },
        ],
    },
    "api gateway": {
        "service_code": "AmazonApiGateway",
        "variants": {
            "rest": {
                "operation": "ApiGatewayRequest",
                "usage_key": "rest_requests",
            },
            "http": {
                "operation": "ApiGatewayHttpApi",
                "usage_key": "http_requests",
            },
        },
        "default_variant": "rest",
    },
    "amazon cloudfront": {
        "service_code": "AmazonCloudFront",
        "skus": [
            {
                "usage_key": "transfer_gb",
                "attributes": {"usagetype": "{cf_geo}-DataTransfer-Out-Bytes"},
                "global_sku": True,
            },
            {
                "usage_key": "requests",
                "attributes": {"usagetype": "{cf_geo}-Requests-HTTPS-Proxy"},
                "global_sku": True,
            },
        ],
    },
    "cloudfront": {
        "service_code": "AmazonCloudFront",
        "skus": [
            {
                "usage_key": "transfer_gb",
                "attributes": {"usagetype": "{cf_geo}-DataTransfer-Out-Bytes"},
                "global_sku": True,
            },
            {
                "usage_key": "requests",
                "attributes": {"usagetype": "{cf_geo}-Requests-HTTPS-Proxy"},
                "global_sku": True,
            },
        ],
    },
    "amazon route 53": {
        "service_code": "AmazonRoute53",
        "skus": [
            {
                "usage_key": "zones",
                "attributes": {"usagetype": "HostedZone"},
                "global_sku": True,
            },
            {
                "usage_key": "queries",
                "attributes": {"usagetype": "{usage_prefix}-DNS-Queries"},
            },
        ],
    },
    "route53": {
        "service_code": "AmazonRoute53",
        "skus": [
            {
                "usage_key": "zones",
                "attributes": {"usagetype": "HostedZone"},
                "global_sku": True,
            },
            {
                "usage_key": "queries",
                "attributes": {"usagetype": "{usage_prefix}-DNS-Queries"},
            },
        ],
    },
    "route 53": {
        "service_code": "AmazonRoute53",
        "skus": [
            {
                "usage_key": "zones",
                "attributes": {"usagetype": "HostedZone"},
                "global_sku": True,
            },
            {
                "usage_key": "queries",
                "attributes": {"usagetype": "{usage_prefix}-DNS-Queries"},
            },
        ],
    },
    # CloudWatch Logs ingestion and archive storage are separate SKUs.
    # The group selectors follow the Price List product-attribute shape;
    # they are resolved fail-closed: if they match nothing, the
    # dimensions stay unresolved and disclosed instead of being priced
    # from an invented rate. Metric and alarm charges are deployment
    # choices and remain explicitly unsupported in the registry.
    "cloudwatch": {
        "service_code": "AmazonCloudWatch",
        "skus": [
            {
                "usage_key": "log_data_ingested_gb",
                "attributes": {"group": "CW:DataProcessing"},
            },
            {
                "usage_key": "log_storage_gbmo",
                "attributes": {"group": "CW:Storage"},
            },
        ],
    },
}

# Category-level explanation used when no spec maps a component. These name
# the missing usage inputs; nothing is priced from them.
UNPRICED_CATEGORY_HINTS = {
    "compute": "instance type and count, or function duration/memory configuration",
    "database": "instance class/count or provisioned-throughput specification",
    "caching": "node type and count",
    "security": "rule/policy counts",
    "monitoring": "metric and log volumes",
    "hosting": "hosted application count, build minutes, and data transfer",
}

PURCHASE_OPTION_PREFERENCE = [
    "No Upfront",
    "Partial Upfront",
    "All Upfront",
    "Heavy Utilization",
]

LEASE_MONTHS = {"1yr": 12, "3yr": 36}
HOURS_PER_MONTH = 730


def normalize_region(raw: str) -> str:
    """Map free-text region mentions to an AWS region code.

    Compatibility delegate: the real implementation is
    ``services.region.normalize_region``, which also resolves AWS display
    names such as "US East (N. Virginia)". This wrapper keeps the pricing
    call site and its tests unchanged.
    """
    try:
        return region_normalize_region(raw)
    except RegionNormalizationError as exc:
        raise ValueError(str(exc)) from exc


# Descriptive resource nouns that Component Selection may append to a
# canonical service name. Each mapping is explicit and resolves to an
# existing SERVICE_SPECS key; there is no suffix stripping or fuzzy
# matching, so an unlisted name still stays explicitly unmapped.
CANONICAL_COMPONENT_ALIASES: dict[str, str] = {
    "aws api gateway http api": "amazon api gateway",
    "api gateway http api": "amazon api gateway",
    "amazon api gateway http api": "amazon api gateway",
    "amazon lambda": "aws lambda",
    "amazon lambda function": "aws lambda",
    "aws lambda function": "aws lambda",
    "lambda function": "aws lambda",
    "amazon dynamodb table": "amazon dynamodb",
    "aws dynamodb table": "amazon dynamodb",
    "dynamodb table": "amazon dynamodb",
    "amazon cloudwatch log group": "cloudwatch",
    "aws cloudwatch log group": "cloudwatch",
    "cloudwatch log group": "cloudwatch",
    "amazon cloudwatch logs": "cloudwatch",
    "aws cloudwatch logs": "cloudwatch",
    "cloudwatch logs": "cloudwatch",
}


def normalize_component_name(name: str) -> str:
    """Lowercase a component name with parenthetical aliases stripped."""
    text = (name or "").lower()
    text = re.sub(r"\(.*?\)", "", text)
    return " ".join(text.split())


def canonical_component_name(name: str) -> str:
    """Normalize a name and apply only the explicit descriptive aliases."""
    normalized = normalize_component_name(name)
    return CANONICAL_COMPONENT_ALIASES.get(normalized, normalized)


def _match_spec(normalized: str) -> dict | None:
    canonical = CANONICAL_COMPONENT_ALIASES.get(normalized, normalized)
    if canonical in SERVICE_SPECS:
        return SERVICE_SPECS[canonical]
    for prefix in ("amazon ", "aws "):
        if canonical.startswith(prefix):
            short = canonical[len(prefix):]
            if short in SERVICE_SPECS:
                return SERVICE_SPECS[short]
    return None


def _parse_float(value: object, what: str) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as e:
        raise ValueError(f"Unparseable {what}: {value!r}") from e


def _parse_tiers(price_dimensions: dict, warnings: list[str], sku: str) -> list[PriceTier]:
    tiers: list[PriceTier] = []
    for rate_code in sorted(price_dimensions):
        dim = price_dimensions[rate_code]
        try:
            usd = dim.get("pricePerUnit", {}).get("USD")
            if usd is None:
                raise ValueError("missing USD pricePerUnit")
            end_raw = dim.get("endRange", "Inf")
            tiers.append(
                PriceTier(
                    begin=_parse_float(dim.get("beginRange", 0), "beginRange"),
                    end=None if str(end_raw) == "Inf" else _parse_float(end_raw, "endRange"),
                    unit=str(dim.get("unit", "")),
                    unit_price_usd=_parse_float(usd, "USD price"),
                    description=str(dim.get("description", "")),
                    rate_code=str(rate_code),
                )
            )
        except ValueError as e:
            warnings.append(f"Skipped malformed price dimension {rate_code} of {sku}: {e}")
    tiers.sort(key=lambda t: t.begin)
    return tiers


def _parse_reserved_facts(
    service_code: str,
    service_name: str,
    region_code: str,
    usage_key: str,
    terms: dict,
    retrieved_at,
    warnings: list[str],
) -> list[PricingFact]:
    reserved = terms.get("Reserved", {})
    by_lease: dict[str, list[tuple[dict, dict]]] = {}
    for term_id in sorted(reserved):
        term = reserved[term_id]
        attrs = term.get("termAttributes", {})
        lease = str(attrs.get("LeaseContractLength", ""))
        if lease not in LEASE_MONTHS:
            warnings.append(
                f"Skipped reserved term {term_id} with unknown lease length {lease!r}"
            )
            continue
        by_lease.setdefault(lease, []).append((attrs, term.get("priceDimensions", {})))
    facts: list[PricingFact] = []
    for lease, options in by_lease.items():
        months = LEASE_MONTHS[lease]
        model = (
            PricingModel.RESERVED_1YR if months == 12 else PricingModel.RESERVED_3YR
        )
        # Deterministic preference: No Upfront first, else lowest effective monthly.
        def effective_monthly(item: tuple[dict, dict]) -> float:
            attrs, dims = item
            hourly, upfront = _split_reserved_dims(dims)
            rate = hourly["price"] if hourly else float("inf")
            return upfront / months + rate * HOURS_PER_MONTH

        ordered = sorted(
            options,
            key=lambda item: (
                PURCHASE_OPTION_PREFERENCE.index(str(item[0].get("PurchaseOption")))
                if str(item[0].get("PurchaseOption")) in PURCHASE_OPTION_PREFERENCE
                else len(PURCHASE_OPTION_PREFERENCE),
                effective_monthly(item),
            ),
        )
        attrs, dims = ordered[0]
        hourly, upfront = _split_reserved_dims(dims)
        if hourly is None:
            warnings.append(
                f"Skipped reserved {lease} {service_code}/{usage_key}: no hourly dimension"
            )
            continue
        facts.append(
            PricingFact(
                service_code=service_code,
                service_name=service_name,
                region_code=region_code,
                pricing_model=model,
                usage_key=usage_key,
                unit=hourly["unit"],
                tiers=[
                    PriceTier(
                        begin=0.0,
                        end=None,
                        unit=hourly["unit"],
                        unit_price_usd=hourly["price"],
                        description=hourly["description"],
                        rate_code=hourly["rate_code"],
                    )
                ],
                upfront_usd=upfront,
                purchase_option=str(attrs.get("PurchaseOption", "")),
                lease_length=lease,
                offering_class=str(attrs.get("OfferingClass", "")),
                source=PRICING_SOURCE,
                retrieved_at=retrieved_at,
            )
        )
    return facts


def _split_reserved_dims(dims: dict) -> tuple[dict | None, float]:
    """Split reserved price dimensions into hourly rate and upfront fee."""
    hourly: dict | None = None
    upfront = 0.0
    for rate_code in sorted(dims):
        dim = dims[rate_code]
        try:
            price = _parse_float(dim.get("pricePerUnit", {}).get("USD"), "USD price")
        except ValueError:
            continue
        unit = str(dim.get("unit", ""))
        if "Hrs" in unit or "Hour" in unit:
            if hourly is None:
                hourly = {
                    "unit": unit,
                    "price": price,
                    "description": str(dim.get("description", "")),
                    "rate_code": str(rate_code),
                }
        else:
            upfront += price
    return hourly, upfront


def _sku_needs_prefix(sku: dict) -> bool:
    """True when a SKU's attribute templates reference a region prefix."""
    return any(
        placeholder in str(value)
        for value in (sku.get("attributes") or {}).values()
        for placeholder in _PREFIX_PLACEHOLDERS
    )


def _spec_needs_prefix(spec: dict) -> bool:
    return any(_sku_needs_prefix(sku) for sku in spec.get("skus") or [])


class PricingService:
    """Live AWS Price List pricing with typed normalization.

    The boto3 client is injectable so unit tests never touch the network.
    """

    def __init__(self, client=None, query_region: str = PRICING_QUERY_REGION):
        self._client = client
        self._query_region = query_region

    def _get_client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("pricing", region_name=self._query_region)
        return self._client

    def get_price_facts(
        self,
        service_code: str,
        region_code: str,
        attribute_filters: dict[str, str],
    ) -> tuple[list[PricingFact], list[str]]:
        """Fetch and normalize all SKUs matching the filters.

        Returns (facts, warnings). Empty facts with no error means the API
        genuinely has no matching SKU — callers must treat that as
        unavailable pricing, never as zero cost.
        """
        retrieved_at = datetime.utcnow()
        filters = [
            {"Type": "TERM_MATCH", "Field": field, "Value": value}
            for field, value in sorted(attribute_filters.items())
        ]
        facts: list[PricingFact] = []
        warnings: list[str] = []
        token: str | None = None
        service_name = ""
        while True:
            kwargs: dict = {
                "ServiceCode": service_code,
                "Filters": filters,
                "MaxResults": 100,
            }
            if token:
                kwargs["NextToken"] = token
            resp = self._get_client().get_products(**kwargs)
            for raw in resp.get("PriceList", []):
                doc = _json.loads(raw)
                attrs = doc.get("product", {}).get("attributes", {})
                service_name = service_name or str(attrs.get("servicename", ""))
                terms = doc.get("terms", {})
                usage_key = str(attrs.get("usagetype", ""))
                on_demand = terms.get("OnDemand", {})
                if on_demand:
                    term = on_demand[sorted(on_demand)[0]]
                    dims = term.get("priceDimensions", {})
                    tiers = _parse_tiers(dims, warnings, usage_key or service_code)
                    if tiers:
                        facts.append(
                            PricingFact(
                                service_code=service_code,
                                service_name=service_name,
                                region_code=region_code,
                                pricing_model=PricingModel.ON_DEMAND,
                                usage_key=usage_key,
                                unit=tiers[0].unit,
                                tiers=tiers,
                                source=PRICING_SOURCE,
                                retrieved_at=retrieved_at,
                            )
                        )
                    else:
                        warnings.append(
                            f"Skipped {service_code}/{usage_key}: no usable USD tiers"
                        )
                facts.extend(
                    _parse_reserved_facts(
                        service_code, service_name, region_code, usage_key,
                        terms, retrieved_at, warnings,
                    )
                )
            token = resp.get("NextToken")
            if not token:
                break
        return facts, warnings

    def assert_fresh(
        self, facts: list[PricingFact], max_age_hours: int = MAX_FRESHNESS_HOURS
    ) -> None:
        """Reject stale pricing per the 24h freshness requirement."""
        now = datetime.utcnow()
        for fact in facts:
            age_hours = (now - fact.retrieved_at).total_seconds() / 3600
            if age_hours > max_age_hours:
                raise ValueError(
                    f"Stale pricing for {fact.service_code}/{fact.usage_key}: "
                    f"{age_hours:.1f}h old (limit {max_age_hours}h); "
                    "re-verify live before finalizing."
                )

    def price_components(
        self, selection, region_code: str
    ) -> tuple[list[PricedComponent], list[str]]:
        """Resolve each selected component to live price facts or a reason.

        A missing region usage-prefix mapping is not a whole-pipeline
        failure: it only blocks components whose SKU templates actually
        need that prefix. Those components are reported as explicitly
        unpriced with the reason, which is the same fail-closed outcome
        as any other unpriceable component. A prefix is never guessed.
        """
        prefixes = REGION_USAGE_PREFIXES.get(region_code)
        components: list[PricedComponent] = []
        warnings: list[str] = []
        for comp in selection.selected_components:
            normalized = normalize_component_name(comp.name)
            spec = _match_spec(normalized)
            category = str(getattr(comp, "category", "") or "")
            if spec is None:
                hint = UNPRICED_CATEGORY_HINTS.get(
                    category.lower(),
                    "required usage inputs",
                )
                components.append(
                    PricedComponent(
                        component_name=comp.name,
                        category=category,
                        unavailable_reason=(
                            "Cannot price: no Price List mapping and "
                            f"{hint} are not determined by the "
                            "requirements/capacity/component outputs."
                        ),
                    )
                )
                continue
            if _spec_needs_prefix(spec) and prefixes is None:
                components.append(
                    PricedComponent(
                        component_name=comp.name,
                        category=category,
                        service_code=spec["service_code"],
                        unavailable_reason=(
                            "Cannot price: no Price List usage-type prefix "
                            f"mapping is registered for region {region_code}, "
                            f"so the {spec['service_code']} pricing request "
                            "cannot be constructed safely; no prefix is "
                            "guessed."
                        ),
                    )
                )
                continue
            facts: list[PricingFact] = []
            # Configuration values resolved during pricing, stamped on the
            # PricedComponent so the coverage registry can classify
            # mutually exclusive dimensions without re-deriving anything.
            resolved_variant: str | None = None
            resolved_billing_model: str | None = None
            if "variants" in spec:
                if spec["service_code"] == "AmazonApiGateway":
                    variant, variant_warning = _pick_apigateway_variant(
                        comp, spec
                    )
                    if variant_warning:
                        warnings.append(variant_warning)
                    if variant is None:
                        components.append(
                            PricedComponent(
                                component_name=comp.name,
                                category=category,
                                service_code=spec["service_code"],
                                unavailable_reason=(
                                    f"{comp.name}: WebSocket API Gateway "
                                    "pricing needs message and "
                                    "connection-minute dimensions that "
                                    "are not derivable from the "
                                    "requirements/capacity outputs; not "
                                    "priced rather than folded into "
                                    "REST/HTTP or estimated."
                                ),
                            )
                        )
                        continue
                else:
                    variant, variant_warning = (
                        _pick_sqs_variant(comp),
                        None,
                    )
                resolved_variant = variant
                vdef = dict(spec["variants"][variant])
                # Per-variant usage keys (API Gateway) are routing
                # labels, never Price List filter fields. When a variant
                # defines none (SQS), the spec-level usage_key fills the
                # gap; without it the raw usagetype leaked into the
                # usage-key slot, so the calculator had no derivation
                # rule and SQS stayed permanently unpriced.
                variant_usage_key = (
                    vdef.pop("usage_key", None) or spec.get("usage_key")
                )
                attrs = vdef
                attrs["regionCode"] = region_code
                sub_facts, sub_warnings = self.get_price_facts(
                    spec["service_code"], region_code, attrs
                )
                if variant_usage_key is not None:
                    for fact in sub_facts:
                        fact.usage_key = variant_usage_key
                facts.extend(sub_facts)
                warnings.extend(sub_warnings)
            else:
                # Prefix-free specs may still be priced in a region with no
                # registered prefix; their templates never reference one.
                prefix_values = prefixes or {"usage": "", "cf_geo": ""}
                billing_model: str | None = None
                if any(sku.get("billing_model") for sku in spec["skus"]):
                    billing_model, billing_warning = _pick_billing_model(comp)
                    if billing_warning:
                        warnings.append(billing_warning)
                resolved_billing_model = billing_model
                for sku in spec["skus"]:
                    if (
                        billing_model is not None
                        and sku.get("billing_model")
                        and sku["billing_model"] != billing_model
                    ):
                        continue
                    attrs = {
                        field: value.format(
                            region_code=region_code,
                            usage_prefix=prefix_values["usage"],
                            cf_geo=prefix_values["cf_geo"],
                        )
                        for field, value in sku["attributes"].items()
                    }
                    usagetype_overrides = sku.get("usagetype_overrides")
                    if usagetype_overrides and "usagetype" in attrs:
                        # Region-specific live-verified usagetype forms:
                        # an override wins; otherwise the templated
                        # prefixed form stands. Never guessed.
                        attrs["usagetype"] = usagetype_overrides.get(
                            region_code, attrs["usagetype"]
                        )
                    if (
                        spec["service_code"] == "AmazonS3"
                        and "usagetype" in attrs
                    ):
                        # Region-aware S3 storage form: live-verified
                        # overrides win; otherwise the templated prefixed
                        # form stands. Never guessed.
                        attrs["usagetype"] = (
                            S3_STORAGE_USAGETYPE_OVERRIDES.get(
                                region_code, attrs["usagetype"]
                            )
                        )
                    if not sku.get("global_sku", False):
                        attrs["regionCode"] = region_code
                    sub_facts, sub_warnings = self.get_price_facts(
                        spec["service_code"], region_code, attrs
                    )
                    for fact in sub_facts:
                        fact.usage_key = sku["usage_key"]
                    facts.extend(sub_facts)
                    warnings.extend(sub_warnings)
            if not facts:
                components.append(
                    PricedComponent(
                        component_name=comp.name,
                        category=category,
                        service_code=spec["service_code"],
                        resolved_variant=resolved_variant,
                        resolved_billing_model=resolved_billing_model,
                        unavailable_reason=(
                            "Price List API returned no usable SKU for "
                            f"{spec['service_code']} in {region_code}."
                        ),
                    )
                )
                continue
            self.assert_fresh(facts)
            components.append(
                PricedComponent(
                    component_name=comp.name,
                    category=category,
                    service_code=spec["service_code"],
                    resolved_variant=resolved_variant,
                    resolved_billing_model=resolved_billing_model,
                    facts=facts,
                )
            )
        return components, warnings


def _pick_sqs_variant(comp) -> str:
    text = f"{comp.name} {(comp.role or '')}".lower()
    if "fifo" in text:
        return "fifo"
    if "fair" in text:
        return "fair"
    return "standard"


def _pick_billing_model(comp) -> tuple[str, str | None]:
    """Pick DynamoDB's mutually exclusive billing model for a component.

    Inspects name + role text only (the same precedent as the SQS and
    API Gateway pickers). "provisioned"/committed signals choose
    committed-throughput SKUs, whose capacity inputs the pipeline does
    not model, so they stay explicitly unpriced. Explicit on-demand
    signals choose request-based SKUs. With no signal at all the
    pipeline falls back to on-demand request pricing -- the only
    billing model its request-based capacity derivations can price --
    and says so with a warning. Never silently reinterprets an explicit
    user selection.
    """
    text = f"{comp.name} {(comp.role or '')}".lower()
    collapsed = re.sub(r"[^a-z0-9]", "", text)
    if (
        "provisioned" in collapsed
        or "committedcapacity" in collapsed
        or "committedthroughput" in collapsed
    ):
        return "provisioned", None
    if "ondemand" in collapsed or "payperrequest" in collapsed:
        return "pay_per_request", None
    return "pay_per_request", (
        f"{comp.name}: no on-demand/provisioned billing-model signal in "
        "name/role; priced with on-demand request SKUs by default."
    )


def _pick_apigateway_variant(comp, spec) -> tuple[str | None, str | None]:
    """Pick the mutually exclusive API Gateway variant for a component.

    Inspects only name + role text. Returns (variant, warning) where
    variant is "rest", "http", or None for the unsupported WebSocket
    type; warning is None unless REST was assumed by default.

    Matching is phrase-aware, not naive-substring: "https" must not
    count as "http API", and "websocket" must match neither type.
    A text naming both types resolves deterministically to HTTP
    (checked first); ambiguity of any other kind falls to the default.
    """
    text = f"{comp.name} {(comp.role or '')}".lower()
    words = set(re.findall(r"[a-z0-9]+", text))
    collapsed = re.sub(r"[^a-z0-9]", "", text)
    # WebSocket is a third API type with live SKUs (operation
    # "ApiGatewayWebSocket"; usagetypes "USE1-ApiGatewayMessage" and
    # "USE1-ApiGatewayMinute") but no supported mapping: pricing needs
    # both the message and the connection-minute dimension, the minute
    # volume is not derivable from the requirements/capacity outputs,
    # and this machinery fetches exactly one SKU per component. It is
    # therefore reported explicitly unpriced, never folded into REST or
    # HTTP and never estimated.
    if "websocket" in collapsed or "websocket" in words:
        return None, None
    # "http api" as a phrase ("httpapi" collapsed). "https" and
    # "httpsapi" never contain it, so protocol wording is safe.
    if "httpapi" in collapsed or (
        "http" in words and "api" in words
    ):
        return "http", None
    if "restapi" in collapsed or "restful" in collapsed or (
        "rest" in words
    ):
        return "rest", None
    default = spec.get("default_variant", "rest")
    return default, (
        f"{comp.name}: no REST/HTTP API-type signal in name/role; "
        f"priced as {default.upper()} API by default."
    )
