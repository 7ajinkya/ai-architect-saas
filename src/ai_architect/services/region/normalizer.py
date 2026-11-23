"""Provider-aware region normalization.

Region normalization answers exactly one question: *what canonical region
is this?* It is deliberately separate from pricing, which answers *can
this component be priced in that region?*.

Resolution order (deterministic, never a guess):

1. an exact canonical region code, e.g. ``us-east-1``;
2. an exact AWS display name from botocore's endpoint metadata, matched
   case/whitespace/punctuation-insensitively, e.g.
   ``US East (N. Virginia)``;
3. a curated legacy alias, e.g. ``mumbai region`` or ``ireland``;
4. otherwise it fails closed.

Canonical codes and display names come from botocore's bundled
``endpoints.json`` — AWS's own metadata, already available offline as a
transitive dependency of boto3. No region list is duplicated here, and
no LLM or MCP call participates: region resolution must stay
deterministic.
"""

from functools import lru_cache
from typing import Optional

SUPPORTED_PROVIDER = "aws"

# Curated legacy aliases preserved from the former pricing-service table.
# Normalization matches these on the same normalized key as display names,
# so "N. Virginia" still resolves. Only genuinely legacy or informal
# forms belong here; authoritative AWS display names come from botocore.
LEGACY_REGION_ALIASES: dict[str, str] = {
    "mumbai region": "ap-south-1",
    "mumbai": "ap-south-1",
    "ap-south-1": "ap-south-1",
    "us-east-1": "us-east-1",
    "us east 1": "us-east-1",
    "n. virginia": "us-east-1",
    "north virginia": "us-east-1",
    "n virginia": "us-east-1",
    "eu-west-1": "eu-west-1",
    "ireland": "eu-west-1",
}

# AWS display names that do not uniquely identify one region.
AMBIGUOUS = "ambiguous"


class RegionNormalizationError(ValueError):
    """A region could not be resolved to exactly one canonical code."""


def _normalize_key(text: str) -> str:
    """Lowercase and reduce to alphanumeric words for tolerant matching.

    "US East (N. Virginia)", "us east (n. virginia)" and
    "US  East, N. Virginia" all reduce to "us east n virginia".
    """
    lowered = (text or "").lower()
    cleaned = "".join(char if char.isalnum() else " " for char in lowered)
    return " ".join(cleaned.split())


@lru_cache(maxsize=1)
def _aws_region_metadata() -> tuple[frozenset, dict]:
    """(canonical codes, normalized display name -> codes) from botocore."""
    try:
        from botocore.loaders import create_loader
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise RegionNormalizationError(
            "botocore is required for AWS region metadata but is not "
            "installed; refusing to guess a region."
        ) from exc

    endpoints = create_loader().load_data("endpoints") or {}

    partitions = endpoints.get("partitions") or []
    # Prefer the commercial "aws" partition. Gov/China/ISO partitions are
    # deliberately excluded: they are separate deployments with their own
    # region sets and must not be resolved from a generic AWS request.
    aws_partitions = [
        partition
        for partition in partitions
        if partition.get("partition") == "aws"
    ] or partitions

    codes: set = set()
    display: dict = {}
    for partition in aws_partitions:
        for code, meta in (partition.get("regions") or {}).items():
            codes.add(code)
            description = (meta or {}).get("description") or ""
            if description:
                display.setdefault(_normalize_key(description), set()).add(code)

    if not codes:  # pragma: no cover - corrupt metadata
        raise RegionNormalizationError(
            "botocore endpoint metadata contained no AWS regions; "
            "refusing to guess a region."
        )

    return frozenset(codes), display


@lru_cache(maxsize=1)
def _legacy_alias_map() -> dict:
    return {
        _normalize_key(alias): code
        for alias, code in LEGACY_REGION_ALIASES.items()
    }


def supported_region_codes(provider: str = SUPPORTED_PROVIDER) -> tuple:
    """Canonical region codes known for a provider."""
    _require_supported_provider(provider)
    return tuple(sorted(_aws_region_metadata()[0]))


def region_display_names(provider: str = SUPPORTED_PROVIDER) -> dict:
    """Canonical code -> AWS display name, for messages and diagnostics."""
    _require_supported_provider(provider)
    from botocore.loaders import create_loader

    endpoints = create_loader().load_data("endpoints") or {}
    partitions = endpoints.get("partitions") or []
    aws_partitions = [
        partition
        for partition in partitions
        if partition.get("partition") == "aws"
    ] or partitions
    names: dict = {}
    for partition in aws_partitions:
        for code, meta in (partition.get("regions") or {}).items():
            names[code] = (meta or {}).get("description") or ""
    return names


def _require_supported_provider(provider: Optional[str]) -> str:
    normalized = (provider or "").strip().lower()
    if normalized != SUPPORTED_PROVIDER:
        raise RegionNormalizationError(
            f"Region normalization is only implemented for provider "
            f"{SUPPORTED_PROVIDER!r}; got {normalized!r}. AWS region names "
            f"and codes must never be applied to another provider."
        )
    return normalized


def _assumption_region_names() -> frozenset:
    """Normalized assumption names that carry a deployment region.

    Only forms observed in real extraction artifacts are honored
    ("region", "primary region"). Anything else is ignored so an
    unrelated assumption can never become a deployment region.
    """
    return frozenset({"region", "primary region"})


def _assumption_region_values(extraction) -> list[str]:
    """Raw region values from region assumptions, in artifact order."""
    names = _assumption_region_names()
    values: list[str] = []
    for assumption in getattr(extraction, "assumptions", None) or []:
        name = " ".join(str(getattr(assumption, "name", "") or "").split())
        if name.lower() in names:
            values.append(str(getattr(assumption, "value", "") or ""))
    return values


def resolve_effective_region(extraction, provider: str = SUPPORTED_PROVIDER) -> str:
    """Resolve the single canonical region code for downstream stages.

    Resolution order (deterministic, never a guess):

    1. an explicitly stated canonical ``extraction.region``;
    2. otherwise, when the requirements carry an unresolved regional
       constraint (residency, compliance, stated geography), fail
       closed: a constraint must be stated explicitly, never assumed
       away;
    3. otherwise, a single distinct normalizable region value found in
       region assumptions;
    4. otherwise fail closed: no region anywhere (the historical
       empty-region error), or conflicting region assumptions.

    Assumption artifacts are never modified, so provenance survives:
    an empty canonical region alongside a region assumption means the
    code below was promoted; a set canonical region means explicitly
    stated. No global default is introduced.
    """
    _require_supported_provider(provider)

    canonical = ((getattr(extraction, "region", None) or "").strip())
    if canonical:
        return normalize_region(canonical, provider)

    from ai_architect.services.requirements.assumptions import (
        _has_regional_constraint,
    )

    if _has_regional_constraint(extraction):
        raise RegionNormalizationError(
            "Requirements carry a residency, compliance, or geographic "
            "constraint with no explicit region; the region must be "
            "stated explicitly rather than promoted from an assumption."
        )

    distinct: list[str] = []
    for raw in _assumption_region_values(extraction):
        text = (raw or "").strip()
        if not text:
            continue
        try:
            code = normalize_region(text, provider)
        except RegionNormalizationError:
            # Unresolvable assumption text is not a region; ignore it
            # rather than failing on prose that was never a code.
            continue
        if code not in distinct:
            distinct.append(code)

    if len(distinct) == 1:
        return distinct[0]
    if len(distinct) > 1:
        raise RegionNormalizationError(
            "Conflicting region assumptions "
            f"{sorted(distinct)}; state one region explicitly."
        )
    return normalize_region("", provider)


def normalize_region(
    raw: Optional[str],
    provider: str = SUPPORTED_PROVIDER,
) -> str:
    """Resolve a user-stated region to one canonical AWS region code.

    Fails closed on anything empty, unknown, or ambiguous. Never returns a
    default region: choosing a region the user did not ask for would
    change cost and residency outcomes silently.
    """
    _require_supported_provider(provider)

    text = (raw or "").strip()
    if not text:
        raise RegionNormalizationError(
            "Cannot normalize an empty region; no default is assumed."
        )

    codes, display = _aws_region_metadata()

    # 1. Exact canonical region code.
    candidate = text.lower()
    if candidate in codes:
        return candidate

    # 2. Exact AWS display name (case/whitespace/punctuation tolerant).
    key = _normalize_key(text)
    matches = display.get(key)
    if matches:
        if len(matches) > 1:
            raise RegionNormalizationError(
                f"Region {raw!r} is ambiguous: it matches {sorted(matches)}. "
                f"State one region explicitly."
            )
        return next(iter(matches))

    # 3. Curated legacy alias.
    alias = _legacy_alias_map().get(key)
    if alias is not None:
        return alias

    # 4. Fail closed, naming the value that could not be resolved.
    raise RegionNormalizationError(
        f"Cannot normalize region {raw!r} to an AWS region code; "
        f"no default is assumed. Use a region code such as us-east-1 or an "
        f"exact AWS region name such as 'US East (N. Virginia)'."
    )
