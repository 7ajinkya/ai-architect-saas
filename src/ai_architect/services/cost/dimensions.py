"""Human-readable labels for billed usage dimensions.

The authoritative expected-dimension declarations (which dimensions a
service bills, whether each is priceable/estimable/unsupported, and the
audited reasons) live in ``services.cost.coverage_registry``. This
module only carries the label vocabulary shared by pricing-service
facts and registry entries; unknown keys fall back to the raw key,
never a guessed description.
"""

DIMENSION_LABELS: dict[str, str] = {
    "requests": "requests",
    "rest_requests": "REST API requests",
    "http_requests": "HTTP API requests",
    "storage_gbmo": "storage (GB-month)",
    "gb_second": "function duration (GB-seconds)",
    "wru": "write request units",
    "rru": "read request units",
    "wcu_reserved": "provisioned write capacity (WCU-hours)",
    "rcu_reserved": "provisioned read capacity (RCU-hours)",
    "transfer_gb": "data transfer (GB)",
    "queries": "DNS queries",
    "zones": "hosted zones",
    "s3_put_requests": "S3 Tier1 requests (PUT/COPY/POST/LIST)",
    "s3_get_requests": "S3 Tier2 requests (GET/SELECT)",
    # CloudWatch log dimensions are estimable from retrieved SKUs;
    # metric/alarm charges remain unsupported.
    "log_data_ingested_gb": "log ingestion (GB)",
    "log_storage_gbmo": "log storage (GB-month)",
    "custom_metrics": "custom metrics",
    "alarms": "alarms",
}


def label_for_dimension(dimension: str) -> str:
    return DIMENSION_LABELS.get(dimension, dimension)
