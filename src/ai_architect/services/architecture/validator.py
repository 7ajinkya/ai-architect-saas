"""Deterministic cross-artifact validation for ArchitectureResult.

Same convention as component selection: returns a list of error
strings; empty means valid. Raises nothing for invalid content except
unknown capacity metric keys passed directly to helpers.
"""

import math
import re
from collections import Counter

from ai_architect.agents.component_selection.models import (
    ComponentSelectionResult,
)
from ai_architect.domain.architecture.models import (
    DATA_STORE_CATEGORIES,
    EXTERNAL_ENDPOINTS,
    SCHEMA_VERSION,
    ArchitectureResult,
)
from ai_architect.domain.capacity.models import CapacityModelResult
from ai_architect.domain.cost.models import CostEstimationResult
from ai_architect.domain.requirements.models import RequirementsExtraction
from ai_architect.services.architecture.core import (
    CAPACITY_UNIT_BY_METRIC,
    build_cost_links,
    hash_upstream_inputs,
)

# Free-text numeric claims that must live only in CapacityValue/CostLink.
# The throughput guard is deliberately numeric-gated: qualitative words
# (throughput, load, traffic, or a bare unit with no quantity) are
# legitimate architectural prose, while a number attached to a
# throughput unit is an authoritative capacity claim.
_PRICE_PATTERN = re.compile(r"\$\s*\d|\b\d[\d,\.]*\s*/\s*mo\b", re.IGNORECASE)
_QPS_PATTERN = re.compile(
    r"\b\d[\d,\.]*(?:[kKmMbB])?\s*("
    r"qps|rps"
    r"|(?:req(?:uest)?s|writes?|reads?|msgs?|messages?)\s*/\s*s"
    r"|requests?\s+per\s+sec(ond)?s?"
    r")\b",
    re.IGNORECASE,
)
_STORAGE_PATTERN = re.compile(
    r"\b\d[\d,\.]*\s*(bytes?|kb|mb|gb|tb|pb)\b", re.IGNORECASE
)
_CONNECTIONS_PATTERN = re.compile(
    r"\b\d[\d,\.]*\s*connections?\b", re.IGNORECASE
)

# SLA-shaped claims. Allowed only when the same guarantee is traceable
# to requirements availability/latency text.
_SLA_PATTERN = re.compile(
    r"\d+\s*%|\b9\d\.\d+\b|\bsla\b|\buptime\b", re.IGNORECASE
)

_PROVIDER_KEYWORDS = ("amazon", "aws", "azure", "gcp", "google")

# Conservative short-form -> canonical-fragment map for well-known
# service names. An alias counts ONLY when its canonical target is
# covered by the selected/rejected vocabulary, so aliases can never
# smuggle an unknown service past the closed-world check; they merely
# let shorthand references to known components (e.g. "SQS" for a
# selected "Amazon Simple Queue Service") pass deterministically.
_COMPONENT_ALIASES = {
    "sqs": "simple queue service",
    "sns": "simple notification service",
    "s3": "simple storage service",
    "ec2": "elastic compute cloud",
    "ebs": "elastic block store",
    "elb": "elastic load balancing",
    "alb": "application load balancer",
    "nlb": "network load balancer",
    "eks": "elastic kubernetes service",
    "ecs": "elastic container service",
    "fargate": "fargate",
    "lambda": "lambda",
    "api gateway": "api gateway",
    "apigateway": "api gateway",
    "appsync": "appsync",
    "dynamodb": "dynamodb",
    "rds": "relational database service",
    "aurora": "aurora",
    "elasticache": "elasticache",
    "redshift": "redshift",
    "cloudfront": "cloudfront",
    "cognito": "cognito",
    "route53": "route 53",
    "route 53": "route 53",
    "eventbridge": "eventbridge",
    "kinesis": "kinesis",
    "msk": "msk",
    "sagemaker": "sagemaker",
    "bedrock": "bedrock",
    "waf": "waf",
    "iam": "iam",
    "vpc": "virtual private cloud",
    "step functions": "step functions",
}


_PROVIDER_RE = r"(?:amazon|aws|azure|gcp|google)"

# Unicode hyphen-like and space characters that model output may use
# instead of ASCII hyphen/space in component names (e.g. U+2011 NON-BREAKING
# HYPHEN in "Amazon Route\u201153"). Normalize these to ASCII so names match
# the Component Selection vocabulary deterministically.
_HYPHEN_CHARS = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
_SPACE_CHARS = "\u00a0\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000"


def _normalize_component_name(text: str) -> str:
    """Normalize component names for closed-world matching.

    Model output may use Unicode hyphen/space variants (e.g. U+2011
    NON-BREAKING HYPHEN in "Amazon Route\u201153") where the stored
    Component Selection name uses ASCII hyphen/space
    ("Amazon Route-53"). Normalize both to a canonical form so they match.

    Hyphens and spaces are both normalized to a single space, so that
    "Amazon Route-53" and "Amazon Route 53" compare equal. The canonical
    display name is looked up separately; this returns only the
    comparison key.
    """
    t = text.strip().lower()
    for ch in _HYPHEN_CHARS:
        t = t.replace(ch, " ")
    for ch in _SPACE_CHARS:
        t = t.replace(ch, " ")
    return " ".join(t.split())


def _scrub_known_mentions(lowered: str, known: set[str]) -> str:
    """Remove every vocabulary-covered service mention from text.

    Full selected/rejected names first (longest first), then
    whole-word short-form aliases whose canonical target is covered
    by the vocabulary -- including a leading provider keyword, so
    "AWS SQS" scrubs as one mention rather than leaving a dangling
    provider word behind. What remains is genuinely unknown prose.
    """
    scrubbed = lowered
    for name in sorted(known, key=len, reverse=True):
        scrubbed = scrubbed.replace(name, " ")
    for alias, target in _COMPONENT_ALIASES.items():
        if not any(target in name for name in known):
            continue
        scrubbed = re.sub(
            rf"\b{_PROVIDER_RE}\s+{re.escape(alias)}\b", " ", scrubbed
        )
        scrubbed = re.sub(rf"\b{re.escape(alias)}\b", " ", scrubbed)
    return scrubbed


def _unknown_service_mentions(
    lowered: str, known: set[str]
) -> list[str]:
    """Extract provider-led service mentions outside the vocabulary.

    Each `provider + service words` mention left after scrubbing known
    references names an infrastructure service the closed-world
    contract does not cover. Returns them for loud, specific errors.
    """
    scrubbed = _scrub_known_mentions(lowered, known)
    return [
        match.group(0).strip()
        for match in re.finditer(
            rf"\b{_PROVIDER_RE}\s+[a-z0-9][\w\-/&]*"
            r"(?:\s+[a-z0-9][\w\-/&]*){0,2}",
            scrubbed,
        )
    ]


def _mentions_provider(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in _PROVIDER_KEYWORDS)


def validate_architecture(
    result: ArchitectureResult,
    *,
    selection: ComponentSelectionResult,
    capacity: CapacityModelResult,
    cost: CostEstimationResult,
    extraction: RequirementsExtraction,
    requirements_session_id: str,
    design_session_id: str,
    provider: str,
    region_code: str,
) -> list[str]:
    """Validate an ArchitectureResult against its upstream artifacts."""
    errors: list[str] = []
    provider_norm = provider.strip().lower()

    _validate_metadata(
        result, selection, cost, extraction, capacity,
        requirements_session_id, design_session_id, region_code, errors,
    )

    selected_by_name = {comp.name: comp for comp in selection.selected_components}
    node_by_id = {node.node_id: node for node in result.nodes}

    _validate_nodes(result, selected_by_name, errors)
    _validate_edges(result, node_by_id, errors)
    _validate_entry_points(result, node_by_id, errors)
    _validate_data_stores(result, node_by_id, selected_by_name, errors)
    _validate_evidence(result, selection, errors)
    _validate_capacity(result, capacity, errors)
    _validate_cost(result, selection, cost, errors)
    _validate_provider_region(
        result, selected_by_name, cost, provider_norm, region_code, errors
    )
    _validate_safety(result, extraction, errors)
    _validate_decision_services(result, selection, errors)

    return errors


def _validate_metadata(result, selection, cost, extraction, capacity,
                       requirements_session_id, design_session_id,
                       region_code, errors) -> None:
    if result.schema_version != SCHEMA_VERSION:
        errors.append(
            f"Architecture schema_version {result.schema_version!r} does not "
            f"match {SCHEMA_VERSION!r}."
        )
    if not result.artifact_id.strip():
        errors.append("Architecture artifact_id must be non-empty.")
    if result.session_id != design_session_id:
        errors.append(
            "Architecture session_id does not match the design session."
        )
    if not isinstance(result.version, int) or result.version < 1:
        errors.append("Architecture version must be an integer >= 1.")
    if (result.supersedes_artifact_id is not None
            and not result.supersedes_artifact_id.strip()):
        errors.append(
            "Architecture supersedes_artifact_id must be None or non-empty."
        )

    ref = result.inputs_ref
    if ref.requirements_session_id != requirements_session_id:
        errors.append(
            "Architecture inputs_ref.requirements_session_id does not match "
            "the requirements session."
        )
    if ref.capacity_formula_version != capacity.formula_version:
        errors.append(
            "Architecture inputs_ref.capacity_formula_version does not match "
            "the capacity calculation."
        )
    if ref.selection_generated_at != selection.generated_at:
        errors.append(
            "Architecture inputs_ref.selection_generated_at does not match "
            "the component selection artifact."
        )
    if ref.cost_generated_at != cost.generated_at:
        errors.append(
            "Architecture inputs_ref.cost_generated_at does not match "
            "the cost estimation artifact."
        )
    if ref.cost_pricing_as_of != cost.pricing_as_of:
        errors.append(
            "Architecture inputs_ref.cost_pricing_as_of does not match "
            "the cost estimation artifact."
        )
    if ref.region_code != region_code:
        errors.append(
            f"Architecture inputs_ref.region_code {ref.region_code!r} does "
            f"not match {region_code!r}."
        )
    expected_hash = hash_upstream_inputs(
        extraction, capacity, selection, cost, region_code
    )
    if ref.upstream_inputs_hash != expected_hash:
        errors.append(
            "Architecture inputs_ref.upstream_inputs_hash does not match "
            "the canonical upstream inputs."
        )


def _validate_nodes(result, selected_by_name, errors) -> None:
    names = [node.component_name for node in result.nodes]
    for duplicate in sorted(
        name for name, count in Counter(names).items() if count > 1
    ):
        errors.append(
            f"Architecture repeats selected component {duplicate!r}; each "
            "component maps to at most one node."
        )
    node_ids = [node.node_id for node in result.nodes]
    for duplicate in sorted(
        node_id for node_id, count in Counter(node_ids).items() if count > 1
    ):
        errors.append(f"Architecture repeats node_id {duplicate!r}.")
    for node in result.nodes:
        if not node.node_id.strip():
            errors.append("Architecture node_id must be non-empty.")
        selected = selected_by_name.get(node.component_name)
        if selected is None:
            errors.append(
                f"Architecture node {node.node_id!r} references unknown "
                f"component {node.component_name!r}; nodes must match "
                "selected components exactly."
            )
            continue
        if str(node.category) != str(selected.category):
            errors.append(
                f"Architecture node {node.node_id!r} category "
                f"{node.category!r} does not match selection "
                f"{str(selected.category)!r}."
            )
        if not node.role_in_system.strip():
            errors.append(
                f"Architecture node {node.node_id!r} role_in_system must "
                "be non-empty."
            )
        if not node.capacity:
            errors.append(
                f"Architecture node {node.node_id!r} has no capacity "
                "linkage; every node needs at least one metric."
            )


def _validate_edges(result, node_by_id, errors) -> None:
    allowed = set(node_by_id) | set(EXTERNAL_ENDPOINTS)
    edge_ids = [edge.edge_id for edge in result.edges]
    for duplicate in sorted(
        edge_id for edge_id, count in Counter(edge_ids).items() if count > 1
    ):
        errors.append(f"Architecture repeats edge_id {duplicate!r}.")
    for edge in result.edges:
        if not edge.edge_id.strip():
            errors.append("Architecture edge_id must be non-empty.")
        for endpoint in (edge.from_node, edge.to_node):
            if endpoint not in allowed:
                errors.append(
                    f"Architecture edge {edge.edge_id!r} references "
                    f"dangling endpoint {endpoint!r}."
                )
        if not edge.description.strip():
            errors.append(
                f"Architecture edge {edge.edge_id!r} description must "
                "be non-empty."
            )
    connected = {
        endpoint
        for edge in result.edges
        for endpoint in (edge.from_node, edge.to_node)
    }
    for node in result.nodes:
        if node.node_id not in connected:
            errors.append(
                f"Architecture node {node.node_id!r} "
                f"({node.component_name!r}) is disconnected; every node "
                "must participate in at least one edge."
            )
    if not any(str(edge.kind) != "control" for edge in result.edges):
        errors.append(
            "Architecture must contain at least one non-control edge."
        )


def _validate_entry_points(result, node_by_id, errors) -> None:
    if not result.entry_points:
        errors.append("Architecture must declare at least one entry point.")
    for entry in result.entry_points:
        if entry not in node_by_id:
            errors.append(
                f"Architecture entry point {entry!r} is not a declared node."
            )


def _validate_data_stores(result, node_by_id, selected_by_name, errors) -> None:
    for store_id in result.data_store_node_ids:
        node = node_by_id.get(store_id)
        if node is None:
            errors.append(
                f"Architecture data store {store_id!r} is not a declared node."
            )
            continue
        selected = selected_by_name.get(node.component_name)
        category = str(
            selected.category if selected is not None else node.category
        ).lower()
        if category not in DATA_STORE_CATEGORIES:
            errors.append(
                f"Architecture data store {store_id!r} has category "
                f"{category!r}; data stores must be one of "
                f"{sorted(DATA_STORE_CATEGORIES)}. A monitoring, logging, "
                "or observability component is not a data store even when "
                "it retains logs; keep it as a node connected by a control "
                "or async edge."
            )


def _validate_evidence(result, selection, errors) -> None:
    citable = set(selection.evidence_references)
    declared = set(result.evidence_references)
    for invented in sorted(declared - citable):
        errors.append(
            f"Architecture evidence reference {invented!r} was not in the "
            "component selection evidence set."
        )
    cited: list[tuple[str, str]] = []
    for node in result.nodes:
        cited.extend((node.node_id, ref) for ref in node.evidence_refs)
    for edge in result.edges:
        cited.extend((edge.edge_id, ref) for ref in edge.evidence_refs)
    for index, decision in enumerate(result.decisions):
        cited.extend(
            (f"decision[{index}]", ref)
            for ref in decision.evidence_refs
        )
    for holder, ref in cited:
        if ref not in declared:
            errors.append(
                f"Architecture {holder!r} cites evidence {ref!r} which is "
                "not declared in evidence_references."
            )


def _validate_capacity(result, capacity, errors) -> None:
    values: list[tuple[str, object]] = []
    for node in result.nodes:
        values.extend((f"node {node.node_id!r}", item) for item in node.capacity)
    for edge in result.edges:
        if edge.load is not None:
            values.append((f"edge {edge.edge_id!r}", edge.load))
    for holder, item in values:
        key = str(item.metric)
        if key not in CAPACITY_UNIT_BY_METRIC:
            errors.append(
                f"Architecture {holder} uses unknown capacity metric "
                f"{key!r}."
            )
            continue
        expected_value = float(getattr(capacity, key))
        if not math.isclose(
            float(item.value), expected_value, rel_tol=1e-9, abs_tol=1e-12
        ):
            errors.append(
                f"Architecture {holder} metric {key!r} value "
                f"{item.value!r} does not match the capacity "
                f"calculation {expected_value!r}."
            )
        if item.unit != CAPACITY_UNIT_BY_METRIC[key]:
            errors.append(
                f"Architecture {holder} metric {key!r} unit {item.unit!r} "
                f"does not match {CAPACITY_UNIT_BY_METRIC[key]!r}."
            )


def _validate_cost(result, selection, cost, errors) -> None:
    node_names = [node.component_name for node in result.nodes]
    link_names = [link.component_name for link in result.cost_links]
    if Counter(link_names) != Counter(node_names):
        errors.append(
            "Architecture must contain exactly one cost link per node."
        )
    expected_by_name = {
        link.component_name: link
        for link in build_cost_links(selection, cost)
    }
    actual_by_name = {link.component_name: link for link in result.cost_links}
    for name, expected in expected_by_name.items():
        actual = actual_by_name.get(name)
        if actual is None or name not in node_names:
            continue
        if actual.priced != expected.priced:
            errors.append(
                f"Architecture cost link {name!r} priced flag "
                f"{actual.priced!r} does not match upstream "
                f"{expected.priced!r}."
            )
            continue
        if expected.priced:
            for field in ("pricing_model", "unit", "pricing_source"):
                if getattr(actual, field) != getattr(expected, field):
                    errors.append(
                        f"Architecture cost link {name!r} field {field} "
                        f"{getattr(actual, field)!r} does not match "
                        f"upstream {getattr(expected, field)!r}."
                    )
            for field in ("monthly_cost_usd",):
                actual_value = getattr(actual, field)
                expected_value = getattr(expected, field)
                if actual_value is None or not math.isclose(
                    float(actual_value), float(expected_value),
                    rel_tol=1e-9, abs_tol=1e-9,
                ):
                    errors.append(
                        f"Architecture cost link {name!r} field {field} "
                        f"{actual_value!r} does not match upstream "
                        f"{expected_value!r}."
                    )
            if actual.unpriced_reason is not None:
                errors.append(
                    f"Architecture cost link {name!r} is priced and must "
                    "not carry an unpriced reason."
                )
        else:
            if actual.unpriced_reason != expected.unpriced_reason:
                errors.append(
                    f"Architecture cost link {name!r} unpriced reason "
                    "does not match the upstream reason."
                )
            for field in (
                "pricing_model", "unit", "monthly_cost_usd",
                "pricing_source",
            ):
                if getattr(actual, field) is not None:
                    errors.append(
                        f"Architecture cost link {name!r} is unpriced and "
                        f"must not carry {field}."
                    )
    for key, expected_total in cost.totals_usd_per_month.items():
        actual_total = result.cost_totals_snapshot.get(key)
        if actual_total is None or not math.isclose(
            float(actual_total), float(expected_total),
            rel_tol=1e-9, abs_tol=1e-9,
        ):
            errors.append(
                f"Architecture cost totals snapshot {key!r} does not match "
                "the cost estimation result."
            )
    for key in result.cost_totals_snapshot:
        if key not in cost.totals_usd_per_month:
            errors.append(
                f"Architecture cost totals snapshot {key!r} has no upstream "
                "total."
            )


def _validate_provider_region(result, selected_by_name, cost,
                              provider_norm, region_code, errors) -> None:
    for node in result.nodes:
        selected = selected_by_name.get(node.component_name)
        if selected is None:
            continue
        comp_provider = str(selected.provider or "").lower()
        if bool(selected.provider_specific) and comp_provider != provider_norm:
            errors.append(
                f"Architecture node {node.node_id!r} provider "
                f"{selected.provider!r} does not match {provider_norm!r}."
            )
    for line in cost.lines:
        if line.region_code != region_code:
            errors.append(
                f"Cost line {line.component_name!r} region "
                f"{line.region_code!r} does not match {region_code!r}."
            )
    if f"region:{region_code.lower()}" not in (
        boundary.strip().lower() for boundary in result.boundaries
    ):
        errors.append(
            f"Architecture boundaries must include 'region:{region_code}'."
        )


def _validate_safety(result, extraction, errors) -> None:
    allowed_sla = (
        "%" in (extraction.availability + extraction.latency)
        or "sla" in (extraction.availability + extraction.latency).lower()
        # Word-form percentages ("99.9 percent") are equally traceable
        # to requirements; without this, legitimate word-form targets
        # could never support even a restated availability note.
        or "percent" in (
            extraction.availability + extraction.latency
        ).lower()
    )
    for node in result.nodes:
        for field in ("scaling_note", "availability_note"):
            note = getattr(node, field)
            if note and _SLA_PATTERN.search(note) and not allowed_sla:
                errors.append(
                    f"Architecture node {node.node_id!r} {field} makes an "
                    "SLA-style claim without upstream support; omit it or "
                    "represent it as a warning/unresolved tradeoff."
                )
    free_text: list[tuple[str, str]] = []
    for node in result.nodes:
        free_text.append((f"node {node.node_id!r} role_in_system",
                          node.role_in_system))
        free_text.append((f"node {node.node_id!r} scaling_note",
                          node.scaling_note))
        free_text.append((f"node {node.node_id!r} availability_note",
                          node.availability_note))
    for edge in result.edges:
        free_text.append((f"edge {edge.edge_id!r} description",
                          edge.description))
    for index, decision in enumerate(result.decisions):
        free_text.append((f"decision[{index}] decision", decision.decision))
        free_text.append((f"decision[{index}] rationale", decision.rationale))
        for alt_index, alternative in enumerate(decision.alternatives_considered):
            free_text.append(
                (f"decision[{index}] alternative[{alt_index}]", alternative)
            )
    for index, assumption in enumerate(result.assumptions):
        free_text.append((f"assumption[{index}]", assumption))
    for holder, text in free_text:
        if not text:
            continue
        if _PRICE_PATTERN.search(text):
            errors.append(
                f"Architecture {holder} states a price; cost facts live "
                "only in cost links."
            )
        if _QPS_PATTERN.search(text):
            errors.append(
                f"Architecture {holder} states throughput; capacity facts "
                "live only in capacity linkage."
            )
        if _STORAGE_PATTERN.search(text):
            errors.append(
                f"Architecture {holder} states a storage size; capacity "
                "facts live only in capacity linkage."
            )
        if _CONNECTIONS_PATTERN.search(text):
            errors.append(
                f"Architecture {holder} states a connection count; "
                "capacity facts live only in capacity linkage."
            )


def _validate_decision_services(result, selection, errors) -> None:
    known = {
        _normalize_component_name(comp.name)
        for comp in (
            list(selection.selected_components)
            + list(selection.rejected_candidates)
        )
        if comp.name.strip()
    }
    texts: list[tuple[str, str]] = []
    for index, decision in enumerate(result.decisions):
        texts.append((f"decision[{index}] decision", decision.decision))
        texts.append((f"decision[{index}] rationale", decision.rationale))
        for alt_index, alternative in enumerate(decision.alternatives_considered):
            texts.append(
                (f"decision[{index}] alternative[{alt_index}]", alternative)
            )
    for holder, text in texts:
        if not text or not _mentions_provider(text):
            continue
        lowered = _normalize_component_name(text)
        for mention in _unknown_service_mentions(lowered, known):
            errors.append(
                f"Architecture {holder} names infrastructure service "
                f"{mention!r} outside the selected and rejected "
                "components."
            )
