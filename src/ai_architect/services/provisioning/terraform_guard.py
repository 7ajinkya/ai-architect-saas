"""Structural safety guard for generated Terraform.

The guard is the last thing that runs before Terraform source becomes
executable. It is deliberately independent of the renderer: it re-reads
the produced source, parses its block structure, and allows only a
closed vocabulary derived from the Slice 4A registry. If the structure
cannot be parsed or established, the guard **fails closed**.

Design notes:

- Structural, not substring matching. Top-level block types, resource
  types, nested block types and traversals are parsed from the source.
- Self-contained on purpose. This is a security boundary, so it does not
  import the renderer's credential patterns; a change to the renderer
  must not be able to weaken the guard.
- No resource type can enter the output unless the Slice 4A registry
  knows the corresponding provisioning kind. There is no path from an
  LLM or user string to a Terraform type.
- ``local-exec`` / ``remote-exec`` / provisioners / managed policies are
  not merely discouraged: their block types are absent from the allowed
  vocabulary, and ``aws_iam_role_policy`` /
  ``aws_iam_role_policy_attachment`` are rejected outright.
- ``module`` blocks are denied by default. They become representable
  only through an explicit ``module_policy`` of digest-pinned registry
  sources: the ``source`` must equal a pinned string exactly, every
  argument must be a declared input of that module, references may only
  target declared module instances and declared outputs, and a module's
  in-tree files are exempted from structural parsing only after their
  bytes re-verify against the pinned digest. The guard recomputes that
  digest itself, mirroring ``module_registry``'s canonical payload by
  convention; any divergence refuses, which is the safe direction.
"""

import hashlib
import json
import re
from typing import Optional

from ai_architect.domain.provisioning.render import (
    RenderDiagnosticCategory,
    RenderDiagnostic,
    RenderErrorCode,
)

# Top-level block types a generated tree may contain.
ALLOWED_TOP_LEVEL = frozenset(
    {"terraform", "provider", "resource", "variable", "output", "locals"}
)

# Nested block types that would grant execution or embed external input.
FORBIDDEN_BLOCK_TYPES = frozenset(
    {
        "provisioner",
        "local-exec",
        "remote-exec",
        "connection",
        "external",
        "dynamic",
        "module",
        "import",
        "lifecycle",
        "moved",
        "removed",
        "check",
        "ephemeral",
    }
)

# Resource types that are never renderable: execution hooks, null
# provider escape hatches, and IAM surfaces broader than a single
# inline role policy. Two types were deliberately ADMITTED for the
# bounded demo path, each justified:
# - aws_iam_role_policy: inline least-privilege policies generated
#   deterministically (CloudWatch Logs on one explicit group;
#   DynamoDB read/write on the spec's tables only). Managed policies,
#   attachments, and user/group policy surfaces stay forbidden.
# - aws_lambda_permission: fixed principal apigateway.amazonaws.com
#   with source_arn constrained to the same tree's API execution ARN.
#   No other principal or open invocation is expressible.
FORBIDDEN_RESOURCE_TYPES = frozenset(
    {
        "null_resource",
        "null_data_source",
        "terraform_data",
        "aws_iam_role_policy_attachment",
        "aws_iam_user_policy",
        "aws_iam_user_policy_attachment",
        "aws_iam_group_policy",
        "aws_iam_group_policy_attachment",
        "aws_iam_policy",
        "aws_iam_policy_attachment",
        "aws_s3_bucket_policy",
        "aws_iam_instance_profile",
    }
)

# Constructs that can smuggle arbitrary text or evaluation into HCL.
_FORBIDDEN_CONSTRUCTS = (
    ("${", "interpolation"),
    ("<<", "heredoc"),
    ("\r", "carriage return"),
)

_SECRET_NAME = re.compile(
    r"(password|passwd|secret|token|private[_-]?key|access[_-]?key"
    r"|credential|authorization|api[_-]?key)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(AKIA[0-9A-Z]{16}|-----BEGIN[A-Z ]*PRIVATE KEY-----|"
    r"aws_secret_access_key\s*[=:]|aws_session_token\s*[=:])",
    re.IGNORECASE,
)

# A traversal such as aws_s3_bucket.s3-bucket.id
_TRAVERSAL = re.compile(
    r"(?<![A-Za-z0-9_.-])([a-z][a-z0-9_]*)\.([A-Za-z][A-Za-z0-9_-]*)"
    r"\.([a-z][a-z0-9_]*)"
)

# String literals, stripped before the traversal scan: dotted names
# inside quotes (service principals such as "apigateway.amazonaws.com",
# "$context.identity.sourceIp" inside a log format string) are data,
# never resource references. Genuine references are bare HCL.
_STRING_LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"')


class GuardViolation(Exception):
    """The guard refused the source."""


# Nested block types the renderer may emit, closed by hand and reviewed.
# `environment` carries only the derived TABLE_NAME variable;
# `access_log_settings` carries only the log-group ARN and a static
# format string. Neither takes operator text.
ALLOWED_NESTED_BLOCK_TYPES = frozenset(
    {
        "required_providers",
        "required_version",
        "versioning_configuration",
        "rule",
        "apply_server_side_encryption_by_default",
        "attribute",
        "point_in_time_recovery",
        "tags",
        "environment",
        "access_log_settings",
        "logging_config",
    }
)

# Object-form maps the renderer may emit, each bound to its parent:
# `variables = {...}` is allowed ONLY inside `environment`. Anywhere
# else it is the provider-object smuggling the base rule exists to
# stop.
ALLOWED_OBJECT_BLOCKS = frozenset({("variables", "environment")})

# --- module policy ---------------------------------------------------
# ``module`` blocks stay forbidden unless the caller supplies a policy
# keyed by the exact rendered ``source`` string. Each value is:
#
#     {"tree_dir": <repo-relative dir copied into the tree>,
#      "digest": <sha256 pin over the dir's .tf files>,
#      "inputs": frozenset of declared module variables,
#      "outputs": frozenset of declared module outputs}
#
# Files directly under a pinned ``tree_dir`` are exempt from structural
# parsing (verified module content legitimately contains constructs the
# closed vocabulary refuses, such as ``dynamic`` blocks) but only after
# the guard recomputes their content digest and it equals the pin.
# Everything else about module blocks is validated here: exact source,
# flat arguments only, arguments a subset of the declared inputs, and
# references limited to declared instances and declared outputs.

_MODULE_HEADER = re.compile(r'^module\s+"([^"]+)"\s*\{\s*$')
_MODULE_ASSIGN = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$')
_MODULE_LABEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_QUOTED_STRING = re.compile(r'^"([^"]*)"$')


def _modules_canonical(payload) -> str:
    """Canonical JSON mirroring module_registry's digest convention."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )


def _module_content_digest(entries) -> str:
    """sha256 over (basename, content) pairs, sorted by basename.

    ``module_registry.module_source_digest`` sorts its directory
    listing by path; for the flat, lowercase ``.tf`` names the catalog
    pins, basename order is the same order. A convention mismatch
    makes the comparison fail, which refuses (the safe direction).
    """
    payload = [
        {"path": name, "content": content}
        for name, content in sorted(entries)
    ]
    return hashlib.sha256(
        _modules_canonical(payload).encode("utf-8")
    ).hexdigest()


def _scan_module_blocks(text: str) -> tuple:
    """Line-scan ``module`` blocks into (instances, problems).

    ``instances`` is a list of ``(label, source, arguments)`` with
    ``arguments`` mapping argument name -> raw value text. Only the
    flat canonical form the renderer emits is recognized; anything
    else is a problem string. This is a second, independent reading of
    the same text ``_parse_blocks`` parses, so structure smuggled into
    a module body cannot hide behind the header parser.
    """
    instances = []
    problems = []
    label = None
    source = None
    arguments = {}
    for raw in text.splitlines():
        line = raw.strip()
        if label is None:
            match = _MODULE_HEADER.match(line)
            if match:
                label = match.group(1)
                source = None
                arguments = {}
            continue
        if line == "}":
            instances.append((label, source, arguments))
            label = None
            continue
        if not line or line.startswith("#"):
            continue
        if line.endswith("{"):
            problems.append(
                f"Module block {label!r} contains a nested block or "
                f"object; only flat argument assignments are permitted."
            )
            continue
        match = _MODULE_ASSIGN.match(line)
        if not match:
            problems.append(
                f"Module block {label!r} contains an unrecognized line; "
                f"refusing because safety could not be established."
            )
            continue
        key, value = match.group(1), match.group(2)
        if key == "source":
            if source is not None:
                problems.append(
                    f"Module block {label!r} sets source more than once."
                )
            source = value
            continue
        if key in arguments:
            problems.append(
                f"Module block {label!r} assigns {key!r} more than once."
            )
            continue
        arguments[key] = value
    if label is not None:
        problems.append(
            f"Module block {label!r} is unterminated; refusing because "
            f"safety could not be established."
        )
    return instances, problems


def _pinned_tree_files(source_tree: dict, policy: dict,
                       diagnostics: list) -> set:
    """Paths exempted as digest-verified module content.

    For each policy entry the files directly under its ``tree_dir``
    must exist, be ``.tf`` files, and digest to the pinned value; only
    then are they exempted from further scanning. Anything else adds a
    violation and exempts nothing for that entry.
    """
    exempted = set()
    for source in sorted(policy):
        entry = policy[source]
        tree_dir = entry["tree_dir"]
        prefix = f"{tree_dir}/"
        candidates = [
            path for path in sorted(source_tree)
            if path.startswith(prefix)
            and "/" not in path[len(prefix):]
            and path.endswith(".tf")
        ]
        if not candidates:
            diagnostics.append(
                _guard(
                    RenderErrorCode.GUARD_VIOLATION,
                    f"Module source {source!r} has no pinned content in "
                    f"the tree; refusing to render an unverified module.",
                )
            )
            continue
        entries = [
            (path[len(prefix):], source_tree[path]) for path in candidates
        ]
        if _module_content_digest(entries) != entry["digest"]:
            diagnostics.append(
                _guard(
                    RenderErrorCode.GUARD_VIOLATION,
                    f"Module source {source!r} content does not match "
                    f"the pinned content digest; refusing to trust it.",
                )
            )
            continue
        exempted.update(candidates)
    return exempted


def _guard(code, message) -> RenderDiagnostic:
    return RenderDiagnostic(
        category=RenderDiagnosticCategory.GUARD,
        code=code,
        message=message,
    )


def _parse_blocks(text: str) -> Optional[list]:
    """Parse HCL block structure.

    Returns a list of ``(type, labels, depth, parent_type, is_object)``
    tuples. ``is_object`` marks the ``name = {`` form used for provider
    objects; ``parent_type`` is the enclosing block's type.

    Returns None when braces are unbalanced or a block header is not
    recognizable; the caller treats that as fail closed.
    """
    blocks = []
    stack = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("{"):
            header = line[:-1].strip()
            match = re.fullmatch(
                r'([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)?((?:\s+"[^"]*")*)\s*',
                header,
            )
            if not match:
                return None
            block_type = match.group(1)
            labels = tuple(re.findall(r'"([^"]*)"', match.group(3)))
            parent_type = stack[-1] if stack else None
            blocks.append(
                (
                    block_type,
                    labels,
                    len(stack),
                    parent_type,
                    bool(match.group(2)),
                )
            )
            stack.append(block_type)
        elif line == "}":
            if not stack:
                return None
            stack.pop()
    if stack:
        return None
    return blocks


def _brace_balance(text: str) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not in_string


def looks_like_secret(name: str, value: str) -> bool:
    """Credential-shaped name or value detection (guard-local by design)."""
    return bool(
        _SECRET_NAME.search(name or "")
        or _SECRET_VALUE.search(value or "")
    )


def guard_terraform_source(
    source_tree: dict,
    *,
    allowed_resource_types: frozenset,
    module_policy: Optional[dict] = None,
) -> list:
    """Validate a generated Terraform tree against the closed vocabulary.

    Returns a list of :class:`RenderDiagnostic`; empty means the source is
    structurally safe to hand to Terraform. Raises nothing.
    ``module_policy`` opts into pinned ``module`` blocks; see the module
    docstring for the exact trust rules.
    """
    diagnostics = []

    if not source_tree:
        diagnostics.append(
            _guard(
                RenderErrorCode.GUARD_VIOLATION,
                "Guard received an empty Terraform source tree.",
            )
        )
        return diagnostics

    policy = {}
    for source, entry in (module_policy or {}).items():
        try:
            policy[source] = {
                "tree_dir": str(entry["tree_dir"]),
                "digest": str(entry["digest"]),
                "inputs": frozenset(entry["inputs"]),
                "outputs": frozenset(entry["outputs"]),
            }
        except (KeyError, TypeError):
            diagnostics.append(
                _guard(
                    RenderErrorCode.GUARD_VIOLATION,
                    f"Module policy entry {source!r} is malformed; "
                    f"refusing to trust it.",
                )
            )
    module_mode = module_policy is not None

    exempted = (
        _pinned_tree_files(source_tree, policy, diagnostics)
        if policy
        else set()
    )

    combined = []
    for path in sorted(source_tree):
        if path in exempted:
            continue
        content = source_tree[path]
        combined.append(content)
        for construct, label in _FORBIDDEN_CONSTRUCTS:
            if construct in content:
                diagnostics.append(
                    _guard(
                        RenderErrorCode.GUARD_VIOLATION,
                        f"{path} contains a forbidden {label} construct.",
                    )
                )
        for match in _SECRET_VALUE.finditer(content):
            diagnostics.append(
                _guard(
                    RenderErrorCode.GUARD_VIOLATION,
                    f"{path} contains credential-shaped material.",
                )
            )
            break
        for line in content.splitlines():
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            if looks_like_secret(key.strip(), value.strip().strip('"')):
                diagnostics.append(
                    _guard(
                        RenderErrorCode.GUARD_VIOLATION,
                        f"{path} assigns a credential-shaped value to "
                        f"{key.strip()!r}.",
                    )
                )

    text = "\n".join(combined)

    if not _brace_balance(text):
        diagnostics.append(
            _guard(
                RenderErrorCode.GUARD_UNPARSEABLE,
                "Rendered Terraform has unbalanced or unterminated blocks; "
                "refusing because safety could not be established.",
            )
        )
        return diagnostics

    blocks = _parse_blocks(text)
    if blocks is None:
        diagnostics.append(
            _guard(
                RenderErrorCode.GUARD_UNPARSEABLE,
                "Rendered Terraform block structure could not be parsed; "
                "refusing because safety could not be established.",
            )
        )
        return diagnostics

    module_instances = []
    if module_mode:
        module_instances, scan_problems = _scan_module_blocks(text)
        for problem in scan_problems:
            diagnostics.append(
                _guard(RenderErrorCode.GUARD_UNPARSEABLE, problem)
            )

    declared = set()
    module_labels: dict = {}
    parsed_module_count = 0
    for block_type, labels, depth, parent_type, is_object in blocks:
        if block_type == "module" and module_mode:
            parsed_module_count += 1
            if is_object or depth != 0:
                diagnostics.append(
                    _guard(
                        RenderErrorCode.GUARD_UNPARSEABLE,
                        "module must be a top-level block; refusing "
                        "because safety could not be established.",
                    )
                )
                continue
            if len(labels) != 1:
                diagnostics.append(
                    _guard(
                        RenderErrorCode.GUARD_UNPARSEABLE,
                        "module block does not have exactly one label.",
                    )
                )
                continue
            if not _MODULE_LABEL.match(labels[0]):
                diagnostics.append(
                    _guard(
                        RenderErrorCode.GUARD_VIOLATION,
                        f"Module instance label {labels[0]!r} is not a "
                        f"safe Terraform module name.",
                    )
                )
            continue
        if block_type in FORBIDDEN_BLOCK_TYPES:
            diagnostics.append(
                _guard(
                    RenderErrorCode.GUARD_VIOLATION,
                    f"Blocked {block_type!r} block; execution hooks and "
                    f"external-input blocks are not renderable.",
                )
            )
            continue
        if is_object:
            # ``name = { ... }`` may only appear as a provider object,
            # except the renderer-emitted ``variables`` map inside an
            # ``environment`` block, whose parent is pinned here.
            if parent_type != "required_providers" and (
                block_type,
                parent_type,
            ) not in ALLOWED_OBJECT_BLOCKS:
                diagnostics.append(
                    _guard(
                        RenderErrorCode.GUARD_VIOLATION,
                        f"Object block {block_type!r} is only permitted "
                        f"inside required_providers.",
                    )
                )
            continue
        if depth > 0:
            if block_type not in ALLOWED_NESTED_BLOCK_TYPES:
                diagnostics.append(
                    _guard(
                        RenderErrorCode.GUARD_VIOLATION,
                        f"Nested block type {block_type!r} is not in the "
                        f"allowed vocabulary.",
                    )
                )
            continue
        if block_type not in ALLOWED_TOP_LEVEL:
            diagnostics.append(
                _guard(
                    RenderErrorCode.GUARD_VIOLATION,
                    f"Top-level block type {block_type!r} is not allowed; "
                    f"allowed: {', '.join(sorted(ALLOWED_TOP_LEVEL))}.",
                )
            )
            continue
        if block_type == "resource":
            if len(labels) != 2:
                diagnostics.append(
                    _guard(
                        RenderErrorCode.GUARD_UNPARSEABLE,
                        "resource block does not have exactly two labels.",
                    )
                )
                continue
            resource_type, resource_name = labels
            if resource_type in FORBIDDEN_RESOURCE_TYPES:
                diagnostics.append(
                    _guard(
                        RenderErrorCode.GUARD_VIOLATION,
                        f"Resource type {resource_type!r} is never renderable.",
                    )
                )
                continue
            if resource_type not in allowed_resource_types:
                diagnostics.append(
                    _guard(
                        RenderErrorCode.GUARD_VIOLATION,
                        f"Resource type {resource_type!r} is not represented "
                        f"by the provisioning registry.",
                    )
                )
                continue
            declared.add((resource_type, resource_name))

    if module_mode:
        if parsed_module_count != len(module_instances):
            diagnostics.append(
                _guard(
                    RenderErrorCode.GUARD_UNPARSEABLE,
                    "Module block structure could not be read "
                    "consistently; refusing because safety could not "
                    "be established.",
                )
            )
        seen_labels = set()
        for label, source_value, arguments in module_instances:
            if label in seen_labels:
                diagnostics.append(
                    _guard(
                        RenderErrorCode.GUARD_VIOLATION,
                        f"Duplicate module instance label {label!r}.",
                    )
                )
                continue
            seen_labels.add(label)
            if source_value is None:
                diagnostics.append(
                    _guard(
                        RenderErrorCode.GUARD_VIOLATION,
                        f"Module block {label!r} declares no source.",
                    )
                )
                continue
            match = _QUOTED_STRING.match(source_value)
            if not match:
                diagnostics.append(
                    _guard(
                        RenderErrorCode.GUARD_VIOLATION,
                        f"Module block {label!r} source must be a "
                        f"quoted literal path.",
                    )
                )
                continue
            source = match.group(1)
            entry = policy.get(source)
            if entry is None:
                diagnostics.append(
                    _guard(
                        RenderErrorCode.GUARD_VIOLATION,
                        f"Module block {label!r} uses source {source!r} "
                        f"which is not an exact pinned registry entry; "
                        f"refusing to invoke it.",
                    )
                )
                continue
            module_labels[label] = source
            for argument in sorted(arguments):
                if argument not in entry["inputs"]:
                    diagnostics.append(
                        _guard(
                            RenderErrorCode.GUARD_VIOLATION,
                            f"Module block {label!r} argument "
                            f"{argument!r} is not a declared input of "
                            f"the pinned module; source overrides and "
                            f"meta-arguments are refused.",
                        )
                    )

    for match in _TRAVERSAL.finditer(_STRING_LITERAL.sub('""', text)):
        resource_type, resource_name = match.group(1), match.group(2)
        if resource_type == "module":
            output = match.group(3)
            source = module_labels.get(resource_name)
            if source is None:
                diagnostics.append(
                    _guard(
                        RenderErrorCode.INCONSISTENT_REFERENCE,
                        f"Reference to undeclared module instance "
                        f"{resource_name!r}.",
                    )
                )
                continue
            if output not in policy[source]["outputs"]:
                diagnostics.append(
                    _guard(
                        RenderErrorCode.INCONSISTENT_REFERENCE,
                        f"Reference to undeclared output {output!r} on "
                        f"module {resource_name!r}.",
                    )
                )
            continue
        if (resource_type, resource_name) not in declared:
            diagnostics.append(
                _guard(
                    RenderErrorCode.INCONSISTENT_REFERENCE,
                    f"Reference to undeclared resource "
                    f"{resource_type}.{resource_name}.",
                )
            )

    return diagnostics


def resource_types_in_source(source_tree: dict) -> list:
    """(type, name) pairs declared in a tree; used by tests and reporting."""
    text = "\n".join(source_tree[path] for path in sorted(source_tree))
    return re.findall(r'resource\s+"([^"]+)"\s+"([^"]+)"', text)


def find_policy_surface(source_tree: dict) -> list:
    """Any IAM policy surface in a tree. Must always be empty."""
    found = []
    for path in sorted(source_tree):
        content = source_tree[path]
        for forbidden in sorted(FORBIDDEN_RESOURCE_TYPES):
            if forbidden in content:
                found.append((path, forbidden))
        if _SECRET_NAME.search(content):
            found.append((path, "credential-shaped-token"))
    return found
