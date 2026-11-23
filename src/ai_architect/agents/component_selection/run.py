import json
import re

from langchain_core.messages import HumanMessage, SystemMessage
from rich.console import Console

from ai_architect.agents.component_selection.agent import (
    create_component_selection_agent_raw,
    create_component_selection_agent_with_raw,
)
from ai_architect.agents.component_selection.models import (
    ComponentCategory,
    ComponentSelectionLLMOutput,
    ComponentSelectionResult,
    normalize_component_selection,
    validate_component_selection,
)
from ai_architect.agents.component_selection.prompt import (
    COMPONENT_SELECTION_SYSTEM_PROMPT,
)
from ai_architect.domain.capacity.models import CapacityModelResult
from ai_architect.domain.provisioning.models import ResourceKind
from ai_architect.domain.requirements.models import RequirementsExtraction
from ai_architect.services.knowledge.contracts import EvidenceBundle, RetrievalFilters
from ai_architect.services.knowledge.retrieval import RetrievalService
from ai_architect.services.provisioning.registry import (
    UnsupportedResourceError,
    match_contract,
    provisionable_component_names,
)


console = Console()


SUPPORTED_PROVIDERS = ("aws", "azure", "gcp")

# Deterministic bound on per-evidence content rendered into the LLM prompt.
# Proven working regime is ~14k prompt chars; live bundles reached ~40k when
# two long provider docs were included verbatim. Only the content field is
# truncated (with an explicit marker); evidence IDs, titles, scores, and
# provenance are always preserved so grounding is unaffected.
EVIDENCE_CONTENT_MAX_CHARS = 2000


def resolve_provider(requirements: RequirementsExtraction) -> str:
    """Resolve and normalize the single target cloud provider."""
    raw = (requirements.cloud_provider or "").strip().lower()
    if raw not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Component selection requires exactly one supported cloud "
            f"provider {SUPPORTED_PROVIDERS}; got {raw!r}"
        )
    return raw


def _evidence_bundle_for_prompt(bundle: EvidenceBundle) -> dict:
    """Render the evidence bundle for the prompt with bounded content length."""
    data = bundle.model_dump(mode="json")
    for item in data.get("evidence", []):
        content = item.get("content", "") or ""
        if len(content) > EVIDENCE_CONTENT_MAX_CHARS:
            item["content"] = (
                content[:EVIDENCE_CONTENT_MAX_CHARS]
                + f"...[truncated {len(content) - EVIDENCE_CONTENT_MAX_CHARS} chars]"
            )
    return data


def _parse_text_envelope(content: str) -> ComponentSelectionLLMOutput:
    """Parse a text-envelope model response against the boundary schema.

    Used ONLY when the model answered in plain JSON text instead of emitting
    the requested function call. The text must still validate against the
    same ComponentSelectionLLMOutput schema; anything else raises loudly.
    """
    import json as _json

    text = (content or "").strip()
    if not text:
        raise ValueError(
            "Component Selection model returned no function call and empty text; "
            "nothing to parse."
        )
    try:
        return ComponentSelectionLLMOutput.model_validate_json(text)
    except Exception:
        pass
    try:
        start = text.index("{")
        obj, _ = _json.JSONDecoder().raw_decode(text[start:])
        return ComponentSelectionLLMOutput.model_validate(obj)
    except Exception as e:
        raise ValueError(
            "Component Selection model did not emit a function call and its "
            f"text output is not valid ComponentSelectionLLMOutput JSON: {e}. "
            f"Preview: {text[:300]!r}"
        ) from e


class ComponentSelectionError(ValueError):
    """Selection failure carrying structured per-attempt diagnostics.

    Subclasses ValueError so every existing ``except ValueError``
    caller keeps working unchanged. ``diagnostics`` is a plain-JSON
    list of ``{attempt, kind, rejected_output, errors, truncated}``
    dicts (no pydantic objects), carried on the exception only — never
    persisted to checkpoint state.
    """

    def __init__(self, message: str, *, diagnostics: list[dict]):
        super().__init__(message)
        self.diagnostics = diagnostics


# Bounds for selection failure diagnostics (mirrors the composer caps).
# Rejected LLM outputs are small in practice (a few KB); these caps only
# engage on pathological verbosity so a single failed run cannot bloat
# logs or error payloads.
MAX_DIAGNOSTIC_OUTPUT_CHARS = 12_000
MAX_DIAGNOSTIC_ERROR_CHARS = 2_000

# Hard bound on LLM attempts per selection run: the initial attempt plus
# exactly one recovery attempt (strict-format or validator-feedback,
# whichever the first failure calls for). A second failure of any kind
# fails loudly; there is no third attempt and no loop.
MAX_SELECTION_ATTEMPTS = 2

STRICT_FORMAT_RETRY_PROMPT = (
    "Your previous response could not be parsed as structured output. "
    "Respond again with ONLY the ComponentSelectionLLMOutput function call and no "
    "surrounding prose, tables, Markdown, or explanations, using the exact "
    "field names from the system prompt."
)

# Exact parenthetical markers the model uses to describe a capability that is
# embedded in another service rather than provisioned as its own resource.
# Matching is deliberately narrow: descriptors such as "(On-Demand)" or
# "(Hosting)" name a provisionable variant and must remain valid.
_NON_PROVISIONABLE_MARKERS = (
    "embedded",
    "built-in",
    "builtin",
    "implicit",
    "automatic",
)


class _Unparseable(ValueError):
    """Parse failure carrying a capped preview of the rejected text.

    Internal only: lets the recovery loop record diagnostics for the
    attempt without changing the public ValueError contract.
    """

    def __init__(self, message: str, *, rejected_preview: str = ""):
        super().__init__(message)
        self.rejected_preview = rejected_preview


def _diagnostic_entry(
    attempt: int,
    kind: str,
    rejected: object,
    error: ValueError,
) -> dict:
    """Build one plain-JSON diagnostics entry for a rejected attempt."""
    preview = (
        getattr(rejected, "rejected_preview", None)
        if isinstance(rejected, _Unparseable)
        else None
    )
    if preview is None:
        try:
            if hasattr(rejected, "model_dump"):
                preview = json.dumps(
                    rejected.model_dump(mode="json"),
                    default=str,
                    ensure_ascii=True,
                )
            elif rejected is None:
                preview = ""
            else:
                preview = str(rejected)
        except Exception as exc:  # noqa: BLE001 - diagnostics must not raise
            preview = f"{type(rejected).__name__}: {exc}"[:500]
    truncated = False
    if len(preview) > MAX_DIAGNOSTIC_OUTPUT_CHARS:
        preview = preview[:MAX_DIAGNOSTIC_OUTPUT_CHARS]
        truncated = True
    return {
        "attempt": attempt,
        "kind": kind,
        "rejected_output": preview,
        "errors": [str(error)[:MAX_DIAGNOSTIC_ERROR_CHARS]],
        "truncated": truncated,
    }


def _resolve_parsed(agent, unbound_agent, messages):
    """One attempt: function call, empty-response retry, envelope fallback.

    Returns (parsed_output, fell_back_to_text). Raises _Unparseable
    (a ValueError) when the model output cannot be parsed against the
    boundary schema, carrying a capped preview for diagnostics.
    """
    with console.status("Selecting components..."):
        outcome = agent.invoke(messages)
    raw = outcome.get("raw") if isinstance(outcome, dict) else None
    parsed = outcome.get("parsed") if isinstance(outcome, dict) else outcome
    parsing_error = outcome.get("parsing_error") if isinstance(outcome, dict) else None

    fell_back_to_text = False
    if parsed is None and parsing_error is None:
        content = getattr(raw, "content", "") or ""
        if not content.strip():
            # Objectively empty response with tools bound: the model
            # aborted tool calling without producing anything. Retry the
            # identical prompt WITHOUT tools and parse the text through
            # the same boundary schema. Generic: keyed on the empty
            # response only, never on model identity.
            with console.status("Selecting components..."):
                retry_msg = unbound_agent.invoke(messages)
            content = getattr(retry_msg, "content", "") or ""
        # Non-empty text here (original or retried) must still satisfy
        # the same boundary schema; empty/invalid text raises loudly.
        try:
            parsed = _parse_text_envelope(content)
        except ValueError as envelope_error:
            raise _Unparseable(
                str(envelope_error),
                rejected_preview=content[:MAX_DIAGNOSTIC_OUTPUT_CHARS],
            ) from envelope_error
        fell_back_to_text = True

    if isinstance(parsed, dict):
        try:
            parsed = ComponentSelectionLLMOutput(**parsed)
        except ValueError as dict_error:
            raise _Unparseable(
                "Component Selection LLM output could not be parsed: "
                f"{dict_error}",
                rejected_preview=json.dumps(parsed, default=str)[
                    :MAX_DIAGNOSTIC_OUTPUT_CHARS
                ],
            ) from dict_error

    if not isinstance(parsed, ComponentSelectionLLMOutput):
        raise _Unparseable(
            "Component Selection LLM output could not be parsed: "
            f"{parsing_error or type(parsed).__name__}",
            rejected_preview="",
        )
    return parsed, fell_back_to_text


def _closed_world_names(provider: str) -> list[str]:
    """Canonical names of the components the platform can provision.

    Only the AWS provisioning path has a closed world: the platform
    provisions exactly the capabilities in the resource registry, and a
    component outside that registry has no module and fails closed later
    at spec-build time. Naming the list here keeps the model inside the
    capability the platform actually has (a CloudWatch dashboard, for
    example, is a separate AWS resource with no module here, so the
    model must map that requirement onto the log group and alarm bundle
    instead of inventing a component).
    """
    if provider != "aws":
        return []
    return provisionable_component_names()


def _closed_world_section(provider: str) -> str:
    """Render the provisionable closed world for the selection prompt."""
    names = _closed_world_names(provider)
    if not names:
        return ""
    return (
        "## Provisionable components (closed world)\n"
        "The platform provisions EXACTLY these components and nothing "
        "else. Select from this list only:\n"
        + "\n".join(f"- {name}" for name in names)
        + "\nIf a requirement needs a capability that is not on this list, "
        "use the closest component that IS on the list and state the "
        "unmet part as an explicit entry in assumptions. Never name a "
        "component that is not on this list.\n"
    )


def _correction_prompt(
    errors: list[str],
    evidence_ids: list[str],
    provider: str,
) -> str:
    """Validator-feedback correction nudge for the single recovery attempt."""
    taxonomy = ", ".join(sorted(c.value for c in ComponentCategory))
    # One evidence ID per line. The allowed IDs used to be rendered on a
    # single comma-joined line, and models copied that whole line into
    # evidence_references as one entry; the packed entry then declared no
    # real ID, so every component citing an ID failed the "declared"
    # check even though the first attempt had cited them correctly.
    allowed_evidence = (
        "\n".join(f"- {evidence_id}" for evidence_id in evidence_ids[:50])
        or "(no evidence in bundle)"
    )
    closed_world = _closed_world_names(provider)
    closed_world_block = ""
    if closed_world:
        closed_world_block = (
            "- Select only components from the provisionable closed world "
            "listed below; nothing outside it can be provisioned. If a "
            "requirement needs a capability outside the list, use the "
            "closest component in the list and state the gap in "
            "assumptions.\n"
            "Provisionable components (closed world):\n"
            + "\n".join(f"- {name}" for name in closed_world)
            + "\n"
        )
    return (
        "Your previous Component Selection output was REJECTED by "
        "deterministic validation. Produce a corrected output that fixes "
        "EVERY item below without changing anything else. Rules:\n"
        "- category MUST be exactly one of the allowed taxonomy values: "
        f"{taxonomy}. Do not invent, combine, or approximate categories.\n"
        "- rejected_candidates entries MUST be compact "
        '{"name", "reason"} objects, not full component objects.\n'
        "- warnings, assumptions, and evidence_references MUST be plain "
        "strings, never objects or lists of objects.\n"
        "- Every provider-specific component MUST cite evidence IDs from "
        "the Evidence Bundle given in the original context and declare "
        "every cited ID in evidence_references. Allowed evidence IDs are "
        "listed one per line below: copy each ID exactly, put ONE ID in "
        "ONE list entry, and never join two IDs with a comma in a single "
        "entry.\n"
        f"{allowed_evidence}\n"
        "- Keep the evidence IDs you already cited. Do not re-type them, "
        "do not merge them into one string, and do not add an ID that is "
        "not listed above.\n"
        f"- Selected components must match provider {provider!r}; do not "
        "mix providers and do not invent components, evidence IDs, "
        "prices, or service capabilities.\n"
        "- Selected components MUST be independently provisionable "
        "services/resources. Do not select automatically emitted "
        "telemetry, embedded features, or built-in platform capabilities "
        "as separate components.\n"
        "- Do not add optional alerting or scoped alarm variants. Use only "
        "a platform-supported alarm bundle when alerting is explicitly "
        "required; otherwise omit alerting.\n"
        f"{closed_world_block}"
        "- Respond with ONLY the ComponentSelectionLLMOutput function call and no "
        "surrounding prose, tables, Markdown, or explanations, using the "
        "exact field names from the system prompt.\n"
        "Rejection reasons:\n" + "; ".join(errors)[:2000]
    )


def _non_provisionable_marker(name: str) -> str | None:
    """Return the exact embedded-capability marker in a component name."""
    normalized = name.casefold()
    for marker in _NON_PROVISIONABLE_MARKERS:
        if f"({marker})" in normalized or f"[{marker}]" in normalized:
            return marker
    return None


def _non_provisionable_component_errors(
    result: ComponentSelectionResult,
) -> list[str]:
    """Reject capabilities the architecture cannot provision separately.

    This guard is intentionally narrow: it only recognizes explicit
    parenthetical markers the model uses for embedded or automatic
    capabilities. It does not infer from prose, provider service names, or
    ordinary variant qualifiers such as "(On-Demand)" or "(Hosting)".
    """
    errors: list[str] = []
    for comp in result.selected_components:
        marker = _non_provisionable_marker(comp.name)
        if marker is None:
            continue
        errors.append(
            f"Component '{comp.name}' is not an independently provisionable "
            f"component (marker '{marker}'); do not select embedded, "
            "built-in, implicit, or automatic capabilities as separate "
            "components."
        )
    return errors


def _unsupported_alarm_component_errors(
    result: ComponentSelectionResult,
) -> list[str]:
    """Reject CloudWatch alarm variants the verifier cannot provision.

    The verified alarm capability is the standard bundle of four API/Lambda
    alarms. Single-metric, error-rate, throttling, composite, anomaly and
    metric-math variants are explicitly outside that capability. Selecting
    one leaves the approved design without a verified module, so reject it
    here and let the bounded correction retry choose the supported bundle or
    omit unrequested alerting.
    """
    alarm_components = [
        comp
        for comp in result.selected_components
        if "cloudwatch" in (comp.name or "").casefold()
        and "alarm" in (comp.name or "").casefold()
    ]
    if not alarm_components:
        return []

    kinds: list[ResourceKind] = []
    unmatched: list[str] = []
    for comp in result.selected_components:
        try:
            kinds.append(match_contract(comp.name).kind)
        except UnsupportedResourceError:
            unmatched.append(comp.name)

    api_count = kinds.count(ResourceKind.APIGATEWAY_HTTP)
    lambda_count = kinds.count(ResourceKind.LAMBDA)
    errors: list[str] = []
    for comp in alarm_components:
        try:
            match_contract(comp.name)
        except UnsupportedResourceError:
            errors.append(
                f"Component '{comp.name}' is not a supported CloudWatch alarm "
                "capability. The platform provisions only the standard "
                "CloudWatch alarms bundle for one HTTP API and one Lambda; "
                "single-metric, error-rate, throttling, composite, anomaly "
                "and metric-math variants are not provisionable. Omit "
                "unrequested alerting or select the supported CloudWatch "
                "alarms component."
            )
            continue
        if api_count != 1 or lambda_count != 1:
            missing = []
            if api_count != 1:
                missing.append(
                    "one HTTP API named exactly 'Amazon API Gateway "
                    "(HTTP API)'"
                )
            if lambda_count != 1:
                missing.append("one Lambda named exactly 'AWS Lambda'")
            detail = ""
            if unmatched:
                detail = (
                    " These selected components are not provisionable "
                    "components and were not counted: "
                    f"{', '.join(sorted(unmatched))}."
                )
            errors.append(
                f"Component '{comp.name}' selects the standard CloudWatch "
                "alarms bundle, but that bundle requires exactly one HTTP API "
                "and one Lambda in the same design (found "
                f"{api_count} API and {lambda_count} Lambda). Add "
                f"{' and '.join(missing)} to the design, or omit "
                "unrequested alerting." + detail
            )
    return errors


def _required_log_group_errors(
    result: ComponentSelectionResult,
) -> list[str]:
    """Reject Lambda/HTTP API designs without exactly one shared log group.

    The verified Lambda and HTTP API modules bind their logging
    configuration to exactly one CloudWatch log group in the same spec;
    without that binding the whole plan is unrepresentable. This is
    required infrastructure, not optional observability, so the bounded
    correction retry adds the one log group (or collapses duplicates).
    """
    kinds: list[ResourceKind] = []
    for comp in result.selected_components:
        try:
            kinds.append(match_contract(comp.name).kind)
        except UnsupportedResourceError:
            continue
    log_count = kinds.count(ResourceKind.CLOUDWATCH_LOG_GROUP)
    needs_log_group = (
        ResourceKind.LAMBDA in kinds
        or ResourceKind.APIGATEWAY_HTTP in kinds
    )
    if not needs_log_group or log_count == 1:
        return []
    if log_count == 0:
        return [
            "The design selects AWS Lambda or the HTTP API but has no "
            "'Amazon CloudWatch Logs' component. The verified Lambda and "
            "HTTP API modules bind their logging configuration to exactly "
            "one shared log group in the same design. Add exactly one "
            "'Amazon CloudWatch Logs' component."
        ]
    return [
        f"The design selects {log_count} 'Amazon CloudWatch Logs' "
        "components. The verified Lambda and HTTP API modules bind to "
        "exactly one shared log group in the same design. Select exactly "
        "one 'Amazon CloudWatch Logs' component."
    ]


def _unsupported_messaging_topology_errors(
    result: ComponentSelectionResult,
) -> list[str]:
    """Reject SNS without exactly one SQS subscriber.

    The verified SNS capability is a topic plus exactly one SQS queue: the
    module creates the subscription and the queue policy scoped to the
    topic. Selecting SNS without a queue (or with several) cannot bind, so
    reject it here and let the bounded correction retry add the subscriber
    or omit SNS.
    """
    sns_components = []
    sqs_count = 0
    for comp in result.selected_components:
        try:
            kind = match_contract(comp.name).kind
        except UnsupportedResourceError:
            continue
        if kind is ResourceKind.SNS_TOPIC:
            sns_components.append(comp)
        elif kind is ResourceKind.SQS_QUEUE:
            sqs_count += 1
    if not sns_components:
        return []

    errors: list[str] = []
    for comp in sns_components:
        if sqs_count == 0:
            errors.append(
                f"Component '{comp.name}' is provisioned as a topic plus "
                "exactly one Amazon SQS subscriber, but this design has no "
                "Amazon SQS queue. Add exactly one Amazon SQS queue to the "
                "design, or omit SNS if no queue consumer is needed."
            )
        elif sqs_count > 1:
            errors.append(
                f"Component '{comp.name}' is provisioned as a topic plus "
                "exactly one Amazon SQS subscriber, but this design has "
                f"{sqs_count} Amazon SQS queues. Select exactly one Amazon "
                "SQS queue as the subscriber."
            )
    return errors


# Separators a model may use to pack several evidence IDs into one
# list entry. Only these are treated as separators; a plain space is
# never a separator because evidence IDs may contain one.
_EVIDENCE_ID_SEPARATORS = re.compile(r"[,;]")


def _split_joined_evidence_ids(
    result: ComponentSelectionResult,
    bundle: EvidenceBundle,
) -> list[str]:
    """Split evidence-ID entries that pack several IDs into one entry.

    Models sometimes emit the whole allowed-ID list as a single string
    ("id-a, id-b") instead of one entry per ID. Such an entry declares
    no real ID: every component citing "id-a" is then "not declared",
    and the packed string itself is not in the bundle, so a run whose
    first attempt was already fully grounded fails closed on a pure
    formatting slip.

    The repair is strictly bounded: an entry is split only when EVERY
    separated part is an exact evidence ID present in the bundle, so it
    can never introduce, drop, reorder, or invent an ID. An entry with
    any unknown part is left exactly as it was, and the normal validators
    still fail it closed. Returns the packed entries that were split.
    """
    bundle_ids = {
        str(getattr(item, "evidence_id", "") or "").strip()
        for item in (getattr(bundle, "evidence", None) or [])
    }
    if not bundle_ids:
        return []

    def _split(entry: str) -> list[str] | None:
        text = str(entry or "").strip()
        if not text:
            return None
        parts = [part.strip() for part in _EVIDENCE_ID_SEPARATORS.split(text)]
        if len(parts) < 2 or not all(part in bundle_ids for part in parts):
            return None
        return parts

    split_entries: list[str] = []

    declared: list[str] = []
    for entry in result.evidence_references:
        parts = _split(entry)
        if parts is None:
            declared.append(entry)
            continue
        split_entries.append(entry)
        declared.extend(parts)
    result.evidence_references = declared

    for comp in result.selected_components:
        refs: list[str] = []
        for entry in comp.evidence_refs:
            parts = _split(entry)
            if parts is None:
                refs.append(entry)
                continue
            split_entries.append(entry)
            refs.extend(parts)
        comp.evidence_refs = refs

    return split_entries


def _undeclared_evidence_errors(
    result: ComponentSelectionResult,
    bundle: EvidenceBundle,
) -> list[str]:
    """Declared top-level evidence references must exist in the bundle.

    The frozen validator checks per-component refs against the bundle and
    against the declared list, but not the declared list itself against
    the bundle; hallucinated top-level IDs would otherwise propagate into
    architecture traceability unchecked. Blank entries are formatting
    noise, not claims, and are ignored.
    """
    bundle_ids = {
        str(getattr(item, "evidence_id", "") or "").strip()
        for item in (getattr(bundle, "evidence", None) or [])
    }
    errors: list[str] = []
    seen: set[str] = set()
    for raw_id in result.evidence_references:
        ev_id = str(raw_id or "").strip()
        if not ev_id or ev_id in seen:
            continue
        seen.add(ev_id)
        if ev_id not in bundle_ids:
            errors.append(
                f"evidence_references declares '{ev_id}' which is not in "
                "the evidence bundle"
            )
    return errors


def build_component_query(requirements: RequirementsExtraction) -> str:
    """Build a retrieval query from structured requirements."""
    parts: list[str] = []
    if requirements.application_description:
        parts.append(f"Application: {requirements.application_description}")
    if requirements.target_users_dau:
        parts.append(f"Scale: {requirements.target_users_dau} DAU")
    if requirements.availability:
        parts.append(f"Availability: {requirements.availability}")
    if requirements.latency:
        parts.append(f"Latency: {requirements.latency}")
    if requirements.traffic_characteristics:
        parts.append(f"Traffic: {requirements.traffic_characteristics}")
    if requirements.read_write_characteristics:
        parts.append(f"Read/Write: {requirements.read_write_characteristics}")
    if requirements.storage_characteristics:
        parts.append(f"Storage: {requirements.storage_characteristics}")
    query = " ".join(parts) if parts else "cloud architecture components"
    return f"{query}. Component selection and architecture patterns"


async def retrieve_evidence_for_selection(
    requirements: RequirementsExtraction,
    provider: str,
    top_k: int = 10,
) -> EvidenceBundle:
    """Retrieve evidence for component selection with a hard provider filter."""
    with console.status("Retrieving architecture evidence..."):
        service = RetrievalService()
        filters = RetrievalFilters(provider=provider)
        return await service.retrieve(
            query=build_component_query(requirements),
            top_k=top_k,
            filters=filters,
        )


def run_component_selection(
    requirements: RequirementsExtraction,
    capacity: CapacityModelResult,
    bundle: EvidenceBundle,
    provider: str,
) -> ComponentSelectionResult:
    """
    Run the Component Selection worker.

    Pure worker: takes already-retrieved evidence, reasons with the LLM,
    validates deterministically, and returns a typed result.
    """
    provider = provider.strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Component selection requires exactly one supported cloud "
            f"provider {SUPPORTED_PROVIDERS}; got {provider!r}"
        )

    agent = create_component_selection_agent_with_raw()
    unbound_agent = create_component_selection_agent_raw()

    request_payload = {
        "requirements": requirements.model_dump(mode="json"),
        "capacity": capacity.model_dump(mode="json"),
        "evidence_bundle": _evidence_bundle_for_prompt(bundle),
        "provider": provider,
    }

    user_message = f"""## Requirements
{json.dumps(request_payload["requirements"], indent=2)}

## Capacity Model (deterministic - interpret, do not recalculate)
{json.dumps(request_payload["capacity"], indent=2)}

## Evidence Bundle (cite evidence IDs for provider-specific claims)
{json.dumps(request_payload["evidence_bundle"], indent=2)}

## Provider
{request_payload["provider"]}

{_closed_world_section(provider)}Select appropriate infrastructure components for this architecture."""

    messages = [
        SystemMessage(content=COMPONENT_SELECTION_SYSTEM_PROMPT),
        HumanMessage(content=user_message),
    ]

    # Function calling remains the mechanism. include_raw lets us tell apart
    # a transport flake (model answered in text, no tool call) from a genuine
    # schema violation (parsing_error), without hiding either.
    #
    # Bounded recovery (at most MAX_SELECTION_ATTEMPTS LLM attempts total):
    # a parse failure triggers exactly one strict-format retry; a
    # validation failure triggers exactly one validator-feedback
    # correction retry. A second failure of any kind fails loudly with
    # both attempts identified in the error and in diagnostics.
    evidence_ids = [
        str(getattr(item, "evidence_id", "") or "")
        for item in (getattr(bundle, "evidence", None) or [])
        if str(getattr(item, "evidence_id", "") or "")
    ]
    current_messages = messages
    first_error: ValueError | None = None
    first_diagnostic: dict | None = None
    first_fell_back = False
    attempt = 1
    while True:
        try:
            parsed, fell_back_to_text = _resolve_parsed(
                agent, unbound_agent, current_messages
            )
        except ValueError as parse_error:
            if attempt >= MAX_SELECTION_ATTEMPTS and first_error is not None:
                raise ComponentSelectionError(
                    "Component Selection output could not be parsed, "
                    "even after one strict-format retry. "
                    f"First: {first_error} Retry: {parse_error}",
                    diagnostics=[
                        first_diagnostic,
                        _diagnostic_entry(2, "parse", parse_error, parse_error),
                    ],
                ) from parse_error
            first_error = parse_error
            first_diagnostic = _diagnostic_entry(
                1, "parse", parse_error, parse_error
            )
            current_messages = messages + [
                HumanMessage(content=STRICT_FORMAT_RETRY_PROMPT)
            ]
            attempt = MAX_SELECTION_ATTEMPTS
            continue

        result = normalize_component_selection(parsed)
        # Canonicalize evidence-ID lists before validation: an entry that
        # packs several real bundle IDs into one string is a formatting
        # slip, not an ungrounded claim, and it is split deterministically
        # (see _split_joined_evidence_ids). Unknown IDs are never split
        # and never invented, so the validators still fail closed.
        _split_joined_evidence_ids(result, bundle)

        errors = validate_component_selection(
            result=result,
            evidence_bundle=bundle.model_dump(mode="json"),
            provider=provider,
        )
        errors.extend(_non_provisionable_component_errors(result))
        errors.extend(_unsupported_alarm_component_errors(result))
        errors.extend(_required_log_group_errors(result))
        errors.extend(_unsupported_messaging_topology_errors(result))
        errors.extend(_undeclared_evidence_errors(result, bundle))
        if not errors:
            fell_back_to_text = first_fell_back or fell_back_to_text
            break
        validation_error = ValueError(
            "Component selection validation failed: " + "; ".join(errors)
        )
        if attempt >= MAX_SELECTION_ATTEMPTS and first_error is not None:
            raise ComponentSelectionError(
                "Component selection failed after one correction retry. "
                f"First failure: {first_error} "
                f"Retry failure: {validation_error}",
                diagnostics=[
                    first_diagnostic,
                    _diagnostic_entry(2, "validation", parsed, validation_error),
                ],
            ) from validation_error
        first_error = validation_error
        first_diagnostic = _diagnostic_entry(
            1, "validation", parsed, validation_error
        )
        first_fell_back = fell_back_to_text
        current_messages = messages + [
            HumanMessage(
                content=_correction_prompt(errors, evidence_ids, provider)
            )
        ]
        attempt = MAX_SELECTION_ATTEMPTS
        continue

    if fell_back_to_text:
        note = (
            "Model response arrived as text instead of a function call; "
            "parsed deterministically against the same boundary schema."
        )
        if note not in result.warnings:
            result.warnings.append(note)

    for comp in result.selected_components:
        support = comp.evidence_support
        support_value = support.value if hasattr(support, "value") else support
        if comp.provider_specific and not comp.evidence_refs:
            note = (
                f"Component '{comp.name}' marked evidence_support=insufficient: "
                "no supporting evidence in bundle"
            )
            if note not in result.warnings:
                result.warnings.append(note)

    return result


def run_component_selection_sync(
    requirements: RequirementsExtraction,
    capacity: CapacityModelResult,
    bundle: EvidenceBundle,
    provider: str,
) -> ComponentSelectionResult:
    """Synchronous alias (worker is already sync)."""
    return run_component_selection(
        requirements=requirements,
        capacity=capacity,
        bundle=bundle,
        provider=provider,
    )
