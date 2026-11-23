"""Design Critic run path: LLM boundary + normalize + validate.

Pipeline, mirroring the composer:

LLM output -> tolerant boundary schema -> normalize_critique() ->
deterministic validation -> DesignCritique.

The Critic reviews; it never mutates. Its findings reference the
closed-world component and evidence vocabularies; Python enforces
both. Ratios, thresholds and multiples ("above roughly 70% of peak",
"at roughly 3x load") are the legitimate subject of review, while
absolute capacity values (QPS counts, storage sizes, connection
counts) and prices remain forbidden in prose.
"""

import json
import re
import uuid

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field
from rich.console import Console

from ai_architect.agents.architecture.run import build_component_references
from ai_architect.agents.component_selection.models import LenientStrList
from ai_architect.agents.component_selection.run import resolve_provider
from ai_architect.agents.critic.agent import (
    create_design_critic_agent_raw,
    create_design_critic_agent_with_raw,
)
from ai_architect.agents.critic.prompt import DESIGN_CRITIC_SYSTEM_PROMPT
from ai_architect.domain.architecture.critique import (
    CRITIQUE_SCHEMA_VERSION,
    CritiqueFinding,
    DesignCritique,
    FindingCategory,
    FindingSeverity,
    SuggestedRoute,
)
from ai_architect.domain.architecture.models import ArchitectureResult
from ai_architect.domain.capacity.models import CapacityModelResult
from ai_architect.domain.cost.models import CostEstimationResult
from ai_architect.domain.requirements.models import RequirementsExtraction
from ai_architect.services.architecture.quality_gate import (
    parse_budget_ceiling,
)

console = Console()

FALLBACK_NOTE = (
    "Model response arrived as text instead of a function call; "
    "parsed deterministically against the same boundary schema."
)

# Same diagnostic bounds as the composer: observability only, never a
# behavior change.
MAX_DIAGNOSTIC_OUTPUT_CHARS = 12_000
MAX_DIAGNOSTIC_ERROR_CHARS = 2_000

# Absolute numeric claims forbidden in critique prose. Ratios and
# multiples ("70%", "3x") are the review's subject matter and are NOT
# matched here; only absolute capacity/cost values are rejected.
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


class DesignCritiqueError(ValueError):
    """Critic failure carrying structured per-attempt diagnostics.

    Subclasses ValueError so every existing ``except ValueError``
    caller keeps working unchanged. ``diagnostics`` is a plain-JSON
    list of ``{attempt, rejected_output, errors, truncated}`` dicts
    (no pydantic objects), so it checkpoint-serializes without any
    serde-allowlist change.
    """

    def __init__(self, message: str, *, diagnostics: list[dict]):
        super().__init__(message)
        self.diagnostics = diagnostics


def _diagnostic_entry(
    attempt: int,
    llm_output: "CriticLLMOutput",
    error: ValueError,
) -> dict:
    """Build one plain-JSON diagnostics entry for a rejected attempt."""
    try:
        payload = llm_output.model_dump(mode="json")
    except Exception as exc:  # noqa: BLE001 - diagnostics must not raise
        payload: object = {
            "unserializable_output": (
                f"{type(llm_output).__name__}: {exc}"[:500]
            )
        }
    text = json.dumps(payload, default=str, ensure_ascii=True)
    truncated = False
    if len(text) > MAX_DIAGNOSTIC_OUTPUT_CHARS:
        payload = {
            "truncated_preview": text[:MAX_DIAGNOSTIC_OUTPUT_CHARS],
            "truncated": True,
            "full_chars": len(text),
        }
        truncated = True
    return {
        "attempt": attempt,
        "rejected_output": payload,
        "errors": [str(error)[:MAX_DIAGNOSTIC_ERROR_CHARS]],
        "truncated": truncated,
    }


def _to_str_list(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    return list(value)


class LLMCritiqueFinding(BaseModel):
    """LLM-facing finding proposal. Subjects/evidence validated by code."""

    category: FindingCategory = Field(description="Checklist category")
    severity: FindingSeverity = Field(description="Blocking impact")
    statement: str = Field(
        default="",
        description=(
            "One-sentence problem. No dollar amounts, monthly rates, "
            "usage numbers, or absolute capacity figures."
        ),
    )
    rationale: str = Field(
        default="",
        description=(
            "Why it matters here. No dollar amounts, monthly rates, "
            "usage numbers, or absolute capacity figures."
        ),
    )
    about_component_ref: str = Field(
        default="",
        description=(
            "Exact selected/rejected component name, listed node-ref "
            "slug, or empty for cross-cutting findings."
        ),
    )
    evidence_refs: LenientStrList = Field(default_factory=list)
    recommendation: str = Field(
        default="",
        description=(
            "Concrete next step. No dollar amounts, monthly rates, "
            "usage numbers, or absolute capacity figures."
        ),
    )
    suggested_route: SuggestedRoute = Field(
        default=SuggestedRoute.COMPOSER_REVISION,
        description="Where the loop should address this finding",
    )

    model_config = ConfigDict(extra="ignore", use_enum_values=True)


class CriticLLMOutput(BaseModel):
    """LLM function-calling contract for the critic.

    Tolerant boundary schema: no absolute numbers, no prices, no
    timestamps, no IDs. Unknown extra keys are ignored.
    """

    findings: list[LLMCritiqueFinding] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore", use_enum_values=True)


def _parse_text_envelope(content: str) -> CriticLLMOutput:
    """Parse a text-envelope model response against the boundary schema.

    Used ONLY when the model answered in plain JSON text instead of
    emitting the requested function call. The text must still validate
    against the same CriticLLMOutput schema; anything else raises
    loudly.
    """
    import json as _json

    text = (content or "").strip()
    if not text:
        raise ValueError(
            "Design Critic returned no function call and empty text; "
            "nothing to parse."
        )
    try:
        return CriticLLMOutput.model_validate_json(text)
    except Exception:
        pass
    try:
        start = text.index("{")
        obj, _ = _json.JSONDecoder().raw_decode(text[start:])
        return CriticLLMOutput.model_validate(obj)
    except Exception as e:
        raise ValueError(
            "Design Critic did not emit a function call and its text "
            "output is not valid CriticLLMOutput JSON: "
            f"{e}. Preview: {text[:300]!r}"
        ) from e


def _resolve_parsed(agent, unbound_agent, messages):
    """One attempt: function call, empty-response retry, envelope fallback.

    Returns (parsed_output, fell_back_to_text). Raises loudly when the
    model output cannot be parsed against the boundary schema.
    """
    with console.status("Critiquing architecture..."):
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
            with console.status("Critiquing architecture..."):
                retry_msg = unbound_agent.invoke(messages)
            content = getattr(retry_msg, "content", "") or ""
        # Non-empty text here (original or retried) must still satisfy
        # the same boundary schema; empty/invalid text raises loudly.
        parsed = _parse_text_envelope(content)
        fell_back_to_text = True

    if isinstance(parsed, dict):
        parsed = CriticLLMOutput(**parsed)

    if not isinstance(parsed, CriticLLMOutput):
        raise ValueError(
            "Design Critic LLM output could not be parsed: "
            f"{parsing_error or type(parsed).__name__}"
        )
    return parsed, fell_back_to_text


def _format_evidence_references(evidence_ids: list[str]) -> str:
    cleaned = [e for e in (str(v or "").strip() for v in evidence_ids) if e]
    if not cleaned:
        return "- (no evidence in selection)"
    return "\n".join(f"- {evidence_id}" for evidence_id in sorted(set(cleaned)))


def normalize_critique(
    llm_output: CriticLLMOutput,
    *,
    selection,
    architecture: ArchitectureResult,
    session_id: str,
) -> DesignCritique:
    """Deterministically map LLM boundary output onto DesignCritique.

    - Finding subjects must resolve through the closed-world
      selected/rejected vocabulary (exact name or deterministic slug);
      anything else fails closed without invention or substitution.
    - Evidence references must reproduce selection evidence IDs
      exactly; anything else fails closed.
    - Absolute capacity/cost values in prose fail loudly; ratios,
      thresholds and multiples are the review's subject and pass.
    - Finding-free output is valid: silence means the architecture
      survives the checklist.
    """
    selected_names = [comp.name for comp in selection.selected_components]
    selected_set = set(selected_names)
    rejected_names = [comp.name for comp in selection.rejected_candidates]
    references = build_component_references(selected_names + rejected_names)
    slug_to_name = {slug: name for name, slug in references.items()}
    known_names = selected_set | set(rejected_names)
    citable = set(selection.evidence_references or [])

    def _resolve_subject(token: str, index: int) -> str:
        candidate = (token or "").strip()
        if not candidate:
            return ""
        if candidate in known_names:
            return candidate
        resolved = slug_to_name.get(candidate)
        if resolved is not None:
            return resolved
        raise ValueError(
            "Design Critic finding "
            f"[{index}] about_component_ref references unknown "
            f"component {token!r}: it is not in the Component Selection "
            "vocabulary."
        )

    findings: list[CritiqueFinding] = []
    for index, item in enumerate(llm_output.findings):
        statement = (item.statement or "").strip()
        rationale = (item.rationale or "").strip()
        recommendation = (item.recommendation or "").strip()
        if not statement:
            raise ValueError(
                f"Design Critic finding [{index}] has an empty statement; "
                "each finding must name its problem in one sentence."
            )
        if not rationale:
            raise ValueError(
                f"Design Critic finding [{index}] has an empty rationale; "
                "each finding must say why it matters here."
            )
        if not recommendation:
            raise ValueError(
                f"Design Critic finding [{index}] has an empty "
                "recommendation; each finding must propose a next step."
            )
        for holder, text in (
            ("statement", statement),
            ("rationale", rationale),
            ("recommendation", recommendation),
        ):
            if _PRICE_PATTERN.search(text):
                raise ValueError(
                    f"Design Critic finding [{index}] {holder} states a "
                    "price; cost facts live only in cost links."
                )
            if _QPS_PATTERN.search(text):
                raise ValueError(
                    f"Design Critic finding [{index}] {holder} states "
                    "throughput; capacity facts live only in capacity "
                    "linkage."
                )
            if _STORAGE_PATTERN.search(text):
                raise ValueError(
                    f"Design Critic finding [{index}] {holder} states a "
                    "storage size; capacity facts live only in capacity "
                    "linkage."
                )
            if _CONNECTIONS_PATTERN.search(text):
                raise ValueError(
                    f"Design Critic finding [{index}] {holder} states a "
                    "connection count; capacity facts live only in "
                    "capacity linkage."
                )
        evidence = _to_str_list(item.evidence_refs)
        for ref in evidence:
            if ref not in citable:
                raise ValueError(
                    f"Design Critic finding [{index}] cites evidence "
                    f"{ref!r} which is not in the Component Selection "
                    "evidence set."
                )
        findings.append(
            CritiqueFinding(
                category=item.category,
                severity=item.severity,
                statement=statement,
                rationale=rationale,
                about_component_ref=_resolve_subject(
                    item.about_component_ref, index
                ),
                evidence_refs=evidence,
                recommendation=recommendation,
                suggested_route=item.suggested_route,
            )
        )

    criticals = sum(
        1 for finding in findings
        if str(finding.severity) == FindingSeverity.CRITICAL.value
    )
    warnings = sum(
        1 for finding in findings
        if str(finding.severity) == FindingSeverity.WARNING.value
    )
    infos = len(findings) - criticals - warnings
    categories = sorted({str(finding.category) for finding in findings})
    summary = (
        f"{criticals} critical, {warnings} warnings, {infos} informational "
        f"across [{', '.join(categories) if categories else 'no categories'}]"
    )

    return DesignCritique(
        artifact_id=str(uuid.uuid4()),
        session_id=session_id,
        reviewed_artifact_id=architecture.artifact_id,
        reviewed_version=architecture.version,
        findings=findings,
        summary=summary,
    )


def _budget_status(
    extraction: RequirementsExtraction,
    cost: CostEstimationResult,
) -> str:
    """Deterministic budget posture for the critic prompt.

    The critic must be able to judge ceiling compliance without doing
    arithmetic or quoting figures: this states only whether the priced
    totals sit over, within, or outside any stated ceiling. Totals
    cover priced lines only (unpriced components are excluded, as the
    quality gate enforces from the same numbers). Compliance wording is
    strict: WITHIN is claimed only for a complete estimate; an
    incomplete estimate is PARTIAL (priced lines within, but full
    compliance unprovable) and a missing comparison basis is UNKNOWN.
    """
    ceiling = parse_budget_ceiling(
        (extraction.budget_constraints or "")
    )
    if ceiling is None:
        return (
            "NO_CEILING: no explicit monthly budget ceiling was stated. "
            "Flag the missing ceiling as informational where the "
            "checklist asks for it."
        )
    totals = cost.totals_usd_per_month or {}
    if not totals:
        unpriced_count = len(getattr(cost, "unpriced", None) or [])
        suffix = (
            f" ({unpriced_count} selected component(s) could not be priced)"
            if unpriced_count
            else ""
        )
        return (
            f"UNKNOWN: no priced totals exist{suffix}. Compliance with the "
            "stated ceiling cannot be assessed; do not assert compliance "
            "either way."
        )
    try:
        over = any(
            float(value) > ceiling
            for value in totals.values()
        )
    except (TypeError, ValueError):
        return (
            "UNKNOWN: priced totals could not be compared to the stated "
            "ceiling. Do not assert compliance either way."
        )
    if over:
        return (
            "OVER: the priced total exceeds the stated monthly budget "
            "ceiling. Report it with category cost_ceiling, name the "
            "priced component in about_component_ref, and never write "
            "any dollar amount."
        )
    if not getattr(cost, "fully_priced", True) or (
        getattr(cost, "unpriced", None) or []
    ):
        return (
            "PARTIAL: the priced lines stay within the stated monthly "
            "budget ceiling, but the estimate is incomplete, so full "
            "compliance cannot be asserted. Do not claim the design "
            "fits the ceiling."
        )
    return (
        "WITHIN: the priced total stays within the stated monthly "
        "budget ceiling."
    )


def _offending_excerpt(first_error: ValueError, parsed) -> str:
    """Quote the rejected sentence back for surgical repair, briefly.

    Parses "finding [i] holder ..." from the validator's message and
    returns that field's text (truncated). Returns "" when the message
    shape is unrecognized, in which case the correction stays generic.
    The excerpt is labeled do-not-repeat: it exists so the model
    rephrases exactly the offending sentence instead of regenerating
    the whole output and offending differently.
    """
    match = re.search(
        r"finding \[(\d+)\] (\w+)", str(first_error)
    )
    if match is None:
        return ""
    try:
        finding = list(parsed.findings)[int(match.group(1))]
        text = str(getattr(finding, match.group(2), "") or "")
    except (IndexError, ValueError, AttributeError):
        return ""
    text = " ".join(text.split())
    if not text:
        return ""
    return text[:200]


def run_design_critic(
    extraction: RequirementsExtraction,
    capacity: CapacityModelResult,
    selection,
    cost: CostEstimationResult,
    architecture: ArchitectureResult,
    region_code: str,
    session_id: str,
    *,
    requirements_session_id: str,
) -> DesignCritique:
    """Run the Design Critic worker.

    Pure worker: takes the composed architecture plus its already-
    validated upstream artifacts, reasons adversarially with the LLM,
    normalizes deterministically, and returns a validated
    DesignCritique. Reviews only; never mutates the architecture.
    """
    provider = resolve_provider(extraction)

    agent = create_design_critic_agent_with_raw()

    budget_status = _budget_status(extraction, cost)

    user_message = f"""## Requirements
{json.dumps(extraction.model_dump(mode="json"), indent=2)}

## Budget Assessment (deterministic - interpret, do not recalculate)
{budget_status}

## Capacity Model (deterministic - interpret, do not recalculate)
{json.dumps(capacity.model_dump(mode="json"), indent=2)}

## Component Selection (closed world - name only these components)
{json.dumps(selection.model_dump(mode="json"), indent=2)}

## Cost (deterministic - interpret, do not recalculate)
{json.dumps(cost.model_dump(mode="json"), indent=2)}

## Architecture Under Review (do not mutate, only critique)
{json.dumps(architecture.model_dump(mode="json"), indent=2)}

## Authoritative evidence IDs (closed world)
ONLY these IDs may appear in evidence_refs. Reproduce each ID
exactly. Never invent, truncate, extend, or guess an ID.
{_format_evidence_references(list(selection.evidence_references))}

## Provider
{provider}

## Region
{region_code}

Critique the architecture above against the fixed checklist."""

    messages = [
        SystemMessage(content=DESIGN_CRITIC_SYSTEM_PROMPT),
        HumanMessage(content=user_message),
    ]

    # Function calling remains the mechanism. include_raw lets us tell apart
    # a transport flake (model answered in text, no tool call) from a genuine
    # schema violation (parsing_error), without hiding either.
    unbound_agent = create_design_critic_agent_raw()
    try:
        parsed, fell_back_to_text = _resolve_parsed(
            agent, unbound_agent, messages
        )
    except ValueError as parse_error:
        # Bounded strict-format retry (exactly one): the model sometimes
        # answers in text instead of emitting the tool call, and that
        # text is occasionally malformed. Nudge it to answer with ONLY
        # the function call and parse through the same boundary schema.
        # A second failure still fails loudly with both errors.
        strict_retry = messages + [
            HumanMessage(
                content=(
                    "Your previous response could not be parsed as "
                    "structured output. Respond again with ONLY the "
                    "DesignCritic function call and no surrounding prose, "
                    "using the exact field names from the system prompt."
                )
            )
        ]
        try:
            parsed, fell_back_to_text = _resolve_parsed(
                agent, unbound_agent, strict_retry
            )
        except ValueError as retry_parse_error:
            raise ValueError(
                "Design Critic output could not be parsed, even after "
                "one strict-format retry. "
                f"First: {parse_error} Retry: {retry_parse_error}"
            ) from retry_parse_error

    try:
        result = normalize_critique(
            parsed,
            selection=selection,
            architecture=architecture,
            session_id=session_id,
        )
    except ValueError as first_error:
        # Bounded correction retry (exactly one): the model sees the
        # deterministic validator's exact complaints and resubmits
        # through the same schema, normalization, and validation. A
        # second failure still fails loudly with both errors.
        evidence_vocab = _format_evidence_references(
            list(selection.evidence_references)
        )
        excerpt = _offending_excerpt(first_error, parsed)
        correction = (
            "Your previous output was REJECTED by deterministic "
            "validation. Repair ONLY the rejected sentences: rephrase "
            "each one below to remove the absolute figure while keeping "
            "its meaning, and resubmit the FULL finding set with every "
            "other field byte-identical. Do not regenerate unrelated "
            "findings and do not repeat the quoted figures. Rules:\n"
            "- category is exactly one of: spof, cost_ceiling, "
            "capacity_headroom, observability, durability, security, "
            "scaling_cliff.\n"
            "- severity is exactly one of: critical, warning, info.\n"
            "- Every finding needs a one-sentence statement, a rationale, "
            "and a concrete recommendation.\n"
            "- about_component_ref is an exact selected/rejected "
            "component name, a listed node-ref slug, or empty. Never "
            "invent a component.\n"
            "- ONLY use evidence IDs from this supplied set; reproduce "
            "each ID exactly and never invent, truncate, extend, or "
            "guess one:\n"
            f"{evidence_vocab}\n"
            "- Remove every ABSOLUTE numeric value from all text fields: "
            "no QPS counts, no storage sizes, no connection counts, no "
            "prices. Ratios, thresholds and multiples (roughly 70% of "
            "peak, roughly 3x load) are acceptable.\n"
            "- Never restate a dollar amount or monthly rate from the "
            "inputs, including the budget ceiling figure itself: a "
            "restated figure is a price and is rejected. To report a "
            "budget breach, write that the priced total exceeds the "
            "stated monthly budget ceiling and name the priced "
            "component in about_component_ref.\n"
        )
        if excerpt:
            correction += (
                "Rephrase this sentence without figures "
                "(do not repeat it):\n"
                f"{excerpt}\n"
            )
        correction += (
            "- suggested_route is exactly one of: composer_revision, "
            "component_reselection.\n"
            "Rejection reasons:\n"
            + str(first_error)[:2000]
        )
        retry_messages = messages + [HumanMessage(content=correction)]
        retry_parsed, retry_fell_back = _resolve_parsed(
            agent, unbound_agent, retry_messages
        )
        fell_back_to_text = fell_back_to_text or retry_fell_back
        try:
            result = normalize_critique(
                retry_parsed,
                selection=selection,
                architecture=architecture,
                session_id=session_id,
            )
        except ValueError as second_error:
            diagnostics = [
                _diagnostic_entry(1, parsed, first_error),
                _diagnostic_entry(2, retry_parsed, second_error),
            ]
            raise DesignCritiqueError(
                "Design critique failed after one correction retry. "
                f"First failure: {first_error} "
                f"Retry failure: {second_error}",
                diagnostics=diagnostics,
            ) from second_error

    if fell_back_to_text:
        # The fallback is recorded on the critique only as a finding of
        # last resort is never invented: surface it in the summary.
        result = result.model_copy(
            update={
                "summary": result.summary + " " + FALLBACK_NOTE,
            }
        )

    return result
