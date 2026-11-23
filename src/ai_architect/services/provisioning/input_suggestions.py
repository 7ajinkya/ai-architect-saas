"""Assisted suggestions for the pending human decisions of a plan.

A plan that cannot be derived from the architecture alone stops at a
list of *pending human decisions* (required provisioning parameters such
as a Lambda's memory size, an S3 bucket's versioning mode, or an alarm
threshold). Those decisions belong to a human, but a human should not
have to type every one of them from scratch to get a first plan.

This module fills that gap without weakening the human decision:

    collect pending inputs
      -> split them into three closed classes
           platform_default : values the platform already knows
                              deterministically (the packaged demo
                              artifact's runtime/handler and the key
                              schema its handler requires)
           ai_suggestion    : policy values an LLM proposes from the
                              user's own request, validated against the
                              module contract before they are returned
           human_only       : values that can only come from the
                              operator (a globally unique bucket name)
      -> return suggestions for the operator to review, edit and submit

The suggestions are advisory end to end:

- Nothing is written to the plan, the checkpoint, or AWS. The operator
  reviews them in the UI and submits them through the normal
  human-input path, where the same deterministic contract validation
  runs again.
- Every proposed value is decoded by the same contract decoder that
  will later accept or refuse it. A value outside the closed allowlist
  is dropped with a reason, never replaced with a different value.
  Scalar spellings are normalized first (a model's ``1024`` or ``true``
  becomes the plan's ``"1024"`` / ``"true"``), because the plan stores
  strings and a sensible scalar is not a contract violation.
- The model proposes defaults as well as request-derived values: when
  the user's request is silent on an input, it still answers with the
  value a careful AWS engineer would deploy. A detailed request is
  context, never a prerequisite.
- Suggestions never hard-fail. A malformed model answer is salvaged
  item by item, and a model outage degrades to the platform-owned
  values plus an explicit note - the operator can always type values.
- Values that encode a fact the platform owns (the packaged artifact's
  interface) are never proposed by a model; values that encode a fact
  only the operator has (a globally unique name) are never proposed at
  all.
- The model never sees or proposes account identifiers, ARNs, URLs,
  secrets, code artifacts or prices.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ai_architect.domain.provisioning.module import (
    ModuleContract,
    ModuleInputDefinition,
    ParameterType,
)
from ai_architect.domain.provisioning.models import ResourceKind
from ai_architect.services.provisioning.capability_mapping import (
    module_for_kind,
)
from ai_architect.services.provisioning.module_parameters import (
    ParameterDecodeError,
    decode_parameter_value,
    definition_for_parameter,
)
from ai_architect.services.provisioning.module_registry import (
    VerifiedModuleRegistry,
    build_default_registry,
)

#: Provenance of a returned suggestion. Both are advisory; the operator
#: submits the value either way.
SOURCE_PLATFORM_DEFAULT = "platform_default"
SOURCE_AI_SUGGESTION = "ai_suggestion"

#: Values the platform owns deterministically, keyed by (resource kind,
#: parameter name). They describe the packaged demo artifact, which is
#: project-owned material with a known interface - they are facts, not
#: guesses, and no model is allowed to propose them.
_ARTIFACT_DEFAULTS: dict = {
    (ResourceKind.LAMBDA, "runtime"): (
        "python3.12",
        "The packaged demo handler is a Python 3.12 module.",
    ),
    (ResourceKind.LAMBDA, "handler"): (
        "handler.handler",
        "The packaged demo artifact exposes handler.py with a module-level "
        "handler() entry point.",
    ),
    (ResourceKind.DYNAMODB, "attribute_definitions"): (
        "pk:S",
        "The packaged demo handler reads and writes one item per request "
        "keyed by the string partition key pk.",
    ),
    (ResourceKind.DYNAMODB, "key_schema"): (
        "HASH:pk",
        "The packaged demo handler uses a single partition key pk and no "
        "sort key.",
    ),
}

#: Values only the operator can decide. A bucket name is unique across
#: all of S3, so a proposed one would be a guess that collides; it stays
#: with the human.
_HUMAN_ONLY: dict = {
    (ResourceKind.S3, "bucket_name"): (
        "A bucket name is globally unique in S3, so it has to be yours: "
        "the platform never proposes one."
    ),
}

#: Bound on the correction loop: the first proposal plus at most one
#: repair round for the values the contract refused.
MAX_SUGGESTION_ATTEMPTS = 2

#: Upper bound on how many inputs one proposal round may carry. A plan
#: with more pending inputs than this is pathological, and the bound
#: keeps the prompt and the response bounded.
MAX_SUGGESTED_INPUTS = 64


class SuggestionError(ValueError):
    """Unexpected suggestion failure; kept for the API's safety net.

    The service itself degrades instead of raising (see
    :func:`suggest_pending_inputs`); the API maps this class to a 409 in
    case a future change breaks that guarantee.
    """


@dataclass(frozen=True)
class PendingInput:
    """One required input a plan could not derive."""

    resource_id: str
    kind: str
    name: str
    component_name: str = ""
    reason: str = ""


@dataclass(frozen=True)
class InputSuggestion:
    """One proposed value, with its provenance and reasoning."""

    resource_id: str
    name: str
    value: str
    rationale: str
    source: str


@dataclass(frozen=True)
class SkippedInput:
    """One pending input that was deliberately not proposed."""

    resource_id: str
    name: str
    reason: str


@dataclass(frozen=True)
class InputSuggestionResult:
    """The full outcome of one suggestion round."""

    suggestions: tuple[InputSuggestion, ...] = ()
    skipped: tuple[SkippedInput, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_payload(self) -> dict:
        return {
            "suggestions": [
                {
                    "resource_id": item.resource_id,
                    "name": item.name,
                    "value": item.value,
                    "rationale": item.rationale,
                    "source": item.source,
                }
                for item in self.suggestions
            ],
            "skipped": [
                {
                    "resource_id": item.resource_id,
                    "name": item.name,
                    "reason": item.reason,
                }
                for item in self.skipped
            ],
            "notes": list(self.notes),
        }


def canonical_value(raw) -> str:
    """Render one JSON scalar in the contract's own string spelling.

    Models routinely answer with a number or a boolean where the plan
    stores a string ("1024", "true"). The plan's inputs are strings, so the
    boundary normalizes the scalar instead of refusing it; the contract
    decoder still decides whether the string is acceptable.
    """
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if isinstance(raw, int):
        return str(raw)
    if isinstance(raw, float):
        return str(int(raw)) if raw.is_integer() else str(raw)
    if isinstance(raw, str):
        return raw.strip()
    if raw is None:
        return ""
    if isinstance(raw, (list, dict)):
        return json.dumps(raw)
    return str(raw)


class InputValueProposal(BaseModel):
    """One proposed value for one pending input."""

    model_config = ConfigDict(extra="ignore")

    resource_id: str = Field(description="Resource the value belongs to")
    name: str = Field(description="Required input name")
    value: str | int | float | bool = Field(
        description='Proposed value in the contract spelling, e.g. "1024"'
    )
    rationale: str = Field(default="", description="Why this value fits")

    @field_validator("value", mode="before")
    @classmethod
    def _canonical(cls, raw):
        return canonical_value(raw)


class InputValueProposals(BaseModel):
    """The model's proposals for the pending inputs it was asked about."""

    model_config = ConfigDict(extra="ignore")

    proposals: list[InputValueProposal] = Field(default_factory=list)


_SUGGESTION_SYSTEM = """You configure a real AWS deployment by proposing \
concrete values for the pending inputs of a provisioning plan.

Rules:
- Propose exactly ONE concrete value for EVERY input you are asked about,
  and propose nothing else. The user's request explains intent; a detailed
  request is not a prerequisite. When the request constrains an input,
  honor it. When the request is silent or partial, choose the value an
  experienced AWS engineer would deploy for this architecture, workload
  size and risk - a sensible, safe production default - and explain the
  choice in one short sentence. Never leave an input unanswered because
  the request did not mention it.
- Every value MUST be one of the allowed values or within the stated
  range. If a value is not allowed, it does not exist for you.
- Emit the value as a JSON string in the contract's spelling, for example
  "1024", "true", "PAY_PER_REQUEST"; never guess around a refusal.
- Never propose names, account identifiers, ARNs, URLs, IAM policies,
  secrets, credentials, prices, code artifacts or file paths.
- Never propose a value for an input you were not asked about.
- The rationale is one short sentence for the operator who will review it.
- Respond with ONLY the JSON object, no prose and no code fences.

Exact response shape:
{"proposals": [{"resource_id": "...", "name": "...", "value": "...",
"rationale": "..."}]}
"""


def _kind(value) -> ResourceKind | None:
    """Resolve a resource kind from its value or enum member."""
    if isinstance(value, ResourceKind):
        return value
    try:
        return ResourceKind(str(value))
    except ValueError:
        return None


def _contract_for(
    kind: ResourceKind, registry: VerifiedModuleRegistry
) -> ModuleContract | None:
    module_id = module_for_kind(kind)
    if module_id is None:
        return None
    try:
        return registry.get(module_id)
    except KeyError:
        return None


def _describe_constraints(definition: ModuleInputDefinition) -> str:
    """Human-readable statement of what a value may be."""
    constraints = definition.constraints
    if definition.type is ParameterType.ENUM:
        allowed = ", ".join(sorted(constraints.allowed_values))
        return f"exactly one of: {allowed}"
    if definition.type is ParameterType.BOOL:
        return "true or false"
    if definition.type is ParameterType.INT:
        minimum = constraints.minimum
        maximum = constraints.maximum
        if minimum is not None and maximum is not None:
            return f"a whole number between {minimum} and {maximum}"
        if minimum is not None:
            return f"a whole number of at least {minimum}"
        if maximum is not None:
            return f"a whole number of at most {maximum}"
        return "a whole number"
    if definition.type is ParameterType.ATTRIBUTE_LIST:
        return (
            'attribute tokens such as "pk:S" (name:type, type is S, N or B), '
            'comma separated, or the equivalent JSON array'
        )
    if definition.type is ParameterType.KEY_SCHEMA:
        return (
            'key tokens such as "HASH:pk" (and optionally "RANGE:sk"), comma '
            "separated, or the equivalent JSON array"
        )
    parts = []
    if constraints.min_length is not None:
        parts.append(f"at least {constraints.min_length} characters")
    if constraints.max_length is not None:
        parts.append(f"at most {constraints.max_length} characters")
    if constraints.pattern:
        parts.append(f"matching {constraints.pattern}")
    if parts:
        return "a string " + " and ".join(parts)
    return "a plain string"


def _proposal_payload(
    pending: list[PendingInput],
    definitions: dict[tuple[str, str], ModuleInputDefinition],
) -> list[dict]:
    """The exact list of inputs the model is allowed to answer."""
    items = []
    for entry in pending:
        definition = definitions[(entry.resource_id, entry.name)]
        items.append({
            "resource_id": entry.resource_id,
            "component": entry.component_name,
            "name": entry.name,
            "accepted_values": _describe_constraints(definition),
            "what_it_controls": definition.description or entry.reason,
        })
    return items


def _context_payload(context: dict) -> dict:
    """Trim the session context to what a value decision may depend on."""
    requirements = context.get("requirements") or {}
    capacity = context.get("capacity") or {}
    return {
        "user_request": context.get("user_request") or "",
        "requirements": {
            key: requirements.get(key)
            for key in (
                "availability",
                "latency",
                "traffic_characteristics",
                "read_write_characteristics",
                "storage_characteristics",
                "retention",
                "compliance_requirements",
                "budget_constraints",
                "managed_service_preference",
                "region",
                "deployment_scope",
            )
            if requirements.get(key)
        },
        "capacity": {
            key: capacity.get(key)
            for key in (
                "peak_read_qps",
                "peak_write_qps",
                "concurrent_connections",
                "storage_growth_bytes_per_day",
                "egress_bytes_per_second",
            )
            if capacity.get(key) is not None
        },
        "components": context.get("components") or [],
    }


def _coerce_item(item) -> InputValueProposal | None:
    """Salvage one proposal item; return None when it carries no value."""
    model_dump = getattr(item, "model_dump", None)
    if callable(model_dump):
        try:
            item = model_dump(mode="json")
        except Exception:  # noqa: BLE001 - never trust the payload
            return None
    if not isinstance(item, dict):
        return None
    resource_id = canonical_value(item.get("resource_id"))
    name = canonical_value(item.get("name"))
    if not resource_id or not name or "value" not in item:
        return None
    return InputValueProposal(
        resource_id=resource_id,
        name=name,
        value=item.get("value"),
        rationale=canonical_value(item.get("rationale")),
    )


def _coerce_proposals(raw) -> list[InputValueProposal]:
    """Salvage proposals from one model response. Never raises.

    The proposal schema is an input format, not a trust boundary: a
    malformed or unusual envelope must never abort the suggestion round.
    Each item is normalized independently, and the contract decoder still
    decides whether a value is acceptable.
    """
    if raw is None:
        return []
    model_dump = getattr(raw, "model_dump", None)
    if callable(model_dump):
        try:
            raw = model_dump(mode="json")
        except Exception:  # noqa: BLE001 - never trust the payload
            return []
    if isinstance(raw, str):
        return _coerce_text(raw)
    if isinstance(raw, dict):
        raw = raw.get("proposals", raw)
        if isinstance(raw, dict):
            raw = [raw]
    if not isinstance(raw, list):
        return []
    proposals: list[InputValueProposal] = []
    for item in raw:
        proposal = _coerce_item(item)
        if proposal is not None:
            proposals.append(proposal)
    return proposals


def _coerce_text(content: str) -> list[InputValueProposal]:
    """Parse a text-envelope response leniently; never raises."""
    text = (content or "").strip()
    if not text:
        return []
    try:
        return _coerce_proposals(json.loads(text))
    except json.JSONDecodeError:
        pass
    try:
        start = text.index("{")
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except (ValueError, json.JSONDecodeError):
        return []
    return _coerce_proposals(obj)


def _is_empty_proposal_list(raw) -> bool:
    """True when the model answered with a valid, deliberately empty list."""
    model_dump = getattr(raw, "model_dump", None)
    if callable(model_dump):
        try:
            raw = model_dump(mode="json")
        except Exception:  # noqa: BLE001 - never trust the payload
            return False
    if isinstance(raw, list):
        return not raw
    if isinstance(raw, dict):
        items = raw.get("proposals")
        return isinstance(items, list) and not items
    return False


def _resolve_proposals(
    bound, raw_llm, messages
) -> tuple[list[InputValueProposal], str]:
    """One attempt: function call, else text-envelope fallback.

    Returns the proposals that could be salvaged plus a short statement of
    what went wrong (empty when the response was usable). Never raises.
    """
    problem = ""
    try:
        parsed = bound.invoke(messages)
    except Exception as exc:  # noqa: BLE001 - transport failure
        parsed = None
        problem = f"the structured attempt failed: {exc}"
    else:
        if parsed is not None:
            proposals = _coerce_proposals(parsed)
            if proposals:
                return proposals, ""
            if _is_empty_proposal_list(parsed):
                return [], ""
            # A non-empty answer that carried nothing usable: let the text
            # retry try again before reporting a problem.
    try:
        answer = raw_llm.invoke(messages)
    except Exception as exc:  # noqa: BLE001 - transport failure
        return [], problem or f"the text retry failed: {exc}"
    if isinstance(answer, dict):
        proposals = _coerce_proposals(answer)
    else:
        content = getattr(answer, "content", "") or ""
        proposals = _coerce_proposals(content)
    if proposals:
        return proposals, ""
    return [], problem or "the model returned no usable proposals"


def _validate_proposal(
    entry: PendingInput,
    value: str,
    definition: ModuleInputDefinition,
) -> tuple[bool, str]:
    """Decode one proposed value with the contract decoder."""
    try:
        decode_parameter_value(definition, value)
    except ParameterDecodeError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 - never trust the payload
        return False, f"Value could not be validated: {exc}."
    return True, ""


def suggest_pending_inputs(
    *,
    pending: list[PendingInput],
    context: dict | None = None,
    registry: VerifiedModuleRegistry | None = None,
    artifact_available: bool = True,
    llm=None,
    raw_llm=None,
    max_attempts: int = MAX_SUGGESTION_ATTEMPTS,
) -> InputSuggestionResult:
    """Propose values for the pending inputs of a plan.

    ``pending`` is the plan's list of required inputs the architecture
    could not derive. ``context`` carries the session's own request,
    requirements, capacity model and components, so the proposal follows
    the user's stated intent instead of a generic default.

    Returns suggestions, the inputs that were deliberately left to the
    operator (with the reason), and notes. Never raises for a refused
    proposal: a refused value is reported, not guessed around.
    """
    context = context or {}
    active_registry = registry or build_default_registry()

    suggestions: list[InputSuggestion] = []
    skipped: list[SkippedInput] = []
    notes: list[str] = []

    proposable: list[PendingInput] = []
    definitions: dict[tuple[str, str], ModuleInputDefinition] = {}

    for entry in pending:
        kind = _kind(entry.kind)
        if kind is None:
            skipped.append(SkippedInput(
                entry.resource_id, entry.name,
                f"Unknown resource kind {entry.kind!r}; the contract cannot "
                "be resolved for it.",
            ))
            continue
        human_only = _HUMAN_ONLY.get((kind, entry.name))
        if human_only is not None:
            skipped.append(SkippedInput(entry.resource_id, entry.name, human_only))
            continue
        artifact_default = _ARTIFACT_DEFAULTS.get((kind, entry.name))
        if artifact_default is not None:
            if not artifact_available:
                skipped.append(SkippedInput(
                    entry.resource_id, entry.name,
                    "The packaged demo artifact is not available in this "
                    "checkout, so its runtime, handler and key schema cannot "
                    "be stated; enter the values that match your own code.",
                ))
                continue
            value, rationale = artifact_default
            suggestions.append(InputSuggestion(
                resource_id=entry.resource_id,
                name=entry.name,
                value=value,
                rationale=rationale,
                source=SOURCE_PLATFORM_DEFAULT,
            ))
            continue
        contract = _contract_for(kind, active_registry)
        if contract is None:
            skipped.append(SkippedInput(
                entry.resource_id, entry.name,
                "No verified module contract covers this resource, so the "
                "value cannot be validated.",
            ))
            continue
        definition = definition_for_parameter(contract, entry.name)
        if definition is None:
            skipped.append(SkippedInput(
                entry.resource_id, entry.name,
                "The module contract does not accept this input, so no "
                "value can be proposed for it.",
            ))
            continue
        proposable.append(entry)
        definitions[(entry.resource_id, entry.name)] = definition

    if not proposable:
        return InputSuggestionResult(
            suggestions=tuple(suggestions),
            skipped=tuple(skipped),
            notes=tuple(notes),
        )

    if len(proposable) > MAX_SUGGESTED_INPUTS:
        proposable = proposable[:MAX_SUGGESTED_INPUTS]
        notes.append(
            f"Only the first {MAX_SUGGESTED_INPUTS} pending inputs were "
            "considered; the rest stay for manual entry."
        )

    if llm is None or raw_llm is None:
        from ai_architect.infrastructure.llm.factory import create_llm
        from ai_architect.infrastructure.llm.profiles import LLMProfile

        try:
            base = create_llm(LLMProfile.MEDIUM)
            if llm is None:
                llm = base.with_structured_output(
                    InputValueProposals, method="function_calling"
                )
            if raw_llm is None:
                raw_llm = base
        except Exception as exc:  # noqa: BLE001 - configuration failure
            return _without_ai(
                proposable, suggestions, skipped, notes, str(exc)
            )

    human = json.dumps({
        "context": _context_payload(context),
        "inputs_to_propose": _proposal_payload(proposable, definitions),
    }, indent=2, default=str)

    base_messages = [SystemMessage(content=_SUGGESTION_SYSTEM)]
    errors: list[str] = []
    parse_problem = ""
    accepted: dict[tuple[str, str], InputValueProposal] = {}
    rejections: dict[tuple[str, str], str] = {}

    for attempt in range(1, max(1, max_attempts) + 1):
        messages = list(base_messages)
        messages.append(HumanMessage(content=human))
        if errors:
            messages.append(HumanMessage(
                content=(
                    "Your previous proposals were REJECTED for the inputs "
                    "listed below. Propose corrected values ONLY for those "
                    "inputs and repeat your accepted values unchanged. "
                    "Rejection reasons:\n" + "\n".join(errors[:12])
                ),
            ))
        elif parse_problem:
            messages.append(HumanMessage(
                content=(
                    "Your previous response could not be used: "
                    f"{parse_problem}. Respond with ONLY the JSON object in "
                    "the exact shape shown and give a concrete value for "
                    "every input listed."
                ),
            ))
        proposals, parse_problem = _resolve_proposals(llm, raw_llm, messages)
        errors = []
        for proposal in proposals:
            key = (proposal.resource_id, proposal.name)
            entry = next(
                (
                    item for item in proposable
                    if item.resource_id == proposal.resource_id
                    and item.name == proposal.name
                ),
                None,
            )
            if entry is None:
                # The model answered for something it was not asked about.
                continue
            if key in accepted:
                continue
            definition = definitions[key]
            ok, reason = _validate_proposal(entry, proposal.value, definition)
            if not ok:
                rejections[key] = reason
                errors.append(
                    f"{proposal.resource_id} Â· {proposal.name}: {reason}"
                )
                continue
            accepted[key] = proposal
        if not errors and not parse_problem:
            break

    if parse_problem:
        if accepted:
            notes.append(
                "The model's answer was only partly usable: "
                f"{parse_problem}. The inputs below are the values that "
                "could be validated; the rest stay for manual entry."
            )
        else:
            notes.append(
                "AI suggestions are unavailable right now "
                f"({parse_problem}). The platform defaults below still "
                "apply; enter the remaining values manually."
            )

    for entry in proposable:
        key = (entry.resource_id, entry.name)
        proposal = accepted.get(key)
        if proposal is None:
            reason = rejections.get(
                key,
                "No value was proposed for this input; enter it manually.",
            )
            skipped.append(SkippedInput(
                entry.resource_id,
                entry.name,
                reason,
            ))
            continue
        suggestions.append(InputSuggestion(
            resource_id=entry.resource_id,
            name=entry.name,
            value=proposal.value,
            rationale=proposal.rationale
            or "Proposed from the requirements of this design.",
            source=SOURCE_AI_SUGGESTION,
        ))

    return InputSuggestionResult(
        suggestions=tuple(suggestions),
        skipped=tuple(skipped),
        notes=tuple(notes),
    )


def _without_ai(
    proposable: list[PendingInput],
    suggestions: list[InputSuggestion],
    skipped: list[SkippedInput],
    notes: list[str],
    reason: str,
) -> InputSuggestionResult:
    """Degrade to platform defaults when the model cannot be reached.

    Suggestions are advisory; a model outage must never turn the panel
    into an error. The operator still gets every platform-owned value and
    an explicit reason for the rest.
    """
    notes.append(
        "AI suggestions are unavailable right now "
        f"({reason}). The platform defaults below still apply; enter the "
        "remaining values manually."
    )
    for entry in proposable:
        skipped.append(SkippedInput(
            entry.resource_id,
            entry.name,
            "No automatic suggestion is available; enter this value manually.",
        ))
    return InputSuggestionResult(
        suggestions=tuple(suggestions),
        skipped=tuple(skipped),
        notes=tuple(notes),
    )

