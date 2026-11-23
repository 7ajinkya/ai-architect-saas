"""Architecture Composer run path (Slice B: LLM boundary + normalize).

Pipeline, mirroring capacity/component selection:

LLM output -> tolerant boundary schema -> normalize_architecture() ->
deterministic Slice A validation -> ArchitectureResult.

The LLM proposes component names, roles, capacity KEYS, edges, and
decisions. Python resolves every value: node/edge IDs, categories,
capacity values/units, cost links, evidence unions, artifact identity.
"""

import json
from typing import Annotated

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
)
from rich.console import Console

from ai_architect.agents.architecture.agent import (
    create_architecture_composer_agent_raw,
    create_architecture_composer_agent_with_raw,
)
from ai_architect.agents.architecture.prompt import (
    ARCHITECTURE_COMPOSER_SYSTEM_PROMPT,
)
from ai_architect.agents.component_selection.models import (
    LenientStrList,
    LLMUnresolvedTradeoff,
    UnresolvedTradeoff,
)
from ai_architect.agents.component_selection.run import resolve_provider
from ai_architect.domain.architecture.models import (
    EXTERNAL_ENDPOINTS,
    ArchitectureDecision,
    ArchitectureEdge,
    ArchitectureNode,
    ArchitectureResult,
    CapacityMetricKey,
    EdgeKind,
)
from ai_architect.domain.capacity.models import CapacityModelResult
from ai_architect.domain.cost.models import CostEstimationResult
from ai_architect.domain.requirements.models import RequirementsExtraction
from ai_architect.services.architecture.core import (
    build_cost_links,
    build_inputs_ref,
    new_artifact_id,
    resolve_capacity_value,
)
from ai_architect.services.architecture.validator import (
    _normalize_component_name,
    validate_architecture,
)

console = Console()

FALLBACK_NOTE = (
    "Model response arrived as text instead of a function call; "
    "parsed deterministically against the same boundary schema."
)

# Bounds for composer failure diagnostics (Slice A: observability only).
# The rejected LLM outputs are small in practice (a few KB); these caps
# only engage on pathological verbosity so a single failed run cannot
# bloat the LangGraph checkpoint.
MAX_DIAGNOSTIC_OUTPUT_CHARS = 12_000
MAX_DIAGNOSTIC_ERROR_CHARS = 2_000


class ArchitectureCompositionError(ValueError):
    """Composer failure carrying structured per-attempt diagnostics.

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
    llm_output: "ArchitectureLLMOutput",
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


def _coerce_key_list(value: object) -> object:
    """Accept a single capacity key string or a list of keys."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return value


# Conservative metric-key aliases: each maps to exactly one of the six
# canonical capacity metrics, so the mapping invents no values. Bare
# "qps" (read vs write ambiguous) and any unknown key stay loud
# rejections, mirroring the edge-kind alias philosophy.
_METRIC_KEY_ALIASES = {
    "write_qps": "peak_write_qps",
    "read_qps": "peak_read_qps",
    "storage_growth": "storage_growth_bytes_per_day",
    "hot_working_set": "hot_working_set_bytes",
    "egress": "egress_bytes_per_second",
    "connections": "concurrent_connections",
}

_METRIC_KEY_VALUES = {key.value for key in CapacityMetricKey}


def _coerce_metric_key(value: object) -> object:
    """Normalize unambiguous metric-key spellings into the canonical six.

    Anything else is returned untouched so pydantic still rejects
    genuinely invalid or ambiguous values loudly with the valid
    choices listed.
    """
    if isinstance(value, CapacityMetricKey):
        return value
    if not isinstance(value, str):
        return value
    text = _normalize_kind_text(value)
    if text in _METRIC_KEY_VALUES:
        return text
    if text in _METRIC_KEY_ALIASES:
        return _METRIC_KEY_ALIASES[text]
    return value


LenientMetricKey = Annotated[CapacityMetricKey, BeforeValidator(_coerce_metric_key)]


LenientKeyList = Annotated[
    list[LenientMetricKey] | str, BeforeValidator(_coerce_key_list)
]


# Conservative kind aliases: unambiguous spellings/plurals of the six
# taxonomy values only. Compound or bidirectional forms ("read/write")
# are deliberately excluded: they cannot map to one value without
# inventing direction semantics, so they stay loud rejections.
_EDGE_KIND_ALIASES = {
    "requests": "request",
    "reads": "read",
    "writes": "write",
    "event": "async",
    "events": "async",
    "message": "async",
    "messages": "async",
    "queue": "async",
    "queues": "async",
    "stream": "async",
    "streams": "async",
    "streaming": "async",
    "monitoring": "control",
}

_EDGE_KIND_VALUES = {kind.value for kind in EdgeKind}


def _normalize_kind_text(text: str) -> str:
    out: list[str] = []
    prev_underscore = True
    for ch in text.strip().lower():
        if ch.isalnum():
            out.append(ch)
            prev_underscore = False
        elif not prev_underscore:
            out.append("_")
            prev_underscore = True
    return "".join(out).strip("_")


def _coerce_kind(value: object) -> object:
    """Normalize unambiguous kind spellings into the taxonomy.

    Anything else is returned untouched so pydantic still rejects
    genuinely invalid or ambiguous values (e.g. "read/write") loudly
    with the valid choices listed.
    """
    if isinstance(value, EdgeKind):
        return value
    if not isinstance(value, str):
        return value
    text = _normalize_kind_text(value)
    if text in _EDGE_KIND_VALUES:
        return text
    if text in _EDGE_KIND_ALIASES:
        return _EDGE_KIND_ALIASES[text]
    return value


LenientKind = Annotated[EdgeKind, BeforeValidator(_coerce_kind)]


def _coerce_boundaries(value: object) -> object:
    """Accept a boundaries list, a single label, or a label mapping.

    The observed model shape is a {"region": ..., ...} object; mapping
    it to sorted "key:value" labels is deterministic and invents no
    infrastructure facts (values stay the model's own, labeled).
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        return [
            f"{key}:{value[key]}"
            for key in sorted(value, key=lambda k: str(k))
        ]
    return value


LenientBoundaries = Annotated[
    list[str] | str | dict, BeforeValidator(_coerce_boundaries)
]


def _warn_to_str(item: object) -> object:
    """Extract warning text from the observed {id, message} shape.

    Unrecognized shapes are returned untouched so pydantic still
    rejects them loudly instead of being silently dropped.
    """
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("message", "description", "text", "warning"):
            text = item.get(key)
            if isinstance(text, str) and text.strip():
                return text
    return item


def _coerce_warn_list(value: object) -> object:
    if value is None:
        return []
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return [_warn_to_str(item) for item in value]
    return value


LenientWarnList = Annotated[
    list[str] | str, BeforeValidator(_coerce_warn_list)
]


def _to_str_list(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    return list(value)


def _slugify(text: str) -> str:
    out: list[str] = []
    prev_dash = True
    for ch in text.strip().lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-") or "node"


def _normalize_tradeoff_text(text: str) -> str:
    """Canonical form for tradeoff dedup: lowercase, whitespace folded."""
    return " ".join((text or "").strip().lower().split())


def build_component_references(component_names) -> dict[str, str]:
    """Deterministic component_name -> node_id reference map.

    Single source of truth for the closed-world component references.
    Rendered into the Composer prompt as the allowed vocabulary and
    reused by normalize_architecture() to assign node_ids, so the two
    can never disagree. Derived only from the upstream Component
    Selection result (selection order), never from model output.
    Collisions are disambiguated deterministically with a numeric
    suffix, exactly as before.
    """
    references: dict[str, str] = {}
    used: set[str] = set()
    for name in component_names:
        if not name or not name.strip():
            continue
        base = _slugify(name)
        reference = base
        suffix = 2
        while reference in used:
            reference = f"{base}-{suffix}"
            suffix += 1
        used.add(reference)
        references[name] = reference
    return references


def _component_categories(selection) -> dict[str, str]:
    """Deterministic component-name -> category map from the selection.

    Categories are copied from the already-validated upstream artifact;
    nothing is inferred from names. Selected entries win over rejected
    entries if the same name appears in both lists.
    """
    categories: dict[str, str] = {}
    for comp in list(selection.selected_components) + list(
        selection.rejected_candidates
    ):
        name = (comp.name or "").strip()
        if name:
            categories.setdefault(name, str(comp.category))
    return categories


def _format_component_references(
    references: dict[str, str],
    categories: dict[str, str] | None = None,
) -> str:
    """Render the allowed reference vocabulary for prompts/retry text."""
    if not references:
        return "- (no components were selected)"
    lines: list[str] = []
    for name, reference in sorted(
        references.items(), key=lambda item: item[1]
    ):
        category = (categories or {}).get(name)
        suffix = f" [{category}]" if category else ""
        lines.append(f"- {reference} -> {name}{suffix}")
    return "\n".join(lines)


def _composer_selection_payload(selection) -> dict:
    """Selection JSON for the Composer with non-vocabulary prose removed.

    Per-component ``alternatives`` are Component Selection's internal
    considerations, not canonical Composer vocabulary. Leaving them in
    the prompt invites the model to copy a service name that was never
    selected or rejected into a reference field (the observed
    "Amazon API Gateway REST API" failure). The upstream artifact is
    unchanged; only this prompt view omits the field.
    """
    payload = selection.model_dump(mode="json")
    for group in ("selected_components", "rejected_candidates"):
        for comp in payload.get(group) or []:
            if isinstance(comp, dict):
                comp.pop("alternatives", None)
    return payload


def _upstream_alternative_names(selection) -> set[str]:
    """Normalized names that appear only as upstream alternative prose."""
    names: set[str] = set()
    for comp in list(selection.selected_components) + list(
        selection.rejected_candidates
    ):
        for alternative in getattr(comp, "alternatives", []) or []:
            text = str(alternative or "").strip()
            if text:
                names.add(_normalize_component_name(text))
    return names


def _format_labeled_component_references(
    selected_names: list[str],
    rejected_names: list[str],
    categories: dict[str, str] | None = None,
) -> str:
    """Render the correction-retry vocabulary with membership labeled.

    Slugs are built over the combined selected+rejected list in upstream
    order, exactly as the previous unlabeled correction rendering did,
    so every slug is identical to before — only grouping labels are
    added. Membership comes from the already-separated selection
    artifacts; nothing is inferred from names.
    """
    combined = list(selected_names) + list(rejected_names)
    references = build_component_references(combined)
    selected_set = set(selected_names)
    selected_lines: list[str] = []
    rejected_lines: list[str] = []
    seen: set[str] = set()
    for name in combined:
        if not name or not name.strip() or name in seen:
            continue
        seen.add(name)
        category = (categories or {}).get(name)
        suffix = f" [{category}]" if category else ""
        line = f"- {references[name]} -> {name}{suffix}"
        if name in selected_set:
            selected_lines.append(line)
        else:
            rejected_lines.append(line)
    selected_block = (
        "\n".join(selected_lines) if selected_lines else "- (none)"
    )
    rejected_block = (
        "\n".join(rejected_lines) if rejected_lines else "- (none)"
    )
    return (
        "Selected components (may be nodes, edges, subjects, alternatives):\n"
        f"{selected_block}\n"
        "Rejected components (subjects and alternatives only, never nodes):\n"
        f"{rejected_block}"
    )


def _format_evidence_references(evidence_ids: list[str]) -> str:
    """Render the authoritative evidence-ID vocabulary for prompts.

    Derived only from the upstream Component Selection result; the
    composer must reproduce these IDs exactly and never invent others.
    """
    cleaned = [e for e in (str(v or "").strip() for v in evidence_ids) if e]
    if not cleaned:
        return "- (no evidence in selection)"
    return "\n".join(f"- {evidence_id}" for evidence_id in sorted(set(cleaned)))


def _format_rejected_names(rejected_names: list[str]) -> str:
    """Render rejected names for the first-attempt prompt.

    Membership comes from the already-separated selection artifacts;
    nothing is inferred from names.
    """
    cleaned = [n for n in (str(v or "").strip() for v in rejected_names) if n]
    if not cleaned:
        return "- (none)"
    seen: set[str] = set()
    lines: list[str] = []
    for name in cleaned:
        if name in seen:
            continue
        seen.add(name)
        lines.append(f"- {name}")
    return "\n".join(lines)


class LLMArchitectureNode(BaseModel):
    """LLM-facing node proposal. Keys only, never values."""

    component_ref: str = Field(
        default="",
        description=(
            "Deterministic reference of a selected component, as "
            "listed in the Allowed components vocabulary. Preferred "
            "over repeating the component name."
        ),
    )
    component_name: str = Field(
        default="",
        description="Exact selected component name (alternative to ref)",
        validation_alias=AliasChoices("component_name", "name"),
    )
    role_in_system: str = Field(
        default="", description="Role/purpose in this topology"
    )
    capacity_keys: LenientKeyList = Field(
        default_factory=list,
        description="Capacity metric keys; values resolved by Python",
        validation_alias=AliasChoices("capacity_keys", "capacity_metrics"),
    )
    scaling_note: str = Field(default="", description="Scaling note, if supported")
    availability_note: str = Field(
        default="", description="Availability note, if supported"
    )
    evidence_refs: LenientStrList = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore", use_enum_values=True)


class LLMArchitectureEdge(BaseModel):
    """LLM-facing edge proposal with component-name or component-ref
    endpoints."""

    from_component: str = Field(
        description=(
            "Source: a selected component_ref, its exact name, "
            "or client/external"
        ),
        validation_alias=AliasChoices(
            "from_component", "from", "source"
        ),
    )
    to_component: str = Field(
        description=(
            "Destination: a selected component_ref, its exact name, "
            "or client/external"
        ),
        validation_alias=AliasChoices("to_component", "to", "target"),
    )
    kind: LenientKind = Field(description="Flow kind")
    protocol: str = Field(default="", description="Protocol if known")
    description: str = Field(default="", description="Purpose of this flow")
    load_key: LenientMetricKey | None = Field(
        default=None, description="Single load key, or null",
        validation_alias=AliasChoices("load_key", "load"),
    )
    evidence_refs: LenientStrList = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore", use_enum_values=True)


class LLMArchitectureAlternative(BaseModel):
    """LLM-facing alternative: a reference to known vocabulary or generic
    reasoning, never an arbitrary service-name string.

    - ``component_ref`` names an already-selected or already-rejected
      component (exact component name, or a selected component's
      deterministic node-ref slug). Empty means "no specific
      component": generic reasoning only.
    - ``note`` is required generic architectural reasoning. It must not
      smuggle in a provider-prefixed service name; the deterministic
      validator still checks the rendered text as the final authority.
    """

    component_ref: str = Field(
        default="",
        description=(
            "Exact selected/rejected component name, or a selected "
            "component's node-ref slug from the Allowed components "
            "vocabulary. Empty for generic reasoning with no component. "
            "Never copy a name from upstream alternative/tradeoff prose."
        ),
    )
    note: str = Field(
        default="",
        description=(
            "Required generic explanation of the tradeoff. Must not "
            "name a provider-prefixed service outside the vocabulary."
        ),
    )

    model_config = ConfigDict(extra="ignore", use_enum_values=True)


class LLMArchitectureDecision(BaseModel):
    """LLM-facing architectural decision.

    Component identity belongs in ``about_component_ref`` (closed
    vocabulary); ``decision``/``rationale`` carry explanation only and
    stay free-form prose under the unchanged validator.
    """

    about_component_ref: str = Field(
        default="",
        description=(
            "The single selected/rejected component this decision is "
            "about: exact component name or selected node-ref slug. "
            "Empty for cross-cutting decisions about no one component."
        ),
    )
    decision: str = Field(
        default="",
        validation_alias=AliasChoices("decision", "description"),
    )
    rationale: str = Field(default="")
    alternatives_considered: list[LLMArchitectureAlternative] = Field(
        default_factory=list
    )
    evidence_refs: LenientStrList = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore", use_enum_values=True)


class ArchitectureLLMOutput(BaseModel):
    """LLM function-calling contract for the composer.

    Tolerant boundary schema: no numeric capacity values, no prices, no
    timestamps, no IDs, no versions, no hashes, no cost links, no
    resolved CapacityValue objects. Unknown extra keys are ignored.
    """

    nodes: list[LLMArchitectureNode] = Field(default_factory=list)
    edges: list[LLMArchitectureEdge] = Field(default_factory=list)
    entry_points: LenientStrList = Field(
        default_factory=list,
        description="Ingress components by exact component name",
    )
    data_stores: LenientStrList = Field(
        default_factory=list,
        description=(
            "Persisting components by exact component name. Only "
            "components whose selected category is one of database, "
            "storage, caching, or backup may appear here. Monitoring/"
            "observability components (log groups, alarms, dashboards) "
            "are never data stores, even when they retain logs."
        ),
    )
    boundaries: LenientBoundaries = Field(default_factory=list)
    decisions: list[LLMArchitectureDecision] = Field(default_factory=list)
    assumptions: LenientStrList = Field(default_factory=list)
    warnings: LenientWarnList = Field(default_factory=list)
    new_tradeoffs: list[LLMUnresolvedTradeoff] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore", use_enum_values=True)


def _parse_text_envelope(content: str) -> ArchitectureLLMOutput:
    """Parse a text-envelope model response against the boundary schema.

    Used ONLY when the model answered in plain JSON text instead of
    emitting the requested function call. The text must still validate
    against the same ArchitectureLLMOutput schema; anything else raises
    loudly.
    """
    import json as _json

    text = (content or "").strip()
    if not text:
        raise ValueError(
            "Architecture Composer returned no function call and empty "
            "text; nothing to parse."
        )
    try:
        return ArchitectureLLMOutput.model_validate_json(text)
    except Exception:
        pass
    try:
        start = text.index("{")
        obj, _ = _json.JSONDecoder().raw_decode(text[start:])
        return ArchitectureLLMOutput.model_validate(obj)
    except Exception as e:
        raise ValueError(
            "Architecture Composer did not emit a function call and its "
            "text output is not valid ArchitectureLLMOutput JSON: "
            f"{e}. Preview: {text[:300]!r}"
        ) from e


def _resolve_parsed(agent, unbound_agent, messages):
    """One attempt: function call, empty-response retry, envelope fallback.

    Returns (parsed_output, fell_back_to_text). Raises loudly when the
    model output cannot be parsed against the boundary schema.
    """
    with console.status("Composing architecture..."):
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
            with console.status("Composing architecture..."):
                retry_msg = unbound_agent.invoke(messages)
            content = getattr(retry_msg, "content", "") or ""
        # Non-empty text here (original or retried) must still satisfy
        # the same boundary schema; empty/invalid text raises loudly.
        parsed = _parse_text_envelope(content)
        fell_back_to_text = True

    if isinstance(parsed, dict):
        parsed = ArchitectureLLMOutput(**parsed)

    if not isinstance(parsed, ArchitectureLLMOutput):
        raise ValueError(
            "Architecture Composer LLM output could not be parsed: "
            f"{parsing_error or type(parsed).__name__}"
        )
    return parsed, fell_back_to_text


def normalize_architecture(
    llm_output: ArchitectureLLMOutput,
    *,
    extraction: RequirementsExtraction,
    capacity: CapacityModelResult,
    selection,
    cost: CostEstimationResult,
    region_code: str,
    requirements_session_id: str,
    design_session_id: str,
    provider: str,
    version: int = 1,
    supersedes_artifact_id: str | None = None,
) -> ArchitectureResult:
    """Deterministically map LLM boundary output onto ArchitectureResult.

    - Unknown component references raise loudly (never repaired,
      substituted, or invented). A proposal must resolve either through
      a deterministic component_ref from the upstream Component
      Selection result or through an exact selected component name.
    - Node/edge IDs come from build_component_references(), the same
      vocabulary rendered into the prompt, so the LLM's references and
      the artifact's node_ids cannot drift apart.
    - Categories are copied from Component Selection.
    - Capacity keys resolve via Slice A; cost links/totals/identity via
      Slice A builders. The result is validated before return.
    """
    selected_by_name = {
        comp.name: comp for comp in selection.selected_components
    }
    references = build_component_references(selected_by_name)
    reference_to_name = {
        reference: name for name, reference in references.items()
    }
    categories = _component_categories(selection)
    allowed_vocabulary = _format_component_references(references, categories)
    # Normalized lookups so component names that differ only in Unicode
    # hyphen/space characters (e.g. U+2011 NON-BREAKING HYPHEN vs U+002D
    # HYPHEN-MINUS) still resolve to the selected component. Ambiguous
    # normalized keys (two distinct approved names collapsing to the same
    # key) are rejected rather than resolved arbitrarily.
    normalized_by_name: dict[str, str] = {}
    normalized_ambiguous: set[str] = set()
    for name in selected_by_name:
        key = _normalize_component_name(name)
        if key in normalized_by_name and normalized_by_name[key] != name:
            normalized_ambiguous.add(key)
        normalized_by_name[key] = name
    # Vocabulary-wide normalized lookup (selected + rejected) for
    # reference contexts (alternatives, decision subjects). A rejected
    # component that normalizes to a selected component's key is still
    # ambiguous: selected wins, rejected has no normalized form.
    normalized_vocab_by_name: dict[str, str] = {}
    normalized_vocab_ambiguous: set[str] = set()
    for name in list(selected_by_name) + [c.name for c in selection.rejected_candidates]:
        if not name or not name.strip():
            continue
        key = _normalize_component_name(name)
        if key in normalized_vocab_by_name and normalized_vocab_by_name[key] != name:
            normalized_vocab_ambiguous.add(key)
        normalized_vocab_by_name[key] = name
    # Extended lookup for reference contexts (alternatives, decision
    # subjects): selected slugs first, then rejected slugs that do not
    # collide. Selected wins every collision deterministically; a
    # colliding rejected slug simply has no slug form and still
    # resolves by exact name. Node assignment below keeps using the
    # selected-only maps, so a rejected reference can never become a
    # node.
    reference_lookup = dict(reference_to_name)
    for comp in selection.rejected_candidates:
        name = comp.name or ""
        if not name.strip():
            continue
        slug = _slugify(name)
        if not slug:
            continue
        reference_lookup.setdefault(slug, name)
    rejected_lines = [
        f"- {slug} -> {name}"
        + (f" [{categories[name]}]" if name in categories else "")
        for slug, name in sorted(reference_lookup.items())
        if name not in selected_by_name
    ]
    allowed_reference_vocabulary = allowed_vocabulary
    if rejected_lines:
        allowed_reference_vocabulary += (
            "\nRejected references (alternatives only, never nodes):\n"
            + "\n".join(rejected_lines)
        )
    upstream_alternative_tokens = _upstream_alternative_names(selection)

    def _upstream_alternative_note(token: str) -> str:
        """Explain a reference that came from upstream alternative prose.

        Per-component alternatives are not Composer vocabulary; naming
        that explicitly keeps the failure actionable without ever
        accepting the token or substituting a different service.
        """
        if _normalize_component_name(
            str(token or "")
        ) in upstream_alternative_tokens:
            return (
                " This token appears only in upstream Component Selection "
                "alternative prose, which is descriptive and not a "
                "component reference. Use a listed canonical component ID, "
                "or leave the reference empty and explain the tradeoff "
                "generically."
            )
        return ""

    def _reference_recovery_hint(token: str) -> str:
        """Actionable distinction between a variant and a typo.

        Neither case is accepted or substituted. The message tells the
        model how to express a legitimate configuration variant versus
        how to correct a malformed reference.
        """
        return _upstream_alternative_note(token) + (
            " If you intended a configuration variant of a selected "
            "component, use that component's canonical ID and explain the "
            "variant in prose; if the reference is a typo, correct it to a "
            "listed canonical ID."
        )

    def _resolve_component(token: str, context: str) -> str:
        """Resolve a component_ref or exact name to its selected name.

        Fails closed: an unresolvable reference is an error, never a
        substitute or a generic placeholder.
        """
        candidate = (token or "").strip()
        if candidate in selected_by_name:
            return candidate
        if candidate in reference_to_name:
            return reference_to_name[candidate]
        normalized = _normalize_component_name(candidate)
        if normalized in normalized_ambiguous:
            raise ValueError(
                f"Architecture Composer {context} references component "
                f"{token!r} which is ambiguous after Unicode normalization: "
                "multiple approved components share the same normalized "
                "name."
            )
        if normalized in normalized_by_name:
            return normalized_by_name[normalized]
        raise ValueError(
            f"Architecture Composer {context} references unknown "
            f"component {token!r}: it is not in the Component Selection "
            "vocabulary. Allowed references:\n"
            f"{allowed_vocabulary}"
            + _reference_recovery_hint(token)
        )

    node_id_by_component: dict[str, str] = {}
    nodes: list[ArchitectureNode] = []
    role_fallback_notes: list[str] = []
    for item in llm_output.nodes:
        token = item.component_ref.strip() or item.component_name.strip()
        if not token:
            raise ValueError(
                "Architecture Composer proposed a node with no "
                "component_ref or component_name."
            )
        component_name = _resolve_component(token, "node")
        if component_name in node_id_by_component:
            raise ValueError(
                f"Architecture Composer proposed unknown component "
                f"{component_name!r} more than once; each selected "
                "component maps to at most one node."
            )
        selected = selected_by_name[component_name]
        node_id = references[component_name]
        node_id_by_component[component_name] = node_id
        capacity_keys = (
            item.capacity_keys
            if isinstance(item.capacity_keys, list)
            else _to_str_list(item.capacity_keys)
        )
        # Role fallback is a deterministic copy of validated upstream
        # text (the selection's own role), never an invented role. The
        # fallback is recorded transparently in warnings below.
        role_in_system = item.role_in_system.strip() or selected.role
        if not item.role_in_system.strip():
            role_fallback_notes.append(
                f"Node {node_id!r} role copied from component selection."
            )
        nodes.append(
            ArchitectureNode(
                node_id=node_id,
                component_name=component_name,
                category=str(selected.category),
                role_in_system=role_in_system,
                capacity=[
                    resolve_capacity_value(key, capacity)
                    for key in capacity_keys
                ],
                scaling_note=item.scaling_note,
                availability_note=item.availability_note,
                evidence_refs=_to_str_list(item.evidence_refs),
            )
        )

    def _endpoint(token: str, edge_id: str) -> str:
        lowered = (token or "").strip().lower()
        if lowered in EXTERNAL_ENDPOINTS:
            return lowered
        component_name = _resolve_component(
            token, f"edge {edge_id!r}"
        )
        if component_name not in node_id_by_component:
            raise ValueError(
                f"Architecture edge {edge_id!r} references unknown "
                f"component {token!r}: it is not a declared node. "
                "Declare the node before referencing it in a flow."
            )
        return node_id_by_component[component_name]

    edges: list[ArchitectureEdge] = []
    for index, item in enumerate(llm_output.edges):
        edge_id = f"e{index + 1}"
        edges.append(
            ArchitectureEdge(
                edge_id=edge_id,
                from_node=_endpoint(item.from_component, edge_id),
                to_node=_endpoint(item.to_component, edge_id),
                kind=item.kind,
                protocol=item.protocol,
                description=item.description,
                load=(
                    resolve_capacity_value(item.load_key, capacity)
                    if item.load_key is not None
                    else None
                ),
                evidence_refs=_to_str_list(item.evidence_refs),
            )
        )

    def _to_node_ids(names: list[str] | str, field: str) -> list[str]:
        resolved: list[str] = []
        for token in _to_str_list(names):
            component_name = _resolve_component(token, field)
            if component_name not in node_id_by_component:
                raise ValueError(
                    f"Architecture {field} references unknown component "
                    f"{token!r}: it is not a declared node."
                )
            resolved.append(node_id_by_component[component_name])
        return resolved

    rejected_by_name = {
        comp.name: comp for comp in selection.rejected_candidates
    }

    def _lookup_vocab_name(token: str) -> str | None:
        """Resolve exact selected/rejected names and slugs.

        Shared by decision subjects and alternatives so both fields
        speak the same closed vocabulary. Slug collisions resolve to
        selected components first; a colliding rejected slug has no
        slug form and still resolves by exact name. Returns None when
        the token names nothing known; callers fail closed.
        """
        candidate = (token or "").strip()
        if not candidate:
            return None
        if candidate in selected_by_name or candidate in rejected_by_name:
            return candidate
        normalized = _normalize_component_name(candidate)
        if normalized in normalized_vocab_ambiguous:
            return None
        if normalized in normalized_vocab_by_name:
            return normalized_vocab_by_name[normalized]
        return reference_lookup.get(candidate)

    def _resolve_subject(token: str, decision_index: int) -> str | None:
        """Resolve a decision's about_component_ref, if present.

        Empty means cross-cutting: no subject, no error. A non-empty
        token that names nothing known fails closed. The subject is a
        label only: it creates no node, cost link, or selection entry.
        """
        candidate = (token or "").strip()
        if not candidate:
            return None
        resolved = _lookup_vocab_name(candidate)
        if resolved is None:
            raise ValueError(
                "Architecture Composer decision "
                f"[{decision_index}] about_component_ref references "
                f"unknown component {token!r}: it is not in the "
                "Component Selection vocabulary. Allowed references:\n"
                f"{allowed_reference_vocabulary}"
                + _reference_recovery_hint(token)
            )
        return resolved

    def _resolve_alternative(
        alt: LLMArchitectureAlternative, decision_index: int
    ) -> str:
        """Deterministically render one structured alternative.

        A present component_ref must resolve to an exact selected or
        rejected component name (or a listed node-ref slug); anything
        else fails closed without inventing or substituting a service.
        A blank note is rejected: an alternative with nothing to say
        carries no information. The rendered text is still checked by
        the deterministic validator, which remains the final authority.
        """
        if not isinstance(alt, LLMArchitectureAlternative):
            raise ValueError(
                "Architecture Composer decision "
                f"[{decision_index}] alternative must be a "
                "{component_ref, note} object, not "
                f"{type(alt).__name__}."
            )
        note = (alt.note or "").strip()
        if not note:
            raise ValueError(
                "Architecture Composer decision "
                f"[{decision_index}] alternative has an empty note; "
                "each alternative must explain its tradeoff."
            )
        token = (alt.component_ref or "").strip()
        if not token:
            return note
        resolved = _lookup_vocab_name(token)
        if resolved is None:
            raise ValueError(
                "Architecture Composer decision "
                f"[{decision_index}] alternative references unknown "
                f"component {token!r}: it is not in the Component "
                "Selection vocabulary. Allowed references:\n"
                f"{allowed_reference_vocabulary}"
                + _reference_recovery_hint(token)
            )
        return f"{resolved}: {note}"

    def _render_decision(item: LLMArchitectureDecision, index: int) -> ArchitectureDecision:
        subject = _resolve_subject(item.about_component_ref, index)
        decision_text = (
            f"[About: {subject}] {item.decision}"
            if subject is not None
            else item.decision
        )
        return ArchitectureDecision(
            decision=decision_text,
            rationale=item.rationale,
            alternatives_considered=[
                _resolve_alternative(alt, index)
                for alt in item.alternatives_considered
            ],
            evidence_refs=_to_str_list(item.evidence_refs),
        )

    decisions = [
        _render_decision(item, index)
        for index, item in enumerate(llm_output.decisions)
    ]

    carried = list(selection.unresolved_tradeoffs)
    # Deterministic dedup: the model frequently restates a carried
    # tradeoff as "new". Descriptions matching (case/whitespace
    # normalized) an already-carried tradeoff add no information, so
    # only genuinely new descriptions are appended. Python deduplicates;
    # the model is never asked to.
    seen_descriptions = {
        _normalize_tradeoff_text(t.description) for t in carried
    }
    raised: list[UnresolvedTradeoff] = []
    for item in llm_output.new_tradeoffs:
        key = _normalize_tradeoff_text(item.description)
        if key in seen_descriptions:
            continue
        seen_descriptions.add(key)
        raised.append(
            UnresolvedTradeoff(
                description=item.description,
                options=_to_str_list(item.options),
                impact=item.impact,
                recommended_resolution=item.recommended_resolution,
                evidence_refs=_to_str_list(item.evidence_refs),
            )
        )

    evidence_references = sorted(
        {
            ref
            for holder in (
                [node.evidence_refs for node in nodes]
                + [edge.evidence_refs for edge in edges]
                + [dec.evidence_refs for dec in decisions]
            )
            for ref in holder
        }
    )

    result_warnings = _to_str_list(llm_output.warnings)
    for note in role_fallback_notes:
        if note not in result_warnings:
            result_warnings.append(note)

    result = ArchitectureResult(
        artifact_id=new_artifact_id(),
        session_id=design_session_id,
        version=version,
        supersedes_artifact_id=supersedes_artifact_id,
        nodes=nodes,
        edges=edges,
        entry_points=_to_node_ids(llm_output.entry_points, "entry_points"),
        data_store_node_ids=_to_node_ids(
            llm_output.data_stores, "data_stores"
        ),
        boundaries=_to_str_list(llm_output.boundaries),
        cost_links=build_cost_links(selection, cost),
        cost_totals_snapshot=dict(cost.totals_usd_per_month),
        decisions=decisions,
        assumptions=_to_str_list(llm_output.assumptions),
        warnings=result_warnings,
        unresolved_tradeoffs=carried + raised,
        evidence_references=evidence_references,
        inputs_ref=build_inputs_ref(
            requirements_session_id=requirements_session_id,
            capacity=capacity,
            selection=selection,
            cost=cost,
            extraction=extraction,
            region_code=region_code,
        ),
    )

    errors = validate_architecture(
        result,
        selection=selection,
        capacity=capacity,
        cost=cost,
        extraction=extraction,
        requirements_session_id=requirements_session_id,
        design_session_id=design_session_id,
        provider=provider,
        region_code=region_code,
    )
    if errors:
        raise ValueError(
            "Architecture composition validation failed: " + "; ".join(errors)
        )
    return result


def run_architecture_composer(
    extraction: RequirementsExtraction,
    capacity: CapacityModelResult,
    selection,
    cost: CostEstimationResult,
    region_code: str,
    session_id: str,
    *,
    requirements_session_id: str,
    revision_brief: str | None = None,
    version: int = 1,
    supersedes_artifact_id: str | None = None,
) -> ArchitectureResult:
    """Run the Architecture Composer worker.

    Pure worker: takes already-validated upstream artifacts, reasons
    with the LLM over keys only, normalizes deterministically, and
    returns a validated ArchitectureResult. No retrieval occurs here;
    evidence comes only from the selection result.

    ``revision_brief`` carries deterministic critic findings for a
    bounded revision cycle: the composer revises topology within the
    same closed-world contract (same selected set, same evidence set,
    same validation). It can never introduce a component, so a finding
    that faults the component set itself must be routed through
    Component Selection instead. ``version``/``supersedes_artifact_id``
    chain the revision onto the critiqued artifact.
    """
    provider = resolve_provider(extraction)
    categories = _component_categories(selection)
    selected_references = build_component_references(
        [comp.name for comp in selection.selected_components]
    )

    agent = create_architecture_composer_agent_with_raw()

    user_message = f"""## Requirements
{json.dumps(extraction.model_dump(mode="json"), indent=2)}

## Capacity Model (deterministic - select keys, do not recalculate)
{json.dumps(capacity.model_dump(mode="json"), indent=2)}

## Component Selection (descriptive upstream context)
This JSON records why upstream components were selected or rejected.
Its per-component `alternatives` and `tradeoffs` strings are
descriptive prose, NOT component vocabulary. Never copy a service name
from that prose into any component_ref field.
{json.dumps(_composer_selection_payload(selection), indent=2)}

## Allowed components (closed world)
These are the ONLY components you may use as nodes and edge
endpoints. Each line is
`canonical-id -> exact-name [selected-category]`. Prefer the canonical
ID in a node's `component_ref` and in edge endpoints; the exact
component_name also works. Any other reference is rejected by
deterministic validation. Do not invent, rename, or substitute a
component.
{_format_component_references(selected_references, categories)}

## Rejected components (subjects and alternatives only, never nodes)
{_format_rejected_names(
    [comp.name for comp in selection.rejected_candidates]
)}

## Authoritative evidence IDs (closed world)
ONLY these IDs may appear in evidence_refs. Reproduce each ID
exactly. Never invent, truncate, extend, or guess an ID.
{_format_evidence_references(list(selection.evidence_references))}

## Provider
{provider}

## Region
{region_code}

Compose the architecture from these artifacts."""

    messages = [
        SystemMessage(content=ARCHITECTURE_COMPOSER_SYSTEM_PROMPT),
        HumanMessage(content=user_message),
    ]
    if revision_brief and revision_brief.strip():
        # Bounded revision: the deterministic loop supplies critic
        # findings verbatim. Minimal-change instruction only; the same
        # closed-world contract and validation apply unchanged.
        messages.append(
            HumanMessage(
                content=(
                    "REVISION BRIEF (deterministic critic findings - "
                    "address each with minimal topology changes, "
                    "introduce no new component):\n"
                    + revision_brief.strip()[:4000]
                )
            )
        )

    # Function calling remains the mechanism. include_raw lets us tell apart
    # a transport flake (model answered in text, no tool call) from a genuine
    # schema violation (parsing_error), without hiding either.
    unbound_agent = create_architecture_composer_agent_raw()
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
                    "ArchitectureComposer function call and no "
                    "surrounding prose, using the exact field names "
                    "from the system prompt."
                )
            )
        ]
        try:
            parsed, fell_back_to_text = _resolve_parsed(
                agent, unbound_agent, strict_retry
            )
        except ValueError as retry_parse_error:
            raise ValueError(
                "Architecture Composer output could not be parsed, "
                "even after one strict-format retry. "
                f"First: {parse_error} Retry: {retry_parse_error}"
            ) from retry_parse_error

    try:
        result = normalize_architecture(
            parsed,
            extraction=extraction,
            capacity=capacity,
            selection=selection,
            cost=cost,
            region_code=region_code,
            requirements_session_id=requirements_session_id,
            design_session_id=session_id,
            provider=provider,
            version=version,
            supersedes_artifact_id=supersedes_artifact_id,
        )
    except ValueError as first_error:
        # Bounded correction retry (exactly one): the model sees the
        # deterministic validator's exact complaints and resubmits
        # through the same schema, normalization, and validation. A
        # second failure still fails loudly with both errors.
        # The allowed vocabulary is listed explicitly: abstract rules
        # alone do not reliably stop fresh inventions on resubmission.
        vocabulary = _format_labeled_component_references(
            [comp.name for comp in selection.selected_components],
            [comp.name for comp in selection.rejected_candidates],
            categories,
        )
        evidence_vocab = _format_evidence_references(
            list(selection.evidence_references)
        )
        correction = (
            "Your previous output was REJECTED by deterministic "
            "validation. Produce a corrected output that fixes EVERY "
            "item below without changing anything else. Rules:\n"
            "- Remove every NUMERIC value from all text fields: no QPS "
            "counts (never write '347 QPS'), no storage sizes (never "
            "write '500 GB'), no "
            "connection counts, no prices, no availability "
            "percentages. Capacity facts live only in capacity linkage "
            "(capacity_keys/load_key); assumptions carry no numbers. "
            "Bare qualitative words with no number "
            "(throughput, load, traffic, hot working set) are acceptable.\n"
            "- ONLY use evidence IDs from this supplied set; reproduce "
            "each ID exactly and never invent, truncate, extend, or "
            "guess one:\n"
            f"{evidence_vocab}\n"
            "- Every provider-specific infrastructure service named in "
            "decision or rationale prose MUST be one of "
            "the Allowed components listed below (exact name, or a "
            "well-known short form such as SQS, SNS, S3, DynamoDB, "
            "Lambda, EC2, or RDS that resolves to one of them).\n"
            "- Each alternatives_considered entry MUST be an object "
            "{\"component_ref\", \"note\"}: component_ref is an exact "
            "Allowed component name (selected or rejected), a listed "
            "node-ref slug, or empty; "
            "note is required generic reasoning with no "
            "provider-prefixed service names. A bare service-name "
            "string is never valid. Example: {\"component_ref\": \"\", "
            "\"note\": \"a managed cache could cut read load\"}. "
            "Rejected references are alternatives only, never nodes.\n"
            "- Each decision MUST carry its subject in "
            "\"about_component_ref\": an exact Allowed component name "
            "(selected or rejected), a listed node-ref slug, or empty "
            "for cross-cutting "
            "decisions. Do not encode the subject as a service name "
            "inside decision prose.\n"
            "- If a decision concerns an infrastructure service that "
            "appears in NEITHER list below, set \"about_component_ref\" "
            "to \"\" and describe it in provider-free generic words "
            "(never name the service); an unknown service name is "
            "rejected wherever it appears.\n"
            "- Never copy a service name from the upstream Component "
            "Selection per-component alternatives/tradeoffs prose into "
            "a component_ref: that prose is descriptive, not vocabulary. "
            "A configuration variant of a selected component (different "
            "API style, runtime, capacity mode, or billing mode) is not "
            "a new component; keep component_ref pointed at the selected "
            "component or empty and explain the variant generically.\n"
            "- data_stores may contain ONLY nodes whose category is "
            "database, storage, caching, or backup. Never put a "
            "monitoring/logging/observability component there, even if "
            "it retains logs; keep it as a node connected by a control "
            "or asynchronous edge. Do not drop a selected component to "
            "satisfy this rule.\n"
            "- Reference nodes and edge endpoints by their component_ref "
            "from the Allowed components list. Do not introduce, rename, "
            "or substitute any component.\n"
            "- Give every node at least one capacity key.\n"
            "Allowed components (membership matters):\n"
            f"{vocabulary}\n"
            "Rejection reasons:\n"
            + str(first_error)[:2000]
        )
        retry_messages = messages + [HumanMessage(content=correction)]
        retry_parsed, retry_fell_back = _resolve_parsed(
            agent, unbound_agent, retry_messages
        )
        fell_back_to_text = fell_back_to_text or retry_fell_back
        try:
            result = normalize_architecture(
                retry_parsed,
                extraction=extraction,
                capacity=capacity,
                selection=selection,
                cost=cost,
                region_code=region_code,
                requirements_session_id=requirements_session_id,
                design_session_id=session_id,
                provider=provider,
                version=version,
                supersedes_artifact_id=supersedes_artifact_id,
            )
        except ValueError as second_error:
            diagnostics = [
                _diagnostic_entry(1, parsed, first_error),
                _diagnostic_entry(2, retry_parsed, second_error),
            ]
            raise ArchitectureCompositionError(
                "Architecture composition failed after one correction "
                f"retry. First failure: {first_error} "
                f"Retry failure: {second_error}",
                diagnostics=diagnostics,
            ) from second_error

    if fell_back_to_text and FALLBACK_NOTE not in result.warnings:
        result.warnings.append(FALLBACK_NOTE)

    return result
