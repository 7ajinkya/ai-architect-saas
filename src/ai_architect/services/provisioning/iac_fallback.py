"""Controlled LLM Terraform fallback for components outside the verified catalog.

Priority stays verified-first: this stage runs ONLY for architecture
nodes that ``match_contract()`` refuses, and only after design
approval (``plan_builder`` invokes it post-approval, post-selection).

The LLM PROPOSES structured resource specs; it never writes HCL,
never executes anything, never provisions. Deterministic code
validates, renders, guards, and hash-binds everything downstream
exactly like verified content:

    propose (LLM, strict JSON schema, bounded: 1 + 1 correction)
      -> validate_fallback_proposal (deterministic policy)
      -> render_fallback_file (deterministic HCL into fallback.tf)
      -> existing guard (union vocabulary) -> fmt/init/validate/plan
      -> generated[] metadata + tree_hash binding (existing approval)

Safety properties (enforced in code, never by prompt alone):

- Closed type policy: ``FALLBACK_POLICY_TYPES`` only. Anything else
  is refused, no matter how plausible.
- Standalone resources only: no references outside the proposal, no
  ``provider``/``count``/``for_each``/``lifecycle``/``depends_on``
  meta-arguments, no provisioners (structurally unemittable: the
  renderer has no code path for them).
- No fabricated remotes: URL- and ARN-shaped values are refused (a
  repository URL or account ARN can only come from the operator, and
  fallback v1 has no human-input channel, so any such value is
  invented by definition).
- No secrets: credential-shaped names/values are refused.
- Deterministic labels derived from the node id (never LLM-chosen);
  intra-proposal references use closed ``$ref`` indices resolved by
  code against declared labels.
- Any failure fails that component closed with an explicit
  diagnostic; a failed component suppresses the whole tree
  (all-or-nothing, matching selection philosophy). Nothing is
  silently dropped: uncovered resources are reported with reasons.

Known v1 boundaries (documented, not hidden): at most 4 resources per
component; flat scalar/list attributes only (no nested blocks or
maps); no human-supplied values (a component whose valid config
requires operator text the proposal cannot know fails closed at
``terraform validate`` with the provider's own message).
"""

import hashlib
import json
import math
import re

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

from ai_architect.services.cost.pricing_service import normalize_component_name
from ai_architect.services.provisioning.registry import (
    VENDOR_PREFIXES,
    UnsupportedResourceError,
    match_contract,
)
from ai_architect.services.provisioning.spec_builder import (
    is_bare_iam_node,
)
from ai_architect.services.provisioning.terraform_guard import (
    looks_like_secret,
)

# Explicit Terraform safety policy for LLM-proposed resources. Every
# entry is justified; extension is a reviewed change, never inference.
# All four are configuration-only surface (static hosting config,
# observability config): no compute, no shell, no network ingress
# rules, no IAM, no provisioners.
FALLBACK_POLICY_TYPES: frozenset = frozenset({
    # Static frontend hosting: app container + branch target.
    "aws_amplify_app",
    "aws_amplify_branch",
    # Observability configuration: trace grouping + sampling config.
    "aws_xray_group",
    "aws_xray_sampling_rule",
})

FALLBACK_POLICY_NOTES: dict = {
    "aws_amplify_app": "static hosting app record (name/description only)",
    "aws_amplify_branch": "branch deployment target, app-bound by ref",
    "aws_xray_group": "trace group filter (name/filter only)",
    "aws_xray_sampling_rule": "sampling tuning (literals only)",
}

# At most this many resources per component: a fallback covers one
# component's realizable surface; more indicates scope creep.
MAX_RESOURCES_PER_COMPONENT = 4

# Meta-arguments that would escape the standalone sandbox. Rejected
# as keys even though the renderer could never emit most of them:
# defense in depth, with an explicit message.
_FORBIDDEN_KEYS = frozenset({
    "provider",
    "count",
    "for_each",
    "lifecycle",
    "depends_on",
})

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,64}$")
_URL_RE = re.compile(r"https?://", re.IGNORECASE)
_ARN_RE = re.compile(r"\barn:[a-z0-9-]+:", re.IGNORECASE)
_REF_ATTRS = frozenset({"id", "arn", "name"})

_MAX_STRING = 256
_MAX_LIST_ITEMS = 16
_MAX_INT = 2 ** 31 - 1


class FallbackError(ValueError):
    """A fallback component failed closed (proposal/validation/render)."""


class FallbackResourceProposal(BaseModel):
    """One LLM-proposed Terraform resource (strict boundary schema)."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(description="Terraform resource type (policy only)")
    attributes: dict = Field(
        default_factory=dict,
        description=(
            "Flat arguments only: strings, numbers, booleans, lists of "
            "scalars. No nested blocks or maps. Cross-references use "
            '{"$ref": [sibling_index, "id"|"arn"|"name"]} only.'
        ),
    )


class FallbackProposal(BaseModel):
    """LLM proposal for exactly one unsupported architecture component."""

    model_config = ConfigDict(extra="forbid")

    resources: list[FallbackResourceProposal] = Field(
        min_length=1, max_length=MAX_RESOURCES_PER_COMPONENT
    )


# Components the controlled fallback policy explicitly covers. The LLM
# may propose content ONLY for these: every other unregistered component
# is refused deterministically instead of being mapped to a different
# service (CloudFront, Route 53, and S3 static website hosting must
# never silently become Amplify or an object bucket).
FALLBACK_ELIGIBLE_COMPONENT_NAMES: frozenset = frozenset({
    "amplify",
    "amplify hosting",
    "x-ray",
    "x-ray tracing",
    "xray",
})


def fallback_refusal_reason(component_name: str) -> str | None:
    """Why this component is outside the fallback policy, or None.

    Normalization is the repository-wide one (lowercase, parenthetical
    aliases stripped, vendor prefix stripped), so "AWS Amplify
    (Hosting)" and "AWS Amplify Hosting" resolve identically. There is
    no fuzzy match and no capability inference.
    """
    normalized = normalize_component_name(component_name)
    for prefix in VENDOR_PREFIXES:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):].strip()
    if normalized in FALLBACK_ELIGIBLE_COMPONENT_NAMES:
        return None
    return (
        f"Fallback refused: component {component_name!r} is outside the "
        "controlled fallback policy (only AWS Amplify Hosting and AWS "
        "X-Ray are covered); it is reported unsupported instead of "
        "being mapped to a different service."
    )


def fallback_resource_id(node_id: str) -> str:
    """Deterministic resource identity for a fallback node.

    Refuses empty or label-unsafe node ids: identities are derived,
    never negotiated.
    """
    text = (node_id or "").strip()
    if not text:
        raise FallbackError(
            "Fallback refused: architecture node has an empty node_id, "
            "so no deterministic resource identity can be derived."
        )
    resource_id = f"iac_fallback-{text}"
    if not _LABEL_RE.fullmatch(resource_id):
        raise FallbackError(
            f"Fallback refused: derived identity {resource_id!r} is not "
            "a safe Terraform label."
        )
    return resource_id


def _fallback_label(resource_id: str, index: int) -> str:
    """Deterministic HCL label for the index-th sibling resource."""
    return f"{resource_id}-{index + 1}"


def partition_architecture(architecture):
    """Split nodes into (supported, unsupported, iam_skipped).

    Supported means ``match_contract`` resolves; unsupported means it
    raises ``UnsupportedResourceError`` (fallback candidates); bare-IAM
    nodes are skipped exactly like ``build_infra_spec`` does. Anything
    else (missing names etc.) raises, preserving fail-closed behavior
    for malformed input. Never calls the LLM.
    """
    supported = []
    unsupported = []
    iam_skipped = []
    for node in architecture.nodes or []:
        if is_bare_iam_node(node.component_name):
            iam_skipped.append(node)
            continue
        try:
            match_contract(node.component_name)
        except UnsupportedResourceError:
            unsupported.append(node)
        else:
            supported.append(node)
    return supported, unsupported, iam_skipped


def _check_scalar(name: str, value) -> list:
    """Validate one scalar attribute value. Returns error strings."""
    if isinstance(value, bool):
        return []
    if isinstance(value, int):
        if not (-_MAX_INT <= value <= _MAX_INT):
            return [f"{name!r} integer is out of range."]
        return []
    if isinstance(value, float):
        if not math.isfinite(value) or abs(value) >= 1e12:
            return [f"{name!r} number is not finite or too large."]
        return []
    if isinstance(value, str):
        if len(value) > _MAX_STRING:
            return [f"{name!r} string exceeds {_MAX_STRING} characters."]
        if "${" in value or "<<" in value:
            return [
                f"{name!r} contains an interpolation/heredoc construct."
            ]
        if "\n" in value or "\r" in value:
            return [f"{name!r} contains a line break."]
        if _URL_RE.search(value):
            return [
                f"{name!r} is URL-shaped: remote locators can only come "
                "from the operator, never from a proposal."
            ]
        if _ARN_RE.search(value):
            return [
                f"{name!r} is ARN-shaped: account ARNs can only come "
                "from the operator, never from a proposal."
            ]
        if looks_like_secret(name, value):
            return [f"{name!r} looks like a credential; refused."]
        return []
    return [f"{name!r} has an unsupported value type."]


def _check_ref(value, index: int, count: int) -> list:
    """Validate one {"$ref": [sibling, attr]} reference."""
    if not isinstance(value, dict) or set(value.keys()) != {"$ref"}:
        return ["malformed $ref (expected {\"$ref\": [index, attr]})."]
    target = value["$ref"]
    if (
        not isinstance(target, list)
        or len(target) != 2
        or not isinstance(target[0], int)
        or isinstance(target[0], bool)
        or not isinstance(target[1], str)
    ):
        return ["malformed $ref (expected [sibling_index, attribute])."]
    sibling, attr = target
    if not 0 <= sibling < count:
        return [f"$ref sibling index {sibling} is out of range."]
    if sibling == index:
        return ["$ref to the same resource is pointless; refused."]
    if attr not in _REF_ATTRS:
        return [
            f"$ref attribute {attr!r} is not exposable; "
            f"allowed: {sorted(_REF_ATTRS)}."
        ]
    return []


def validate_fallback_proposal(proposal, *, component_name: str):
    """Deterministically validate a proposal. Returns (specs, errors).

    ``specs`` is a list of ``{"type": ..., "attributes": {...}}`` with
    refs preserved as ``{"$ref": [index, attr]}`` for the renderer.
    Never raises for content reasons: every rejection is an error
    string naming the exact violation.
    """
    try:
        bound = FallbackProposal.model_validate(proposal)
    except Exception as exc:
        return [], [f"Proposal is not valid fallback JSON: {exc}."]
    errors = []
    specs = []
    count = len(bound.resources)
    for index, item in enumerate(bound.resources):
        if item.type not in FALLBACK_POLICY_TYPES:
            errors.append(
                f"Resource {index} type {item.type!r} is outside the "
                "fallback safety policy."
            )
            continue
        if not isinstance(item.attributes, dict):
            errors.append(f"Resource {index} attributes must be a map.")
            continue
        clean = {}
        for key, value in item.attributes.items():
            if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
                errors.append(
                    f"Resource {index} argument name {key!r} is unsafe."
                )
                continue
            if key in _FORBIDDEN_KEYS or "provisioner" in key:
                errors.append(
                    f"Resource {index} argument {key!r} would escape "
                    "the standalone sandbox; refused."
                )
                continue
            if isinstance(value, dict):
                if set(value.keys()) == {"$ref"}:
                    ref_errors = _check_ref(value, index, count)
                    if ref_errors:
                        errors.extend(
                            f"Resource {index} {message}"
                            for message in ref_errors
                        )
                        continue
                    clean[key] = {"$ref": [value["$ref"][0], value["$ref"][1]]}
                    continue
                errors.append(
                    f"Resource {index} argument {key!r}: nested blocks "
                    "and maps are not expressible in fallback v1."
                )
                continue
            if isinstance(value, list):
                if len(value) > _MAX_LIST_ITEMS:
                    errors.append(
                        f"Resource {index} argument {key!r} list is too "
                        "long."
                    )
                    continue
                bad = False
                for entry in value:
                    entry_errors = _check_scalar(key, entry)
                    if entry_errors or isinstance(entry, (dict, list)):
                        bad = True
                        break
                if bad:
                    errors.append(
                        f"Resource {index} argument {key!r} list holds "
                        "an unsafe value."
                    )
                    continue
                clean[key] = list(value)
                continue
            scalar_errors = _check_scalar(key, value)
            if scalar_errors:
                errors.extend(
                    f"Resource {index} {message}" for message in scalar_errors
                )
                continue
            clean[key] = value
        specs.append({"type": item.type, "attributes": clean})
    if errors:
        return [], errors
    return specs, []


def proposal_sha256(specs) -> str:
    """Content hash over the normalized specs (sorted, canonical JSON)."""
    import hashlib

    canonical = json.dumps(
        specs, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _hcl_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _hcl_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return _hcl_string(str(value))


def render_fallback_file(entries) -> str:
    """Deterministically render fallback entries into fallback.tf.

    ``entries`` is a list of ``(resource_id, specs)`` with specs as
    returned by ``validate_fallback_proposal``. Labels derive from the
    resource identity (never LLM-chosen); ``$ref`` indices resolve
    against sibling labels. Output is fmt-canonical (aligned ``=``,
    2-space indent, trailing newline) so ``terraform fmt -check``
    passes by construction; the real fmt run still verifies.
    """
    seen_labels = set()
    blocks = []
    for resource_id, specs in entries:
        for index, spec in enumerate(specs):
            label = _fallback_label(resource_id, index)
            if label in seen_labels:
                raise FallbackError(
                    f"Fallback label collision on {label!r}; refusing "
                    "ambiguous references."
                )
            seen_labels.add(label)
            blocks.append((spec["type"], label, index, specs))
    label_of = {}
    for resource_id, specs in entries:
        for index, _ in enumerate(specs):
            label_of[(resource_id, index)] = _fallback_label(
                resource_id, index
            )

    lines = []
    for resource_id, specs in entries:
        for index, spec in enumerate(specs):
            lines.append(
                f'resource "{spec["type"]}" '
                f'"{_fallback_label(resource_id, index)}" {{'
            )
            items = []
            for key in sorted(spec["attributes"]):
                value = spec["attributes"][key]
                if isinstance(value, dict) and "$ref" in value:
                    sibling, attr = value["$ref"]
                    target_label = label_of[(resource_id, sibling)]
                    target_type = specs[sibling]["type"]
                    items.append(
                        (key, f"{target_type}.{target_label}.{attr}")
                    )
                elif isinstance(value, list):
                    items.append(
                        (key, "[" + ", ".join(
                            _hcl_scalar(v) for v in value
                        ) + "]")
                    )
                else:
                    items.append((key, _hcl_scalar(value)))
            width = max((len(k) for k, _ in items), default=0)
            for key, rendered in items:
                lines.append(f"  {key.ljust(width)} = {rendered}")
            lines.append("}")
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


_FALLBACK_SYSTEM = """You propose AWS Terraform resource configurations as strict JSON. Rules:
- Provider is AWS only, in the fixed region given below (never emit region/provider arguments).
- Allowed resource types ONLY: aws_amplify_app, aws_amplify_branch, aws_xray_group, aws_xray_sampling_rule.
- Minimal attributes only: names and behavior literals the component needs. No tags.
- Flat scalars or lists of scalars. No nested blocks or maps.
- Cross-references between your own resources ONLY, as {"$ref": [sibling_index, "id"|"arn"|"name"]}.
- NEVER write repository URLs, account ARNs, credentials, secrets, tokens, or keys: any such value is refused. Omit arguments you cannot know instead of inventing them.
- NEVER emit count, for_each, lifecycle, depends_on, provider, provisioner, or local-exec/remote-exec content.
- At most 4 resources. Respond with ONLY the JSON object.
- Exact response shape (no prose, no code fences, attributes nested
  under "attributes", never flat):
  {"resources": [{"type": "<allowed type>",
                  "attributes": {"arg_name": "value"}}}]
- Cross-reference example:
  {"resources": [{"type": "aws_amplify_app",
                  "attributes": {"name": "frontend"}},
                 {"type": "aws_amplify_branch",
                  "attributes": {"branch_name": "main",
                                 "app_id": {"$ref": [0, "id"]}}}]}"""


def _proposal_prompt(*, component: dict, context: dict) -> tuple:
    """System + human messages for one fallback component."""
    human = {
        "component_name": component["component_name"],
        "node_id": component["node_id"],
        "category": component.get("category", ""),
        "role_in_system": component.get("role_in_system", ""),
        "provider": context["provider"],
        "region_code": context["region_code"],
        "design_artifact_id": context.get("design_artifact_id", ""),
        "evidence_refs": list(context.get("evidence_refs", []))[:12],
    }
    return _FALLBACK_SYSTEM, json.dumps(human, sort_keys=True, indent=2)


def _parse_proposal(raw) -> dict:
    """Coerce one LLM output into a proposal dict. Raises FallbackError."""
    if isinstance(raw, dict):
        return raw
    model_dump = getattr(raw, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(mode="json")
        except Exception as exc:
            raise FallbackError(
                f"Fallback proposal is not serializable: {exc}."
            ) from exc
        if isinstance(dumped, dict):
            return dumped
    raise FallbackError(
        "Fallback proposal has an unrecognized shape; refusing to guess."
    )


def _parse_text_envelope(content: str) -> dict:
    """Parse a text-envelope model response as strict fallback JSON.

    Used ONLY when the model answered in plain text instead of
    emitting the requested function call (some providers do not
    support structured output). The text must still decode to the
    strict proposal shape; anything else raises loudly.
    """
    text = (content or "").strip()
    if not text:
        raise FallbackError(
            "Fallback model returned no function call and empty text; "
            "nothing to parse."
        )
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    try:
        start = text.index("{")
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
        if isinstance(obj, dict):
            return obj
    except Exception as exc:
        raise FallbackError(
            "Fallback model text is not valid proposal JSON: "
            f"{exc}. Preview: {text[:300]!r}"
        ) from exc
    raise FallbackError(
        "Fallback model text decoded to a non-object; refusing to guess."
    )


def _resolve_parsed(bound, raw_llm, messages) -> dict:
    """One attempt: function call, else text-envelope fallback.

    Mirrors the critic's transport discipline: an absent tool call is
    a transport limitation, retried once without tools through the
    same strict schema — never a license to guess.
    """
    try:
        parsed = bound.invoke(messages)
    except Exception as exc:
        raise FallbackError(
            f"Fallback LLM invocation failed: {exc}."
        ) from exc
    if parsed is not None:
        return _parse_proposal(parsed)
    content = ""
    try:
        answer = raw_llm.invoke(messages)
        content = getattr(answer, "content", "") or ""
        if isinstance(answer, dict):
            return _parse_proposal(answer)
    except Exception as exc:
        raise FallbackError(
            f"Fallback text retry failed: {exc}."
        ) from exc
    return _parse_text_envelope(content)


def generate_fallback_content(
    *,
    component: dict,
    context: dict,
    llm=None,
    raw_llm=None,
    max_attempts: int = 2,
    prior_errors: list | None = None,
) -> tuple:
    """Propose + validate one component's fallback specs (bounded).

    ``llm`` is the structured-output runnable (tests inject fakes
    returning proposal dicts); ``raw_llm`` is the plain model used
    when the provider cannot make the function call (tests inject
    fakes returning text). Production builds both from the active
    provider. ``prior_errors`` seeds the first attempt as a correction
    (used when a later gate, e.g. ``terraform validate``, rejected a
    previous proposal); otherwise the first attempt is fresh.
    Returns ``(specs, proposal_sha256)``. Raises ``FallbackError``
    after ``max_attempts`` LLM calls. Transport/parse failures fail
    closed immediately with no retry budget spent on guessing.
    """
    if max_attempts < 1:
        raise FallbackError("Fallback requires at least one attempt.")
    refusal = fallback_refusal_reason(
        str((component or {}).get("component_name", ""))
    )
    if refusal is not None:
        raise FallbackError(refusal)
    if llm is None or raw_llm is None:
        from ai_architect.infrastructure.llm.factory import create_llm
        from ai_architect.infrastructure.llm.profiles import LLMProfile

        try:
            base = create_llm(LLMProfile.MEDIUM)
            if llm is None:
                llm = base.with_structured_output(
                    FallbackProposal, method="function_calling"
                )
            if raw_llm is None:
                raw_llm = base
        except Exception as exc:
            raise FallbackError(
                f"Fallback LLM is not configured: {exc}."
            ) from exc
    system, human = _proposal_prompt(component=component, context=context)
    base_messages = [SystemMessage(content=system)]
    errors: list[str] = list(prior_errors or [])
    for attempt in range(1, max_attempts + 1):
        if attempt == 1 and not errors:
            messages = base_messages + [HumanMessage(content=human)]
        else:
            messages = base_messages + [
                HumanMessage(content=human),
                HumanMessage(
                    content=(
                        "Your previous proposal was REJECTED. Repair ONLY "
                        "the rejected parts and resubmit the FULL proposal "
                        "with every other field byte-identical. "
                        "Rejection reasons:\n"
                        + "\n".join(errors[:8])
                    ),
                ),
            ]
        try:
            raw_proposal = _resolve_parsed(llm, raw_llm, messages)
            specs, errors = validate_fallback_proposal(
                raw_proposal,
                component_name=component["component_name"],
            )
        except FallbackError as exc:
            raise exc
        except Exception as exc:
            errors = [f"Proposal handling failed: {exc}."]
            specs = []
        if not errors:
            return specs, proposal_sha256(specs)
    raise FallbackError(
        "Fallback proposal failed deterministic validation after "
        f"{max_attempts} attempt(s): " + "; ".join(errors[:8])
    )
